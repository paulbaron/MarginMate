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
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import cast

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
            box["angle"] for box in long_boxes if abs(box["cy"] - item["cy"]) < SLOPE_NEIGHBOURHOOD_ROWS * median_height
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

# What reading one document may cost (security audit UPLOAD-1). A page with
# no photo is rendered at RENDER_DPI at whatever size its MediaBox DECLARES,
# and a file of a few hundred bytes declares what it likes: 1200 pt square
# came out at 5000 px (+96 MB), the format's 14 400 pt would be 60 000 px
# square, about 11 GB - in the one process serving every bar. So every page
# is weighed from its declared size and its images' pixel sizes BEFORE
# anything is rendered or decoded, and a document that cannot be read within
# these is refused (DocumentTooBig, said on that file's line).
#
#: Pages read in one document: a ticket is one, a supplier's invoice a few.
#: Counted before ANY page is read - rendered, or its text taken by
#: pdfplumber (`check_page_count`, `pdf_pages`) - not only before a render.
MAX_PAGES = 30
RENDER_DPI = 300
#: Pixels of one rendered page: 40 Mpx is an A2 sheet at 300 dpi (A4 is 8.7).
#: A bigger page is rendered at a lower resolution to fit...
RENDER_MAX_PIXELS = 40_000_000
#: ...down to 100 dpi, below which print is no longer read: a page that does
#: not fit even then (1.6 m square) is no ticket and no invoice.
MIN_RENDER_SCALE = 100 / 72
#: Pixels of one image taken as it is - a photo file, or a scan's embedded
#: photo: Pillow's own bomb threshold (89 Mpx), above a 48 Mpx phone photo.
IMAGE_MAX_PIXELS = 89_478_485
#: What the content streams of one PDF may inflate together (one bound per
#: stream holds for the whole process: returnables.reading.MAX_INFLATE_STAGE).
#: Weighed before any page is read or rendered (`_weigh_contents`), and
#: shared by the readers (`bounded_reading`): pdfminer keeps what it decoded
#: until the file is closed, and PDFium decodes it all again. The heaviest
#: of 1 374 real invoices inflates 1,1 MB, its fonts included.
MAX_INFLATE_TOTAL = 64 * 1024 * 1024
#: What pdfminer's interpreter may run of one PDF, a stream counted each
#: time it runs - a page drawing one form a thousand times runs it a
#: thousand times (returnables.reading.bound_pdf_interpreting), at 6 to
#: 11 s of CPU a MB. The most of 1 374 real invoices is 340 KB.
MAX_RUN_TOTAL = 8 * 1024 * 1024

TOO_MANY_PAGES = "Document trop long pour être lu : {pages} pages, {limit} au plus."
PAGE_TOO_LARGE = (
    "Page trop grande pour être lue (page {number} : {width} × {height} cm) : ce n'est ni un ticket ni une facture."
)
IMAGE_TOO_LARGE = "Image trop grande pour être lue (page {number} : {pixels} millions de pixels, {limit} au plus)."
TOO_MANY_GLYPHS = "Document trop chargé pour être lu : plus de {limit} caractères ou traits sur une page."
TOO_HEAVY_CONTENT = "Document trop lourd pour être lu : plus de {weight} une fois décompressé."
TOO_LONG_CONTENT = "Document trop long à lire : plus de {weight} de contenu à dessiner."


class DocumentTooBig(ValueError):
    """A document refused for what reading it would cost. French, for the
    person: its words are said on the file's line."""


#: PDFium is not thread-safe (pypdfium2 says so: one thread at a time in a
#: process), and `page_images` runs in a folder import's thread
#: (receipt_batches, which takes no OCR_LOCK), a gather's, and the requests'
#: - all in the one process serving every bar. Every call into it holds this
#: lock; a page handed to the caller holds nothing. Re-entrant: a generator
#: left half read closes its document whenever it is collected, possibly in
#: a thread already inside the lock.
PDFIUM_LOCK = threading.RLock()


def _centimetres(points: float) -> str:
    return f"{points / 72 * 2.54:.1f}".replace(".", ",")


def _millions(pixels: int) -> str:
    return f"{pixels / 1_000_000:.0f}"


def _check_pixels(width: int, height: int, number: int) -> None:
    if width * height > IMAGE_MAX_PIXELS:
        raise DocumentTooBig(
            IMAGE_TOO_LARGE.format(number=number, pixels=_millions(width * height), limit=_millions(IMAGE_MAX_PIXELS))
        )


def render_scale(width: float, height: float) -> float | None:
    """The scale a page of `width` × `height` points is rendered at: 300 dpi,
    or lower to stay within RENDER_MAX_PIXELS - None when even
    MIN_RENDER_SCALE would not."""
    scale = RENDER_DPI / 72
    area = max(width, 1.0) * max(height, 1.0)
    if area * scale * scale <= RENDER_MAX_PIXELS:
        return scale
    # A hair under the exact fit: the renderer rounds each side up.
    scale = math.sqrt(RENDER_MAX_PIXELS / area) * 0.99
    return scale if scale >= MIN_RENDER_SCALE else None


def _plan_pdf(document, pdfium_raw) -> list:
    """(page, the one image to take as it is or None, the render scale) for
    every page - every page weighed before the first is read."""
    if len(document) > MAX_PAGES:
        raise DocumentTooBig(TOO_MANY_PAGES.format(pages=len(document), limit=MAX_PAGES))
    plan = []
    for number, page in enumerate(document, start=1):
        width, height = page.get_size()
        images = list(page.get_objects(filter=(pdfium_raw.FPDF_PAGEOBJ_IMAGE,)))
        for image in images:
            _check_pixels(*cast("tuple[int, int]", image.get_px_size()), number)
        if len(images) == 1 and covers_page(images[0].get_bounds(), width, height):
            plan.append((page, images[0], None))
            continue
        scale = render_scale(width, height)
        if scale is None:
            raise DocumentTooBig(
                PAGE_TOO_LARGE.format(number=number, width=_centimetres(width), height=_centimetres(height))
            )
        plan.append((page, None, scale))
    return plan


def page_images(path: str):
    """Yield one PIL image per page.

    A photo file is one page (a multi-page TIFF, several), turned upright by
    its EXIF orientation first: a phone stores a portrait photo on its side
    and records the rotation separately, and a receipt recognised sideways is
    noise. A PDF gives its embedded photo where the page is a single
    full-page image (the normal case for a phone scan), else the rendered
    page - which keeps this function total, so the caller never has to
    special-case "no image found".

    Raises DocumentTooBig, before anything is decoded or rendered, for a
    document whose reading would cost more than MAX_PAGES pages,
    IMAGE_MAX_PIXELS for a photo, or RENDER_MAX_PIXELS for a rendered page
    at MIN_RENDER_SCALE (a bigger page is rendered at a lower resolution).
    """
    if path.lower().endswith(IMAGE_EXTENSIONS):
        from PIL import Image, ImageOps, ImageSequence

        with Image.open(path) as image:
            # The header only: nothing is decoded yet.
            frames = getattr(image, "n_frames", 1)
            if frames > MAX_PAGES:
                raise DocumentTooBig(TOO_MANY_PAGES.format(pages=frames, limit=MAX_PAGES))
            _check_pixels(*image.size, 1)
            for number, frame in enumerate(ImageSequence.Iterator(image), start=1):
                _check_pixels(*frame.size, number)
                yield ImageOps.exif_transpose(frame).convert("RGB")
        return

    _weigh_contents(path)
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    # One call into PDFium at a time in the process (PDFIUM_LOCK), and none
    # held while the caller works on a page: its OCR takes seconds.
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(path)
        try:
            plan = _plan_pdf(document, pdfium_raw)
        except BaseException:
            document.close()
            raise
    try:
        for page, image, scale in plan:
            with PDFIUM_LOCK:
                if image is not None:
                    picture = image.get_bitmap().to_pil().convert("RGB")
                else:
                    picture = page.render(scale=scale).to_pil().convert("RGB")
            yield picture
    finally:
        with PDFIUM_LOCK:
            document.close()


#: How much of its page an embedded image has to cover to be the page - a
#: phone scan. A web shop's invoice has one image too: its logo, a strip at
#: the top, which read alone gave the recogniser seven characters.
PAGE_PHOTO_SHARE = 0.8


def covers_page(bounds, page_width: float, page_height: float) -> bool:
    """Whether an image placed at `bounds` (left, bottom, right, top, in
    points) fills its page."""
    left, bottom, right, top = bounds
    area = max(right - left, 0) * max(top - bottom, 0)
    return page_width > 0 and page_height > 0 and area >= PAGE_PHOTO_SHARE * page_width * page_height


#: A page with fewer characters of text than this is a picture: a scanner
#: stamps a page number, a phone app a watermark.
MIN_TEXT_LAYER_CHARS = 40
# Two words further apart than this share of their height are in different
# columns - the gap the recognised lines keep as two spaces.
COLUMN_GAP_SHARE = 0.4
SAME_TEXT_LINE_POINTS = 3


def _walk_pages(path: str, limit: int) -> tuple[int, object]:
    """How many pages pdfplumber would iterate, counted up to `limit`, and
    the /Count the page tree declares."""
    import itertools

    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdftypes import resolve1

    with open(path, "rb") as handle:
        document = PDFDocument(PDFParser(handle))
        counted = sum(1 for _page in itertools.islice(PDFPage.create_pages(document), limit))
        tree = resolve1(document.catalog.get("Pages"))
        declared = resolve1(tree.get("Count")) if isinstance(tree, dict) else None
    return counted, declared


def check_page_count(path: str) -> None:
    """Refuse (DocumentTooBig) a PDF of more than MAX_PAGES pages before
    anything reads one of its pages (security review HARDEN-01).

    pdfplumber keeps every page it has read - its characters, its layout -
    until the file is closed: 0,57 MB a page of 300 glyphs, 5,7 MB one of
    3 000, and a PDF under the 25 MB upload cap can carry tens of thousands
    of pages sharing one content stream. The page cap in `page_images` came
    too late: the text layer (`text_layer_pages`) and a supplier's reader
    (`InvoiceParser.parse`) had read every page by then.

    Counted as pdfplumber will iterate them - pdfminer's own walk of the
    page tree, stopped at MAX_PAGES + 1 leaves, so 31 pages and 30 000 cost
    the same walk (the cross-reference table aside, which opening the file
    reads anyway). Not pdfium's count: pdfium believes the /Count the tree
    declares, and a file declaring 1 over 2 000 pages was one page to
    pdfium and 2 000 to pdfplumber; nor is pdfium safe to call from two
    threads at once, and this runs in a gather's and a folder import's. The
    number said is the one the file declares when that is over the cap,
    else « plus de N ». Measured on a file of 5 000 light pages: refused in
    half a second and 1,8 MB, where pdfplumber's own count (`len(pdf.pages)`,
    every page made) took 4,5 s and 25 MB.

    Then its pages' content streams are weighed (`_weigh_contents`): past
    MAX_INFLATE_TOTAL inflated, DocumentTooBig too.

    Not a PDF (by its name: a photo is `page_images`' to weigh), or one
    pdfminer cannot open or walk: it passes - what is wrong with it is said
    by what reads it next, as before."""
    if path.lower().endswith(".pdf"):
        _refuse_past_the_cap(path)


def _refuse_past_the_cap(path: str) -> None:
    try:
        counted, declared = _walk_pages(path, MAX_PAGES + 1)
    except Exception:  # noqa: BLE001 - pdfminer raises its own zoo for a broken file
        return
    if counted > MAX_PAGES:
        said = declared if isinstance(declared, int) and declared > MAX_PAGES else f"plus de {MAX_PAGES}"
        raise DocumentTooBig(TOO_MANY_PAGES.format(pages=said, limit=MAX_PAGES))
    _weigh_contents(path)


def _too_heavy() -> DocumentTooBig:
    from common import weight

    return DocumentTooBig(TOO_HEAVY_CONTENT.format(weight=weight(MAX_INFLATE_TOTAL)))


def _weigh_contents(path: str) -> None:
    """Refuse (DocumentTooBig) a PDF whose pages' content streams, and the
    forms they draw, inflate past MAX_INFLATE_TOTAL together - before
    pdfplumber or PDFium decodes one (security audit: a 718 KB file listing
    twelve 60 MB streams on its one page took 842 MB in the text layer, then
    PDFium decoded them all again). Each is decoded through pdfminer's
    bounded decoders as often as a page lists it, from a copy dropped at
    once (the object itself stays cached undecoded: uncached, an object
    stream was parsed again for every object it holds - 200 ms on a Free
    invoice). A file pdfminer cannot open or walk, or a stream it cannot decode,
    passes: what is wrong with it is said by what reads it next."""
    import itertools

    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser

    from returnables import reading

    with reading.inflate_budget(MAX_INFLATE_TOTAL) as budget:
        try:
            with open(path, "rb") as handle:
                document = PDFDocument(PDFParser(handle))
                for page in itertools.islice(PDFPage.create_pages(document), MAX_PAGES + 1):
                    _decode_drawn(page, budget)
        except DocumentTooBig:
            raise
        except Exception as error:  # noqa: BLE001 - pdfminer raises its own zoo for a broken file
            if budget[0] < 0 or reading.inflate_refused(error):
                raise _too_heavy() from None
            return
        if budget[0] < 0:
            raise _too_heavy()


def _decode_drawn(page, budget: list) -> None:
    """Decode every content stream `page` draws: its /Contents, and the
    forms its resources hold, theirs too (each form once a page)."""
    import copy

    from pdfminer.pdftypes import PDFStream, dict_value, resolve1
    from pdfminer.psparser import LIT

    from returnables import reading

    pending, seen = [(page.contents, page.resources)], set()
    while pending:
        streams, resources = pending.pop()
        for item in streams:
            stream = resolve1(item)
            if not isinstance(stream, PDFStream):
                continue
            try:
                copy.copy(stream).get_data()
            except Exception as error:  # noqa: BLE001 - a damaged stream is the reader's to say
                if budget[0] < 0 or reading.inflate_refused(error):
                    raise _too_heavy() from None
        for item in dict_value(dict_value(resources).get("XObject")).values():
            objid = getattr(item, "objid", None)
            if objid is not None and objid in seen:
                continue
            seen.add(objid)
            form = resolve1(item)
            if isinstance(form, PDFStream) and form.get("Subtype") is LIT("Form"):
                pending.append(([form], form.get("Resources")))


def pdf_pages(path: str):
    """The pages of the PDF at `path`, through pdfplumber, one at a time -
    each released (`page.close()`) as soon as the caller moves on, so what
    reading a document holds is one page's worth, not the whole document's.

    Raises DocumentTooBig before a page is read for more than MAX_PAGES
    (`check_page_count`, whatever the file is named: pdfplumber reads it as
    a PDF all the same); pdfplumber is also told to make no more than one
    page past the cap."""
    _refuse_past_the_cap(path)
    import pdfplumber

    with pdfplumber.open(path, pages=range(1, MAX_PAGES + 2)) as document:
        if len(document.pages) > MAX_PAGES:
            raise DocumentTooBig(TOO_MANY_PAGES.format(pages=f"plus de {MAX_PAGES}", limit=MAX_PAGES))
        for page in document.pages:
            try:
                yield page
            finally:
                page.close()


@contextmanager
def bounded_reading():
    """Around a reader walking `pdf_pages`: its decodes share
    MAX_INFLATE_TOTAL (returnables.reading.inflate_budget - should a stream
    escape `_weigh_contents`), what it runs MAX_RUN_TOTAL, and what pdfminer
    stopped (those budgets, a page past reading.MAX_PAGE_GLYPHS glyphs) is
    DocumentTooBig, said on the
    file's line - not the PdfminerException pdfplumber wraps it in, which a
    reader's caller takes for a broken file. Entered by the caller, not
    inside `pdf_pages`: the page is read in the caller's loop, never in the
    generator, and a ContextVar set across a generator's yields lives in
    whichever context resumes it."""
    from common import group_thousands, weight
    from returnables import reading

    with reading.inflate_budget(MAX_INFLATE_TOTAL, run=MAX_RUN_TOTAL) as budget:
        try:
            yield
        except DocumentTooBig:
            raise
        except Exception as error:
            if reading.glyphs_refused(error):
                limit = group_thousands(reading.MAX_PAGE_GLYPHS)
                raise DocumentTooBig(TOO_MANY_GLYPHS.format(limit=limit)) from None
            if reading.run_refused(error):
                raise DocumentTooBig(TOO_LONG_CONTENT.format(weight=weight(MAX_RUN_TOTAL))) from None
            if budget[0] < 0 or reading.inflate_refused(error):
                raise _too_heavy() from None
            raise
        # pdfminer may swallow a refused decode and carry on without it.
        if budget[0] < 0:
            raise _too_heavy()


def document_text(path: str) -> str:
    """The text a digital document carries, or "" for a photo or a scan.
    Raises DocumentTooBig as `text_layer_pages` does."""
    return "\n".join(page.text for page in text_layer_pages(path) if page is not None)


def text_layer_pages(path: str) -> list[OcrPage | None]:
    """The text a PDF carries, page by page, as lines the readers take - or
    None for a page that is a picture. A digital invoice needs no OCR: its
    own text is exact, and reading a rendering of it could only add errors.
    Not a PDF, or one this can't open: no layer at all - rendering the page
    says what is wrong with it.

    A PDF of more than MAX_PAGES pages raises DocumentTooBig, before any
    page is read (`pdf_pages`), and so does a page drawing too many glyphs
    (`bounded_reading`) - never swallowed into « no layer » by the handler
    below, which would send it on to be rendered."""
    if not path.lower().endswith(".pdf"):
        return []
    pages: list[OcrPage | None] = []
    try:
        with bounded_reading():
            for page in pdf_pages(path):
                words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
                if sum(len(word["text"]) for word in words) < MIN_TEXT_LAYER_CHARS:
                    pages.append(None)
                    continue
                pages.append(OcrPage(lines=_text_lines(words)))
    except DocumentTooBig:
        raise
    except Exception:  # noqa: BLE001 - pdfminer raises its own zoo for a broken file
        return []
    return pages


def _text_lines(words) -> list[OcrLine]:
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda word: (round(word["top"]), word["x0"])):
        for row in rows:
            if abs(row[0]["top"] - word["top"]) <= SAME_TEXT_LINE_POINTS:
                row.append(word)
                break
        else:
            rows.append([word])
    lines = []
    for row in sorted(rows, key=lambda row: row[0]["top"]):
        row.sort(key=lambda word: word["x0"])
        cells: list[OcrCell] = []
        for word in row:
            size = max(word["bottom"] - word["top"], 1.0)
            if cells and word["x0"] - cells[-1].x1 <= COLUMN_GAP_SHARE * size:
                last = cells[-1]
                cells[-1] = OcrCell(text=f"{last.text} {word['text']}", x0=last.x0, x1=word["x1"], confidence=1.0)
            else:
                cells.append(OcrCell(text=word["text"], x0=word["x0"], x1=word["x1"], confidence=1.0))
        lines.append(OcrLine(cells=cells, y=row[0]["top"]))
    return lines


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
    binary = cv2.adaptiveThreshold(grey, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15)
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
    rotated = cv2.warpAffine(array, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
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
    boxes = [(quad.tolist(), text, float(score)) for quad, text, score in zip(output.boxes, output.txts, output.scores)]
    return OcrPage(lines=group_boxes_into_lines(boxes))


def ocr_image(image) -> OcrPage:
    """`image` is a PIL image; returns its lines top to bottom."""
    return ocr_prepared_image(deskew(image))


def ocr_pdf(pdf_path: str) -> list[OcrPage]:
    return [ocr_image(image) for image in page_images(pdf_path)]
