"""The invoice check (returnables/invoice_check.py): is what a slip says was
taken back what the seller's invoice refunds?

Invoices are built with tests/factories.py and an invented source_text laid
out like UBA's (the delivery-note line ends « <BL> <date> Page n/m », cells
two spaces apart as ocr.document_text writes them); every number, delivery
note, date and amount is invented.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext

from returnables import invoice_check
from returnables.comparison import SlipIndex
from returnables.invoice_check import (
    DIFFERS,
    NO_DATE,
    NO_INVOICE,
    NO_REFERENCE,
    NO_TEXT,
    SAME,
    SEVERAL,
    SUPERSEDED,
    TOO_SHORT,
    check_many,
    key,
)
from returnables.tests import texts
from returnables.tests.support import DELIVERY_DAY, make_format, make_slip, make_supplier, uba
from tests.factories import make_invoice, make_invoice_line
from tests.support import NoNetworkTestCase

D = Decimal
DAY = DELIVERY_DAY
NBSP = "\N{NO-BREAK SPACE}"
CHECK = "\N{CHECK MARK}"
DOT = "\N{MIDDLE DOT}"
CATEGORY = "UBA - Consignes"


def invoice_text(number: str, day: date, *delivery_notes) -> str:
    """An invented invoice's text, as the document text layer gives it."""
    lines = [
        "U.B.A.  EXEMPLE",
        f"Facture No : {number}",
        f"DATE FACTURE  {day:%d/%m/%Y}",
        "N° TVA DESTINATAIRE  N° ACCISE DESTINATAIRE  LIVREUR  N° BL  DATE LIVRAISON",
        "Heure Livraison de 06:00h à 12:00h",
    ]
    lines += [f"FR00000000000  EXEMPLE  {delivery_note}  {day:%d/%m/%Y}  Page  1/1" for delivery_note in delivery_notes]
    lines += ["CODE  DESIGNATION  QTE  PRIX", "TOTAL FACTURE  123,45"]
    return "\n".join(lines)


def invoice(number, delivery_notes=(), lines=(), day=DAY, supplier=None, text=None):
    """An invoice of `supplier` (UBA) printing `delivery_notes`, with `lines` as
    (raw_name, quantity, total_ht)."""
    made = make_invoice(
        supplier=supplier or uba(),
        invoice_date=day,
        invoice_number=number,
        source_text=invoice_text(number, day, *delivery_notes) if text is None else text,
    )
    for raw_name, quantity, total_ht in lines:
        make_invoice_line(invoice=made, raw_name=raw_name, quantity=quantity, total_ht=total_ht, category=CATEGORY)
    return made


def new_slip(references=("610001",), lines=((texts.KEG, 3, D("30.0000"), D("90.00")),), **fields):  # noqa: B008 - Decimals are immutable, built once on purpose
    return make_slip(references=list(references), lines=list(lines), **fields)


def refund(raw_name, quantity, unit="30.00"):
    return (raw_name, -quantity, str(-quantity * D(unit)))


class RefundTests(NoNetworkTestCase):
    def check(self, slip):
        return check_many([slip])[slip.pk]

    def test_refunded_on_the_invoice(self):
        slip = new_slip()
        invoice("VE-0000000001", ["610001"], [refund(texts.FULL_NAMES[texts.KEG], 3)])
        result = self.check(slip)
        self.assertEqual((result.state, result.ok, result.css), (SAME, True, "COMPLETE"))
        self.assertEqual(result.label, f"remboursé sur la facture n° VE-0000000001 {CHECK}")
        self.assertEqual([invoice.number for invoice in result.invoices], ["VE-0000000001"])
        self.assertEqual(
            [row.sentence for row in result.rows],
            [f"{texts.KEG} — bon : 3 (90,00{NBSP}€) {DOT} facture : 3 (90,00{NBSP}€) {CHECK}"],
        )

    def test_a_gap_is_said_with_both_figures(self):
        slip = new_slip()
        invoice("VE-0000000002", ["610001"], [refund(texts.KEG, 2)])
        result = self.check(slip)
        self.assertEqual((result.state, result.ok, result.css), (DIFFERS, False, "ERROR"))
        self.assertEqual(
            result.label,
            f"écart avec la facture n° VE-0000000002 : {texts.KEG} — bon : 3 (90,00{NBSP}€) {DOT} "
            f"facture : 2 (60,00{NBSP}€)",
        )

    def test_amounts_of_a_thousand_euros_or_more_are_said_with_their_thousands_grouped(self):
        slip = new_slip(lines=[(texts.KEG, 40, D("30.0000"), D("1200.00"))])
        invoice("VE-0000000008", ["610001"], [refund(texts.KEG, 38), ("Consigne BIERE EXEMPLE 20L", -25, "-1125.00")])
        result = self.check(slip)
        self.assertEqual(
            result.label,
            f"écart avec la facture n° VE-0000000008 : {texts.KEG} — bon : 40 (1{NBSP}200,00{NBSP}€) {DOT} "
            f"facture : 38 (1{NBSP}140,00{NBSP}€)",
        )
        self.assertEqual(
            [other.sentence for other in result.others],
            [f"Consigne BIERE EXEMPLE 20L : -25 (-1{NBSP}125,00{NBSP}€) — facture n° VE-0000000008"],
        )

    def test_amounts_are_compared_to_the_cent(self):
        slip = new_slip()
        invoice("VE-0000000003", ["610001"], [(texts.KEG, -3, "-90.01")])
        self.assertEqual(self.check(slip).state, DIFFERS)

    def test_counts_and_amounts_are_compared_as_absolute_sums(self):
        """A seller printing its refund's quantity positive, and the slip's
        correction line « -1 », both compare."""
        slip = new_slip(lines=[(texts.KEG, 4, D("30.0000"), D("120.00")), (texts.KEG, -1, D("30.0000"), D("-30.00"))])
        invoice("VE-0000000004", ["610001"], [(texts.KEG, 3, "-90.00")])
        self.assertEqual(self.check(slip).state, SAME)

    def test_a_slip_line_without_amount_compares_its_count_only(self):
        slip = new_slip(lines=[(texts.KEG, 3, None, None)])
        invoice("VE-0000000005", ["610001"], [refund(texts.KEG, 3)])
        result = self.check(slip)
        self.assertEqual(result.state, SAME)
        self.assertIsNone(result.rows[0].slip_amount)

    def test_nothing_taken_back_nothing_refunded(self):
        slip = new_slip(lines=[])
        invoice("VE-0000000006", ["610001"], [("CONSIGNE FUT EXEMPLE", 1, "30.00")])
        result = self.check(slip)
        self.assertEqual(result.state, SAME)
        self.assertEqual(
            result.label, f"rien repris sur le bon, rien remboursé sur la facture n° VE-0000000006 {CHECK}"
        )

    def test_an_empty_slip_whose_invoice_refunds_returnables_is_to_check_never_a_tick(self):
        """Spec §1's « empty part corrected to kegs »: the replacement slip has
        not arrived, and the invoice refunds 3 kegs this slip does not list.
        « rien remboursé » would be false; « écart » is not what the other
        credits mean either - no verdict, said, the kegs listed below it."""
        slip = new_slip(lines=[])
        invoice("VE-0000000007", ["610001"], [refund(texts.FULL_NAMES[texts.KEG], 3)])
        result = self.check(slip)
        self.assertNotEqual(result.state, SAME)
        self.assertEqual((result.state, result.ok, result.css), (invoice_check.OTHERS_ONLY, None, "pending"))
        self.assertNotIn("rien remboursé", result.label)
        self.assertEqual(
            result.label,
            "rien repris sur le bon, mais la facture n° VE-0000000007 rembourse d'autres consignes "
            "(non comparées) : à vérifier",
        )
        self.assertEqual(
            [other.sentence for other in result.others],
            [f"{texts.KEG} : -3 (-90,00{NBSP}€) — facture n° VE-0000000007"],
        )
        self.assertEqual(result.rows, [])


class ReferenceTests(NoNetworkTestCase):
    def state(self, slip):
        return check_many([slip])[slip.pk]

    def test_a_reference_is_found_as_a_whole_token_only(self):
        for printed, found in (
            ("6100012", False),
            ("X610001", False),
            ("610001X", False),
            ("BL:610001.", True),
            ("610001", True),
        ):
            with self.subTest(printed=printed):
                slip = new_slip(references=["610001"], delivery_date=DAY)
                made = invoice(f"VE-{printed}", text=f"N° BL  {printed}  Page 1/1")
                make_invoice_line(invoice=made, raw_name=texts.KEG, quantity=-3, total_ht="-90", category=CATEGORY)
                self.assertEqual(self.state(slip).state, SAME if found else NO_INVOICE)
                made.delete()
                slip.delete()

    def test_a_reference_holding_punctuation_is_searched_literally_and_whole(self):
        slip = new_slip(references=["BL-12.34"])
        invoice("VE-0000000010", text="BON BL-12X34 ET BL-12.345 ET XBL-12.34 DU JOUR", lines=[refund(texts.KEG, 3)])
        self.assertEqual(self.state(slip).state, NO_INVOICE)
        invoice("VE-0000000011", text="BON BL-12.34 DU JOUR", lines=[refund(texts.KEG, 3)])
        self.assertEqual(self.state(slip).state, SAME)

    def test_a_short_reference_is_not_searched(self):
        short = new_slip(references=["12"])
        invoice("VE-0000000012", ["12"], [refund(texts.KEG, 3)])
        result = self.state(short)
        self.assertEqual(
            (result.state, result.label, result.ok), (TOO_SHORT, "référence trop courte pour être cherchée", None)
        )
        both = new_slip(references=["12", "610001"])
        invoice("VE-0000000013", ["610001"], [refund(texts.KEG, 3)])
        result = self.state(both)
        self.assertEqual(result.state, SAME)
        self.assertEqual(result.notes, ["référence « 12 » trop courte pour être cherchée"])

    def test_no_reference_read(self):
        result = self.state(new_slip(references=[]))
        self.assertEqual((result.state, result.label, result.css), (NO_REFERENCE, "pas de référence lue", "ignored"))

    def test_no_delivery_date_read(self):
        result = self.state(new_slip(delivery_date=None))
        self.assertEqual((result.state, result.label), (NO_DATE, "date de livraison non lue"))

    def test_no_invoice_yet(self):
        result = self.state(new_slip())
        self.assertEqual(
            (result.state, result.label, result.ok, result.css),
            (NO_INVOICE, "facture pas encore reçue", None, "pending"),
        )

    def test_only_the_supplier_s_invoices_within_the_window_are_candidates(self):
        slip = new_slip()
        invoice("VE-0000000020", ["610001"], [refund(texts.KEG, 3)], day=DAY + timedelta(days=46))
        invoice("VE-0000000021", ["610001"], [refund(texts.KEG, 3)], day=DAY - timedelta(days=8))
        invoice("VE-0000000022", ["610001"], [refund(texts.KEG, 3)], supplier=make_supplier())
        self.assertEqual(self.state(slip).state, NO_INVOICE)
        for number, day in (("VE-0000000023", DAY + timedelta(days=45)), ("VE-0000000024", DAY - timedelta(days=7))):
            with self.subTest(day=day):
                made = invoice(number, ["610001"], [refund(texts.KEG, 3)], day=day)
                self.assertEqual(self.state(slip).state, SAME)
                made.delete()

    def test_invoices_with_no_text_are_said(self):
        slip = new_slip()
        invoice("VE-0000000030", text="")
        result = self.state(slip)
        self.assertEqual((result.state, result.label), (NO_TEXT, "facture sans texte lisible"))
        invoice("VE-0000000031", ["999999"])
        result = self.state(slip)
        self.assertEqual(result.state, NO_INVOICE)
        self.assertEqual(result.notes, ["1 facture de la période sans texte lisible : le BL n'a pas pu y être cherché"])

    def test_a_reference_on_two_invoices_is_to_check(self):
        slip = new_slip()
        invoice("VE-0000000040", ["610001"], [refund(texts.KEG, 3)])
        invoice("VE-0000000041", ["610001"], [refund(texts.KEG, 3)], day=DAY + timedelta(days=2))
        result = check_many([slip])[slip.pk]
        self.assertEqual((result.state, result.ok, result.css), (SEVERAL, None, "pending"))
        self.assertEqual(
            result.label, "BL n° 610001 sur plusieurs factures (n° VE-0000000040, n° VE-0000000041) : à vérifier"
        )
        self.assertEqual(result.rows, [])


class PairingTests(NoNetworkTestCase):
    def test_an_invoice_line_goes_to_the_longest_designation_it_starts_with(self):
        slip = new_slip(lines=[("FÛT 12", 1, D("30.0000"), D("30.00")), (texts.KEG_LONG, 2, D("30.0000"), D("60.00"))])
        invoice("VE-0000000050", ["610001"], [refund("FÛT 12 L", 1), refund(texts.FULL_NAMES[texts.KEG_LONG], 2)])
        result = check_many([slip])[slip.pk]
        self.assertEqual(result.state, SAME, result.label)
        self.assertEqual(
            [(row.key, row.invoice_quantity) for row in result.rows], [("fut 12", 1), ("fut 12/18/24/36/48/6", 2)]
        )

    def test_accents_case_and_spaces_do_not_matter(self):
        slip = new_slip(lines=[("FUT 12/18/24/36 L", 3, D("30.0000"), D("90.00"))])
        invoice("VE-0000000051", ["610001"], [refund("Fût  12/18/24/36 l", 3)])
        self.assertEqual(check_many([slip])[slip.pk].state, SAME)
        self.assertEqual(key("  FÛT   12 L "), "fut 12 l")

    def test_other_negative_lines_are_listed_never_counted(self):
        slip = new_slip()
        invoice(
            "VE-0000000052",
            ["610001"],
            [
                refund(texts.KEG, 3),
                ("Consigne BIERE EXEMPLE 20L", -1, "-45.00"),
                ("Consigne BIERE EXEMPLE 20L", 2, "60.00"),
            ],
        )
        result = check_many([slip])[slip.pk]
        self.assertEqual(result.state, SAME)
        self.assertEqual(
            [other.sentence for other in result.others],
            [f"Consigne BIERE EXEMPLE 20L : -1 (-45,00{NBSP}€) — facture n° VE-0000000052"],
        )

    def test_a_designation_shorter_than_three_characters_never_pairs(self):
        slip = new_slip(lines=[("AB", 1, D("5.0000"), D("5.00"))])
        invoice("VE-0000000053", ["610001"], [("AB CAISSE", -1, "-5.00")])
        result = check_many([slip])[slip.pk]
        self.assertEqual(result.state, DIFFERS)
        self.assertEqual([other.raw_name for other in result.others], ["AB CAISSE"])


class ConnectedGroupTests(NoNetworkTestCase):
    def test_two_delivery_notes_on_two_invoices_are_compared_once(self):
        """One ticket, two delivery notes: the refund sits on one of the two invoices."""
        slip = new_slip(references=["610003", "610004"], lines=[(texts.KEG, 6, D("30.0000"), D("180.00"))])
        invoice("VE-0000000060", ["610003"], [refund(texts.KEG, 6)])
        invoice("VE-0000000061", ["610004"], [("BIERE EXEMPLE", 2, "80.00")], day=DAY + timedelta(days=1))
        result = check_many([slip])[slip.pk]
        self.assertEqual(result.state, SAME)
        self.assertEqual(result.label, f"remboursé sur les factures n° VE-0000000060, n° VE-0000000061 {CHECK}")

    def test_two_slips_on_one_invoice_are_compared_once(self):
        first = new_slip(references=["610101"], lines=[(texts.KEG, 2, D("30.0000"), D("60.00"))])
        second = new_slip(
            references=["610102"],
            lines=[(texts.KEG, 3, D("30.0000"), D("90.00"))],
            delivery_date=DAY + timedelta(days=3),
        )
        invoice("VE-0000000062", ["610101", "610102"], [refund(texts.KEG, 5)], day=DAY + timedelta(days=5))
        both = check_many([first, second])
        self.assertEqual((both[first.pk].state, both[second.pk].state), (SAME, SAME))
        self.assertEqual([info.pk for info in both[first.pk].slips], [first.pk, second.pk])
        # Shown alone, the other slip on that invoice is still counted.
        self.assertEqual(check_many([first])[first.pk].state, SAME)

    def test_a_slip_replaced_is_not_added_to_its_replacement(self):
        original = new_slip(references=["610006"], number="1005", lines=[(texts.KEG, 5, D("30.0000"), D("150.00"))])
        replacement = new_slip(
            references=["610006"], number="1006", replaces=True, lines=[(texts.KEG, 4, D("30.0000"), D("120.00"))]
        )
        invoice("VE-0000000063", ["610006"], [refund(texts.KEG, 4)])
        results = check_many([original, replacement])
        self.assertEqual(results[original.pk].state, SUPERSEDED)
        self.assertEqual(results[original.pk].label, "annulé et remplacé : la facture est vérifiée sur le bon n° 1006")
        self.assertEqual(results[replacement.pk].state, SAME)

    def test_another_supplier_s_slip_never_joins(self):
        mine = new_slip(references=["610201"])
        other_format = make_format(name="Autre vendeur", supplier=make_supplier())
        make_slip(other_format, references=["610201"], lines=[(texts.KEG, 9, D("30.0000"), D("270.00"))])
        invoice("VE-0000000064", ["610201"], [refund(texts.KEG, 3)])
        self.assertEqual(check_many([mine])[mine.pk].state, SAME)


class CalendarEndsTests(NoNetworkTestCase):
    """A delivery date at either end of the calendar (a row a damaged
    archive brought in before « Données » refused it): the window is cut at
    the calendar's ends, never an OverflowError on the page."""

    def test_a_delivery_date_at_either_end_of_the_calendar(self):
        first = new_slip(references=["610301"], delivery_date=date(1, 1, 2))
        last = new_slip(references=["610302"], delivery_date=date(9999, 12, 30))
        latest = new_slip(references=["610303"], delivery_date=date(9999, 12, 31))
        results = check_many([first, last, latest])
        self.assertEqual(
            {pk: result.state for pk, result in results.items()},
            {first.pk: NO_INVOICE, last.pk: NO_INVOICE, latest.pk: NO_INVOICE},
        )


class QueryCountTests(NoNetworkTestCase):
    def slips_with_invoices(self, count: int, start: int) -> list:
        made = []
        for number in range(start, start + count):
            day = DAY + timedelta(days=number)
            reference = f"7{number:05d}"
            made.append(new_slip(references=[reference], delivery_date=day))
            invoice(f"VE-{number:010d}", [reference], [refund(texts.KEG, 3)], day=day)
        return made

    def test_two_queries_whatever_the_number_of_slips(self):
        for count, start in ((1, 0), (20, 100)):
            with self.subTest(count=count):
                slips = self.slips_with_invoices(count, start)
                index = SlipIndex.load({slip.format_id for slip in slips})
                index.ensure_lines(slip.pk for slip in slips)
                with self.assertNumQueries(2):
                    results = check_many(slips, index=index)
                self.assertEqual({result.state for result in results.values()}, {SAME})

    def test_alone_it_costs_the_same_for_one_slip_or_twenty(self):
        counts = []
        for count, start in ((1, 200), (20, 300)):
            slips = self.slips_with_invoices(count, start)
            with CaptureQueriesContext(connection) as captured:
                results = check_many(slips)
            self.assertEqual({result.state for result in results.values()}, {SAME})
            counts.append(len(captured))
        self.assertEqual(counts[0], counts[1])

    def test_nothing_asked_nothing_read(self):
        with self.assertNumQueries(0):
            self.assertEqual(check_many([]), {})
