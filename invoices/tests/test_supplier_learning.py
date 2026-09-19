"""What names a supplier, kept - worked out from what its documents print.

On 18/09 a supplier holding two subscriptions of one company - 30 box bills
printing the header a person gave, 7 mobile bills printing the mobile
company's number and sites - lost every figure it had learned, silently,
when one mobile bill's correction was validated: what it knew was measured
again against a quarter of ALL its documents, and the box bills filed since
by the header had made the mobile figures rare. Known figures now go only
for a reason about them; and a new one may be learned from the documents
that do not print the header.

Data invented (the fixtures of test_subscriptions).
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from invoices.receipts import _recheck, learn_identifiers
from invoices.tests.test_subscriptions import BOX_HEADER, SIREN, box_text, mobile_text
from tests.factories import make_invoice, make_supplier

MOBILE_FIGURES = [f"siren:{SIREN}", "web:mobile.operateur-exemple.fr"]


class OperatorWithManyBoxBills(TestCase):
    """Free's shape: many documents printing the header, a few of another
    subscription that do not."""

    def setUp(self):
        self.today = timezone.localdate()
        self.operator = make_supplier(
            code="OPERATEUR_X", name="Operateur Exemple", parser_key="", expenses_only=True, ticket_header=BOX_HEADER,
        )
        # The customer's own phone, printed on the mobile bills, is on a
        # wholesaler's invoices too: it names nobody.
        grossiste = make_supplier(code="GROSSISTE_X", name="Grossiste Exemple", parser_key="")
        make_invoice(supplier=grossiste, source_text="GROSSISTE EXEMPLE\nClient : 06 12 34 56 78\nTOTAL 12,00")

    def bills(self, text_of, count, start=10):
        return [
            make_invoice(supplier=self.operator, invoice_date=day, ocr_text=text_of(day))
            for day in (self.today - timedelta(days=start + n) for n in range(count))
        ]

    def test_validating_one_mobile_bill_keeps_what_the_mobile_bills_print(self):
        self.operator.ticket_identifiers = list(MOBILE_FIGURES)
        self.operator.save()
        self.bills(box_text, 30)
        mobiles = self.bills(mobile_text, 7, start=100)
        learn_identifiers(self.operator, mobiles[0].ocr_text)  # a correction validated
        self.operator.refresh_from_db()
        self.assertEqual(sorted(self.operator.ticket_identifiers), sorted(MOBILE_FIGURES))

    def test_a_box_bill_named_by_hand_keeps_them_too(self):
        self.operator.ticket_identifiers = list(MOBILE_FIGURES)
        self.operator.save()
        boxes = self.bills(box_text, 30)
        self.bills(mobile_text, 7, start=100)
        learn_identifiers(self.operator, boxes[0].ocr_text)
        self.operator.refresh_from_db()
        # The box's own support site may be learned beside them: kept, they are.
        self.assertLessEqual(set(MOBILE_FIGURES), set(self.operator.ticket_identifiers))

    def test_a_new_figure_counts_over_the_documents_without_the_header(self):
        self.bills(box_text, 30)
        mobiles = self.bills(mobile_text, 2, start=100)
        self.assertEqual(sorted(learn_identifiers(self.operator, mobiles[0].ocr_text)), sorted(MOBILE_FIGURES))

    def test_one_document_without_the_header_teaches_nothing_on_its_own(self):
        self.bills(box_text, 30)
        (mobile,) = self.bills(mobile_text, 1, start=100)
        self.assertEqual(learn_identifiers(self.operator, mobile.ocr_text), [])


class KnownFiguresTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="QUINCAILLERIE_X", name="Quincaillerie Exemple", parser_key="")

    def test_a_known_figure_survives_documents_of_another_family(self):
        """No header: five documents print it, thirty more do not."""
        self.shop.ticket_identifiers = ["web:quincaillerie-exemple.fr"]
        self.shop.save()
        for n in range(5):
            make_invoice(supplier=self.shop, ocr_text=f"QUINCAILLERIE\nwww.quincaillerie-exemple.fr\nTicket {n}\nTOTAL 3,00")
        for n in range(30):
            make_invoice(supplier=self.shop, ocr_text=f"QUINCAILLERIE\nTicket {n + 10}\nTOTAL 4,00")
        learn_identifiers(self.shop, "QUINCAILLERIE\nTicket 99\nTOTAL 5,00")
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.ticket_identifiers, ["web:quincaillerie-exemple.fr"])

    def test_a_known_figure_goes_when_none_of_its_documents_prints_it(self):
        self.shop.ticket_identifiers = ["web:ancienne-adresse.fr"]
        self.shop.save()
        make_invoice(supplier=self.shop, ocr_text="QUINCAILLERIE\nTicket 1\nTOTAL 3,00")
        _recheck(self.shop)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.ticket_identifiers, [])

    def test_a_known_figure_goes_when_another_suppliers_document_prints_it(self):
        self.shop.ticket_identifiers = ["web:quincaillerie-exemple.fr"]
        self.shop.save()
        make_invoice(supplier=self.shop, ocr_text="QUINCAILLERIE\nwww.quincaillerie-exemple.fr\nTOTAL 3,00")
        other = make_supplier(code="AUTRE_X", name="Autre Exemple", parser_key="")
        make_invoice(supplier=other, ocr_text="AUTRE\npartenaire www.quincaillerie-exemple.fr\nTOTAL 9,00")
        _recheck(self.shop)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.ticket_identifiers, [])

    def test_a_supplier_with_no_documents_keeps_what_it_holds(self):
        self.shop.ticket_identifiers = ["web:quincaillerie-exemple.fr"]
        self.shop.save()
        _recheck(self.shop)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.ticket_identifiers, ["web:quincaillerie-exemple.fr"])

    def test_few_documents_one_without_the_header_keep_everything_on_a_full_relearn(self):
        """A copy shop: four tickets, three printing its header, all its
        number - measured over the headerless one alone, a relearn from
        scratch had wiped it."""
        self.shop.ticket_header = "COPIE EXEMPLE"
        self.shop.save()
        texts = [f"COPIE EXEMPLE\nSIREN {SIREN[:3]} {SIREN[3:6]} {SIREN[6:]}\nTicket {n}\nTOTAL 2,00" for n in range(3)]
        texts.append(f"(en-tête illisible)\nSIREN {SIREN[:3]} {SIREN[3:6]} {SIREN[6:]}\nTicket 9\nTOTAL 2,00")
        for text in texts:
            make_invoice(supplier=self.shop, ocr_text=text)
        self.assertEqual(learn_identifiers(self.shop, *texts), [f"siren:{SIREN}"])
