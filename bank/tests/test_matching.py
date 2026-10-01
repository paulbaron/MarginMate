"""Which invoices a bank payment paid, decided by bank.matching from plain
values - no database, no statement file."""

from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase

from bank.matching import (
    LATER_PAYMENT_WINDOW,
    NEAR_SURE,
    NEAR_SURE_GAP,
    RECURRING_DAYS_BEFORE,
    RECURRING_MARGIN,
    SURE,
    TIER_RULES,
    TO_CONFIRM,
    InvoiceCandidate,
    Match,
    Naming,
    Payment,
    alias_key,
    match,
    names_supplier,
    payee_of,
    supplier_words,
    words,
)

METRO, UBA, FRANPRIX, MONOPRIX = 1, 2, 3, 4
NAMING = {
    METRO: Naming(supplier_words("Metro", "METRO")),
    UBA: Naming(supplier_words("UBA (Bar Exemple)", "UBA")),
    FRANPRIX: Naming(supplier_words("Franprix", "FRANPRIX")),
    MONOPRIX: Naming(supplier_words("Monoprix", "MONOPRIX")),
}
JULY_15 = date(2026, 7, 15)
#: What separates an amount's thousands in a sentence (common.THOUSANDS_SEPARATOR).
NBSP = "\N{NO-BREAK SPACE}"


def invoice(pk, supplier, day, total):
    return InvoiceCandidate(pk=pk, supplier_id=supplier, invoice_date=day, total=Decimal(total))


def card(day, merchant, amount):
    return Payment(
        kind="CARD",
        operation_date=day + timedelta(days=2),
        card_date=day,
        counterparty=merchant,
        amount_due=Decimal(amount),
    )


def debit(day, creditor, amount):
    return Payment(kind="DEBIT", operation_date=day, card_date=None, counterparty=creditor, amount_due=Decimal(amount))


class NamingTests(SimpleTestCase):
    def test_dotted_initials_are_one_word(self):
        self.assertEqual(words("METRO FRANCE S.A.S.-METRO FRANCE"), ["METRO", "FRANCE", "SAS", "METRO", "FRANCE"])
        self.assertEqual(words("U.B.A."), ["UBA"])

    def test_the_bank_names_a_shop_however_it_spells_it(self):
        for payee, naming in (
            ("FRANPRIX 5333 PARIS", NAMING[FRANPRIX]),
            ("U.B.A.", NAMING[UBA]),
            ("SABBAH", Naming(supplier_words("Sabbh Oriental", "SABBH"))),
            ("WING SENG PARIS", Naming(supplier_words("Wing Seng", "WINGSENG"))),
            ("SCEA PLOU ET FILS", Naming(supplier_words("SCEA Plou & Fils", "PLOUFILS"))),
        ):
            with self.subTest(payee=payee):
                self.assertTrue(names_supplier(payee, naming))

    def test_another_shop_is_not_named(self):
        self.assertFalse(names_supplier("MONOPRIX PARIS", NAMING[FRANPRIX]))

    def test_generic_words_name_nobody(self):
        """ "SCEA", "FILS" and "PARIS" are in half the payees on a statement."""
        self.assertEqual(supplier_words("Autre (analyse IA)", "OTHER"), frozenset())
        self.assertEqual(supplier_words("SCEA Plou & Fils"), frozenset({"PLOU"}))

    def test_a_payee_a_person_linked_names_the_supplier(self):
        naming = Naming(frozenset({"MONOPRIX"}), aliases=frozenset({"SUMUP BK PREM"}))
        self.assertTrue(names_supplier("SUMUP *BK PREM", naming))


class CardPaymentTests(SimpleTestCase):
    def test_the_amount_the_day_and_the_shop_link_a_receipt(self):
        receipt = invoice(1, FRANPRIX, JULY_15, "13.06")
        found = match(card(JULY_15, "FRANPRIX 5333 PARIS", "13.06"), [receipt], NAMING)
        self.assertEqual((found.confident, found.options), (True, [(receipt,)]))

    def test_the_same_amount_at_a_shop_the_bank_does_not_name_is_only_suggested(self):
        receipt = invoice(1, MONOPRIX, JULY_15, "13.06")
        found = match(card(JULY_15, "SUMUP *BK PREM", "13.06"), [receipt], NAMING)
        self.assertEqual((found.confident, found.options), (False, [(receipt,)]))

    def test_a_receipt_from_days_before_the_card_was_used_is_not_it(self):
        found = match(card(JULY_15, "FRANPRIX", "13.06"), [invoice(1, FRANPRIX, date(2026, 7, 10), "13.06")], NAMING)
        self.assertIsNone(found)

    def test_two_receipts_of_that_amount_go_by_the_nearest_day(self):
        same_day, next_day = invoice(1, FRANPRIX, JULY_15, "1.96"), invoice(2, FRANPRIX, date(2026, 7, 16), "1.96")
        found = match(card(JULY_15, "FRANPRIX", "1.96"), [next_day, same_day], NAMING)
        self.assertEqual((found.confident, found.options), (True, [(same_day,)]))

    def test_a_misread_receipt_is_suggested_rather_than_linked(self):
        """The bank's 13,06 is right; a ticket read as 13,02 is not."""
        misread = invoice(1, FRANPRIX, JULY_15, "13.02")
        found = match(card(JULY_15, "FRANPRIX", "13.06"), [misread], NAMING)
        self.assertEqual((found.confident, found.options), (False, [(misread,)]))


class LaterPaymentTests(SimpleTestCase):
    def test_a_debit_pays_an_invoice_dated_before_it(self):
        delivery = invoice(1, METRO, date(2026, 6, 29), "1234.50")
        found = match(debit(date(2026, 7, 9), "METRO FRANCE S.A.S.-METRO FRANCE", "1234.50"), [delivery], NAMING)
        self.assertEqual((found.confident, found.options), (True, [(delivery,)]))

    def test_never_an_invoice_dated_after_it(self):
        found = match(
            debit(date(2026, 7, 9), "METRO FRANCE", "1234.50"),
            [invoice(1, METRO, date(2026, 7, 10), "1234.50")],
            NAMING,
        )
        self.assertIsNone(found)

    def test_one_debit_for_two_deliveries_and_a_returned_deposit(self):
        paid = (
            invoice(1, UBA, date(2026, 7, 3), "120.00"),
            invoice(2, UBA, date(2026, 7, 10), "80.50"),
            invoice(3, UBA, date(2026, 7, 10), "-15.00"),
        )
        unrelated = invoice(4, UBA, date(2026, 7, 1), "999.00")
        found = match(debit(date(2026, 7, 27), "U.B.A.", "185.50"), [*paid, unrelated], NAMING)
        self.assertEqual((found.confident, found.options), (True, [paid]))

    def test_two_ways_to_add_up_to_it_are_not_decided_between(self):
        pool = [
            invoice(pk, UBA, date(2026, 7, pk), total)
            for pk, total in ((1, "100.00"), (2, "50.00"), (3, "60.00"), (4, "90.00"))
        ]
        found = match(debit(date(2026, 7, 27), "U.B.A.", "150.00"), pool, NAMING)
        self.assertEqual((found.confident, len(found.options)), (False, 2))

    def test_two_invoices_of_that_amount_are_not_decided_between(self):
        pair = [invoice(1, METRO, date(2026, 6, 20), "104.77"), invoice(2, METRO, date(2026, 6, 25), "104.77")]
        found = match(debit(date(2026, 7, 1), "METRO FRANCE", "104.77"), pair, NAMING)
        self.assertEqual((found.confident, len(found.options)), (False, 2))

    def test_a_supplier_the_bank_does_not_name_is_never_offered(self):
        """Months of invoices are in reach of a debit: an amount alone would
        always find something."""
        found = match(
            debit(date(2026, 7, 9), "TOTALENERGIES ELECTRICITE ET GAZ FRANCE", "104.77"),
            [invoice(1, METRO, date(2026, 6, 20), "104.77")],
            NAMING,
        )
        self.assertIsNone(found)

    def test_an_empty_invoice_is_never_part_of_a_sum(self):
        pool = [
            invoice(1, UBA, date(2026, 7, 1), "60.00"),
            invoice(2, UBA, date(2026, 7, 2), "40.00"),
            invoice(3, UBA, date(2026, 7, 3), "0"),
        ]
        found = match(debit(date(2026, 7, 20), "U.B.A.", "100.00"), pool, NAMING)
        self.assertEqual((found.confident, found.options), (True, [tuple(pool[:2])]))

    def test_money_coming_in_is_not_a_payment(self):
        found = match(
            debit(date(2026, 7, 9), "METRO FRANCE", "-120.00"), [invoice(1, METRO, date(2026, 7, 1), "120.00")], NAMING
        )
        self.assertIsNone(found)


class UndatedReceiptTests(SimpleTestCase):
    """OCR misses a receipt's date more often than its total. The shop and the
    amount can still point at it - for a person to confirm."""

    def test_an_undated_receipt_from_the_named_shop_is_suggested(self):
        receipt = invoice(1, MONOPRIX, None, "7.76")
        found = match(card(JULY_15, "MONOPRIX PARIS", "7.76"), [receipt], NAMING)
        self.assertEqual((found.confident, found.options), (False, [(receipt,)]))

    def test_a_cent_of_rounding_is_allowed(self):
        """Rebuilt from its HT lines, a receipt printed 7,76 totals 7,75."""
        self.assertIsNotNone(
            match(card(JULY_15, "MONOPRIX PARIS", "7.76"), [invoice(1, MONOPRIX, None, "7.75")], NAMING)
        )

    def test_another_shop_or_another_amount_is_not(self):
        for candidate in (invoice(1, FRANPRIX, None, "7.76"), invoice(2, MONOPRIX, None, "7.90")):
            with self.subTest(candidate=candidate):
                self.assertIsNone(match(card(JULY_15, "MONOPRIX PARIS", "7.76"), [candidate], NAMING))

    def test_a_debit_is_never_offered_an_undated_invoice(self):
        self.assertIsNone(match(debit(JULY_15, "METRO FRANCE", "104.77"), [invoice(1, METRO, None, "104.77")], NAMING))


def days_before(day, days):
    return day - timedelta(days=days)


# The bank's own fee line: the same words under a new number and a new date
# every month. Structurally faithful, wording and figures invented.
FEE_LABEL = "FRAIS TENUE DE COMPTE N° 000123 DU 05/06/26"
FEE_KEY = "FRAIS TENUE DE COMPTE N DU"


def other(day, label, amount):
    """A line of kind OTHER with no counterparty at all: the bank's own fee,
    whose label alone says who was paid."""
    return Payment(
        kind="OTHER", operation_date=day, card_date=None, counterparty="", amount_due=Decimal(amount), label=label
    )


class TierTests(SimpleTestCase):
    """SURE is `confident` and nothing else; NEAR_SURE is one best option with
    a clear margin; everything else is TO_CONFIRM. Every figure invented."""

    def test_sure_is_confident_and_confident_is_sure(self):
        delivery = invoice(1, METRO, date(2026, 6, 29), "1234.50")
        found = match(debit(date(2026, 7, 9), "METRO FRANCE", "1234.50"), [delivery], NAMING)
        self.assertEqual((found.confident, found.tier, found.tier_label), (True, SURE, "certaine"))
        # A sure match says how far back its invoice is, like the others do.
        self.assertEqual((found.days_back, found.far_back), (10, False))
        self.assertEqual(found.tier_reason, "Facture datée 10 jours avant le paiement.")
        self.assertEqual(Match([(delivery,)], True, "x").tier, SURE)
        with self.assertRaises(ValueError):
            Match([(delivery,)], False, "x", SURE)
        with self.assertRaises(ValueError):
            Match([(delivery,)], False, "x", "maybe")

    def test_a_recurring_debit_is_near_sure_when_the_month_s_invoice_stands_alone(self):
        """Three identical invoices, dated 4, 34 and 65 days before the debit:
        a subscription. The first by date distance is the month's."""
        paid = date(2026, 7, 6)
        invoices = [invoice(pk, METRO, days_before(paid, days), "23.70") for pk, days in ((1, 34), (2, 4), (3, 65))]
        found = match(debit(paid, "METRO FRANCE", "23.70"), invoices, NAMING)
        self.assertEqual((found.confident, found.tier, found.near_sure), (False, NEAR_SURE, True))
        self.assertEqual([option[0].pk for option in found.options], [2, 1, 3])
        self.assertIn("4 jours avant", found.tier_reason)
        self.assertIn("34 jours au moins", found.tier_reason)
        self.assertIn("3 factures", found.reason)

    def test_a_nearest_invoice_beyond_the_cap_is_a_question(self):
        """A rent whose month's invoice is at another amount (or not imported
        yet): the nearest of THIS amount is last month's, past the cap."""
        paid = date(2026, 8, 12)
        invoices = [invoice(pk, METRO, days_before(paid, days), "640.00") for pk, days in ((1, 40), (2, 71), (3, 101))]
        found = match(debit(paid, "METRO FRANCE", "640.00"), invoices, NAMING)
        self.assertEqual(found.tier, TO_CONFIRM)
        self.assertIn(f"au-delà de {RECURRING_DAYS_BEFORE.days}", found.tier_reason)

    def test_a_sure_debit_reaching_months_back_is_sure_and_says_the_distance(self):
        """The rent's siblings at one figure all linked, the month's invoice
        at ANOTHER figure: the one unpaid invoice left at the debit's amount
        is a five-month-old one. Exact, named, within the 180-day window: it
        IS `confident`, and the pass links it (a rule this stream must not
        move). What the matching owes the page is the distance, said, and
        the flag the page withholds its pre-tick on. Failing first: a sure
        match had no tier_reason at all, and no `far_back`."""
        paid = date(2026, 7, 24)
        far = RECURRING_DAYS_BEFORE.days + 125
        old = invoice(1, METRO, days_before(paid, far), "31.40")
        this_month = invoice(2, METRO, days_before(paid, 10), "44.00")
        found = match(debit(paid, "METRO FRANCE", "31.40"), [old, this_month], NAMING)
        self.assertEqual((found.confident, found.tier, found.options), (True, SURE, [(old,)]))
        self.assertEqual((found.days_back, found.far_back), (far, True))
        self.assertIn(f"datée {far} jours avant le paiement", found.tier_reason)
        self.assertIn(f"au-delà de {RECURRING_DAYS_BEFORE.days}", found.tier_reason)
        self.assertIn(f"{LATER_PAYMENT_WINDOW.days} jours", found.tier_reason)
        self.assertIn("pas cochée d'avance", found.tier_reason)
        # At the cap it is not far; a day past it is.
        at_cap = invoice(1, METRO, days_before(paid, RECURRING_DAYS_BEFORE.days), "31.40")
        found = match(debit(paid, "METRO FRANCE", "31.40"), [at_cap], NAMING)
        self.assertEqual(
            (found.far_back, found.tier_reason),
            (False, f"Facture datée {RECURRING_DAYS_BEFORE.days} jours avant le paiement."),
        )
        past_cap = invoice(1, METRO, days_before(paid, RECURRING_DAYS_BEFORE.days + 1), "31.40")
        self.assertTrue(match(debit(paid, "METRO FRANCE", "31.40"), [past_cap], NAMING).far_back)
        # A card payment's window is days wide: never far. The day itself is
        # said as such, not as « 0 jours ».
        found = match(card(JULY_15, "FRANPRIX", "13.06"), [invoice(1, FRANPRIX, JULY_15, "13.06")], NAMING)
        self.assertEqual(
            (found.days_back, found.far_back, found.tier_reason), (0, False, "Facture du jour du paiement.")
        )
        found = match(
            card(JULY_15, "FRANPRIX", "13.06"), [invoice(1, FRANPRIX, days_before(JULY_15, 2), "13.06")], NAMING
        )
        self.assertEqual((found.days_back, found.tier_reason), (2, "Facture à 2 jours du paiement."))
        # A sum of invoices is dated by its most recent one.
        pair = [invoice(1, UBA, days_before(paid, 40), "120.00"), invoice(2, UBA, days_before(paid, 12), "80.50")]
        found = match(debit(paid, "U.B.A.", "200.50"), pair, NAMING)
        self.assertEqual((found.confident, found.days_back, found.far_back), (True, 12, False))
        self.assertEqual(found.tier_reason, "Somme de 2 factures, la plus récente datée 12 jours avant le paiement.")
        # An undated receipt has no distance, and is never far.
        found = match(card(JULY_15, "MONOPRIX", "7.76"), [invoice(1, MONOPRIX, None, "7.76")], NAMING)
        self.assertEqual((found.days_back, found.far_back), (None, False))

    def test_the_cap_and_the_margin_are_inclusive_at_the_edge(self):
        paid = date(2026, 7, 6)
        at_cap = RECURRING_DAYS_BEFORE.days
        margin = RECURRING_MARGIN.days
        cases = (
            (at_cap, at_cap + margin, NEAR_SURE),
            (at_cap + 1, at_cap + 1 + margin, TO_CONFIRM),
            (at_cap, at_cap + margin - 1, TO_CONFIRM),
        )
        for nearest, second, tier in cases:
            with self.subTest(nearest=nearest, second=second):
                invoices = [
                    invoice(1, METRO, days_before(paid, nearest), "23.70"),
                    invoice(2, METRO, days_before(paid, second), "23.70"),
                ]
                self.assertEqual(match(debit(paid, "METRO FRANCE", "23.70"), invoices, NAMING).tier, tier)

    def test_two_deliveries_a_week_apart_are_a_question(self):
        paid = date(2026, 7, 6)
        invoices = [
            invoice(1, METRO, days_before(paid, 5), "120.00"),
            invoice(2, METRO, days_before(paid, 12), "120.00"),
        ]
        found = match(debit(paid, "METRO FRANCE", "120.00"), invoices, NAMING)
        self.assertEqual(found.tier, TO_CONFIRM)
        self.assertIn(f"moins de {RECURRING_MARGIN.days} jours", found.tier_reason)

    def test_two_candidates_equidistant_are_a_question_for_a_debit_and_a_card(self):
        pair = [invoice(1, METRO, date(2026, 6, 20), "104.77"), invoice(2, METRO, date(2026, 6, 20), "104.77")]
        found = match(debit(date(2026, 7, 1), "METRO FRANCE", "104.77"), pair, NAMING)
        self.assertEqual((found.tier, len(found.options)), (TO_CONFIRM, 2))
        receipts = [invoice(1, FRANPRIX, JULY_15, "1.96"), invoice(2, FRANPRIX, JULY_15, "1.96")]
        found = match(card(JULY_15, "FRANPRIX", "1.96"), receipts, NAMING)
        self.assertEqual((found.confident, found.tier), (False, TO_CONFIRM))

    def test_an_undated_invoice_never_enters_the_recurring_rule(self):
        paid = date(2026, 7, 6)
        invoices = [
            invoice(1, METRO, days_before(paid, 4), "23.70"),
            invoice(2, METRO, days_before(paid, 34), "23.70"),
            invoice(3, METRO, None, "23.70"),
        ]
        found = match(debit(paid, "METRO FRANCE", "23.70"), invoices, NAMING)
        self.assertEqual((found.tier, [option[0].pk for option in found.options]), (NEAR_SURE, [1, 2]))

    def test_two_suppliers_named_by_one_payee_are_never_near_sure(self):
        """A payee may name two suppliers (one word both carry): the
        recurring rule's premise - « the named supplier's invoices » - and
        the cent-gap rule's do not hold, whatever the dates or the gaps say.
        Failing first: both were pre-ticked NEAR_SURE on the nearest."""
        box, mobile = 5, 6
        naming = {
            box: Naming(supplier_words("Box Exemple", "BOX")),
            mobile: Naming(supplier_words("Mobile Exemple", "MOBILE")),
        }
        paid = date(2026, 7, 6)
        recurring = [invoice(1, box, days_before(paid, 5), "31.40"), invoice(2, mobile, days_before(paid, 31), "31.40")]
        found = match(debit(paid, "EXEMPLE SAS", "31.40"), recurring, naming)
        self.assertEqual((found.confident, found.tier), (False, TO_CONFIRM))
        self.assertIn("2 fournisseurs", found.tier_reason)
        close = [invoice(1, box, days_before(paid, 5), "31.38"), invoice(2, mobile, days_before(paid, 5), "31.90")]
        found = match(debit(paid, "EXEMPLE SAS", "31.40"), close, naming)
        self.assertEqual((found.confident, found.tier), (False, TO_CONFIRM))
        self.assertIn("2 fournisseurs", found.tier_reason)
        # The same figures at one supplier: the rules apply as before.
        at_box = lambda candidates: [InvoiceCandidate(c.pk, box, c.invoice_date, c.total) for c in candidates]
        self.assertEqual(match(debit(paid, "EXEMPLE SAS", "31.40"), at_box(recurring), naming).tier, NEAR_SURE)
        self.assertEqual(match(debit(paid, "EXEMPLE SAS", "31.40"), at_box(close), naming).tier, NEAR_SURE)

    def test_one_invoice_a_cent_off_is_near_sure(self):
        """A total rebuilt from its lines, a cent from the bank's figure -
        for a card payment and for a debit alike."""
        misread = invoice(1, FRANPRIX, JULY_15, "13.05")
        found = match(card(JULY_15, "FRANPRIX", "13.06"), [misread], NAMING)
        self.assertEqual((found.confident, found.tier), (False, NEAR_SURE))
        self.assertIn("0.01 €", found.tier_reason)
        rounded = invoice(2, UBA, date(2026, 5, 29), "256.42")
        found = match(debit(date(2026, 6, 15), "U.B.A.", "256.43"), [rounded], NAMING)
        self.assertEqual(found.tier, NEAR_SURE)

    def test_one_close_invoice_months_back_is_a_question_for_a_debit(self):
        """A debit reaches 180 days back. A recurring supplier whose amount
        moved by a few cents, and whose month's invoice is not imported, has
        last month's - or a five-month-old one - a few cents off: that is the
        wrong-month case the recurring rule guards against, not a misreading.
        Failing first: it was pre-ticked NEAR_SURE whatever its date."""
        paid = date(2026, 7, 6)
        at_cap, past_cap = RECURRING_DAYS_BEFORE.days, RECURRING_DAYS_BEFORE.days + 30
        found = match(debit(paid, "U.B.A.", "256.43"), [invoice(1, UBA, days_before(paid, past_cap), "256.39")], NAMING)
        self.assertEqual((found.confident, found.tier), (False, TO_CONFIRM))
        self.assertIn(f"{past_cap} jours avant le paiement", found.tier_reason)
        self.assertIn(f"au-delà de {at_cap}", found.tier_reason)
        self.assertIn("0.04 €", found.tier_reason)
        self.assertEqual(found.options, [(invoice(1, UBA, days_before(paid, past_cap), "256.39"),)])
        # At the cap it still is; and a card payment's window is days wide,
        # so the cap says nothing there.
        self.assertEqual(
            match(debit(paid, "U.B.A.", "256.43"), [invoice(1, UBA, days_before(paid, at_cap), "256.39")], NAMING).tier,
            NEAR_SURE,
        )
        self.assertEqual(
            match(
                card(JULY_15, "FRANPRIX", "13.06"), [invoice(1, FRANPRIX, days_before(JULY_15, 2), "13.05")], NAMING
            ).tier,
            NEAR_SURE,
        )
        # The cap's premise is « not paid on the spot », so a fee line (kind
        # OTHER, named by its alias) past it is a question too - the rule the
        # page states says so in as many words.
        taught = Naming(frozenset(), aliases=frozenset({FEE_KEY}))
        found = match(other(paid, FEE_LABEL, "7.30"), [invoice(1, 9, days_before(paid, past_cap), "7.28")], {9: taught})
        self.assertEqual((found.confident, found.tier), (False, TO_CONFIRM))
        self.assertIn(f"au-delà de {at_cap}", found.tier_reason)
        self.assertEqual(
            match(other(paid, FEE_LABEL, "7.30"), [invoice(1, 9, days_before(paid, at_cap), "7.28")], {9: taught}).tier,
            NEAR_SURE,
        )

    def test_a_second_invoice_far_off_does_not_spoil_it_but_a_second_within_cents_does(self):
        paid = date(2026, 6, 15)
        close, far = invoice(1, UBA, days_before(paid, 10), "256.42"), invoice(2, UBA, days_before(paid, 20), "271.60")
        found = match(debit(paid, "U.B.A.", "256.43"), [close, far], NAMING)
        self.assertEqual((found.tier, found.options[0]), (NEAR_SURE, (close,)))
        also_close = invoice(3, UBA, days_before(paid, 30), "256.45")
        found = match(debit(paid, "U.B.A.", "256.43"), [close, far, also_close], NAMING)
        self.assertEqual(found.tier, TO_CONFIRM)
        self.assertIn(f"2 factures à moins de {NEAR_SURE_GAP:.2f} €", found.tier_reason)

    def test_a_gap_of_euros_is_another_document(self):
        other = invoice(1, UBA, date(2026, 6, 1), "512.00")
        found = match(debit(date(2026, 6, 15), "U.B.A.", "516.10"), [other], NAMING)
        self.assertEqual(found.tier, TO_CONFIRM)
        self.assertIn("4.10 €", found.tier_reason)
        self.assertEqual(found.options, [(other,)])

    def test_a_gap_of_thousands_is_said_with_its_thousands_grouped(self):
        """The reason is read on Banque and « Propositions »: its amount
        groups its thousands with a no-break space, as the page's do."""
        other = invoice(1, METRO, date(2026, 6, 1), "18500.00")
        found = match(debit(date(2026, 6, 15), "METRO FRANCE", "20000.00"), [other], NAMING)
        self.assertEqual(found.tier, TO_CONFIRM)
        self.assertIn(f"L'écart le plus faible est de 1{NBSP}500.00 €", found.tier_reason)

    def test_a_gap_exactly_at_the_threshold_is_still_near_sure(self):
        found = match(
            card(JULY_15, "FRANPRIX", "13.06"),
            [invoice(1, FRANPRIX, JULY_15, str(Decimal("13.06") - NEAR_SURE_GAP))],
            NAMING,
        )
        self.assertEqual(found.tier, NEAR_SURE)

    def test_two_invoices_adding_up_to_a_cent_off_are_never_near_sure(self):
        """A debit equal to two invoices within a cent: not a sum that adds
        up, not a single close invoice - nothing, or a question."""
        pair = [invoice(1, UBA, date(2026, 9, 17), "410.25"), invoice(2, UBA, date(2026, 9, 17), "530.15")]
        found = match(debit(date(2026, 10, 2), "U.B.A.", "940.39"), pair, NAMING)
        self.assertTrue(found is None or found.tier == TO_CONFIRM)

    def test_the_other_suggestions_are_questions(self):
        unnamed = match(
            card(JULY_15, "PAYTERM *EPICERIE 12", "13.06"), [invoice(1, MONOPRIX, JULY_15, "13.06")], NAMING
        )
        undated = match(card(JULY_15, "MONOPRIX", "7.76"), [invoice(1, MONOPRIX, None, "7.76")], NAMING)
        pool = [
            invoice(pk, UBA, date(2026, 7, pk), total)
            for pk, total in ((1, "100.00"), (2, "50.00"), (3, "60.00"), (4, "90.00"))
        ]
        two_sums = match(debit(date(2026, 7, 27), "U.B.A.", "150.00"), pool, NAMING)
        for found in (unnamed, undated, two_sums):
            with self.subTest(reason=found.reason):
                self.assertEqual((found.confident, found.tier, found.tier_label), (False, TO_CONFIRM, "à confirmer"))
                self.assertTrue(found.tier_reason)

    def test_nothing_due_is_nothing(self):
        for due in ("0", "-23.70"):
            with self.subTest(due=due):
                self.assertIsNone(
                    match(debit(JULY_15, "METRO FRANCE", due), [invoice(1, METRO, date(2026, 7, 1), due)], NAMING)
                )

    def test_every_tier_has_a_rule_the_page_can_state(self):
        self.assertEqual([tier for tier, _label, _rule in TIER_RULES], [SURE, NEAR_SURE, TO_CONFIRM])
        rules = {tier: rule for tier, _label, rule in TIER_RULES}
        self.assertIn(f"{RECURRING_DAYS_BEFORE.days} jours", rules[NEAR_SURE])
        self.assertIn(f"{RECURRING_MARGIN.days} jours", rules[NEAR_SURE])
        self.assertIn(f"{NEAR_SURE_GAP:.2f} €", rules[NEAR_SURE])
        # The cent-gap rule's date cap and the one-supplier premise are part
        # of the rule, so they are part of what the page states - and the cap
        # is worded from the code's own premise (any payment not made by
        # card: a fee line is capped too), not from two of the three kinds.
        self.assertIn("autre que par carte", rules[NEAR_SURE])
        self.assertNotIn("prélèvement ou un virement", rules[NEAR_SURE])
        self.assertIn("un seul fournisseur", rules[NEAR_SURE])
        self.assertIn("deux fournisseurs", rules[TO_CONFIRM])
        # A sure line reaches the page in two cases, and the rule says both -
        # and says how far back a sure match may reach, and that one reaching
        # past the cap is not ticked in advance.
        self.assertIn("relancé depuis le dernier import", rules[SURE])
        self.assertIn("déliée à la main", rules[SURE])
        self.assertIn(f"{LATER_PAYMENT_WINDOW.days} jours", rules[SURE])
        self.assertIn(f"au-delà de {RECURRING_DAYS_BEFORE.days} jours", rules[SURE])
        self.assertIn("pas cochée d'avance", rules[SURE].lower())


class BlankCounterpartyTests(SimpleTestCase):
    """When the bank prints no payee, the label's words - digits taken out,
    so the key is the same under next month's number - stand in for it. It
    names a supplier through an alias a person taught, and through nothing
    else: not a word of the supplier's own name, not the one-letter-off
    rule. The automatic pass never linked a line the bank printed no payee
    on, and the fallback is there to let such lines be TAUGHT, not to widen
    the pass."""

    def test_the_payee_is_the_counterparty_when_there_is_one(self):
        self.assertEqual(payee_of("FRANPRIX 5333 PARIS", FEE_LABEL), "FRANPRIX 5333 PARIS")

    def test_the_label_s_words_without_their_digits_stand_in_for_a_blank_one(self):
        self.assertEqual(payee_of("", FEE_LABEL), FEE_KEY)
        self.assertEqual(payee_of("", "FRAIS TENUE DE COMPTE N° 000987 DU 05/07/26"), FEE_KEY)
        self.assertEqual(alias_key(payee_of("", FEE_LABEL)), FEE_KEY)
        self.assertEqual(payee_of("", ""), "")
        self.assertEqual(payee_of("", "12345 67"), "")

    def test_the_fallback_names_nobody_on_its_own(self):
        banking = Naming(supplier_words("Banque Exemple", "BANQUE"))
        found = match(
            other(date(2026, 6, 5), FEE_LABEL, "7.30"), [invoice(1, 9, date(2026, 6, 5), "7.30")], {9: banking}
        )
        self.assertIsNone(found)
        self.assertFalse(names_supplier(payee_of("", ""), banking))

    def test_an_alias_learnt_from_the_label_names_the_supplier(self):
        banking = Naming(supplier_words("Banque Exemple", "BANQUE"), aliases=frozenset({FEE_KEY}))
        found = match(
            other(date(2026, 6, 5), FEE_LABEL, "7.30"), [invoice(1, 9, date(2026, 6, 5), "7.30")], {9: banking}
        )
        self.assertEqual((found.confident, found.tier), (True, SURE))

    def test_an_exact_word_of_the_supplier_s_own_name_in_the_label_names_nobody_until_taught(self):
        """An insurer's premium, debited with no counterparty but a label
        printing the insurer's name. As a COUNTERPARTY that word names the
        insurer; in a LABEL it does not, until a person links one such line:
        the pass never linked a line the bank printed no payee on, and an
        exact word in a label that also carries the bank's text and a reference
        would have linked automatically on a coincidence (a supplier named
        « Assurance Exemple », a fee line reading « ASSURANCE MOYENS DE
        PAIEMENT »). Failing first: (True, SURE) from the label alone."""
        insurer = Naming(supplier_words("Assureur Exemple", "ASSUREUR"))
        premium = other(date(2026, 6, 5), "COTISATION ASSUREUR EXEMPLE CONTRAT 0001234", "42.10")
        self.assertTrue(names_supplier("ASSUREUR EXEMPLE", insurer))
        self.assertFalse(names_supplier(premium.payee, insurer, alias_only=True))
        self.assertIsNone(match(premium, [invoice(1, 9, date(2026, 6, 5), "42.10")], {9: insurer}))
        # Taught - the alias is the label's words without their digits -
        # next month's line links on its own.
        taught = Naming(insurer.words, aliases=frozenset({alias_key(premium.payee)}))
        self.assertEqual(alias_key(premium.payee), "COTISATION ASSUREUR EXEMPLE CONTRAT")
        found = match(
            other(date(2026, 7, 5), "COTISATION ASSUREUR EXEMPLE CONTRAT 0001235", "42.10"),
            [invoice(1, 9, date(2026, 7, 5), "42.10")],
            {9: taught},
        )
        self.assertEqual((found.confident, found.tier), (True, SURE))

    def test_a_label_word_one_letter_off_a_supplier_s_name_names_nobody(self):
        """« COMPTE » is one letter from « COMPTA ». For a counterparty the
        bank printed on purpose that is a spelling; for a label, which also
        carries a reference, a month and the bank's own words, it is a coincidence
        that would link AUTOMATICALLY. Failing first: it was a SURE link."""
        accountant = Naming(supplier_words("Compta Exemple", "COMPTA"))
        self.assertTrue(names_supplier("COMPTE SAS", accountant))
        self.assertFalse(names_supplier(payee_of("", FEE_LABEL), accountant, alias_only=True))
        found = match(
            other(date(2026, 6, 5), FEE_LABEL, "7.30"), [invoice(1, 9, date(2026, 6, 5), "7.30")], {9: accountant}
        )
        self.assertIsNone(found)
        # The alias path is untouched: once a person links it, next month
        # links on its own.
        taught = Naming(accountant.words, aliases=frozenset({FEE_KEY}))
        found = match(
            other(date(2026, 7, 5), "FRAIS TENUE DE COMPTE N° 000456 DU 05/07/26", "7.30"),
            [invoice(1, 9, date(2026, 7, 5), "7.30")],
            {9: taught},
        )
        self.assertEqual((found.confident, found.tier), (True, SURE))

    def test_the_bank_s_own_words_name_nobody_by_themselves(self):
        """« FACTURE », « FRAIS », « CARTE » are on every statement, and a
        parsed counterparty may still carry one (« VIR SEPA … »): they are
        not a supplier's naming words, so a counterparty made of them names
        no supplier whose name carries one. A label printing them names
        nobody either way - it names by alias only - a supplier a letter away
        from one (« FRAISE », « CARTEL ») included."""
        self.assertEqual(supplier_words("Frais Carte Facture Exemple", "FRAIS"), frozenset({"EXEMPLE"}))
        suppliers = {
            11: Naming(supplier_words("Carte Exemple", "CARTE")),
            12: Naming(supplier_words("Fraise Exemple", "FRAISE")),
            13: Naming(supplier_words("Cartel Exemple", "CARTEL")),
            14: Naming(supplier_words("Facturo Exemple", "FACTURO")),
        }
        self.assertFalse(names_supplier("FACTURE CARTE FRAIS DIVERS", suppliers[11]))
        label = "FACTURE CARTE DU 050626 FRAIS DIVERS 000123"
        for supplier_id, naming in suppliers.items():
            with self.subTest(supplier=supplier_id):
                self.assertFalse(names_supplier(payee_of("", label), naming, alias_only=True))
        candidates = [invoice(supplier_id, supplier_id, date(2026, 6, 5), "7.30") for supplier_id in suppliers]
        self.assertIsNone(match(other(date(2026, 6, 5), label, "7.30"), candidates, suppliers))

    def test_two_months_of_fees_unpaid_are_near_sure_by_date(self):
        banking = Naming(frozenset(), aliases=frozenset({FEE_KEY}))
        invoices = [invoice(1, 9, date(2026, 5, 5), "7.30"), invoice(2, 9, date(2026, 6, 5), "7.30")]
        found = match(other(date(2026, 6, 5), FEE_LABEL, "7.30"), invoices, {9: banking})
        self.assertEqual((found.tier, found.options[0][0].pk), (NEAR_SURE, 2))
