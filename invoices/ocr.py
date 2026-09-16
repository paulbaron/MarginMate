"""Optical character recognition for photographed till receipts.

The small-shop receipts ("facturettes") this feeds are photos, not digital
PDFs: `pdfplumber` extracts an empty string from every one of them, so the
normal parser path has no raw material to work from at all. This module is
what stands in for `page.extract_text()` in that case - see
`parsers/receipt_base.py::ReceiptParser`.

Engine choice was measured, not assumed, and has been revisited once. The
first pick (RapidOCR with PP-OCRv4) beat Tesseract 5.4 by a distance -
Tesseract dropped whole item lines from the crumpled Franprix tickets. A
bake-off on the same 42 receipts (2026-09-11) then compared eight engines
through the same parsers, and on a parser-free reading score: the share of the
printed amounts and product names, typed out by hand from the photos, that
turn up in the engine's output.

    PP-OCRv4 (previous engine)   amounts 73%   names  96%   2.6 s/receipt
    PP-OCRv6 medium (this one)   amounts 93%   names 100%   5.1 s/receipt
    Tesseract 5 + French         amounts 22%   names  52%
    local vision LLMs            added tax codes that aren't printed (so
                                 totals parsed as purchases), dropped the
                                 price column, and took 2-3 minutes a
                                 receipt on a 6 GB GPU

PP-OCRv4 also glued Franprix's VAT code to the price ("T1 0.49" read as
"T10.49"), which several parser workarounds only existed to undo. Still
onnxruntime on CPU: no deep-learning framework, no GPU needed.

Two details that look incidental and are not:

*Native pixels, no re-rasterising.* The page is one embedded JPEG; pdfium
hands back that exact bitmap via `get_bitmap()`. Rendering the page to a DPI
instead would resample text that is already marginal, and these photos have
no margin to spare.

*Line grouping from the boxes' own geometry.* OCR returns detached boxes,
and which of them form one printed line has to be inferred. Comparing
vertical positions against a tolerance - first a fixed pixel count, then a
fraction of the text height - failed two ways on the real corpus. A curled
or angled photo leaves the price column a full row above or below the name
column, so items took their neighbour's price (Franprix 0,98 EUR: the first
baguette's price landed on the SOUS-TOTAL line). And tightly printed rows
glued together, two products becoming one at the second one's price
(Monoprix 13,45 EUR lost 4,99 EUR that way).

Two properties of the detector's output fix both. Every box is an oriented
quadrilateral, so the local slope of the text is *measured* rather than
assumed, and positions are compared along it - which absorbs the curl and
tilt a single page-wide deskew cannot (one receipt was photographed 9
degrees off, beyond what `deskew` trusts itself to correct). And two boxes
that overlap horizontally sit in the same column, so they are on different
lines by construction - which is exactly the merge that used to happen. On
the 42 receipts this took the correctly-read count from 31 to 35, with
nothing newly wrong.
"""

from __future__ import annotations

import math
import statistics
import threading
from dataclasses import dataclass, field

# Two boxes are on one line when, measured along the local slope of the
# text, they share at least this fraction of the smaller one's height.
SAME_LINE_MIN_OVERLAP = 0.5
# Two boxes overlapping horizontally by more than this fraction of the
# narrower one sit in the same column - so on different lines, however
# closely the rows are printed.
SAME_COLUMN_OVERLAP = 0.3
# A box at least this many times wider than tall has a trustworthy angle; a
# lone "7" or "EUR" does not.
LONG_BOX_ASPECT = 3
# The local slope is the median angle of the long boxes within this many
# text heights above and below - local, because a curled receipt bends.
SLOPE_NEIGHBOURHOOD_ROWS = 6
# A box is only offered to the last few lines built, which are the ones at
# its height.
LINES_LOOKBACK = 6

# A receipt photographed by hand is never quite square, and the recogniser
# groups text into lines by vertical position - so on a page tilted by even
# one degree, the left of one row sits at the same height as the right of the
# row above and the two merge. That is not cosmetic: it merged Monoprix's
# "EPICERIE/BOISSONS" heading with the price of the item beneath it, and
# glued a Franprix item's amount onto the DUPLICATA banner, turning a rule
# into a phantom product. Correcting the tilt first fixed both.
#
# Beyond this many degrees the estimate is more likely to be wrong than the
# page is to be that crooked, so it is ignored rather than trusted.
MAX_DESKEW_DEGREES = 8.0
# A smeared text row shorter than this is punctuation or noise, not a line
# of print, and its angle says nothing useful.
MIN_TEXT_ROW_WIDTH_PX = 60

_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    """PP-OCRv6 medium, detection and recognition, through RapidOCR's ONNX
    runtime. Built once and shared: loading reads its models (downloaded into
    the rapidocr package on first use), which is never a per-receipt cost.
    Guarded by a lock because the import flow can run inside a background
    thread (see tasks.py) while a request is doing the same thing."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                from rapidocr import ModelType, OCRVersion, RapidOCR

                _engine = RapidOCR(
                    params={
                        "Det.ocr_version": OCRVersion.PPOCRV6,
                        "Rec.ocr_version": OCRVersion.PPOCRV6,
                        "Det.model_type": ModelType.MEDIUM,
                        "Rec.model_type": ModelType.MEDIUM,
                        "Global.log_level": "warning",
                    }
                )
    return _engine


@dataclass
class OcrCell:
    """One text box as the recogniser found it."""

    text: str
    x0: float
    x1: float
    confidence: float


@dataclass
class OcrLine:
    """One visual line of the receipt, left to right.

    `confidence` is the LOWEST of its cells', not the mean: a line reading
    "TOTAL A PAYER 13.06" is only as trustworthy as the number on it, and
    averaging a shaky amount against a confident label hides exactly the
    case the review queue exists to catch.
    """

    cells: list[OcrCell] = field(default_factory=list)
    y: float = 0.0

    @property
    def text(self) -> str:
        # Two spaces, so a column gap survives into the parsers' regexes as
        # something distinguishable from a space inside a product name.
        return "  ".join(cell.text for cell in self.cells)

    @property
    def confidence(self) -> float:
        return min((cell.confidence for cell in self.cells), default=0.0)


@dataclass
class OcrPage:
    lines: list[OcrLine] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def confidence(self) -> float:
        return min((line.confidence for line in self.lines), default=0.0)


def group_boxes_into_lines(boxes) -> list[OcrLine]:
    """`boxes` is RapidOCR's own output shape: (quad, text, confidence),
    where quad is four (x, y) corners clockwise from the top left.

    Kept separate from the engine call so it can be tested on hand-written
    box lists - the grouping rule is the part with the judgement in it. See
    the module docstring for why it works from each box's geometry.
    """
    items = []
    for quad, text, confidence in boxes:
        if not text:
            continue
        item = _box_geometry(quad)
        item["cell"] = OcrCell(text=text, x0=item["x0"], x1=item["x1"], confidence=float(confidence))
        items.append(item)
    if not items:
        return []

    median_height = statistics.median(item["height"] for item in items)
    long_boxes = [item for item in items if item["width"] >= LONG_BOX_ASPECT * item["height"]]
    page_angle = statistics.median(box["angle"] for box in long_boxes) if long_boxes else 0.0
    for item in items:
        nearby = [
            box["angle"]
            for box in long_boxes
            if abs(box["cy"] - item["cy"]) < SLOPE_NEIGHBOURHOOD_ROWS * median_height
        ]
        item["slope"] = math.tan(statistics.median(nearby) if nearby else page_angle)
    # Each box's height on the page once the local slope is taken out - the
    # order lines are built in.
    x_reference = statistics.median(item["cx"] for item in items)
    for item in items:
        item["level"] = item["cy"] - item["slope"] * (item["cx"] - x_reference)
    items.sort(key=lambda item: item["level"])

    groups: list[list[dict]] = []
    for item in items:
        best, best_overlap = None, 0.0
        for group in groups[-LINES_LOOKBACK:]:
            if any(_same_column(item, member) for member in group):
                continue
            # Compared with the member nearest it along the line, projected
            # along that member's own slope: on a curled receipt the far end
            # of a line is not where its start would suggest.
            reference = min(group, key=lambda member: abs(member["cx"] - item["cx"]))
            overlap = _overlap_along_slope(item, reference)
            if overlap > best_overlap:
                best, best_overlap = group, overlap
        if best is not None and best_overlap >= SAME_LINE_MIN_OVERLAP:
            best.append(item)
        else:
            groups.append([item])

    lines = [_finish_line(group) for group in groups]
    lines.sort(key=lambda line: line.y)
    return lines


def _box_geometry(quad) -> dict:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = [(float(x), float(y)) for x, y in quad]
    along_x = ((x1 - x0) + (x2 - x3)) / 2
    along_y = ((y1 - y0) + (y2 - y3)) / 2
    height = (math.hypot(x3 - x0, y3 - y0) + math.hypot(x2 - x1, y2 - y1)) / 2
    return {
        "cx": (x0 + x1 + x2 + x3) / 4,
        "cy": (y0 + y1 + y2 + y3) / 4,
        "width": math.hypot(along_x, along_y),
        "height": max(height, 1.0),
        "angle": math.atan2(along_y, along_x),
        "x0": min(x0, x1, x2, x3),
        "x1": max(x0, x1, x2, x3),
    }


def _same_column(a: dict, b: dict) -> bool:
    shared = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
    narrower = min(a["x1"] - a["x0"], b["x1"] - b["x0"])
    return shared > SAME_COLUMN_OVERLAP * narrower


def _overlap_along_slope(item: dict, reference: dict) -> float:
    """How much of the smaller box's height the two share, once `reference`
    is carried along its own slope to `item`'s position."""
    expected_centre = reference["cy"] + reference["slope"] * (item["cx"] - reference["cx"])
    top = max(item["cy"] - item["height"] / 2, expected_centre - reference["height"] / 2)
    bottom = min(item["cy"] + item["height"] / 2, expected_centre + reference["height"] / 2)
    return (bottom - top) / min(item["height"], reference["height"])


def _finish_line(entries: list[dict]) -> OcrLine:
    entries.sort(key=lambda entry: entry["x0"])
    return OcrLine(
        cells=[entry["cell"] for entry in entries],
        y=sum(entry["level"] for entry in entries) / len(entries),
    )


# Photo files taken as receipts as they are, without being put into a PDF.
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp")


def page_images(path: str):
    """Yield one PIL image per page.

    A photo file is one page (a multi-page TIFF, several), turned upright by
    its EXIF orientation first: a phone stores a portrait photo on its side
    and records the rotation separately, and a receipt recognised sideways is
    noise. A PDF gives its embedded photo where the page is a single
    full-page image (the normal case for a phone scan), else the rendered
    page - which keeps this function total, so the caller never has to
    special-case "no image found".
    """
    if path.lower().endswith(IMAGE_EXTENSIONS):
        from PIL import Image, ImageOps, ImageSequence

        with Image.open(path) as image:
            for frame in ImageSequence.Iterator(image):
                yield ImageOps.exif_transpose(frame).convert("RGB")
        return

    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    document = pdfium.PdfDocument(path)
    try:
        for page in document:
            images = [obj for obj in page.get_objects() if obj.type == pdfium_raw.FPDF_PAGEOBJ_IMAGE]
            if len(images) == 1:
                yield images[0].get_bitmap().to_pil().convert("RGB")
            else:
                yield page.render(scale=300 / 72).to_pil().convert("RGB")
    finally:
        document.close()


def estimate_skew_degrees(image) -> float:
    """The page's tilt, from the angle of its own lines of text.

    Each text row is smeared horizontally into a single blob, and the median
    of those blobs' angles is the page angle - median rather than mean so one
    mis-detected blob (a barcode, a fold, the edge of the table the receipt
    was photographed on) cannot drag the whole page round.
    """
    import cv2
    import numpy

    grey = cv2.cvtColor(numpy.asarray(image), cv2.COLOR_RGB2GRAY)
    grey = cv2.GaussianBlur(grey, (5, 5), 0)
    binary = cv2.adaptiveThreshold(
        grey, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3))
    smeared = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _hierarchy = cv2.findContours(smeared, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    angles = []
    for contour in contours:
        (_centre, (width, height), angle) = cv2.minAreaRect(contour)
        if max(width, height) < MIN_TEXT_ROW_WIDTH_PX or min(width, height) < 4:
            continue
        # minAreaRect reports the angle of the shorter edge; a text row is
        # wide, so a "tall" rect is the same row measured 90 degrees round.
        if width < height:
            angle += 90
        if -30 < angle < 30:
            angles.append(angle)
    if not angles:
        return 0.0
    return float(numpy.median(angles))


def deskew(image):
    """Rotate `image` upright, or return it untouched when it already is."""
    import cv2
    import numpy
    from PIL import Image

    angle = estimate_skew_degrees(image)
    if abs(angle) < 0.1 or abs(angle) > MAX_DESKEW_DEGREES:
        return image
    array = numpy.asarray(image)
    height, width = array.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    rotated = cv2.warpAffine(
        array, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )
    return Image.fromarray(rotated)


def ocr_prepared_image(image) -> OcrPage:
    """Recognise an image that has ALREADY been deskewed.

    Split out from `ocr_image` so the import flow, which keeps the corrected
    image to store as the review screen's preview, does not deskew twice.
    """
    import numpy

    # OpenCV's channel order: what the engine reads a file into, and what it
    # was measured on. A PIL image is RGB.
    pixels = numpy.ascontiguousarray(numpy.asarray(image.convert("RGB"))[:, :, ::-1])
    output = _get_engine()(pixels)
    if output.boxes is None or not len(output.boxes):
        return OcrPage(lines=[])
    boxes = [
        (quad.tolist(), text, float(score)) for quad, text, score in zip(output.boxes, output.txts, output.scores)
    ]
    return OcrPage(lines=group_boxes_into_lines(boxes))


def ocr_image(image) -> OcrPage:
    """`image` is a PIL image; returns its lines top to bottom."""
    return ocr_prepared_image(deskew(image))


def ocr_pdf(pdf_path: str) -> list[OcrPage]:
    return [ocr_image(image) for image in page_images(pdf_path)]
