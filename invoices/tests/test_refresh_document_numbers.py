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

    def test_a_supplier_with_a_reader_of_its_own_is_left_alone(self):
        wholesaler = make_supplier(code="UBA_X", name="Grossiste Exemple", parser_key="UBA")
        own = self.bill("20260410-327.20", supplier=wholesaler)
        self.run_command()
        own.refresh_from_db()
        self.assertEqual(own.invoice_number, "20260410-327.20")
