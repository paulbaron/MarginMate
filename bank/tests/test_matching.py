"""Which invoices a bank payment paid, decided by bank.matching from plain
values - no database, no statement file."""

from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase

from bank.matching import InvoiceCandidate, Naming, Payment, match, names_supplier, supplier_words, words

METRO, UBA, FRANPRIX, MONOPRIX = 1, 2, 3, 4
NAMING = {
    METRO: Naming(supplier_words("Metro", "METRO")),
    UBA: Naming(supplier_words("UBA (Bar Exemple)", "UBA")),
    FRANPRIX: Naming(supplier_words("Franprix", "FRANPRIX")),
    MONOPRIX: Naming(supplier_words("Monoprix", "MONOPRIX")),
}
JULY_15 = date(2026, 7, 15)


def invoice(pk, supplier, day, total):
    return InvoiceCandidate(pk=pk, supplier_id=supplier, invoice_date=day, total=Decimal(total))


def card(day, merchant, amount):
    return Payment(
        kind="CARD", operation_date=day + timedelta(days=2), card_date=day, counterparty=merchant,
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
        """"SCEA", "FILS" and "PARIS" are in half the payees on a statement."""
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
        found = match(debit(date(2026, 7, 9), "METRO FRANCE", "1234.50"), [invoice(1, METRO, date(2026, 7, 10), "1234.50")], NAMING)
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
        pool = [invoice(pk, UBA, date(2026, 7, pk), total) for pk, total in ((1, "100.00"), (2, "50.00"), (3, "60.00"), (4, "90.00"))]
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
        pool = [invoice(1, UBA, date(2026, 7, 1), "60.00"), invoice(2, UBA, date(2026, 7, 2), "40.00"), invoice(3, UBA, date(2026, 7, 3), "0")]
        found = match(debit(date(2026, 7, 20), "U.B.A.", "100.00"), pool, NAMING)
        self.assertEqual((found.confident, found.options), (True, [tuple(pool[:2])]))

    def test_money_coming_in_is_not_a_payment(self):
        found = match(debit(date(2026, 7, 9), "METRO FRANCE", "-120.00"), [invoice(1, METRO, date(2026, 7, 1), "120.00")], NAMING)
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
        self.assertIsNotNone(match(card(JULY_15, "MONOPRIX PARIS", "7.76"), [invoice(1, MONOPRIX, None, "7.75")], NAMING))

    def test_another_shop_or_another_amount_is_not(self):
        for candidate in (invoice(1, FRANPRIX, None, "7.76"), invoice(2, MONOPRIX, None, "7.90")):
            with self.subTest(candidate=candidate):
                self.assertIsNone(match(card(JULY_15, "MONOPRIX PARIS", "7.76"), [candidate], NAMING))

    def test_a_debit_is_never_offered_an_undated_invoice(self):
        self.assertIsNone(match(debit(JULY_15, "METRO FRANCE", "104.77"), [invoice(1, METRO, None, "104.77")], NAMING))
