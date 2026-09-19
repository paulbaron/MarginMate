"""What a document prints that names its seller whatever its layout: a SIREN
(on its own, in a SIRET or in a VAT number), a phone number, a web site.

The numbers are invented; the SIRENs and VAT keys are made valid, as printed
ones are.
"""

from django.test import SimpleTestCase

from invoices.identifiers import (
    describe,
    document_identifiers,
    is_siren,
    may_print,
    vat_key,
)

# 900000019 passes the Luhn check; its French VAT key is 25.
SIREN = "900000019"


class PiecesTests(SimpleTestCase):
    def test_a_siren_carries_a_luhn_check(self):
        self.assertTrue(is_siren(SIREN))
        self.assertFalse(is_siren("900000018"))
        self.assertFalse(is_siren("90000001"))

    def test_a_french_vat_number_carries_its_sirens_key(self):
        self.assertEqual(vat_key(SIREN), "25")


class IdentifiersTests(SimpleTestCase):
    def test_a_vat_number_gives_its_siren(self):
        for line in ("N° TVA : FR25900000019", "TVA intracom FR 25 900 000 019", "VAT No: FR25 900000019"):
            with self.subTest(line=line):
                self.assertEqual(document_identifiers(line), {f"siren:{SIREN}"})

    def test_a_vat_number_whose_key_does_not_match_is_a_misreading(self):
        self.assertEqual(document_identifiers("N° TVA : FR26900000019"), set())

    def test_a_siret_gives_its_siren(self):
        # The SIREN and a Luhn-valid establishment number.
        for line in ("SIRET: 900 000 019 10000", "Siret 90000001910000"):
            with self.subTest(line=line):
                self.assertEqual(document_identifiers(line), {f"siren:{SIREN}"})

    def test_a_siren_is_read_where_it_is_named(self):
        self.assertEqual(document_identifiers(f"RCS Paris B {SIREN[:3]} {SIREN[3:6]} {SIREN[6:]}"), {f"siren:{SIREN}"})
        self.assertEqual(document_identifiers(f"N° SIREN:{SIREN}"), {f"siren:{SIREN}"})
        # Nine digits anywhere else are an amount, a code, a ticket.
        self.assertEqual(document_identifiers(f"CODE {SIREN} 12,50"), set())

    def test_a_phone_number(self):
        for line in ("Tel: 01 23 45 67 89", "TEL.01.23.45.67.89", "Tél : 0123456789", "+33 1 23 45 67 89"):
            with self.subTest(line=line):
                self.assertEqual(document_identifiers(line), {"tel:0123456789"})

    def test_prices_are_not_a_phone_number(self):
        self.assertEqual(document_identifiers("10.49 31.47"), set())
        self.assertEqual(document_identifiers("01.23 45.67 89.00"), set())

    def test_a_web_site(self):
        for line in ("www.brico-exemple.fr", "https://Brico-Exemple.fr/magasins", "WWW.BRICO-EXEMPLE.FR"):
            with self.subTest(line=line):
                self.assertEqual(document_identifiers(line), {"web:brico-exemple.fr"})

    def test_an_email_address_is_not_a_web_site(self):
        """A customer's address is printed on invoices too - and a mail
        provider's domain is everybody's."""
        self.assertEqual(document_identifiers("contact : jean.dupont@gmail.com"), set())

    def test_a_whole_document(self):
        text = "\n".join([
            "BRICO EXEMPLE",
            "12 rue des Planches 75011 PARIS",
            "Tel: 01 23 45 67 89",
            "TOTAL 12,50",
            f"SIRET {SIREN}10000 - TVA FR25{SIREN}",
            "www.brico-exemple.fr",
        ])
        self.assertEqual(
            document_identifiers(text), {f"siren:{SIREN}", "tel:0123456789", "web:brico-exemple.fr"}
        )


class QuickLookTests(SimpleTestCase):
    def test_a_text_that_may_print_them(self):
        found = {"tel:0123456789", f"siren:{SIREN}", "web:brico-exemple.fr"}
        for text in ("+33 1 23 45 67 89", "SIRET 900 000 019 10000", "WWW.BRICO-EXEMPLE.FR"):
            with self.subTest(text=text):
                self.assertTrue(may_print(text, found))
                self.assertTrue(document_identifiers(text) & found)
        self.assertFalse(may_print("TOTAL 12,50\nTEL 01 99 99 99 99", found))

    def test_a_reading_kept_is_not_changed_through_what_it_returned(self):
        """A text's figures are kept once read (a supplier's page reads every
        document): the set handed out is the caller's own."""
        text = "TEL 01 23 45 67 89\nwww.brico-exemple.fr"
        first = document_identifiers(text)
        first.add("siren:000000000")
        first.discard("tel:0123456789")
        self.assertEqual(document_identifiers(text), {"tel:0123456789", "web:brico-exemple.fr"})

    def test_as_the_operator_reads_them(self):
        self.assertEqual(
            [describe(f"siren:{SIREN}"), describe("tel:0123456789"), describe("web:brico-exemple.fr")],
            ["n° SIREN 900 000 019", "téléphone 01 23 45 67 89", "site brico-exemple.fr"],
        )
