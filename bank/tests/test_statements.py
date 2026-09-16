"""Reading a BNP Paribas CSV export.

Structurally faithful to the real export - field order, quoting, the spacing
inside labels, French amounts - with every name, number and amount invented.
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from bank.statements import parse_statement

HEADER = '"Compte de ch&egrave;ques";"Compte de ch&amp;egrave;ques";****0042;14/09/2026;;1 234,56\n'
CARD = (
    "02/07/2026;PAIEMENT CB;FACTURE CARTE;FACTURE CARTE DU 010726 WING SENG       PARIS           "
    "CARTE   4974XXXXXXXX1111;02/07/2026;-12,34\n"
)
DEBIT = (
    "09/07/2026;PRELEVEMENT;PRLV SEPA;PRLV SEPA METRO FRANCE S.A.S.-METRO FRANCE ECH/090726 ID EMETTEUR/FR00ZZZ000000 "
    "MDT/CMR000000000001 REF/00000000002026 LIB//INV/0051.000001;09/07/2026;-1 234,50\n"
)
TRANSFER_OUT = (
    "06/07/2026;VIREMENT INSTANTANE EMIS;VIR SEPA INST EMIS;VIR SEPA INST EMIS /MOTIF FACTURE EN RETARD "
    "/BEN SCEA EXEMPLE ET FILS /REFDO 0000000000000000 /REF NOTPROVIDED;04/07/2026;-250,00\n"
)
TRANSFER_IN = (
    "06/07/2026;VIREMENT;VIR SEPA RECU;VIR SEPA RECU /FRM AU COMPTOIR /EID  /RNF TRANSFERT 1000001 "
    "TOTAL ENCAISSE 1127.5 EUROS;06/07/2026;1 111,11\n"
)
B2B = (
    "25/08/2026;PRLV SEPA B2B;PRLV SEPA B2B;PRLV SEPA B2B DGFIP IMPOT 0750750 ECH/250826 ID EMETTEUR/FR00ZZZ000000 "
    "MDT/NN000000 REF/000000 LIB/TLR SEPA;25/08/2026;-16,00\n"
)
LOAN = "20/07/2026;ECHEANCE PRET;ECHEANCE PRET;ECHEANCE PRET 00000 00000000;19/07/2026;-1 500,00\n"


def parse(*rows, encoding="utf-8"):
    return parse_statement((HEADER + "".join(rows)).encode(encoding))


class StatementTests(SimpleTestCase):
    def test_the_account_comes_from_the_header(self):
        self.assertEqual(parse(CARD).account, "****0042")

    def test_a_card_payment_carries_the_day_the_card_was_used(self):
        (line,) = parse(CARD).lines
        self.assertEqual(
            (line.kind, line.operation_date, line.card_date, line.counterparty, line.amount),
            ("CARD", date(2026, 7, 2), date(2026, 7, 1), "WING SENG PARIS", Decimal("-12.34")),
        )

    def test_a_direct_debit_names_its_creditor_and_keeps_its_thousands(self):
        (line,) = parse(DEBIT).lines
        self.assertEqual(
            (line.kind, line.counterparty, line.amount),
            ("DEBIT", "METRO FRANCE S.A.S.-METRO FRANCE", Decimal("-1234.50")),
        )

    def test_a_b2b_debit_names_its_creditor_too(self):
        self.assertEqual(parse(B2B).lines[0].counterparty, "DGFIP IMPOT 0750750")

    def test_a_transfer_names_who_received_it_or_who_sent_it(self):
        out, incoming = parse(TRANSFER_OUT, TRANSFER_IN).lines
        self.assertEqual(
            (out.kind, out.counterparty, out.value_date), ("TRANSFER", "SCEA EXEMPLE ET FILS", date(2026, 7, 4))
        )
        self.assertEqual((incoming.counterparty, incoming.amount), ("AU COMPTOIR", Decimal("1111.11")))

    def test_anything_else_is_kept_without_a_payee(self):
        (line,) = parse(LOAN).lines
        self.assertEqual((line.kind, line.counterparty, line.amount), ("OTHER", "", Decimal("-1500.00")))

    def test_the_label_is_kept_with_its_spacing_collapsed(self):
        self.assertIn("FACTURE CARTE DU 010726 WING SENG PARIS CARTE", parse(CARD).lines[0].label)

    def test_an_export_in_windows_encoding_is_read(self):
        row = "20/07/2026;ECHEANCE PRET;ECHEANCE PRET;ECHEANCE PRÊT 00000;19/07/2026;-10,00\n"
        (line,) = parse(row, encoding="cp1252").lines
        self.assertIn("PRÊT", line.label)

    def test_the_same_statement_gives_the_same_fingerprints(self):
        first = [line.fingerprint for line in parse(CARD, DEBIT).lines]
        again = [line.fingerprint for line in parse(CARD, DEBIT).lines]
        self.assertEqual(first, again)

    def test_two_identical_operations_are_two_lines(self):
        """Two baguettes bought the same morning read exactly alike."""
        first, second = parse(CARD, CARD).lines
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_a_card_date_that_is_not_a_date_is_left_empty(self):
        (line,) = parse(CARD.replace("DU 010726", "DU 320726")).lines
        self.assertEqual((line.kind, line.card_date), ("CARD", None))

    def test_blank_lines_are_ignored(self):
        self.assertEqual(len(parse(CARD, "\n", DEBIT, "\n").lines), 2)

    def test_a_file_that_is_not_a_statement_is_refused(self):
        with self.assertRaises(ValueError):
            parse_statement(b"nom;prenom\nDupont;Jean\n")

    def test_an_unreadable_amount_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValueError):
            parse(CARD.replace("-12,34", "-12,3x"))
