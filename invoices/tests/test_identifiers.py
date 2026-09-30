"""What a document prints that names its seller whatever its layout: a SIREN
(on its own, in a SIRET or in a VAT number), a phone number, a web site.

The numbers are invented; the SIRENs and VAT keys are made valid, as printed
ones are.
"""

from django.test import SimpleTestCase

from invoices.identifiers import (
    TypedIdentifierError,
    describe,
    document_identifiers,
    is_siren,
    may_print,
    read_typed,
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
        text = "\n".join(
            [
                "BRICO EXEMPLE",
                "12 rue des Planches 75011 PARIS",
                "Tel: 01 23 45 67 89",
                "TOTAL 12,50",
                f"SIRET {SIREN}10000 - TVA FR25{SIREN}",
                "www.brico-exemple.fr",
            ]
        )
        self.assertEqual(document_identifiers(text), {f"siren:{SIREN}", "tel:0123456789", "web:brico-exemple.fr"})


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


class TypedTests(SimpleTestCase):
    """An identifier a person types on a supplier's page.

    The rule that matters: **typed and printed read the same**. Read by two
    different functions, a SIREN typed by hand and the same SIREN found on a
    document would be two keys, and the supplier would be recognised by one
    of them and not the other with nothing on screen able to say why.
    """

    def test_typed_and_printed_come_out_as_one_key(self):
        for typed, printed in (
            (SIREN, f"SIREN {SIREN}"),
            ("900 000 019", f"RCS Paris {SIREN}"),
            (f"FR {vat_key(SIREN)} {SIREN}", f"TVA FR{vat_key(SIREN)}{SIREN}"),
            ("900 000 019 10000", "SIRET 900 000 019 10000"),
            ("01 23 45 67 89", "Tel 01.23.45.67.89"),
            ("+33 1 23 45 67 89", "TEL +33 1 23 45 67 89"),
            ("brico-exemple.fr", "www.brico-exemple.fr"),
            ("https://brico-exemple.fr", "WWW.BRICO-EXEMPLE.FR"),
        ):
            with self.subTest(typed=typed):
                self.assertEqual({read_typed(typed)}, document_identifiers(printed) & {read_typed(typed)})
                self.assertIn(read_typed(typed), document_identifiers(printed))

    def test_what_each_shape_comes_out_as(self):
        self.assertEqual(read_typed(f"  {SIREN}  "), f"siren:{SIREN}")
        self.assertEqual(read_typed("900 000 019 10000"), f"siren:{SIREN}")
        self.assertEqual(read_typed(f"FR{vat_key(SIREN)}{SIREN}"), f"siren:{SIREN}")
        self.assertEqual(read_typed("01 23 45 67 89"), "tel:0123456789")
        self.assertEqual(read_typed("0123456789"), "tel:0123456789")
        self.assertEqual(read_typed("WWW.Brico-Exemple.FR"), "web:brico-exemple.fr")

    def test_a_siren_whose_key_is_wrong_is_refused_by_its_key(self):
        with self.assertRaises(TypedIdentifierError) as refused:
            read_typed("900000018")
        self.assertIn("clé de contrôle", str(refused.exception))

    def test_a_number_of_the_wrong_length_says_how_many_digits_it_has(self):
        with self.assertRaises(TypedIdentifierError) as refused:
            read_typed("90000001")
        self.assertIn("8 chiffres", str(refused.exception))

    def test_a_phone_that_does_not_start_with_zero_says_so(self):
        with self.assertRaises(TypedIdentifierError) as refused:
            read_typed("11 23 45 67 89")
        self.assertIn("commence par 0", str(refused.exception))

    def test_something_that_is_no_identifier_at_all(self):
        for typed in ("", "   ", "Exemple", "12,50"):
            with self.subTest(typed=typed):
                with self.assertRaises(TypedIdentifierError):
                    read_typed(typed)

    def test_a_word_with_a_dot_is_read_as_a_site_and_said_so(self):
        with self.assertRaises(TypedIdentifierError) as refused:
            read_typed("exemple.quelquechose")
        self.assertIn("adresse", str(refused.exception))

    def test_two_identifiers_at_once_are_refused_rather_than_half_taken(self):
        with self.assertRaises(TypedIdentifierError) as refused:
            read_typed("01 23 45 67 89 brico-exemple.fr")
        self.assertIn("un seul", str(refused.exception))

    def test_the_refusal_is_a_value_error(self):
        # receipts and the views already report a ValueError as a message.
        self.assertTrue(issubclass(TypedIdentifierError, ValueError))
