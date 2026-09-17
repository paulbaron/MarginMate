"""A charge document that says what it is made of: the rent apart from the
provisions, and what the statement really charges this month.

The layout is a rent statement's, copied from real ones: two columns on one
line (the account's history on the left, this month's postes on the right),
no spaces between words - that is how the text comes out of these PDFs - and
the month's own total under them. Every name and amount invented.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from invoices.charges import read_charge

D = Decimal

STATEMENT = """AVIS D'ECHEANCE du mois de décembre 2025
Périodedu 01/12/2025au 31/12/2025
Facturen°9000001
BAILLEUREXEMPLE
8boulevardInvente-75924PARIS  cedex19
VOTRE RÉFÉRENCE CLIENT : 2000000
Montant  Détaildel'avisd'échéance  Montant
Détaildesopérationsantérieures
en  au 01/12/2025  en
Soldeantérieurau21/10/2025  0,00  LOYERLOCAUXACTIVITEHT  600,00
ECHEANCEau01/11/2025  830,00  PROV.CHARGESIMMEUBLE  90,00
PRELV.SEPAau10/11/2025  -  830,00  PROVISIONEAUFROIDE  10,00
TVATAUXNORMAL  120,00
Soldeau18/11/2025avantavis  Totaldevotreavisd'échéance(B)  820,00
d'échéance(A)  0,00
Prélevéle10/12/2025  820,00
Avantrèglement,soldetotaldevotrecompteennotrefaveur(A+B):  820,00"""


class RentStatementTests(SimpleTestCase):
    def test_the_postes_are_the_right_column_of_this_month(self):
        """The left column is the account's history - last month's échéance
        and the direct debit that paid it - and only the last amount of each
        line is this month's."""
        total, postes = read_charge(STATEMENT, D("830.00"))
        self.assertEqual(
            [(poste.name, poste.amount, poste.rate) for poste in postes],
            [
                ("LOYERLOCAUXACTIVITEHT", D("720.00"), D("0.20")),
                ("PROV.CHARGESIMMEUBLE", D("90.00"), D("0")),
                ("PROVISIONEAUFROIDE", D("10.00"), D("0")),
            ],
        )
        self.assertEqual(total, D("820.00"))

    def test_the_tax_is_folded_into_the_poste_it_taxes(self):
        """"TVA TAUX NORMAL 120,00" is the 20% of the rent, not a poste of
        its own: the rent is filed at 720,00 TTC, and its HT is the 600,00
        printed."""
        _total, postes = read_charge(STATEMENT, D("830.00"))
        rent = postes[0]
        self.assertEqual((rent.total_ht, rent.rate), (D("600.00"), D("0.20")))
        self.assertNotIn("TVATAUXNORMAL", [poste.name for poste in postes])

    def test_the_postes_settle_what_the_statement_charges(self):
        """830,00 is last month's échéance, printed twice - as the amount
        called and again as the debit paying it - which is what the reader
        takes for the total. The run adding up says 820,00."""
        self.assertEqual(read_charge(STATEMENT, D("830.00"))[0], D("820.00"))

    def test_a_refund_among_the_postes_is_taken_off(self):
        refunded = STATEMENT.replace(
            "PRELV.SEPAau10/11/2025  -  830,00  PROVISIONEAUFROIDE  10,00",
            "PRELV.SEPAau10/11/2025  -  830,00  PROVISIONEAUFROIDE  10,00\nREMBOURSEMENTDEPOTGARANTIE  -  3,00",
        ).replace("(B)  820,00", "(B)  817,00").replace("Prélevéle10/12/2025  820,00", "Prélevéle10/12/2025  817,00")
        total, postes = read_charge(refunded, D("830.00"))
        self.assertEqual(total, D("817.00"))
        self.assertIn(("REMBOURSEMENTDEPOTGARANTIE", D("-3.00")), [(poste.name, poste.amount) for poste in postes])

    def test_what_was_owed_before_is_not_charged_again(self):
        """A statement with arrears debits more than it charges: the postes
        stop at the month's own total, and the arrears were charged on the
        avis they come from."""
        with_arrears = STATEMENT.replace(
            "d'échéance(A)  0,00", "d'échéance(A)  301,99"
        ).replace("Prélevéle10/12/2025  820,00", "Pland'apurement(C)  301,99\nPrélevéle10/12/2025  1121,99")
        total, postes = read_charge(with_arrears, D("830.00"))
        self.assertEqual((total, len(postes)), (D("820.00"), 3))

    def test_a_document_that_names_nothing_gets_no_postes(self):
        """A phone bill details its calls, not its charges: one line for what
        it costs is the whole point of a charge supplier."""
        bill = """Forfait Exemple
Total de la facture HT  8.33
TVA [20.00%]  1.66
Somme a payer TTC*  9.99
Appels depuis la France  1 h 13 min  0.00"""
        self.assertEqual(read_charge(bill, D("9.99")), (D("9.99"), []))

    def test_two_postes_never_overrule_the_total_that_was_read(self):
        """Two labelled amounts making a third is a coincidence a document
        can print; three in a row is not."""
        thin = """Facture
Abonnement  10,00
Option  5,00
Sous-total  15,00
Total  20,00"""
        self.assertEqual(read_charge(thin, D("20.00"))[0], D("20.00"))
