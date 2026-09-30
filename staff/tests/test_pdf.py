"""The month's timesheet as a PDF (`staff/pdf.py`), read back the way a
reader reads it: through pdfplumber, the words where they landed on the
page. Every name and address here is INVENTED - the repository is public
and a timesheet is personal data."""

import hashlib
import io
import re
import zlib
from datetime import date
from decimal import Decimal

import pdfplumber
from django.http import HttpResponse
from django.test import SimpleTestCase, TestCase

from staff.models import ABSENCE_KINDS, Establishment
from staff.pdf import (
    ADDRESS_LINES,
    ELECTRONIC_SIGNATURE_BOXES,
    EMPLOYEE_ELECTRONIC_NOTE,
    EMPLOYEE_SIGNATURE_NOTE,
    EMPLOYER_ELECTRONIC_NOTE,
    EMPLOYER_SIGNATURE_NOTE,
    GREY,
    HOURS_RIGHT,
    MARGIN_BOTTOM,
    MARGIN_TOP,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    RIGHT,
    SIGNATURE_BOXES,
    SUMMARY_LINES,
    _pdf_string,
    _summary_lines,
    ascii_filename,
    content_disposition,
    fit,
    pdf_filename,
    printable,
    printable_name,
    render_month_pdf,
    signature_boxes,
    text_width,
    wrap,
)
from staff.tests.support import employee, fields_as_drawn
from staff.timesheet import (
    DayEntry,
    PostedDay,
    build_sheet,
    month_days,
    month_sheet,
    read_posted_month,
    save_month,
)

JUNE = date(2026, 6, 1)  # starts on a Monday, 30 days
MARCH = date(2026, 3, 1)  # starts on a Sunday, 31 days: six week totals, the most a month can have
MAY = date(2026, 5, 1)  # four public holidays
AUGUST = date(2026, 8, 1)  # « Mois d'août », starts on a Saturday: six week totals too

BAR = Establishment(name="BAR EXEMPLE", address="12 rue Imaginaire\n75000 PARIS")

#: A long note: the 255 characters the column allows, far wider than the page.
LONG_NOTE = ("Remplacement imprévu en salle et fermeture tardive après l'inventaire des fûts. " * 4)[:255]


def pdf_of(sheet, establishment=BAR):
    return render_month_pdf(sheet, establishment)


def opened(data):
    return pdfplumber.open(io.BytesIO(data))


def lines_of(data):
    with opened(data) as pdf:
        return pdf.pages[0].extract_text().splitlines()


def table_of(data):
    """The table's lines, from its header to the last week total."""
    lines = lines_of(data)
    start = lines.index("Jour Heures Motif / note")
    end = lines.index("Récapitulatif du mois")
    return lines[start + 1 : end]


def planned(month, person=None):
    return build_sheet(person or employee(save=False), month)


def changed(month, entries, person=None):
    return build_sheet(person or employee(save=False), month, entries)


def worst_case_sheet():
    """March 2026 - 31 days from a Sunday, six week totals - with a long
    note on every day and an absence every fifth day."""
    person = employee(save=False, last_name="De La Fontaine-Beaumarchais", first_name="Marie-Éléonore Joséphine")
    entries = []
    for index, day in enumerate(month_days(MARCH)):
        if day.weekday() == 6:
            entries.append(DayEntry(day, Decimal("0"), "repos", LONG_NOTE))
        elif index % 5 == 0:
            entries.append(DayEntry(day, Decimal("0"), "maladie", LONG_NOTE))
        else:
            entries.append(DayEntry(day, Decimal("10.75"), "travail", LONG_NOTE))
    return changed(MARCH, entries, person)


LONG_ESTABLISHMENT = Establishment(
    name="BAR EXEMPLE ET FILS, SOCIÉTÉ À RESPONSABILITÉ LIMITÉE AU CAPITAL DE 10 000 € — ÉTABLISSEMENT SECONDAIRE",
    address="Bâtiment B, 3e étage\n12 rue Imaginaire\nCour intérieure\n75000 PARIS\nFRANCE\nSIRET 000 000 000 00000",
)


class FileTests(SimpleTestCase):
    """One A4 page of PDF 1.4, standard fonts, a compressed content stream
    and a cross-reference table a reader can trust."""

    def test_one_a4_portrait_page(self):
        with opened(pdf_of(planned(JUNE))) as pdf:
            self.assertEqual(len(pdf.pages), 1)
            self.assertAlmostEqual(float(pdf.pages[0].width), 595.28, places=2)
            self.assertAlmostEqual(float(pdf.pages[0].height), 841.89, places=2)

    def test_the_structure_is_pdf_1_4_with_a_correct_cross_reference_table(self):
        data = pdf_of(planned(JUNE))
        self.assertTrue(data.startswith(b"%PDF-1.4\n"))
        self.assertTrue(data.endswith(b"%%EOF\n"))
        startxref = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", data)[1])
        self.assertTrue(data[startxref:].startswith(b"xref\n0 8\n"))
        entries = re.findall(rb"(\d{10}) (\d{5}) ([nf]) \n", data[startxref:])
        self.assertEqual(len(entries), 8)
        self.assertEqual(entries[0], (b"0000000000", b"65535", b"f"))
        for number, (offset, _generation, _kind) in enumerate(entries[1:], start=1):
            self.assertTrue(data[int(offset) :].startswith(f"{number} 0 obj\n".encode()), number)
        self.assertIn(b"/Root 1 0 R", data)

    def test_standard_fonts_in_winansi_and_a_flate_content_stream(self):
        data = pdf_of(planned(JUNE))
        self.assertIn(b"/BaseFont /Helvetica /Encoding /WinAnsiEncoding", data)
        self.assertIn(b"/BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding", data)
        self.assertNotIn(b"/FontFile", data)
        match = re.search(rb"<< /Length (\d+) /Filter /FlateDecode >>\nstream\n", data)
        length = int(match[1])
        stream = data[match.end() : match.end() + length]
        self.assertEqual(data[match.end() + length : match.end() + length + 10], b"\nendstream")
        content = zlib.decompress(stream).decode("ascii")
        self.assertIn("/F1 9 Tf", content)
        # Text is cp1252 written in octal, so the stream stays ASCII: « é » is \351.
        self.assertIn(r"(Salari\351 : ) Tj", content)

    def test_the_same_month_gives_the_same_bytes(self):
        self.assertEqual(pdf_of(planned(JUNE)), pdf_of(planned(JUNE)))

    def test_the_document_title_carries_the_whole_name(self):
        """The properties are UTF-16: a name the page must print with « ? »
        is whole there."""
        with opened(pdf_of(planned(AUGUST, employee(save=False, last_name="Nguyễn", first_name="Anh")))) as pdf:
            self.assertEqual(pdf.metadata["Title"], "Fiche de temps — NGUYỄN Anh — août 2026")


class ContentTests(SimpleTestCase):
    def test_the_header_the_title_and_the_employee(self):
        lines = lines_of(pdf_of(planned(JUNE)))
        self.assertEqual(
            lines[:6],
            [
                "BAR EXEMPLE",
                "12 rue Imaginaire",
                "75000 PARIS",
                "Fiche de temps — Mois de juin 2026",
                "Salarié : DUPONT Jeanne",
                "Semaine type : 36 h",
            ],
        )

    def test_the_title_is_centred(self):
        with opened(pdf_of(planned(JUNE))) as pdf:
            words = [word for word in pdf.pages[0].extract_words(extra_attrs=["size"]) if word["size"] == 15]
        self.assertEqual(" ".join(word["text"] for word in words), "Fiche de temps — Mois de juin 2026")
        self.assertAlmostEqual((words[0]["x0"] + words[-1]["x1"]) / 2, PAGE_WIDTH / 2, delta=0.05)

    def test_every_day_every_figure_and_every_week_total_of_a_typical_june(self):
        week = [
            "Lundi {}",
            "Mardi {} 7,5",
            "Mercredi {} 6",
            "Jeudi {} 7,5",
            "Vendredi {} 7,5",
            "Samedi {} 7,5",
            "Dimanche {}",
        ]
        expected = []
        for monday in (1, 8, 15, 22):
            expected += [row.format(monday + offset) for offset, row in enumerate(week)]
            expected.append("Total semaine 36")
        expected += ["Lundi 29", "Mardi 30 7,5", "Total (semaine incomplète) 7,5"]
        self.assertEqual(table_of(pdf_of(planned(JUNE))), expected)

    def test_a_month_that_was_not_typical(self):
        sheet = changed(
            JUNE,
            [
                DayEntry(date(2026, 6, 2), Decimal("7.25"), "travail"),
                DayEntry(date(2026, 6, 3), Decimal("9.5"), "travail", "inventaire"),
                DayEntry(date(2026, 6, 4), Decimal("4"), "travail", "demi-journée de congés"),
                DayEntry(date(2026, 6, 5), Decimal("0"), "travail", "fermé pour travaux"),
                DayEntry(date(2026, 6, 7), Decimal("5"), "travail"),
                DayEntry(date(2026, 6, 9), Decimal("0"), "conges"),
                DayEntry(date(2026, 6, 10), Decimal("0"), "repos_comp"),
                DayEntry(date(2026, 6, 11), Decimal("0"), "maladie", "certificat reçu"),
                DayEntry(date(2026, 6, 12), Decimal("0"), "absence"),
                DayEntry(date(2026, 6, 13), Decimal("0"), "ferie"),
            ],
        )
        table = table_of(pdf_of(sheet))
        self.assertEqual(
            table[:16],
            [
                "Lundi 1",
                "Mardi 2 7,25",
                "Mercredi 3 9,5 inventaire",
                "Jeudi 4 4 demi-journée de congés",
                # A « Travail » day at 0 h prints « 0 », as the owner's sheets do.
                "Vendredi 5 0 fermé pour travaux",
                "Samedi 6 7,5",
                "Dimanche 7 5",
                "Total semaine 33,25",
                "Lundi 8",
                # An absence prints its label in the note column and no hours.
                "Mardi 9 Congés payés",
                "Mercredi 10 Repos compensateur",
                "Jeudi 11 Arrêt maladie — certificat reçu",
                "Vendredi 12 Absence",
                "Samedi 13 Férié chômé",
                "Dimanche 14",
                "Total semaine 0",
            ],
        )

    def test_the_hours_are_right_aligned(self):
        sheet = changed(
            JUNE,
            [
                DayEntry(date(2026, 6, 2), Decimal("7.25"), "travail"),
                DayEntry(date(2026, 6, 3), Decimal("10"), "travail"),
            ],
        )
        with opened(pdf_of(sheet)) as pdf:
            words = pdf.pages[0].extract_words()
        table_top = next(word["top"] for word in words if word["text"] == "Jour")
        table_bottom = next(word["top"] for word in words if word["text"] == "Récapitulatif")
        figures = [
            word
            for word in words
            if table_top <= word["top"] < table_bottom and word["x0"] > 150 and word["x1"] < HOURS_RIGHT + 1
        ]
        # The first week: 7,25 + 10 + 7,5 × 3 = 39,75; the others 36; Tuesday 30 alone, 7,5.
        self.assertEqual({word["text"] for word in figures}, {"Heures", "7,25", "10", "7,5", "6", "39,75", "36"})
        for word in figures:
            self.assertAlmostEqual(word["x1"], HOURS_RIGHT, delta=0.05, msg=word["text"])

    def test_the_summary_of_a_typical_month(self):
        lines = lines_of(pdf_of(planned(JUNE)))
        summary = lines[lines.index("Récapitulatif du mois") + 1 :]
        self.assertEqual(
            summary[:3],
            [
                "Heures travaillées : 151,5 h Jours travaillés : 21 jours",
                "Prévues par la semaine type : 151,5 h Absences : aucune",
                "Écart : 0 h",
            ],
        )

    def test_the_summary_counts_every_kind_of_absence(self):
        """Every kind of absence, and the difference below the typical week
        printed with a minus cp1252 has (the app's own « − » is not in it)."""
        kinds = ["conges", "repos_comp", "ferie", "maladie", "absence"]
        sheet = changed(
            AUGUST, [DayEntry(day, Decimal("0"), kinds[index % 5]) for index, day in enumerate(month_days(AUGUST))]
        )
        text = "\n".join(lines_of(pdf_of(sheet)))
        for fragment in (
            "Heures travaillées : 0 h",
            # Five Saturdays and four of every other working day: 37,5 + 4 × (7,5 + 6 + 7,5 + 7,5).
            "Prévues par la semaine type : 151,5 h",
            # Every day an absence: none of it is a shortfall.
            "dont absences : 151,5 h",
            "Écart hors absences : 0 h",
            "Jours travaillés : 0 jours",
            "Congés payés : 7 jours",
            "Repos compensateur : 6 jours",
            "Férié chômé : 6 jours",
            "Arrêt maladie : 6 jours",
            "Absence : 6 jours",
        ):
            self.assertIn(fragment, text)
        self.assertNotIn("?", text)

    def test_the_summary_box_holds_every_kind_of_absence(self):
        """Days worked and one line per kind of absence fit the box's two
        columns as they are; were a kind added, the box would grow a line
        rather than drop one (and the one-page tests would say whether the
        page still fits)."""
        self.assertLessEqual(len(ABSENCE_KINDS) + 1, 2 * SUMMARY_LINES)
        hours = [("Heures", "1 h")] * 3
        self.assertEqual(_summary_lines(hours, [("Jours", "1 jour")] * 6), SUMMARY_LINES)
        self.assertEqual(_summary_lines(hours, [("Jours", "1 jour")] * 7), SUMMARY_LINES + 1)
        self.assertEqual(_summary_lines(hours, [("Jours", "1 jour")] * 2), SUMMARY_LINES)
        # With absences, the hours take a fourth line (« dont absences »).
        self.assertEqual(_summary_lines(hours + [("dont", "1 h")], [("Jours", "1 jour")] * 6), SUMMARY_LINES + 1)

    def test_a_difference_above_the_typical_week(self):
        sheet = changed(JUNE, [DayEntry(date(2026, 6, 7), Decimal("3"), "travail")])
        self.assertIn("Écart : +3 h", "\n".join(lines_of(pdf_of(sheet))))

    def test_a_week_of_leave_prints_no_shortfall(self):
        """What the employee signs under « Lu et approuvé »: the leave's
        hours said as such, and a difference of 0 h - not « Écart : -36 h »
        (review, 28/09)."""
        leave = [DayEntry(date(2026, 6, day), Decimal("0"), "conges") for day in range(9, 14)]
        lines = lines_of(pdf_of(changed(JUNE, leave)))
        summary = lines[lines.index("Récapitulatif du mois") + 1 :]
        self.assertEqual(
            summary[:4],
            [
                "Heures travaillées : 115,5 h Jours travaillés : 16 jours",
                "Prévues par la semaine type : 151,5 h Congés payés : 5 jours",
                "dont absences : 36 h",
                "Écart hors absences : 0 h",
            ],
        )

    def test_a_public_holiday_says_so_worked_or_not(self):
        """1 May and the Ascension worked (as planned), 8 May marked off:
        each says which holiday it is. The Pentecost Monday falls on the
        typical week's day off, and a day off prints blank."""
        sheet = changed(MAY, [DayEntry(date(2026, 5, 8), Decimal("0"), "ferie")])
        data = pdf_of(sheet)
        table = table_of(data)
        self.assertIn("Vendredi 1 7,5 Férié : Fête du Travail", table)
        self.assertIn("Vendredi 8 Férié chômé — Victoire 1945", table)
        self.assertIn("Jeudi 14 7,5 Férié : Ascension", table)
        self.assertIn("Lundi 25", table)
        # Worked, the holiday is said in grey: it is information, not an absence's label.
        with opened(data) as pdf:
            words = pdf.pages[0].extract_words(extra_attrs=["non_stroking_color"])
        colours = {word["text"]: word["non_stroking_color"] for word in words}
        self.assertNotEqual(colours["Ascension"], (0.0,))
        self.assertEqual(colours["Victoire"], (0.0,))

    def test_a_worked_holiday_with_a_note(self):
        sheet = changed(AUGUST, [DayEntry(date(2026, 8, 15), Decimal("6"), "travail", "fermeture à 20 h")])
        self.assertIn("Samedi 15 6 Férié : Assomption — fermeture à 20 h", table_of(pdf_of(sheet)))

    def test_a_holiday_inside_a_leave_says_so(self):
        """A public holiday inside a week of paid leave is not a day of
        leave: the sheet shows which day it was, beside the absence's label."""
        sheet = changed(AUGUST, [DayEntry(date(2026, 8, day), Decimal("0"), "conges") for day in range(11, 16)])
        table = table_of(pdf_of(sheet))
        self.assertIn("Vendredi 14 Congés payés", table)
        self.assertIn("Samedi 15 Férié : Assomption — Congés payés", table)

    def test_accents_survive(self):
        text = "\n".join(lines_of(pdf_of(planned(AUGUST))))
        for fragment in (
            "Fiche de temps — Mois d'août 2026",
            "Salarié : DUPONT Jeanne",
            "Total (semaine incomplète)",
            "Récapitulatif du mois",
            "Prévues par la semaine type",
            "Écart",
            "Samedi 15 7,5 Férié : Assomption",
            "Le salarié",
            "L'employeur",
            "Date et signature, précédées de la mention “Lu et approuvé”",
        ):
            self.assertIn(fragment, text)

    def test_two_signature_boxes_side_by_side_tall_enough_to_sign(self):
        with opened(pdf_of(planned(JUNE))) as pdf:
            page = pdf.pages[0]
            boxes = sorted(
                (rect for rect in page.rects if rect["stroke"] and not rect["fill"] and rect["top"] > 700),
                key=lambda rect: rect["x0"],
            )
            self.assertEqual(len(boxes), 2)
            employee_box, employer_box = boxes
            self.assertEqual(employee_box["top"], employer_box["top"])
            self.assertLess(employee_box["x1"], employer_box["x0"])
            for box in boxes:
                # 30 mm at least, under the box's own two lines of print.
                self.assertGreaterEqual(box["bottom"] - box["top"], 85)
                self.assertAlmostEqual(box["bottom"], PAGE_HEIGHT - MARGIN_BOTTOM, places=2)
            self.assertEqual(
                page.crop(
                    (employee_box["x0"], employee_box["top"], employee_box["x1"], employee_box["bottom"])
                ).extract_text(),
                "Le salarié\nDate et signature, précédées de la mention “Lu et approuvé”",
            )
            self.assertEqual(
                page.crop(
                    (employer_box["x0"], employer_box["top"], employer_box["x1"], employer_box["bottom"])
                ).extract_text(),
                "L'employeur\nDate et signature",
            )

    def test_a_long_note_is_cut_with_an_ellipsis(self):
        sheet = changed(JUNE, [DayEntry(date(2026, 6, 3), Decimal("8"), "travail", LONG_NOTE)])
        data = pdf_of(sheet)
        row = next(line for line in table_of(data) if line.startswith("Mercredi 3 "))
        self.assertTrue(row.endswith("…"), row)
        self.assertTrue(row.startswith("Mercredi 3 8 Remplacement imprévu en salle"), row)
        self.assertNotIn(LONG_NOTE, "\n".join(lines_of(data)))
        with opened(data) as pdf:
            self.assertLessEqual(max(char["x1"] for char in pdf.pages[0].chars), RIGHT)

    def test_characters_cp1252_lacks_print_a_question_mark_and_never_raise(self):
        person = employee(save=False, last_name="Nguyễn", first_name="Łucja")
        sheet = changed(
            JUNE,
            [
                DayEntry(date(2026, 6, 2), Decimal("7"), "travail", "réunion 東京 😀"),
                # What cp1252 lacks but has an equivalent for: a narrow
                # no-break space before a colon, the minus sign.
                DayEntry(
                    date(2026, 6, 3),
                    Decimal("8"),
                    "travail",
                    "départ\N{NARROW NO-BREAK SPACE}: 22\N{NARROW NO-BREAK SPACE}h, écart \N{MINUS SIGN}1 h",
                ),
                # An « é » typed as « e » + a combining accent is one « é ».
                DayEntry(date(2026, 6, 4), Decimal("8"), "travail", "Jose\N{COMBINING ACUTE ACCENT} remplace"),
                DayEntry(date(2026, 6, 5), Decimal("8"), "travail", "(voir \\ plus bas)"),
            ],
            person,
        )
        data = pdf_of(sheet)
        lines = lines_of(data)
        # A name loses an accent rather than a letter (`printable_name`): the
        # stamp of his signature prints it the same way.
        self.assertIn("Salarié : NGUYÊN Lucja", lines)
        table = table_of(data)
        self.assertIn("Mardi 2 7 réunion ?? ?", table)
        self.assertIn("Mercredi 3 8 départ : 22 h, écart -1 h", table)
        self.assertIn("Jeudi 4 8 José remplace", table)
        self.assertIn("Vendredi 5 8 (voir \\ plus bas)", table)

    def test_no_establishment_prints_no_header(self):
        for establishment in (None, Establishment()):
            with self.subTest(establishment=establishment):
                self.assertEqual(
                    lines_of(pdf_of(planned(JUNE), establishment))[0], "Fiche de temps — Mois de juin 2026"
                )

    def test_an_address_without_a_name_prints_no_header(self):
        """What the page says where the header is typed (« Sans nom, les
        fiches s'impriment sans en-tête ») and warns when it is saved."""
        for name in ("", "   "):
            with self.subTest(name=name):
                establishment = Establishment(name=name, address="12 rue Imaginaire\n\n  75000 PARIS  ")
                lines = lines_of(pdf_of(planned(JUNE), establishment))
                self.assertEqual(lines[0], "Fiche de temps — Mois de juin 2026")
                self.assertNotIn("12 rue Imaginaire", lines)

    def test_an_address_is_printed_as_typed_under_the_name(self):
        establishment = Establishment(name="BAR EXEMPLE", address="12 rue Imaginaire\n\n  75000 PARIS  ")
        lines = lines_of(pdf_of(planned(JUNE), establishment))
        self.assertEqual(
            lines[:4], ["BAR EXEMPLE", "12 rue Imaginaire", "75000 PARIS", "Fiche de temps — Mois de juin 2026"]
        )

    def test_an_employee_with_no_first_name(self):
        self.assertIn(
            "Salarié : MARTIN", lines_of(pdf_of(planned(JUNE, employee(save=False, last_name="Martin", first_name=""))))
        )


def content_stream(data) -> bytes:
    """The page's content stream, decompressed: what it draws, one operation
    a line - and none of the bytes zlib chose to compress it into."""
    match = re.search(rb"<< /Length (\d+) /Filter /FlateDecode >>\nstream\n", data)
    return zlib.decompress(data[match.end() : match.end() + int(match[1])])


#: The paper sheet's content stream for invented months, as it was drawn
#: BEFORE `render_month_pdf` took its `electronic` option (28/09): what the
#: document frozen for the e-signature prints must not move the download
#: « Personnel » offers by one byte. Pinned on the stream, not the file, whose
#: bytes also depend on the zlib that compressed it. A deliberate change to
#: the paper sheet updates these - never a change meant for the frozen one.
PAPER_SHEETS = (
    (JUNE, BAR, "9be26459c7ef129992e4e631cb843707cd768ae6a397e4ed1a36df892b9fa8fd"),
    (JUNE, None, "3d6f61def778ea827f355e5d743d73ab88897e40aa89f3b3eace14e64d1adbb6"),
    (MARCH, BAR, "c138bd183dfecfbe8befd398ca2db4af085fc8a3fede9123250988e3d12268cc"),
    (AUGUST, BAR, "75c9bab653400a39a016521245bf8289e7c1424e162f37a26432f41bbf94afa4"),
    (MAY, BAR, "e1326d82d7a220baa4e98808650ba775ff5aa479060f99a31e94d00d5c7ce15d"),
)


class ElectronicCaptionTests(SimpleTestCase):
    """The document frozen for the electronic signature (`signing.freeze`,
    `electronic=True`) does not ask for a handwritten « Lu et approuvé » over
    the place its stamp goes: its boxes say « Signature électronique du
    salarié » / « … de l'employeur », short, grey, where the paper words
    are. The paper sheet - the download - keeps its words, byte for byte."""

    def test_the_download_is_the_paper_sheet_byte_for_byte(self):
        for month, establishment, digest in PAPER_SHEETS:
            with self.subTest(month=month, establishment=bool(establishment)):
                sheet = planned(month)
                data = render_month_pdf(sheet, establishment)
                self.assertEqual(data, render_month_pdf(sheet, establishment, electronic=False))
                self.assertEqual(hashlib.sha256(content_stream(data)).hexdigest(), digest)
                text = "\n".join(lines_of(data))
                self.assertIn(EMPLOYEE_SIGNATURE_NOTE, text)
                self.assertIn(EMPLOYER_SIGNATURE_NOTE, text)
                self.assertNotIn("électronique", text)

    def test_the_frozen_sheet_says_electronic_signature_in_the_boxes(self):
        data = render_month_pdf(planned(JUNE), BAR, electronic=True)
        with opened(data) as pdf:
            page = pdf.pages[0]
            texts = [
                page.crop((box.left, PAGE_HEIGHT - box.top, box.right, PAGE_HEIGHT - box.bottom)).extract_text()
                for box in ELECTRONIC_SIGNATURE_BOXES
            ]
        self.assertEqual(
            texts,
            ["Le salarié\nSignature électronique du salarié", "L'employeur\nSignature électronique de l'employeur"],
        )
        text = "\n".join(lines_of(data))
        self.assertNotIn("Lu et approuvé", text)
        self.assertNotIn("Date et signature", text)

    def test_only_the_words_change_same_grey_same_size_same_place(self):
        """Operation for operation, the two sheets draw the same page: two
        lines differ, and in those only the string - never the grey, the
        font, the size or the position."""
        for month in (JUNE, MARCH):
            with self.subTest(month=month):
                sheet = planned(month)
                paper = content_stream(render_month_pdf(sheet, BAR)).decode("ascii").splitlines()
                frozen = content_stream(render_month_pdf(sheet, BAR, electronic=True)).decode("ascii").splitlines()
                self.assertEqual(len(paper), len(frozen))
                differing = [(was, now) for was, now in zip(paper, frozen) if was != now]
                words = (
                    (EMPLOYEE_SIGNATURE_NOTE, EMPLOYEE_ELECTRONIC_NOTE),
                    (EMPLOYER_SIGNATURE_NOTE, EMPLOYER_ELECTRONIC_NOTE),
                )
                self.assertEqual(len(differing), len(words))
                for (was, now), (old, new) in zip(differing, words):
                    old, new = _pdf_string(printable(old)), _pdf_string(printable(new))
                    self.assertIn(old, was)
                    self.assertEqual(was.replace(old, "…"), now.replace(new, "…"))
                    self.assertTrue(now.startswith(f"{GREY} g BT /F1 8 Tf "), now)

    def test_the_electronic_boxes_are_the_paper_frames(self):
        for paper, electronic in zip(SIGNATURE_BOXES, ELECTRONIC_SIGNATURE_BOXES):
            with self.subTest(box=paper.label):
                self.assertEqual((electronic.label, electronic.rect), (paper.label, paper.rect))
                self.assertNotEqual(electronic.note, paper.note)
        self.assertEqual(signature_boxes(), SIGNATURE_BOXES)
        self.assertEqual(signature_boxes(electronic=True), ELECTRONIC_SIGNATURE_BOXES)


class OnePageTests(SimpleTestCase):
    """Everything fits on one page, the worst case included: the signatures
    are at the foot of the one sheet the employee signs."""

    def assert_fits(self, data):
        with opened(data) as pdf:
            self.assertEqual(len(pdf.pages), 1)
            page = pdf.pages[0]
            floor = PAGE_HEIGHT - MARGIN_BOTTOM
            self.assertLessEqual(max(char["bottom"] for char in page.chars), floor)
            self.assertGreaterEqual(min(char["top"] for char in page.chars), MARGIN_TOP - 1)
            self.assertLessEqual(max(char["x1"] for char in page.chars), RIGHT)
            for shape in page.rects + page.lines:
                self.assertLessEqual(shape["bottom"], floor + 0.01)
                self.assertLessEqual(shape["x1"], RIGHT + 0.01)
            return page.extract_text().splitlines(), page

    def test_the_worst_case_fits(self):
        """31 days from a Sunday, six week totals, a long note on every day,
        a long name, a name and an address that do not fit: one page."""
        lines, page = self.assert_fits(pdf_of(worst_case_sheet(), LONG_ESTABLISHMENT))
        table = lines[lines.index("Jour Heures Motif / note") + 1 : lines.index("Récapitulatif du mois")]
        self.assertEqual(len(table), 37)
        self.assertEqual(sum(line.startswith("Total") for line in table), 6)
        self.assertEqual(
            [line.split()[1] for line in table if not line.startswith("Total")], [str(n) for n in range(1, 32)]
        )
        self.assertTrue(table[0].startswith("Dimanche 1 Remplacement imprévu"), table[0])
        self.assertTrue(table[0].endswith("…"))
        self.assertEqual(table[-1], "Total (semaine incomplète) 10,75")

        # The rows stay readable: about 12,5 points each.
        words = page.extract_words()
        mondays = [word["top"] for word in words if word["text"] == "Lundi"]
        self.assertGreaterEqual(mondays[1] - mondays[0], 8 * 12)

        # Nothing overlaps: the last row, then the summary, then the signatures.
        last_total = max(word["bottom"] for word in words if word["text"] == "Total")
        summary, *signatures = sorted(
            (rect for rect in page.rects if rect["stroke"] and not rect["fill"]),
            key=lambda rect: (rect["top"], rect["x0"]),
        )
        self.assertLess(last_total, summary["top"])
        self.assertLess(summary["bottom"], min(box["top"] for box in signatures))

    def test_a_long_header_is_bounded(self):
        """At most ADDRESS_LINES lines of address, the rest joined onto the
        last; a name too wide is cut."""
        lines, _page = self.assert_fits(pdf_of(worst_case_sheet(), LONG_ESTABLISHMENT))
        self.assertEqual(ADDRESS_LINES, 3)
        self.assertTrue(lines[0].startswith("BAR EXEMPLE ET FILS, SOCIÉTÉ À RESPONSABILITÉ LIMITÉE"))
        self.assertTrue(lines[0].endswith("…"))
        self.assertEqual(
            lines[1:5],
            [
                "Bâtiment B, 3e étage",
                "12 rue Imaginaire",
                "Cour intérieure, 75000 PARIS, FRANCE, SIRET 000 000 000 00000",
                "Fiche de temps — Mois de mars 2026",
            ],
        )

    def test_every_month_of_two_years_fits(self):
        for year in (2026, 2028):
            for number in range(1, 13):
                month = date(year, number, 1)
                with self.subTest(month=month):
                    lines, _page = self.assert_fits(pdf_of(planned(month), LONG_ESTABLISHMENT))
                    self.assertIn("Le salarié L'employeur", lines)


class TextTests(SimpleTestCase):
    """The helpers the layout rests on."""

    def test_printable(self):
        self.assertEqual(printable(None), "")
        self.assertEqual(printable("  7 h\n30\tle soir  "), "7 h 30 le soir")
        self.assertEqual(printable("\N{MINUS SIGN}8 h"), "-8 h")
        self.assertEqual(printable("Cafe\N{COMBINING ACUTE ACCENT}"), "Café")
        self.assertEqual(printable("co\N{SOFT HYPHEN}op"), "coop")  # a soft hyphen is not printed
        self.assertEqual(printable("a\x00b"), "a?b")
        self.assertEqual(printable("10 €, « oui », l’été…"), "10 €, « oui », l’été…")
        self.assertEqual(printable("Łódź"), "?ód?")

    def test_printable_name(self):
        """A name loses an accent rather than a letter: « ?ukasz » is a name
        misspelt, « Lukasz » is still his (review, 28/09)."""
        self.assertEqual(printable_name("Łódź"), "Lódz")  # « ó » is cp1252's: kept
        self.assertEqual(printable_name("WÓJCIK Łukasz"), "WÓJCIK Lukasz")
        self.assertEqual(printable_name("NGUYỄN Thị Đào"), "NGUYÊN Thi Dào")
        self.assertEqual(printable_name("  DUPONT\tJeanne-Marie  "), "DUPONT Jeanne-Marie")
        self.assertEqual(printable_name("Martin et ﬁls"), "Martin et fils")  # a ligature, spelt out
        # A letter nothing brings back into cp1252 is still « ? », never an error.
        self.assertEqual(printable_name("李 Jeanne"), "? Jeanne")

    def test_widths_come_from_the_font_metrics(self):
        # Helvetica: « 0 » is 556/1000 em, « i » 222; the bold « 0 » is 556 too.
        self.assertAlmostEqual(text_width("0", size=10), 5.56)
        self.assertAlmostEqual(text_width("i", size=10), 2.22)
        self.assertAlmostEqual(text_width("0", "Helvetica-Bold", 10), 5.56)
        self.assertAlmostEqual(text_width("€", size=10), 5.56)

    def test_fit(self):
        self.assertEqual(fit("Congés payés", 200), "Congés payés")
        cut = fit(LONG_NOTE, 100)
        self.assertTrue(cut.endswith("…"))
        self.assertLessEqual(text_width(cut), 100)
        self.assertGreater(text_width(cut), 90)
        self.assertEqual(fit("abc", 0), "")
        exact = text_width("Congés")
        self.assertEqual(fit("Congés", exact), "Congés")

    def test_wrap(self):
        self.assertEqual(wrap("Date et signature", 500), ["Date et signature"])
        lines = wrap("Date et signature, précédées de la mention", 60)
        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(lines), "Date et signature, précédées de la mention")
        for line in lines:
            self.assertLessEqual(text_width(line), 60)


class FilenameTests(SimpleTestCase):
    def test_the_filename(self):
        self.assertEqual(pdf_filename(planned(JUNE)), "Fiche de temps DUPONT Jeanne juin 2026.pdf")

    def test_the_header_has_an_ascii_fallback_and_the_exact_name(self):
        header = content_disposition(planned(AUGUST))
        self.assertEqual(
            header,
            'attachment; filename="Fiche de temps DUPONT Jeanne aout 2026.pdf"; '
            "filename*=UTF-8''Fiche%20de%20temps%20DUPONT%20Jeanne%20ao%C3%BBt%202026.pdf",
        )
        header.encode("ascii")

    def test_a_response_carries_the_header_as_it_is(self):
        """Plain ASCII, so Django sets it untouched (a header it has to
        encode comes out as =?utf-8?b?…?=, which browsers do not read in a
        file name)."""
        sheet = planned(AUGUST, employee(save=False, last_name="D'Éon", first_name="Zoé"))
        response = HttpResponse(render_month_pdf(sheet), content_type="application/pdf")
        response["Content-Disposition"] = content_disposition(sheet)
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="Fiche de temps D\'EON Zoe aout 2026.pdf"; '
            "filename*=UTF-8''Fiche%20de%20temps%20D%27%C3%89ON%20Zo%C3%A9%20ao%C3%BBt%202026.pdf",
        )

    def test_a_name_cannot_make_a_folder_or_break_the_header(self):
        person = employee(save=False, last_name='Dupont/"X"', first_name="Jeanne\r\nSet-Cookie: x")
        filename = pdf_filename(planned(JUNE, person))
        self.assertNotIn("/", filename)
        self.assertNotIn('"', filename)
        self.assertNotIn("\n", filename)
        header = content_disposition(planned(JUNE, person))
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertEqual(header.count('"'), 2)
        self.assertEqual(ascii_filename("Łódź été 東京.pdf"), "_odz ete __.pdf")


class SavedMonthTests(TestCase):
    def test_a_planned_month_prints_exactly_what_saving_it_stores(self):
        person = employee()
        establishment = Establishment.current()
        establishment.name, establishment.address = "BAR EXEMPLE", "12 rue Imaginaire\n75000 PARIS"
        establishment.save()

        unsaved = month_sheet(person, MAY)
        self.assertFalse(unsaved.saved)
        before = render_month_pdf(unsaved, establishment)

        posted = read_posted_month(MAY, fields_as_drawn(unsaved))
        save_month(person, MAY, posted.days)
        saved = month_sheet(person, MAY)
        self.assertTrue(saved.saved)
        self.assertEqual(render_month_pdf(saved, Establishment.current()), before)

    def test_a_saved_month_prints_the_same_once_the_typical_week_changes(self):
        """The sheet the employee signed, downloaded again after the week
        changed (Saturday 7,5 h → 4 h): the same bytes - not a « Semaine
        type », a « Prévues » and an « Écart » worked out from a week that
        was not June's."""
        person = employee()
        save_month(person, JUNE, [PostedDay(date(2026, 6, 2), kind="conges")])
        before = render_month_pdf(month_sheet(person, JUNE), BAR)
        person.saturday_hours = Decimal("4")
        person.save()
        after = render_month_pdf(month_sheet(person, JUNE), BAR)
        self.assertIn("Semaine type : 36 h", lines_of(after))
        self.assertEqual(after, before)

    def test_drawing_a_sheet_reads_nothing_from_the_database(self):
        person = employee()
        sheet = month_sheet(person, JUNE)
        establishment = Establishment.current()
        with self.assertNumQueries(0):
            render_month_pdf(sheet, establishment)
