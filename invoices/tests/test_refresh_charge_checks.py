"""`manage.py refresh_charge_checks`: charge documents filed before a charge
kept its own checks through the ticket import get them back - Eau de Paris'
water bills, filed at exactly what they charge, waited in « À vérifier »
under the ticket reader's « 5.5 % supposé ». Data invented."""

from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from invoices.models import Invoice
from tests.factories import make_invoice, make_invoice_line, make_supplier

D = Decimal
TICKET_CHECKS = [
    {"label": "Taux par article", "passed": False, "detail": "5.5 % supposé, à vérifier."},
    {"label": "Somme HT des lignes = base HT du ticket", "passed": False, "detail": "lignes 310.15 € / ticket 303.28 €"},
]


class RefreshChargeChecksTests(TestCase):
    def setUp(self):
        self.water = make_supplier(code="EAU_X", name="Eau Exemple", parser_key="", expenses_only=True)

    def bill(self, **fields):
        invoice = make_invoice(
            supplier=self.water, ocr_text="EAU EXEMPLE\nTotal 327,20", printed_total_ttc=D("327.20"),
            parse_checks=TICKET_CHECKS, status=Invoice.Status.NEEDS_REVIEW, **fields,
        )
        make_invoice_line(invoice=invoice, raw_name="Eau Exemple", total_ht=D("142.34"), vat_rate=D("0.055"), printed_ttc=D("150.16"))
        make_invoice_line(invoice=invoice, raw_name="Eau Exemple", total_ht=D("160.94"), vat_rate=D("0.10"), printed_ttc=D("177.04"))
        return invoice

    def test_a_charge_gets_its_own_checks_and_is_settled(self):
        invoice = self.bill()
        out = StringIO()
        call_command("refresh_charge_checks", stdout=out)
        invoice.refresh_from_db()
        self.assertEqual({check["label"] for check in invoice.parse_checks} & {"Taux par article"}, set())
        self.assertIn("Total de la charge", {check["label"] for check in invoice.parse_checks})
        self.assertEqual(invoice.review_state["label"], "Charge")
        self.assertIn("Eau Exemple : 1", out.getvalue())

    def test_the_dry_run_changes_nothing(self):
        invoice = self.bill()
        call_command("refresh_charge_checks", "--dry-run", stdout=StringIO())
        invoice.refresh_from_db()
        self.assertEqual(invoice.parse_checks, TICKET_CHECKS)

    def test_one_a_person_validated_is_left_as_it_is(self):
        invoice = self.bill(reviewed_at=timezone.now())
        call_command("refresh_charge_checks", stdout=StringIO())
        invoice.refresh_from_db()
        self.assertEqual(invoice.parse_checks, TICKET_CHECKS)

    def test_goods_are_not_charges(self):
        grocer = make_supplier(code="EPICERIE_X", name="Epicerie Exemple", parser_key="")
        ticket = make_invoice(supplier=grocer, ocr_text="EPICERIE", parse_checks=TICKET_CHECKS)
        call_command("refresh_charge_checks", stdout=StringIO())
        ticket.refresh_from_db()
        self.assertEqual(ticket.parse_checks, TICKET_CHECKS)
