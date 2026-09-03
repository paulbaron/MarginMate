"""Tests for reading L'Addition's sales export.

Two layers: the .xlsx reader itself, and the interpretation of the
SalesDocumentLines sheet on top of it.

The reader exists because openpyxl refuses these files outright - the export
declares its "Total" row's cells as numeric and then writes "-" into them,
which is invalid SpreadsheetML. That exact cell is built into the fixture
below, because it is the whole reason this code exists and a reader that
can't survive it is no use here.
"""

import zipfile
from datetime import date

from django.test import SimpleTestCase

from recipes.pos.laddition_xlsx import LadditionExportError, parse_rows, parse_sales_export
from recipes.pos.xlsx_reader import XlsxError, read_sheet, sheet_names

CONTENT_TYPES = """<?xml version="1.0"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
</Types>"""

WORKBOOK = """<?xml version="1.0"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>
  <sheet name="Accueil" sheetId="1" r:id="rId9"/>
  <sheet name="SalesDocumentLines" sheetId="2" r:id="rId4"/>
</sheets>
</workbook>"""

# Deliberately NOT in the same order as the sheet files, and with ids whose
# alphabetical order differs from the sheet order - pairing workbook order
# with a sorted file list would hand back the wrong sheet.
RELS = """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId4" Target="worksheets/sheet2.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
<Relationship Id="rId9" Target="worksheets/sheet1.xml"
  Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
</Relationships>"""

SHARED = """<?xml version="1.0"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<si><t>Jour</t></si>
<si><t>Nom</t></si>
<si><t>Qte</t></si>
<si><t>TAG_Offered</t></si>
<si><t>2026-06-01</t></si>
<si><r><t>Pinte </t></r><r><t>Blonde</t></r></si>
<si><t>NON</t></si>
<si><t>OUI</t></si>
</sst>"""


def _sheet(rows_xml: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows_xml}</sheetData></worksheet>"
    )


SALES_SHEET_XML = _sheet(
    # Header, from shared strings.
    '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
    '<c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c></row>'
    # A normal line; the name comes from a multi-run shared string.
    '<row r="2"><c r="A2" t="s"><v>4</v></c><c r="B2" t="s"><v>5</v></c>'
    '<c r="C2"><v>2</v></c><c r="D2" t="s"><v>6</v></c></row>'
    # Same product, same day - must be summed with the row above.
    '<row r="3"><c r="A3" t="s"><v>4</v></c><c r="B3" t="s"><v>5</v></c>'
    '<c r="C3"><v>3</v></c><c r="D3" t="s"><v>6</v></c></row>'
    # An offered one, with the name given inline rather than shared.
    '<row r="4"><c r="A4" t="s"><v>4</v></c>'
    '<c r="B4" t="inlineStr"><is><t>Spritz Aperol</t></is></c>'
    '<c r="C4"><v>1</v></c><c r="D4" t="s"><v>7</v></c></row>'
    # THE reason this reader exists: numeric-typed cells holding "-".
    '<row r="5"><c r="A5"><v>-</v></c><c r="B5" t="inlineStr"><is><t>Total</t></is></c>'
    '<c r="C5"><v>-</v></c></row>'
    # A gap: column B is absent entirely, so C must not shift into its place.
    '<row r="6"><c r="A6" t="s"><v>4</v></c><c r="C6"><v>9</v></c></row>'
)


def write_workbook(path, sales_xml=SALES_SHEET_XML):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", RELS)
        archive.writestr("xl/sharedStrings.xml", SHARED)
        archive.writestr("xl/worksheets/sheet1.xml", _sheet(""))
        archive.writestr("xl/worksheets/sheet2.xml", sales_xml)
    return str(path)


class XlsxReaderTests(SimpleTestCase):
    def setUp(self):
        import tempfile

        self.path = write_workbook(tempfile.mkstemp(suffix=".xlsx")[1])

    def rows(self):
        return list(read_sheet(self.path, "SalesDocumentLines"))

    def test_sheet_names_come_from_the_workbook(self):
        self.assertEqual(sheet_names(self.path), ["Accueil", "SalesDocumentLines"])

    def test_the_sheet_is_resolved_through_its_relationship_id(self):
        """Not by pairing workbook order with a sorted file list, which
        agrees often enough to look right and then silently returns the
        wrong sheet."""
        self.assertEqual(self.rows()[0][:3], ["Jour", "Nom", "Qte"])

    def test_shared_strings_are_resolved(self):
        self.assertEqual(self.rows()[1][0], "2026-06-01")

    def test_a_string_split_across_runs_is_joined(self):
        self.assertEqual(self.rows()[1][1], "Pinte Blonde")

    def test_inline_strings_are_read(self):
        self.assertEqual(self.rows()[3][1], "Spritz Aperol")

    def test_a_dash_in_a_numeric_cell_does_not_blow_up(self):
        """openpyxl raises `invalid literal for int() with base 10: '-'` on
        exactly this, and rejects the whole workbook."""
        self.assertEqual(self.rows()[4][0], "-")
        self.assertEqual(self.rows()[4][1], "Total")

    def test_a_missing_cell_leaves_a_gap_rather_than_shifting(self):
        """Cells are addressed, not ordered. If an absent B were skipped
        instead of blanked, C's value would land in B and every column after
        it would be read as the one before."""
        row = self.rows()[5]
        self.assertEqual(row[0], "2026-06-01")
        self.assertEqual(row[1], "")
        self.assertEqual(row[2], "9")

    def test_an_unknown_sheet_is_an_error_naming_what_is_there(self):
        with self.assertRaises(XlsxError) as caught:
            list(read_sheet(self.path, "Nope"))
        self.assertIn("SalesDocumentLines", str(caught.exception))


class ParseSalesRowsTests(SimpleTestCase):
    HEADER = ["Jour", "Nom", "Qte", "TAG_Offered"]

    def parse(self, rows, header=None):
        return parse_rows([header or self.HEADER, *rows])

    def test_one_line(self):
        result = self.parse([["2026-06-01", "Pinte Blonde", "2", "NON"]])
        self.assertEqual(result.entries, [("Pinte Blonde", date(2026, 6, 1), 2)])
        self.assertEqual(result.total_quantity, 2)

    def test_lines_for_the_same_product_and_day_are_summed(self):
        """The till writes one row per item rung up, so five pints on one
        ticket is five rows."""
        result = self.parse(
            [["2026-06-01", "Pinte Blonde", "2", "NON"], ["2026-06-01", "Pinte Blonde", "3", "NON"]]
        )
        self.assertEqual(result.entries, [("Pinte Blonde", date(2026, 6, 1), 5)])

    def test_the_same_product_on_different_days_stays_apart(self):
        result = self.parse(
            [["2026-06-01", "Pinte Blonde", "2", "NON"], ["2026-06-02", "Pinte Blonde", "3", "NON"]]
        )
        self.assertEqual(len(result.entries), 2)
        self.assertEqual(result.days, (date(2026, 6, 1), date(2026, 6, 2)))

    def test_the_total_row_is_skipped_and_counted(self):
        result = self.parse(
            [["2026-06-01", "Pinte Blonde", "2", "NON"], ["-", "Total", "-", ""]]
        )
        self.assertEqual(result.total_quantity, 2)
        self.assertEqual(result.skipped, 1)

    def test_offered_items_are_counted_but_still_included(self):
        """A comped drink is poured from the same bottle as a paid one and
        consumes exactly the same stock, so it belongs in the quantity.
        Recording it again as a known loss would subtract it twice."""
        result = self.parse(
            [["2026-06-01", "Pinte Blonde", "2", "NON"], ["2026-06-01", "Spritz", "1", "OUI"]]
        )
        self.assertEqual(result.total_quantity, 3)
        self.assertEqual(result.offered, 1)

    def test_columns_are_found_by_name_not_position(self):
        """The export has 40 columns and their order isn't a promise."""
        result = parse_rows(
            [
                ["Etablissement", "Qte", "Nom", "TAG_Salle", "Jour"],
                ["Le Dipsomaniac", "4", "Pinte IPA", "Bar", "2026-06-01"],
            ]
        )
        self.assertEqual(result.entries, [("Pinte IPA", date(2026, 6, 1), 4)])

    def test_a_missing_column_says_which_one(self):
        with self.assertRaises(LadditionExportError) as caught:
            parse_rows([["Jour", "Nom"], ["2026-06-01", "x"]])
        self.assertIn("Qte", str(caught.exception))

    def test_an_empty_sheet_is_an_error(self):
        with self.assertRaises(LadditionExportError):
            parse_rows([])

    def test_a_header_only_sheet_yields_nothing(self):
        result = self.parse([])
        self.assertEqual(result.entries, [])
        self.assertIsNone(result.days)

    def test_a_short_row_is_skipped_rather_than_raising(self):
        result = self.parse([["2026-06-01"], ["2026-06-01", "Pinte Blonde", "2", "NON"]])
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.total_quantity, 2)

    def test_a_blank_name_is_skipped(self):
        self.assertEqual(self.parse([["2026-06-01", "", "2", "NON"]]).skipped, 1)

    def test_an_unparseable_quantity_is_skipped(self):
        self.assertEqual(self.parse([["2026-06-01", "Pinte", "n/a", "NON"]]).skipped, 1)

    def test_a_quantity_written_as_a_float_is_accepted(self):
        result = self.parse([["2026-06-01", "Pinte", "2.0", "NON"]])
        self.assertEqual(result.total_quantity, 2)

    def test_a_real_date_object_is_accepted(self):
        result = self.parse([[date(2026, 6, 1), "Pinte", "2", "NON"]])
        self.assertEqual(result.entries[0][1], date(2026, 6, 1))

    def test_an_export_without_the_offered_column_still_parses(self):
        result = parse_rows([["Jour", "Nom", "Qte"], ["2026-06-01", "Pinte", "2"]])
        self.assertEqual(result.total_quantity, 2)
        self.assertEqual(result.offered, 0)


class ParseSalesExportFileTests(SimpleTestCase):
    def test_reading_a_whole_workbook(self):
        import tempfile

        path = write_workbook(tempfile.mkstemp(suffix=".xlsx")[1])
        result = parse_sales_export(path)
        self.assertEqual(result.entries[0], ("Pinte Blonde", date(2026, 6, 1), 5))
        self.assertEqual(result.offered, 1)
        # The "-" total row, plus the row whose name cell is missing.
        self.assertEqual(result.skipped, 2)

    def test_a_workbook_without_the_sales_sheet_says_so(self):
        import tempfile
        import zipfile as zf

        path = tempfile.mkstemp(suffix=".xlsx")[1]
        with zf.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr(
                "xl/workbook.xml",
                WORKBOOK.replace('name="SalesDocumentLines"', 'name="Autre"'),
            )
            archive.writestr("xl/_rels/workbook.xml.rels", RELS)
            archive.writestr("xl/worksheets/sheet1.xml", _sheet(""))
            archive.writestr("xl/worksheets/sheet2.xml", _sheet(""))
        with self.assertRaises(LadditionExportError) as caught:
            parse_sales_export(path)
        self.assertIn("Lignes de ventes", str(caught.exception))
