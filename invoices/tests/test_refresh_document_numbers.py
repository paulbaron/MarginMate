"""`manage.py refresh_document_numbers`: a number that stood in for one the
reader did not find - made up from the date and the total, or a payment
reference, a long digit run - gives way to the number the document prints,
now read. Eau de Paris' bills were filed under such stand-ins, and its
portal's list, printing the real numbers, never recognised an invoice
already imported. A number that is one, and a collision, are left alone.
Data invented."""

from datetime import date
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from tests.factories import make_invoice, make_supplier

BILL = """EAU EXEMPLE
FACTURE TRIMESTRIELLE
Evolution de vos consommations
N° 2026100000001 DU 10 AVRIL 2026
Total  327,20
5300000000000000000000001  327,20"""


class RefreshDocumentNumbersTests(TestCase):
    def setUp(self):
        self.water = make_supplier(code="EAU_X", name="Eau Exemple", parser_key="", expenses_only=True)

    def bill(self, number, text=BILL, supplier=None):
        return make_invoice(
            supplier=supplier or self.water, invoice_number=number, invoice_date=date(2026, 4, 10), ocr_text=text
        )

    def run_command(self, *args):
        out = StringIO()
        call_command("refresh_document_numbers", *args, stdout=out)
        return out.getvalue()

    def test_a_made_up_number_and_a_payment_reference_give_way(self):
        made_up = self.bill("20260410-327.20")
        other = self.bill("5300000000000000000000001", text=BILL.replace("2026100000001", "2025100000009"))
        said = self.run_command()
        made_up.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((made_up.invoice_number, other.invoice_number), ("2026100000001", "2025100000009"))
        self.assertIn("Eau Exemple : 2", said)

    def test_the_dry_run_changes_nothing(self):
        made_up = self.bill("20260410-327.20")
        self.run_command("--dry-run")
        made_up.refresh_from_db()
        self.assertEqual(made_up.invoice_number, "20260410-327.20")

    def test_a_real_number_is_kept(self):
        typed = self.bill("F-2026-001")
        self.run_command()
        typed.refresh_from_db()
        self.assertEqual(typed.invoice_number, "F-2026-001")

    def test_a_number_another_document_holds_is_said_not_taken(self):
        self.bill("2026100000001", text="EAU EXEMPLE\nTotal 12,00")
        stand_in = self.bill("20260410-327.20")
        said = self.run_command()
        stand_in.refresh_from_db()
        self.assertEqual(stand_in.invoice_number, "20260410-327.20")
        self.assertIn("déjà", said)

    def test_a_count_of_the_day_filed_bare_takes_its_date(self):
        """Wing Seng's « 000172 », filed bare before its zeros stopped
        counting: dated now, the same ticket photographed again is still
        recognised, and another day's « 000172 » is not refused for it."""
        shop = make_supplier(code="WINGSENG_X", name="Épicerie Exemple", parser_key="WINGSENG")
        ticket = make_invoice(
            supplier=shop,
            invoice_number="000172",
            invoice_date=date(2025, 11, 19),
            ocr_text="EPICERIE EXEMPLE\nTicket:000172 19/11/2025 10H02\nTOTAL EUR: 3.60",
        )
        said = self.run_command()
        ticket.refresh_from_db()
        self.assertEqual(ticket.invoice_number, "000172-20251119")
        self.assertIn("Épicerie Exemple : 1", said)

    def test_a_short_number_read_some_other_way_is_the_document_s_own(self):
        """« Facture n° 0042 » is that document's number, not a count."""
        invoice = self.bill("0042", text="EXEMPLE\nFacture n° 0042\nTotal 12,00")
        self.run_command()
        invoice.refresh_from_db()
        self.assertEqual(invoice.invoice_number, "0042")

    def test_a_supplier_with_a_reader_of_its_own_is_left_alone(self):
        wholesaler = make_supplier(code="UBA_X", name="Grossiste Exemple", parser_key="UBA")
        own = self.bill("20260410-327.20", supplier=wholesaler)
        self.run_command()
        own.refresh_from_db()
        self.assertEqual(own.invoice_number, "20260410-327.20")
