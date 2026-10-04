"""The .xlsx reader on a file from outside (recipes/pos/xlsx_reader.py).

A bar's own till export is uploaded on « Ventes », so the reader that only
ever read the owner's downloaded L'Addition exports now reads anything. Each
test below is a way a small file could take the server down or put a path
on a page, and every one holds for EVERY caller - the owner's downloads
included, which never come near any of it (test_pos_xlsx and
test_pos_revenue read them as before).

Every workbook is built here, by hand: names and figures invented.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from recipes.pos import xlsx_reader
from recipes.pos.xlsx_reader import Limits, Number, XlsxError, check_untrusted, date1904, read_sheet, sheet_names

CONTENT_TYPES = """<?xml version="1.0"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
</Types>"""

RELS = """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Target="worksheets/sheet1.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
<Relationship Id="rId2" Target="worksheets/sheet2.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
</Relationships>"""


def workbook_xml(pr: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"{pr}<sheets>"
        '<sheet name="Ventes" sheetId="1" r:id="rId1"/>'
        '<sheet name="Autre" sheetId="2" r:id="rId2"/>'
        "</sheets></workbook>"
    )


def sheet_xml(rows_xml: str, prolog: str = '<?xml version="1.0" encoding="UTF-8"?>') -> str:
    return (
        f"{prolog}"
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows_xml}</sheetData></worksheet>"
    )


FIRST_SHEET = sheet_xml(
    '<row r="1"><c r="A1" t="inlineStr"><is><t>Date</t></is></c><c r="B1" t="inlineStr"><is><t>Montant</t></is></c>'
    '<c r="C1" t="s"><v>0</v></c></row>'
    '<row r="2"><c r="A2"><v>46206</v></c><c r="B2"><v>10.499999999999998</v></c>'
    '<c r="C2" t="inlineStr"><is><t>10,50</t></is></c></row>'
)
SECOND_SHEET = sheet_xml('<row r="1"><c r="A1" t="inlineStr"><is><t>Autre feuille</t></is></c></row>')
SHARED = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Taux</t></si></sst>'
)


def build(members: dict | None = None, *, workbook: str | None = None, compression=zipfile.ZIP_DEFLATED) -> bytes:
    """A workbook as bytes: two sheets, a string table - any member
    replaced or added by `members`."""
    parts = {
        "[Content_Types].xml": CONTENT_TYPES,
        "xl/workbook.xml": workbook or workbook_xml(),
        "xl/_rels/workbook.xml.rels": RELS,
        "xl/sharedStrings.xml": SHARED,
        "xl/worksheets/sheet1.xml": FIRST_SHEET,
        "xl/worksheets/sheet2.xml": SECOND_SHEET,
    }
    parts.update(members or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def saved(content: bytes) -> str:
    """The workbook in a folder of its own, under a name that says where
    it is - the name a refusal must never repeat."""
    path = Path(tempfile.mkdtemp(prefix="dossier-du-serveur-")) / "export.xlsx"
    path.write_bytes(content)
    return str(path)


class FirstSheetAndFileObjectTests(SimpleTestCase):
    def test_the_first_sheet_is_read_when_none_is_named(self):
        rows = list(read_sheet(saved(build())))
        self.assertEqual(rows[0], ["Date", "Montant", "Taux"])

    def test_a_sheet_is_still_read_by_its_name(self):
        self.assertEqual(list(read_sheet(saved(build()), "Autre")), [["Autre feuille"]])

    def test_a_seekable_file_object_reads_as_a_path_does(self):
        """« Tester » reads the upload in memory and keeps no file."""
        content = build()
        self.assertEqual(list(read_sheet(io.BytesIO(content))), list(read_sheet(saved(content))))
        handle = io.BytesIO(content)
        self.assertEqual(sheet_names(handle), ["Ventes", "Autre"])
        # Read twice from the same object: each call starts it again.
        self.assertEqual(next(read_sheet(handle))[0], "Date")

    def test_a_workbook_with_no_sheet_says_so(self):
        content = build(
            workbook=workbook_xml().replace(
                '<sheet name="Ventes" sheetId="1" r:id="rId1"/><sheet name="Autre" sheetId="2" r:id="rId2"/>', ""
            )
        )
        with self.assertRaises(XlsxError):
            list(read_sheet(io.BytesIO(content)))


class TypedCellsTests(SimpleTestCase):
    def test_a_numeric_cell_comes_back_as_a_number_its_text_as_written(self):
        """A dot decimal and Excel's serial, whatever the locale: the caller
        reads them as numbers - and a text cell as printed (« 10,50 »)."""
        row = list(read_sheet(io.BytesIO(build()), typed=True))[1]
        self.assertIsInstance(row[0], Number)
        self.assertEqual(row[0], "46206")
        self.assertIsInstance(row[1], Number)
        self.assertEqual(row[1], "10.499999999999998")
        self.assertNotIsInstance(row[2], Number)
        self.assertEqual(row[2], "10,50")

    def test_untyped_every_cell_is_plain_text_as_before(self):
        row = list(read_sheet(io.BytesIO(build())))[1]
        self.assertFalse(any(isinstance(cell, Number) for cell in row))

    def test_a_cell_without_its_reference_follows_the_one_before(self):
        content = build(
            {
                "xl/worksheets/sheet1.xml": sheet_xml(
                    '<row r="1"><c t="inlineStr"><is><t>a</t></is></c><c t="inlineStr"><is><t>b</t></is></c>'
                    '<c r="E1" t="inlineStr"><is><t>e</t></is></c><c t="inlineStr"><is><t>f</t></is></c></row>'
                )
            }
        )
        self.assertEqual(list(read_sheet(io.BytesIO(content))), [["a", "b", "", "", "e", "f"]])


class Date1904Tests(SimpleTestCase):
    def test_the_usual_calendar(self):
        self.assertFalse(date1904(io.BytesIO(build())))

    def test_an_old_mac_workbook_counts_from_1904(self):
        content = build(workbook=workbook_xml('<workbookPr date1904="1"/>'))
        self.assertTrue(date1904(io.BytesIO(content)))
        content = build(workbook=workbook_xml('<workbookPr date1904="true"/>'))
        self.assertTrue(date1904(io.BytesIO(content)))


class ColumnTests(SimpleTestCase):
    """One cell reference asked for a row as wide as its column: about
    10^17 cells for « ZZZZZZZZZZZZ1 », in a file of a few hundred bytes -
    the server, one process for every espace, out of memory."""

    def sheet(self, ref: str) -> bytes:
        return build({"xl/worksheets/sheet1.xml": sheet_xml(f'<row r="1"><c r="{ref}"><v>1</v></c></row>')})

    def test_a_column_far_past_the_last_is_refused_in_french(self):
        with self.assertRaises(XlsxError) as caught:
            list(read_sheet(io.BytesIO(self.sheet("ZZZZZZZZZZZZ1"))))
        self.assertIn("XFD", str(caught.exception))

    def test_the_column_just_past_xfd_is_refused(self):
        with self.assertRaises(XlsxError):
            list(read_sheet(io.BytesIO(self.sheet("XFE1"))))

    def test_xfd_itself_is_a_cell(self):
        row = next(read_sheet(io.BytesIO(self.sheet("XFD1"))))
        self.assertEqual(len(row), 16384)
        self.assertEqual(row[-1], "1")

    def test_a_caller_reading_the_first_columns_never_reads_the_rest(self):
        content = build(
            {
                "xl/worksheets/sheet1.xml": sheet_xml(
                    '<row r="1"><c r="A1"><v>1</v></c><c r="B1"><v>2</v></c><c r="XFD1"><v>3</v></c></row>'
                )
            }
        )
        self.assertEqual(list(read_sheet(io.BytesIO(content), max_columns=2)), [["1", "2"]])


class DoctypeTests(SimpleTestCase):
    """ElementTree expands internal entities: a DOCTYPE is the
    billion-laughs door, and no export ever carries one."""

    BOMB = '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'

    def refused(self, members: dict, **kwargs):
        with self.assertRaises(XlsxError) as caught:
            list(read_sheet(io.BytesIO(build(members, **kwargs))))
        return str(caught.exception)

    def test_a_doctype_in_the_sheet_is_refused(self):
        said = self.refused({"xl/worksheets/sheet1.xml": sheet_xml("", prolog='<?xml version="1.0"?>' + self.BOMB)})
        self.assertIn("DOCTYPE", said)

    def test_a_doctype_in_the_string_table_is_refused(self):
        self.refused({"xl/sharedStrings.xml": '<?xml version="1.0"?>' + self.BOMB + "<sst/>"})

    def test_a_doctype_in_the_workbook_or_its_relations_is_refused(self):
        self.refused({"xl/_rels/workbook.xml.rels": '<?xml version="1.0"?>' + self.BOMB + "<Relationships/>"})
        self.refused({}, workbook='<?xml version="1.0"?>' + self.BOMB + workbook_xml().split("?>", 1)[1])

    def test_a_doctype_after_kilobytes_of_comments_is_found(self):
        """A check of the first 4 KB alone misses it."""
        comments = "<!--" + "x" * 5000 + "-->"
        self.refused({"xl/worksheets/sheet1.xml": '<?xml version="1.0"?>' + comments + self.BOMB + "<worksheet/>"})

    def test_a_doctype_astride_two_chunks_is_found(self):
        """The parser reads 16 KB at a time: the scan overlaps them."""
        padding = "<!--" + "x" * (16 * 1024 - len('<?xml version="1.0"?>') - 7 - 4) + "-->"
        self.refused({"xl/worksheets/sheet1.xml": '<?xml version="1.0"?>' + padding + self.BOMB + "<worksheet/>"})

    def test_an_entity_declaration_alone_is_refused(self):
        self.refused({"xl/worksheets/sheet1.xml": sheet_xml('<!ENTITY x "y">')})

    def test_a_member_in_utf_16_is_refused_before_anything_parses_it(self):
        """In UTF-16 « <!DOCTYPE » is two bytes a letter: a byte grep misses
        it while the parser reads the mark and expands every entity."""
        text = '<?xml version="1.0" encoding="UTF-16"?>' + self.BOMB + "<worksheet/>"
        said = self.refused(
            {"xl/worksheets/sheet1.xml": "\N{ZERO WIDTH NO-BREAK SPACE}".encode("utf-16-le") + text.encode("utf-16-le")}
        )
        self.assertIn("encodage", said)

    def test_utf_16_with_no_mark_is_refused_by_its_nul_bytes(self):
        text = '<?xml version="1.0"?><worksheet/>'
        self.refused({"xl/worksheets/sheet1.xml": text.encode("utf-16-le")})

    def test_an_encoding_the_scan_cannot_read_is_refused(self):
        self.refused({"xl/worksheets/sheet1.xml": sheet_xml("", prolog='<?xml version="1.0" encoding="cp037"?>')})

    def test_a_utf_8_mark_and_an_ascii_compatible_declaration_read(self):
        row = '<row r="1"><c r="A1"><v>1</v></c></row>'
        for member in (
            "\N{ZERO WIDTH NO-BREAK SPACE}" + sheet_xml(row, prolog='<?xml version="1.0" encoding="utf-8"?>'),
            sheet_xml(row, prolog='<?xml version="1.0" encoding="ISO-8859-1"?>'),
        ):
            with self.subTest(member=member[:50]):
                content = build({"xl/worksheets/sheet1.xml": member.encode("utf-8")})
                self.assertEqual(list(read_sheet(io.BytesIO(content))), [["1"]])


class UntrustedTests(SimpleTestCase):
    """A file a person uploaded: the zip's own bounds, read off its
    directory before anything inflates."""

    def test_a_workbook_within_the_bounds_passes(self):
        check_untrusted(io.BytesIO(build()))
        self.assertEqual(next(read_sheet(io.BytesIO(build()), untrusted=True))[0], "Date")

    def test_a_member_inflating_far_past_its_size_is_refused(self):
        """A few kilobytes of deflate make megabytes of zeros: tested with
        the bounds patched small, never a real bomb."""
        content = build({"xl/worksheets/sheet1.xml": "<worksheet>" + " " * 400_000 + "</worksheet>"})
        small = Limits(ratio=10, ratio_min_bytes=1000)
        with self.assertRaises(XlsxError) as caught:
            check_untrusted(io.BytesIO(content), small)
        self.assertIn("compressée", str(caught.exception))

    def test_a_member_too_big_once_inflated_is_refused(self):
        content = build({"xl/worksheets/sheet1.xml": "<worksheet>" + " " * 5000 + "</worksheet>"})
        with mock.patch.object(xlsx_reader, "UPLOAD_LIMITS", Limits(member_bytes=4000)):
            with self.assertRaises(XlsxError):
                list(read_sheet(io.BytesIO(content), untrusted=True))
            # The owner's downloads are never held to an upload's bounds.
            self.assertEqual(list(read_sheet(io.BytesIO(content))), [])

    def test_the_whole_too_big_is_refused(self):
        with self.assertRaises(XlsxError):
            check_untrusted(io.BytesIO(build()), Limits(total_bytes=100))

    def test_too_many_members_is_refused(self):
        extra = {f"xl/media/image{n}.xml": "<x/>" for n in range(12)}
        with self.assertRaises(XlsxError):
            check_untrusted(io.BytesIO(build(extra)), Limits(members=10))

    def test_too_many_strings_is_refused(self):
        strings = "".join(f"<si><t>{n}</t></si>" for n in range(20))
        content = build({"xl/sharedStrings.xml": SHARED.replace("<si><t>Taux</t></si>", strings)})
        with mock.patch.object(xlsx_reader, "UPLOAD_LIMITS", Limits(shared_strings=10)):
            with self.assertRaises(XlsxError):
                list(read_sheet(io.BytesIO(content), untrusted=True))


class NoPathTests(SimpleTestCase):
    """A refusal may reach a page: it never names the server's folders."""

    def test_an_unknown_sheet_names_the_sheets_not_the_file(self):
        path = saved(build())
        with self.assertRaises(XlsxError) as caught:
            list(read_sheet(path, "Nope"))
        said = str(caught.exception)
        self.assertIn("Ventes", said)
        self.assertNotIn("dossier-du-serveur", said)
        self.assertNotIn(str(Path(path).parent), said)

    def test_a_file_that_is_no_zip_or_no_file_is_refused_in_french(self):
        path = saved(b"pas un zip")
        for source in (path, str(Path(path).parent / "absent.xlsx")):
            with self.subTest(source=source):
                with self.assertRaises(XlsxError) as caught:
                    list(read_sheet(source))
                self.assertNotIn("dossier-du-serveur", str(caught.exception))
                self.assertEqual(str(caught.exception), xlsx_reader.NOT_A_WORKBOOK)

    def test_a_member_left_open_never_holds_the_file(self):
        """On Windows a file still held open cannot be deleted - and a
        refused upload is deleted at once."""
        path = saved(build({"xl/worksheets/sheet1.xml": sheet_xml("<!DOCTYPE x>")}))
        with self.assertRaises(XlsxError):
            list(read_sheet(path))
        rows = read_sheet(saved(build()))
        next(rows)
        rows.close()
        Path(path).unlink()
        self.assertFalse(Path(path).exists())


class MemoryTests(SimpleTestCase):
    """A member costing far more memory than its size: one `<row>` of a few
    million empty cells - a 20 KB upload - was built whole before its end, a
    string of a million runs too, and anything outside the rows piled up on
    the root. Every element is dropped as soon as it is read now, and what a
    member may hold at once is bounded. The bounds are patched small: never
    a real bomb in a test."""

    def sheet(self, rows_xml: str) -> bytes:
        return build({"xl/worksheets/sheet1.xml": sheet_xml(rows_xml)})

    def refused(self, content: bytes, **kwargs) -> str:
        with self.assertRaises(XlsxError) as caught:
            list(read_sheet(io.BytesIO(content), **kwargs))
        return str(caught.exception)

    def test_a_row_of_more_cells_than_excel_has_columns_is_refused(self):
        """Every reference A1: no cell past XFD, and still no row."""
        cells = '<c r="A1"/>' * (xlsx_reader.MAX_COLUMNS + 1)
        said = self.refused(self.sheet(f'<row r="1">{cells}</row>'))
        self.assertEqual(said, xlsx_reader.ROW_TOO_WIDE)

    def test_a_row_of_as_many_cells_as_excel_has_columns_reads(self):
        cells = "<c><v>1</v></c>" * xlsx_reader.MAX_COLUMNS
        row = next(read_sheet(io.BytesIO(self.sheet(f'<row r="1">{cells}</row>'))))
        self.assertEqual(len(row), xlsx_reader.MAX_COLUMNS)

    def test_a_cell_or_a_row_holding_too_many_elements_is_refused(self):
        runs = "<r><t>a</t></r>" * 30
        with mock.patch.object(xlsx_reader, "MAX_HELD", 50):
            said = self.refused(self.sheet(f'<row r="1"><c r="A1" t="inlineStr"><is>{runs}</is></c></row>'))
            self.assertEqual(said, xlsx_reader.TOO_DENSE)
            junk = "<x/>" * 60
            self.assertEqual(self.refused(self.sheet(f'<row r="1">{junk}</row>')), xlsx_reader.TOO_DENSE)
            # Read cell by cell, a row of many cells holds one at a time.
            cells = "<c><v>1</v></c>" * 60
            row = next(read_sheet(io.BytesIO(self.sheet(f'<row r="1">{cells}</row>'))))
            self.assertEqual(len(row), 60)

    def test_a_string_of_too_many_runs_is_refused(self):
        runs = "<r><t>a</t></r>" * 30
        strings = SHARED.replace("<si><t>Taux</t></si>", f"<si>{runs}</si>")
        with mock.patch.object(xlsx_reader, "MAX_HELD", 50):
            said = self.refused(build({"xl/sharedStrings.xml": strings}))
        self.assertEqual(said, xlsx_reader.TOO_DENSE)

    def test_elements_nested_past_any_export_are_refused(self):
        deep = "<x>" * (xlsx_reader.MAX_DEPTH + 1) + "</x>" * (xlsx_reader.MAX_DEPTH + 1)
        self.assertEqual(self.refused(self.sheet(deep)), xlsx_reader.TOO_DEEP)
        self.assertEqual(self.refused(build(workbook=workbook_xml(deep))), xlsx_reader.TOO_DEEP)

    def test_too_many_distinct_names_are_refused(self):
        """The parser keeps every distinct name of element or attribute for
        good: millions of them in one member is memory no row frees."""
        names = "".join(f"<n{number}/>" for number in range(40))
        attributes = "".join(f'<x a{number}="1"/>' for number in range(40))
        with mock.patch.object(xlsx_reader, "MAX_NAMES", 30):
            self.assertEqual(self.refused(self.sheet(names)), xlsx_reader.TOO_MANY_NAMES)
            self.assertEqual(self.refused(self.sheet(attributes)), xlsx_reader.TOO_MANY_NAMES)
            strings = SHARED.replace("<si><t>Taux</t></si>", f"<si><t>Taux</t></si>{names}")
            self.assertEqual(self.refused(build({"xl/sharedStrings.xml": strings})), xlsx_reader.TOO_MANY_NAMES)
            # The usual names of a sheet are far fewer.
            self.assertEqual(len(list(read_sheet(io.BytesIO(build())))), 2)

    def test_a_tag_or_a_text_longer_than_any_export_s_is_refused(self):
        """One start tag of a million attributes inflates tenfold once
        parsed; no export's cell holds more than 32 767 characters. The
        stretch is measured across the parser's 16 KB chunks."""
        long = "x" * 45_000
        rows = "".join(f'<row r="{n}"><c r="A{n}"><v>{n}</v></c></row>' for n in range(1, 4000))
        with mock.patch.object(xlsx_reader, "MAX_STRETCH", 20_000):
            said = self.refused(self.sheet(f'<row r="1"><c r="A1" a="{long}"/></row>'))
            self.assertEqual(said, xlsx_reader.STRETCH_REFUSED)
            inline = f'<row r="1"><c r="A1" t="inlineStr"><is><t>{long}</t></is></c></row>'
            self.assertEqual(self.refused(self.sheet(inline)), xlsx_reader.STRETCH_REFUSED)
            # Carried to the next chunk's first « > », whatever follows it.
            split = "<!--" + "y" * 28_000 + "--><x/>"
            self.assertEqual(self.refused(self.sheet(split)), xlsx_reader.STRETCH_REFUSED)
            # A long sheet of short tags reads.
            self.assertEqual(len(list(read_sheet(io.BytesIO(self.sheet(rows))))), 3999)

    def test_a_long_row_costs_one_cell_at_a_time(self):
        """Measured: a row of 16 000 one-letter cells held about 10 MB built
        whole, against half a megabyte read a cell at a time."""
        import tracemalloc

        cells = '<c t="inlineStr"><is><t>a</t></is></c>' * 16_000
        content = self.sheet(f'<row r="1">{cells}</row>')
        tracemalloc.start()
        try:
            rows = list(read_sheet(io.BytesIO(content), max_columns=5))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(rows, [["a"] * 5])
        self.assertLess(peak, 2_000_000)


class HeaderColumnsTests(SimpleTestCase):
    """A caller reading by its header says which titles (`header_columns`):
    past the last of them, the rows below are never read - a row padded to a
    cell at XFD, row after row, took minutes."""

    def test_the_rows_below_are_read_no_wider_than_the_titles_named(self):
        content = build(
            {
                "xl/worksheets/sheet1.xml": sheet_xml(
                    '<row r="1"><c r="A1" t="inlineStr"><is><t>Jour</t></is></c>'
                    '<c r="B1" t="inlineStr"><is><t> Nom </t></is></c>'
                    '<c r="C1" t="inlineStr"><is><t>Autre</t></is></c></row>'
                    '<row r="2"><c r="A2"><v>1</v></c><c r="B2"><v>2</v></c><c r="C2"><v>3</v></c>'
                    '<c r="XFD2"><v>4</v></c></row>'
                )
            }
        )
        rows = list(read_sheet(io.BytesIO(content), header_columns=("Nom", "Jour", "Absent")))
        self.assertEqual(rows, [["Jour", " Nom ", "Autre"], ["1", "2"]])

    def test_a_header_naming_none_of_them_reads_nothing_below(self):
        rows = list(read_sheet(io.BytesIO(build()), header_columns=("Absent",)))
        self.assertEqual(rows[0], ["Date", "Montant", "Taux"])
        self.assertEqual(rows[1:], [[]])


class DamagedZipTests(SimpleTestCase):
    """A zip damaged on the way: zipfile raises NotImplementedError for a
    version it does not know and ValueError for a directory pointing before
    the file's start - neither a zip error. Each was a 500 on « Tester » and
    the fixed sentence in the job."""

    def assert_no_workbook(self, content: bytes) -> None:
        with self.assertRaises(XlsxError) as caught:
            list(read_sheet(io.BytesIO(content)))
        self.assertEqual(str(caught.exception), xlsx_reader.NOT_A_WORKBOOK)

    def test_a_zip_version_nobody_knows_is_no_workbook(self):
        content = bytearray(build())
        directory = content.find(b"PK\x01\x02")
        content[directory + 6] = 160  # « version needed to extract »: 16.0
        self.assert_no_workbook(bytes(content))

    def test_a_directory_pointing_before_the_file_is_no_workbook(self):
        content = bytearray(build())
        end = content.rfind(b"PK\x05\x06")
        offset = int.from_bytes(content[end + 16 : end + 20], "little")
        content[end + 16 : end + 20] = (offset + 100_000).to_bytes(4, "little")
        self.assert_no_workbook(bytes(content))


class SheetListTests(SimpleTestCase):
    def test_an_unknown_sheet_lists_a_few_sheets_each_cut(self):
        """A workbook may name a thousand sheets of any length: the refusal
        reaches a job's log, which shows the first few."""
        names = [f"Feuille {number} " + "x" * 200 for number in range(30)]
        sheets = "".join(f'<sheet name="{name}" sheetId="{n}" r:id="rId1"/>' for n, name in enumerate(names, 1))
        workbook = workbook_xml().replace(
            '<sheet name="Ventes" sheetId="1" r:id="rId1"/><sheet name="Autre" sheetId="2" r:id="rId2"/>', sheets
        )
        with self.assertRaises(XlsxError) as caught:
            list(read_sheet(io.BytesIO(build(workbook=workbook)), "Absente"))
        said = str(caught.exception)
        self.assertIn("Feuille 0 xxx", said)
        self.assertIn("Feuille 9 xxx", said)
        self.assertNotIn("Feuille 10 ", said)
        self.assertNotIn("x" * 41, said)
        self.assertTrue(said.endswith("…)."))
