"""The month's timesheet as a PDF: one A4 page, written by hand.

What the employee signs is the month exactly as `staff.timesheet` hands it
over (`MonthSheet`), drawn and nothing else: nothing is added up here, and
every figure on the page goes through the same `format_hours` as the
month's page, so the two cannot print one day two ways.

No PDF library draws it, on purpose: a timesheet is text, a few rules and
boxes, in the two fonts every PDF reader already carries (Helvetica and
Helvetica-Bold, standard Type 1 fonts: nothing embedded). pyHanko, installed
for the monthly electronic signature (`staff/signing.py`), never draws or
rewrites these bytes: it appends the signature fields and the signatures to
them as incremental updates, placed by `SIGNATURE_BOXES` below - the one
definition of where the two boxes are (the document frozen for that
signature draws the same frames, with « Signature électronique du salarié »
and « … de l'employeur » in place of the handwritten instructions:
`ELECTRONIC_SIGNATURE_BOXES`). What drawing text properly does need is each glyph's
width - to right-align the hours, centre the title, cut a note that would
run off the page - and pdfminer.six, installed through pdfplumber, ships
the Adobe metrics of both fonts (`pdfminer.fontmetrics.FONT_METRICS`).

Four rules:

* **One page, always.** A timesheet is one sheet signed at its foot; a
  second page would put the signatures under days nobody initialled. The
  rows get what is left once the header, the summary and the signature
  boxes have their room (`_row_height`). The worst case is a 31-day month
  starting on a Saturday or a Sunday (March 2026): 31 days and six week
  totals, 37 rows. Everything above the table is bounded for that reason -
  the address prints `ADDRESS_LINES` lines at most - and every text is cut
  to its column with « … » rather than wrapped onto a line nobody counted.
* **The text is cp1252**, which is what `/WinAnsiEncoding` draws: é, è, à,
  û, ô, ç, ’, « », — and … are all in it. A character it lacks prints « ? »
  and never raises: a name written in another script must not make the
  month unprintable. A NAME loses an accent rather than a letter
  (`printable_name`: « Łukasz » is « Lukasz »), and the month's form
  refuses a note holding such a character (`unprintable_characters`) rather
  than send a « ? » to be signed. The minus of a negative difference (U+2212,
  `timesheet.MINUS`) and the narrow spaces of French typography are not in
  it either, but have an exact equivalent that is, and are printed as that.
* **The same month gives the same bytes.** No date, no random identifier:
  the PDF of a month nobody saved is byte for byte the PDF of that month
  once saved untouched - « a planned sheet is exactly what saving would
  store », and a test holds it to that.
* **A public holiday says so, worked or not**: « Férié chômé — Assomption »
  when the owner marked it off, « Férié : Fête nationale » in grey when it
  was worked (or taken as leave). The employee signs for a holiday's hours
  too, and has to see which day it was. A day off of the typical week stays
  blank, holiday or not, as the owner's sheets print it.
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from dataclasses import dataclass, replace
from urllib.parse import quote

from pdfminer.fontmetrics import FONT_METRICS

from .timesheet import PUBLIC_HOLIDAY, REST, MonthDay, MonthSheet, MonthWeek

# -- The page ---------------------------------------------------------------------------------------------------

PAGE_WIDTH, PAGE_HEIGHT = 595.28, 841.89   # A4 portrait, in points
MARGIN_X = 42.0
MARGIN_TOP = 36.0
MARGIN_BOTTOM = 34.0
LEFT, RIGHT = MARGIN_X, PAGE_WIDTH - MARGIN_X
PAD = 6.0

#: The establishment's address prints this many lines at most (the rest is
#: joined onto the last one and cut): the header is what the rows are sized
#: against, and an address typed on ten lines must not push the signatures
#: off the page.
ADDRESS_LINES = 3

#: A row is never taller than this, however short the month: a February of
#: 32 rows spread over the whole page reads as a form with gaps in it.
MAX_ROW_HEIGHT = 14.0

TEXT_SIZE = 9.0
TITLE_SIZE = 15.0

# The table's columns: the day's name and its number right-aligned (so the
# numbers read as a column, as on a calendar), the hours right-aligned, the
# note in what is left.
DAY_X = LEFT + PAD
DAY_NUMBER_RIGHT = DAY_X + 56
HOURS_RIGHT = LEFT + 160
NOTE_X = HOURS_RIGHT + 14
NOTE_RIGHT = RIGHT - PAD

TABLE_HEADER_HEIGHT = 16.0
GAP_AFTER_TABLE = 10.0
SUMMARY_TITLE_HEIGHT = 15.0
SUMMARY_LINE_HEIGHT = 12.0
#: The summary's lines: the hours take three (four with absences, whose
#: planned hours get a line of their own), and the days - worked, then one
#: line per kind of absence that occurs, five kinds so six lines at most - go
#: in two columns beside them. More would lengthen the box rather than be
#: dropped (`_summary_lines`); the one-page test is what says it still fits.
SUMMARY_LINES = 3
GAP_BEFORE_SIGNATURES = 12.0
#: About 32 mm: room for a date, the handwritten « Lu et approuvé » and a
#: signature under the box's own two lines of print.
SIGNATURE_HEIGHT = 92.0
SIGNATURE_GAP = 16.0

BLACK = 0.0
GREY = 0.42         # secondary text: « Semaine type », a worked holiday, the signature instructions
RULE_GREY = 0.8     # the thin rule under each day
BAND_GREY = 0.92    # the week totals' band, the summary's title band
BOX_GREY = 0.35     # the summary and signature boxes

REGULAR = "Helvetica"
BOLD = "Helvetica-Bold"
FONT_RESOURCES = {REGULAR: "F1", BOLD: "F2"}

EMPLOYEE_SIGNATURE = "Le salarié"
EMPLOYEE_SIGNATURE_NOTE = "Date et signature, précédées de la mention “Lu et approuvé”"
EMPLOYER_SIGNATURE = "L'employeur"
EMPLOYER_SIGNATURE_NOTE = "Date et signature"
#: The same boxes on the document frozen for the ELECTRONIC signature
#: (`render_month_pdf(..., electronic=True)`, used by `signing.freeze` and
#: nothing else): the stamp goes under them, and the paper instructions
#: would ask for a date, a signature and a « Lu et approuvé » nobody writes
#: by hand there. Same size, same grey, same place; the paper sheet keeps
#: its own words.
EMPLOYEE_ELECTRONIC_NOTE = "Signature électronique du salarié"
EMPLOYER_ELECTRONIC_NOTE = "Signature électronique de l'employeur"


# -- Text: what cp1252 can print, and how wide it is -----------------------------------------------------------

# Characters cp1252 lacks that have an exact equivalent in it. Printed as
# « ? » they would read as a character lost; they are only typography.
# Named, not typed: half of them are spaces nobody can tell apart on screen.
_EQUIVALENTS = str.maketrans(
    {
        "\N{MINUS SIGN}": "-",  # the app's negative figures (timesheet.MINUS)
        "\N{HYPHEN}": "-",
        "\N{NON-BREAKING HYPHEN}": "-",
        "\N{EN SPACE}": " ",
        "\N{EM SPACE}": " ",
        "\N{FIGURE SPACE}": " ",
        "\N{THIN SPACE}": " ",
        "\N{NARROW NO-BREAK SPACE}": " ",  # before « : ; ! ? » in French typography
    }
)


def printable(text) -> str:
    """`text` as the page can print it: one line (whitespace runs, line
    breaks and tabs are one space), composed (an « é » typed as « e » and
    a combining accent is one character, which cp1252 has), invisible
    format characters dropped (a soft hyphen would print as a visible one),
    and « ? » for every character cp1252 does not have."""
    text = unicodedata.normalize("NFC", "" if text is None else str(text))
    text = " ".join(text.translate(_EQUIVALENTS).split())
    shown = []
    for char in text:
        category = unicodedata.category(char)
        if category == "Cf":
            continue
        if category == "Cc":
            shown.append("?")
            continue
        shown.append(char if _in_cp1252(char) else "?")
    return "".join(shown)


def _in_cp1252(text: str) -> bool:
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def unprintable_characters(text) -> list[str]:
    """The characters of `text` the page would print « ? » - each once, in
    order. What has an exact equivalent (a narrow space, the minus sign) or
    is invisible is not one of them. The month's form names them rather
    than sending a « ? » to be signed (staff.timesheet)."""
    text = unicodedata.normalize("NFC", "" if text is None else str(text)).translate(_EQUIVALENTS)
    found = []
    for char in text:
        if unicodedata.category(char) in ("Cc", "Cf") or char.isspace() or _in_cp1252(char) or char in found:
            continue
        found.append(char)
    return found


#: Letters no decomposition brings back to one cp1252 has.
_NEAREST_LETTERS = str.maketrans(
    {
        "Ł": "L", "ł": "l", "Đ": "D", "đ": "d", "Ħ": "H", "ħ": "h", "ı": "i", "Ŧ": "T", "ŧ": "t",
        "Ŋ": "N", "ŋ": "n", "Ə": "E", "ə": "e", "Ɖ": "D", "ɖ": "d", "ĸ": "k",
    }
)


def _nearest(char: str) -> str:
    """A letter cp1252 lacks as the nearest it has: its accents taken off
    one at a time, the last first, until what is left prints (« ễ » is
    « ê », « ị » is « i »), a compatibility form spelt out (« ﬁ » is
    « fi »), a few letters by name (« Ł » is « L »); else the letter as it
    is, which `printable` then prints « ? »."""
    named = char.translate(_NEAREST_LETTERS)
    if named != char:
        return named
    for form in ("NFD", "NFKD"):
        decomposed = unicodedata.normalize(form, char)
        while decomposed:
            candidate = unicodedata.normalize("NFC", decomposed)
            if _in_cp1252(candidate):
                return candidate
            if not unicodedata.combining(decomposed[-1]):
                break
            decomposed = decomposed[:-1]
    return char


def printable_name(text) -> str:
    """A person's or an establishment's name as the page prints it: like
    `printable`, except that a letter cp1252 lacks is written as its nearest
    one (« WÓJCIK Łukasz » is « WÓJCIK Lukasz », « NGUYỄN » « NGUYÊN »)
    rather than « ? » - a letter missing is a name misspelt, an accent less
    is still his name. The sheet's « Salarié : » line and the stamps of the
    signatures (staff.signing) print names through it."""
    text = unicodedata.normalize("NFC", "" if text is None else str(text)).translate(_EQUIVALENTS)
    return printable("".join(char if _in_cp1252(char) else _nearest(char) for char in text))


_WIDTHS = {font: FONT_METRICS[font][1] for font in (REGULAR, BOLD)}
# The Adobe metrics predate the euro, which WinAnsiEncoding draws at 0x80:
# 556 in both fonts, like a digit.
_EXTRA_WIDTHS = {"€": 556}


def _char_width(char: str, font: str) -> float:
    widths = _WIDTHS[font]
    width = widths.get(char)
    if width is None:
        width = _EXTRA_WIDTHS.get(char, widths["?"])
    return width


def text_width(text: str, font: str = REGULAR, size: float = TEXT_SIZE) -> float:
    """The width in points of `text` (already `printable`) set in `font` at
    `size` - the sum of its glyphs' advance widths, as a reader lays it out
    (the standard fonts are not kerned)."""
    return sum(_char_width(char, font) for char in text) * size / 1000


ELLIPSIS = "…"


def fit(text: str, width: float, font: str = REGULAR, size: float = TEXT_SIZE) -> str:
    """`text` as it fits in `width` points: whole when it does, else cut
    and ended with « … » (which fits too). Never wider than `width`."""
    if text_width(text, font, size) <= width:
        return text
    room = width - text_width(ELLIPSIS, font, size)
    used, kept = 0.0, 0
    for char in text:
        advance = _char_width(char, font) * size / 1000
        if used + advance > room:
            break
        used += advance
        kept += 1
    cut = text[:kept].rstrip()
    return cut + ELLIPSIS if room >= 0 else ""


def wrap(text: str, width: float, font: str = REGULAR, size: float = TEXT_SIZE) -> list[str]:
    """`text` in lines no wider than `width`, broken between words; a word
    wider than a line on its own is cut (`fit`)."""
    lines: list[str] = []
    for word in text.split(" "):
        candidate = f"{lines[-1]} {word}" if lines else word
        if lines and text_width(candidate, font, size) <= width:
            lines[-1] = candidate
        else:
            lines.append(fit(word, width, font, size))
    return lines


# -- Drawing ----------------------------------------------------------------------------------------------------


def _number(value: float) -> str:
    """A coordinate as PDF writes it: at most two decimals, no trailing zero."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _pdf_string(text: str) -> str:
    """A literal string for the content stream, from `printable` text:
    cp1252 bytes, the three delimiters escaped, every byte outside printable
    ASCII written in octal - so the stream stays plain ASCII, and a line
    break can never reach it (a reader turns a raw CR inside a string into
    an LF)."""
    data = text.encode("cp1252", errors="replace")
    escaped = []
    for byte in data:
        if byte in (0x28, 0x29, 0x5C):   # ( ) \
            escaped.append("\\" + chr(byte))
        elif 0x20 <= byte < 0x7F:
            escaped.append(chr(byte))
        else:
            escaped.append(f"\\{byte:03o}")
    return "(" + "".join(escaped) + ")"


class Canvas:
    """The page's content stream, drawn from the top: `y` is a baseline or
    an edge in PDF coordinates (0 at the foot of the page). Its fonts are
    named /F1 (Helvetica) and /F2 (Helvetica-Bold) - `FONT_RESOURCES`: a
    stream drawn with it needs those two in its resources. The signature's
    stamp (`staff.signing`) and the proof file (`staff.proof`) draw with it
    too, so there is one way to set French text in these PDFs."""

    def __init__(self):
        self.operations: list[str] = []

    def text(self, x, y, text, *, font=REGULAR, size=TEXT_SIZE, grey=BLACK, align="left") -> float:
        """Draw `text` - `printable` already, and not made so again here:
        that would strip the space a piece of a line starts with - with its
        left edge, right edge or centre at `x`; returns its width."""
        if not text:
            return 0.0
        width = text_width(text, font, size)
        if align == "right":
            x -= width
        elif align == "center":
            x -= width / 2
        self.operations.append(
            f"{_number(grey)} g BT /{FONT_RESOURCES[font]} {_number(size)} Tf "
            f"{_number(x)} {_number(y)} Td {_pdf_string(text)} Tj ET"
        )
        return width

    def line(self, x1, y1, x2, y2, *, width=0.4, grey=RULE_GREY):
        self.operations.append(
            f"{_number(grey)} G {_number(width)} w {_number(x1)} {_number(y1)} m {_number(x2)} {_number(y2)} l S"
        )

    def band(self, x, y, width, height, *, grey=BAND_GREY):
        self.operations.append(f"{_number(grey)} g {_number(x)} {_number(y)} {_number(width)} {_number(height)} re f")

    def box(self, x, y, width, height, *, line_width=0.6, grey=BOX_GREY):
        self.operations.append(
            f"{_number(grey)} G {_number(line_width)} w "
            f"{_number(x)} {_number(y)} {_number(width)} {_number(height)} re S"
        )

    def content(self) -> bytes:
        return ("\n".join(self.operations) + "\n").encode("ascii")


# -- The sheet --------------------------------------------------------------------------------------------------


def _establishment_lines(establishment) -> tuple[str, list[str]]:
    """The header: the establishment's name and at most `ADDRESS_LINES`
    lines of its address, the rest joined onto the last. No name, no header
    at all - not a placeholder, which is worse than none, and not the
    address alone under nothing: « Personnel » says so where the header is
    typed (« Sans nom, les fiches s'impriment sans en-tête ») and warns when
    an address is saved without one."""
    if establishment is None:
        return "", []
    name = printable_name(getattr(establishment, "name", "")).strip()
    if not name:
        return "", []
    lines = [printable(line) for line in getattr(establishment, "address_lines", [])]
    lines = [line for line in lines if line]
    if len(lines) > ADDRESS_LINES:
        lines = lines[: ADDRESS_LINES - 1] + [", ".join(lines[ADDRESS_LINES - 1 :])]
    return name, lines


def _draw_header(canvas: Canvas, top: float, establishment) -> float:
    name, address = _establishment_lines(establishment)
    y = top
    if name:
        canvas.text(LEFT, y - 11, fit(name, RIGHT - LEFT, BOLD, 11.5), font=BOLD, size=11.5)
        y -= 14.5
    for line in address:
        canvas.text(LEFT, y - 8.5, fit(line, RIGHT - LEFT, REGULAR, 9), size=9)
        y -= 11.5
    return y - 10 if y != top else y


def _draw_title(canvas: Canvas, top: float, sheet: MonthSheet) -> float:
    centre = PAGE_WIDTH / 2
    title = fit(printable(f"Fiche de temps — {sheet.title}"), RIGHT - LEFT, BOLD, TITLE_SIZE)
    canvas.text(centre, top - 14, title, font=BOLD, size=TITLE_SIZE, align="center")

    label = "Salarié : "
    label_width = text_width(label, REGULAR, 10.5)
    name = fit(printable_name(sheet.employee_name), RIGHT - LEFT - label_width, BOLD, 10.5)
    start = centre - (label_width + text_width(name, BOLD, 10.5)) / 2
    canvas.text(start, top - 32, label, size=10.5)
    canvas.text(start + label_width, top - 32, name, font=BOLD, size=10.5)

    week = printable(f"Semaine type : {sheet.weekly_hours_text} h")
    canvas.text(centre, top - 45, week, size=TEXT_SIZE, grey=GREY, align="center")
    return top - 58


def _rows(sheet: MonthSheet) -> list[MonthDay | MonthWeek]:
    """The table, top to bottom: each week's days, then its total."""
    rows: list[MonthDay | MonthWeek] = []
    for week in sheet.weeks:
        rows.extend(week.days)
        rows.append(week)
    return rows


def _row_height(table_top: float, rows: int, summary_height: float) -> float:
    """What each row gets of the room between the table's header and the
    summary, the signatures anchored at the foot of the page."""
    room = (
        table_top
        - TABLE_HEADER_HEIGHT
        - GAP_AFTER_TABLE
        - summary_height
        - GAP_BEFORE_SIGNATURES
        - SIGNATURE_HEIGHT
        - MARGIN_BOTTOM
    )
    return min(MAX_ROW_HEIGHT, room / max(rows, 1))


def _note_segments(day: MonthDay) -> list[tuple[str, float]]:
    """The « Motif / note » cell as (text, grey) pieces: an absence's label
    and the note (`MonthDay.note_text`), and - on a public holiday worked or
    taken as another absence - the holiday's name, in grey, first (a public
    holiday inside a week of paid leave is not a day of leave). Not on a
    « Repos » day: a day off prints blank, like the owner's sheets."""
    note = printable(day.note_text)
    segments = []
    if day.holiday and day.kind not in (PUBLIC_HOLIDAY, REST):
        segments.append((printable(f"Férié : {day.holiday}"), GREY))
        if note:
            segments.append((f" — {note}", BLACK))
    elif note:
        segments.append((note, BLACK))
    return segments


def note_cells(day: MonthDay) -> list[tuple[str, float]]:
    """The « Motif / note » cell EXACTLY as the page prints it: its pieces
    (`_note_segments`) one after the other, the one reaching the column's
    edge cut with « … » and those after it dropped, every character cp1252
    lacks a « ? ». The employee's page shows these very words
    (`signature_requests.month_snapshot`): the note he reads on his phone is
    the note he signs, never the whole of one the PDF cut."""
    cells = []
    x = NOTE_X
    for text, grey in _note_segments(day):
        shown = fit(text, NOTE_RIGHT - x, REGULAR, TEXT_SIZE)
        if shown:
            cells.append((shown, grey))
        x += text_width(shown, REGULAR, TEXT_SIZE)
        if shown != text:
            break
    return cells


def _draw_table(canvas: Canvas, top: float, rows, row_height: float) -> float:
    header_baseline = top - 11
    canvas.text(DAY_X, header_baseline, "Jour", font=BOLD, size=8.5)
    canvas.text(HOURS_RIGHT, header_baseline, "Heures", font=BOLD, size=8.5, align="right")
    canvas.text(NOTE_X, header_baseline, "Motif / note", font=BOLD, size=8.5)
    y = top - TABLE_HEADER_HEIGHT
    canvas.line(LEFT, y, RIGHT, y, width=0.8, grey=0.25)

    # The baseline sits so that a capital is centred in its row.
    drop = row_height / 2 + TEXT_SIZE * 0.36
    for row in rows:
        bottom = y - row_height
        baseline = y - drop
        if isinstance(row, MonthWeek):
            canvas.band(LEFT, bottom, RIGHT - LEFT, row_height)
            canvas.text(DAY_X, baseline, fit(printable(row.label), HOURS_RIGHT - 30 - DAY_X, BOLD), font=BOLD)
            canvas.text(HOURS_RIGHT, baseline, row.total_text, font=BOLD, align="right")
        else:
            canvas.text(DAY_X, baseline, row.weekday_name)
            canvas.text(DAY_NUMBER_RIGHT, baseline, str(row.date.day), align="right")
            canvas.text(HOURS_RIGHT, baseline, row.hours_text, align="right")
            x = NOTE_X
            for text, grey in note_cells(row):
                x += canvas.text(x, baseline, text, grey=grey)
            canvas.line(LEFT, bottom, RIGHT, bottom)
        y = bottom
    return y


def summary_items(sheet: MonthSheet) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The summary's figures as (label, value): the hours against the
    typical week - with, when there were absences, what the week planned
    for those days, since the difference is said without them
    (`MonthSummary.difference`) - then the days: worked, and per absence
    kind. Public: the snapshot a signature request keeps of the month
    (`staff.signature_requests.month_snapshot`) takes the same lines, so the
    phone page and the PDF cannot word the summary two ways."""
    summary = sheet.summary
    hours = [
        ("Heures travaillées", f"{summary.worked_text} h"),
        ("Prévues par la semaine type", f"{summary.planned_text} h"),
    ]
    if summary.absence_hours:
        hours.append(("dont absences", f"{summary.absence_hours_text} h"))
    hours.append((summary.difference_label, summary.difference_text))
    days = [("Jours travaillés", summary.days_worked_text)]
    days += [(absence.label, absence.days_text) for absence in summary.absences]
    if not summary.absences:
        days.append(("Absences", "aucune"))
    return hours, days


def _draw_item(canvas: Canvas, x: float, y: float, width: float, label: str, value: str):
    """« Heures travaillées : 151,5 h », the figure in bold; the label is what
    gives way when the column is too narrow for both."""
    value = printable(value)
    label = fit(printable(label) + " : ", max(width - text_width(value, BOLD), 0), REGULAR)
    x += canvas.text(x, y, label)
    canvas.text(x, y, value, font=BOLD)


def _summary_lines(hours, days) -> int:
    """How many lines the summary's box holds: the hours in one column, the
    days in two - never fewer than `SUMMARY_LINES`, and never so few that an
    item is left out."""
    return max(SUMMARY_LINES, len(hours), -(-len(days) // 2))


def _summary_height(lines: int) -> float:
    return SUMMARY_TITLE_HEIGHT + lines * SUMMARY_LINE_HEIGHT + 8


def _draw_summary(canvas: Canvas, top: float, hours, days) -> float:
    lines = _summary_lines(hours, days)
    height = _summary_height(lines)
    bottom = top - height
    canvas.band(LEFT, top - SUMMARY_TITLE_HEIGHT, RIGHT - LEFT, SUMMARY_TITLE_HEIGHT)
    canvas.box(LEFT, bottom, RIGHT - LEFT, height)
    canvas.text(LEFT + PAD, top - 10.5, "Récapitulatif du mois", font=BOLD, size=9.5)

    # Three columns: the hours, then the days - worked, then per kind of
    # absence - down the second column and on into the third.
    columns = [
        (LEFT + PAD, 188.0, hours),
        (LEFT + 200, 150.0, days[:lines]),
        (LEFT + 355, NOTE_RIGHT - (LEFT + 355), days[lines:]),
    ]
    first_baseline = top - SUMMARY_TITLE_HEIGHT - 12.5
    for x, width, items in columns:
        for index, (label, value) in enumerate(items):
            _draw_item(canvas, x, first_baseline - index * SUMMARY_LINE_HEIGHT, width, label, value)
    return bottom


SIGNATURE_WIDTH = (RIGHT - LEFT - SIGNATURE_GAP) / 2
SIGNATURE_LABEL_SIZE = 10.0
SIGNATURE_NOTE_SIZE = 8.0
#: From the box's top: the label's baseline, then the first line of its
#: instructions, then each further line.
SIGNATURE_LABEL_DROP = 14.0
SIGNATURE_NOTE_DROP = 26.0
SIGNATURE_NOTE_LEADING = 10.0
#: What an electronic stamp keeps clear of the frame, and of the lowest line
#: the box prints (a descender of 8-point Helvetica is under 2 points).
STAMP_CLEARANCE = 3.0


@dataclass(frozen=True)
class SignatureBox:
    """One of the two boxes at the foot of the sheet, in PDF points from the
    foot of the page. The page draws them from here (`_draw_signatures`) and
    the electronic signature puts its field and its stamp where `stamp_rect`
    says (`staff.signing`): under the label and its instructions, inside the
    frame. A number written twice would let the two drift apart the day the
    page is laid out again."""

    label: str
    note: str
    left: float
    bottom: float = MARGIN_BOTTOM

    @property
    def right(self) -> float:
        return self.left + SIGNATURE_WIDTH

    @property
    def top(self) -> float:
        return self.bottom + SIGNATURE_HEIGHT

    @property
    def rect(self) -> tuple[float, float, float, float]:
        """(left, bottom, right, top): the frame."""
        return (self.left, self.bottom, self.right, self.top)

    @property
    def note_lines(self) -> list[str]:
        return wrap(printable(self.note), SIGNATURE_WIDTH - 2 * PAD, REGULAR, SIGNATURE_NOTE_SIZE)

    @property
    def printed_bottom(self) -> float:
        """The lowest baseline the box prints (its last line of
        instructions)."""
        return self.top - SIGNATURE_NOTE_DROP - (len(self.note_lines) - 1) * SIGNATURE_NOTE_LEADING

    @property
    def stamp_rect(self) -> tuple[float, float, float, float]:
        """(left, bottom, right, top): where an electronic signature's field
        and stamp go - aligned with the label on the left, clear of the frame
        and of the printed instructions above."""
        return (
            self.left + PAD,
            self.bottom + STAMP_CLEARANCE,
            self.right - PAD,
            self.printed_bottom - 2 * STAMP_CLEARANCE,
        )


EMPLOYEE_BOX = SignatureBox(EMPLOYEE_SIGNATURE, EMPLOYEE_SIGNATURE_NOTE, LEFT)
EMPLOYER_BOX = SignatureBox(EMPLOYER_SIGNATURE, EMPLOYER_SIGNATURE_NOTE, LEFT + SIGNATURE_WIDTH + SIGNATURE_GAP)
SIGNATURE_BOXES = (EMPLOYEE_BOX, EMPLOYER_BOX)
#: The two boxes of the document frozen for the electronic signature: the
#: same frames, their instructions replaced (`EMPLOYEE_ELECTRONIC_NOTE`).
#: `signing.freeze` places its fields by THESE `stamp_rect`s - under what
#: this document prints, which is not what the paper sheet prints.
ELECTRONIC_SIGNATURE_BOXES = (
    replace(EMPLOYEE_BOX, note=EMPLOYEE_ELECTRONIC_NOTE),
    replace(EMPLOYER_BOX, note=EMPLOYER_ELECTRONIC_NOTE),
)


def signature_boxes(electronic: bool = False) -> tuple[SignatureBox, SignatureBox]:
    """The boxes the sheet draws: the paper ones, or - for the document
    frozen to be signed electronically - `ELECTRONIC_SIGNATURE_BOXES`."""
    return ELECTRONIC_SIGNATURE_BOXES if electronic else SIGNATURE_BOXES


def _draw_signatures(canvas: Canvas, boxes=SIGNATURE_BOXES):
    for box in boxes:
        canvas.box(box.left, box.bottom, SIGNATURE_WIDTH, SIGNATURE_HEIGHT)
        canvas.text(box.left + PAD, box.top - SIGNATURE_LABEL_DROP, box.label, font=BOLD, size=SIGNATURE_LABEL_SIZE)
        for index, line in enumerate(box.note_lines):
            canvas.text(
                box.left + PAD,
                box.top - SIGNATURE_NOTE_DROP - index * SIGNATURE_NOTE_LEADING,
                line,
                size=SIGNATURE_NOTE_SIZE,
                grey=GREY,
            )


# -- The file ---------------------------------------------------------------------------------------------------


def _text_string(text: str) -> bytes:
    """A PDF text string for the document's properties (its title): UTF-16BE
    with its byte order mark, which a reader shows whole - accents and any
    script - where the page itself is limited to cp1252."""
    data = ("\N{BYTE ORDER MARK}" + " ".join(str(text).split())).encode("utf-16-be", errors="replace")
    return b"<" + data.hex().upper().encode("ascii") + b">"


def font_dictionary(font: str) -> bytes:
    """The dictionary of a standard font as the pages name it (`Canvas`'s
    /F1 and /F2): Type 1, nothing embedded, WinAnsiEncoding."""
    return f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} /Encoding /WinAnsiEncoding >>".encode("ascii")


def write_document(contents: list[bytes], title: str) -> bytes:
    """A PDF 1.4 of one A4 page per content stream (`Canvas.content()`):
    catalog, pages, each page and its Flate-compressed content, the two
    fonts and the properties, then a cross-reference table whose offsets are
    counted, not guessed. A timesheet is one page; the proof file of a
    signature may run to several."""
    if not contents:
        raise ValueError("Un PDF a au moins une page.")
    count = len(contents)
    # Pages and their contents come in pairs from object 3; the two fonts
    # and the properties follow them. For one page this is the numbering the
    # timesheets were always written with (3, 4, then 5, 6, 7).
    regular, bold = 3 + 2 * count, 4 + 2 * count
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode("ascii"),
    ]
    for index, content in enumerate(contents):
        stream = zlib.compress(content, 9)
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_number(PAGE_WIDTH)} {_number(PAGE_HEIGHT)}] "
                f"/Resources << /Font << /F1 {regular} 0 R /F2 {bold} 0 R >> >> /Contents {4 + 2 * index} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
    objects += [
        font_dictionary(REGULAR),
        font_dictionary(BOLD),
        b"<< /Title " + _text_string(title) + b" /Producer (MarginMate) >>",
    ]
    # The binary comment tells a transfer program the file is not text.
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    output += b"0000000000 65535 f \n"
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode("ascii")
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(output)


def render_month_pdf(sheet: MonthSheet, establishment=None, *, electronic: bool = False) -> bytes:
    """The month's timesheet, one A4 page, as the bytes of a PDF file.

    `sheet` is `timesheet.month_sheet(employee, month)` - saved or not: a
    month nobody saved prints its typical week, which is exactly what saving
    it would store. `establishment` is `Establishment.current()` (anything
    with a `name` and `address_lines`; None prints no header). Reads nothing
    from the database beyond what `sheet` already holds.

    `electronic` is for `signing.freeze` ONLY: the signature boxes then say
    « Signature électronique du salarié / de l'employeur » where the paper
    sheet asks for a handwritten date, signature and « Lu et approuvé »
    (`ELECTRONIC_SIGNATURE_BOXES`). Left out, the sheet is the paper one,
    byte for byte what it always was - the download « Personnel » offers."""
    canvas = Canvas()
    y = _draw_header(canvas, PAGE_HEIGHT - MARGIN_TOP, establishment)
    y = _draw_title(canvas, y, sheet)
    rows = _rows(sheet)
    hours, days = summary_items(sheet)
    summary_height = _summary_height(_summary_lines(hours, days))
    y = _draw_table(canvas, y, rows, _row_height(y, len(rows), summary_height))
    _draw_summary(canvas, y - GAP_AFTER_TABLE, hours, days)
    _draw_signatures(canvas, signature_boxes(electronic))
    return write_document([canvas.content()], f"Fiche de temps — {sheet.employee_name} — {sheet.label}")


# -- The file's name --------------------------------------------------------------------------------------------

# Characters no file name may hold on Windows (the owner's machine), and
# the controls: a name typed with a slash must not become a folder.
_UNSAFE_IN_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_NOT_ASCII_SAFE = re.compile(r"[^A-Za-z0-9 ._,()'-]")


def safe_filename(name: str) -> str:
    """`name` as a file may be called on Windows: no slash (a folder), no
    quote, no control character - each a « - » - and its spaces single."""
    return " ".join(_UNSAFE_IN_FILENAME.sub("-", unicodedata.normalize("NFC", name)).split())


def pdf_filename(sheet: MonthSheet) -> str:
    """« Fiche de temps DUPONT Jeanne juin 2026.pdf »."""
    return f"{safe_filename(f'Fiche de temps {sheet.employee_name} {sheet.label}')}.pdf"


def ascii_filename(filename: str) -> str:
    """`filename` for a client that does not read RFC 5987: the accents
    taken off (« Fiche de temps DUPONT Jeanne aout 2026.pdf »), anything
    else outside plain ASCII an underscore."""
    decomposed = unicodedata.normalize("NFKD", filename)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _NOT_ASCII_SAFE.sub("_", stripped)


def disposition(filename: str, *, inline: bool = False) -> str:
    """A `Content-Disposition` header for `filename` (made safe first): an
    attachment - or `inline`, shown in the browser - with the ASCII fallback
    first and the exact name in RFC 5987's `filename*` (percent-encoded
    UTF-8), which every current browser prefers. Plain ASCII, so no header
    encoding can mangle it. The signature's files use it too
    (staff/signature_views.py, staff/public_views.py)."""
    filename = safe_filename(filename)
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{ascii_filename(filename)}\"; filename*=UTF-8''{quote(filename, safe='')}"


def content_disposition(sheet: MonthSheet) -> str:
    """The `Content-Disposition` header of the month's download: an
    attachment named `pdf_filename`."""
    return disposition(pdf_filename(sheet))
