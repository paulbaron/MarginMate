"""Reading an EN 16931 invoice - the whole point of which is that nothing
is guessed.

Everywhere else in this application a supplier's figures are inferred: OCR
reads a photograph, or a regex finds an amount in a column and the
`parse_checks` apparatus exists to catch the readings that are wrong. A
Factur-X, UBL or CII invoice states the number, the date, the seller's SIREN,
every line, the VAT breakdown and the totals **as data**. So these tests
assert equality, not tolerance: a figure read out of the XML that is not
exactly what the XML says is a bug, not a near miss.

What they still have to pin is everything around that exactness - the
document's own arithmetic when it does not hold, the profiles that carry no
lines at all, the sign of a credit note, and the ways a file arriving from
outside is refused.

Fixtures in `einvoice_files.py`: structurally faithful, data invented.
"""

from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from invoices import einvoice
from invoices.identifiers import document_identifiers
from invoices.tests.einvoice_files import (
    CII_CHARGE_TOTAL_ONLY,
    CII_CREDIT_NOTE,
    CII_DOCUMENT_ALLOWANCE,
    CII_DOCUMENT_CHARGE,
    CII_IN_POUNDS,
    CII_LINE_WITHOUT_FIGURES,
    CII_MINIMUM,
    CII_MORE_DECIMALS,
    CII_NO_NUMBER,
    CII_NO_SIREN,
    CII_SELLER_GLN,
    CII_TAX_IN_TWO_CURRENCIES,
    CII_TOTALS_DISAGREE,
    CII_TWO_RATES,
    CII_ZERO_AND_EXEMPT,
    UBL_CREDIT_NOTE,
    UBL_DOCUMENT_CHARGE,
    UBL_TWO_RATES,
    XML_BARE_INVOICE_ROOT,
    XML_NOT_AN_INVOICE,
    XML_WITH_DOCTYPE,
    XML_WITH_ENTITY,
)

D = Decimal


def read(fixture: str):
    return einvoice.read(fixture.encode("utf-8"))


def check(parsed, label):
    found = [item for item in parsed.checks if item.label == label]
    return found[0] if found else None


class CiiInvoiceTests(SimpleTestCase):
    """A commercial invoice, two lines at two rates."""

    def setUp(self):
        self.parsed = read(CII_TWO_RATES)

    def test_the_header_is_read_as_stated(self):
        self.assertEqual(self.parsed.invoice_number, "FA-2026-0042")
        # BT-2 in format 102 is YYYYMMDD, never a locale-dependent string.
        self.assertEqual(self.parsed.invoice_date, date(2026, 9, 3))
        self.assertEqual(self.parsed.einvoice.seller_name, "Brasserie du Canal")
        self.assertEqual(self.parsed.einvoice.syntax, einvoice.CII)
        self.assertEqual(self.parsed.einvoice.currency, "EUR")

    def test_the_facts_are_the_flag_that_this_was_read_and_not_guessed(self):
        """Everything downstream can tell an exactly-read document from an
        inferred one by this attribute alone."""
        self.assertIsNotNone(self.parsed.einvoice)

    def test_the_lines_are_the_documents_own(self):
        first, second = self.parsed.lines
        self.assertEqual(first.raw_name, "BIERE BLONDE FUT 30L")
        self.assertEqual(first.quantity, 2)
        self.assertIsInstance(first.quantity, int)
        self.assertEqual(first.unit_cost_ht, D("84.50"))
        self.assertEqual(first.total_ht, D("169.00"))
        self.assertEqual(first.vat_rate, D("0.20"))
        self.assertEqual(first.ean, "3560070000012")
        self.assertEqual(second.raw_name, "SIROP CITRON 1L")
        self.assertEqual(second.quantity, 6)
        self.assertEqual(second.total_ht, D("25.20"))
        self.assertEqual(second.vat_rate, D("0.055"))
        self.assertEqual(second.ean, "")

    def test_a_rate_is_stored_as_a_fraction_not_a_percentage(self):
        """The XML says 20.00; this application stores 0.2000. Read across,
        every cost downstream would be a hundredfold wrong."""
        self.assertEqual([line.vat_rate for line in self.parsed.lines], [D("0.2000"), D("0.0550")])

    def test_the_vat_table_is_the_documents_own(self):
        self.assertEqual(
            self.parsed.vat_breakdown,
            [(D("0.20"), D("169.00"), D("33.80")), (D("0.055"), D("25.20"), D("1.39"))],
        )

    def test_the_totals_are_the_documents_own(self):
        self.assertEqual(self.parsed.printed_total_ttc, D("229.39"))
        self.assertEqual(self.parsed.reconciliation_adjustment, D("0"))

    def test_nothing_says_this_was_guessed(self):
        """`from_ocr` makes product names match tolerantly and `confidence`
        is a recogniser's doubt. Neither applies to a figure stated as data,
        and either one set here would loosen a matcher that must stay
        strict."""
        self.assertFalse(self.parsed.from_ocr)
        self.assertIsNone(self.parsed.confidence)

    def test_every_check_passes_on_an_invoice_that_adds_up(self):
        self.assertTrue(all(item.passed for item in self.parsed.checks), self.parsed.checks)
        self.assertEqual(self.parsed.warnings, [])

    def test_the_seller_is_named_through_the_identifiers_module(self):
        """An EN 16931 invoice states the seller's legal id outright, which
        is exactly what `invoices/identifiers.py` recognises - so the reading
        is written into `source_text` for it to find, rather than matched a
        second time here.

        The BUYER's company number is deliberately left out: it is the bar's
        own, printed on every supplier's invoice, and the one thing that must
        never name a supplier.
        """
        found = document_identifiers(self.parsed.source_text)
        self.assertIn("siren:900000019", found)
        self.assertNotIn("siren:800000002", found)

    def test_the_profile_urn_is_not_written_where_it_would_name_a_supplier(self):
        """BT-24 is "urn:cen.eu:en16931:2017", and `identifiers.
        BARE_DOMAIN_RE` reads « cen.eu » out of it as a web site - a figure
        EVERY electronic invoice carries, which would be offered as an
        identifier on whichever supplier filed enough of them first. It
        belongs on `EInvoiceFacts.profile`, where nothing matches it."""
        self.assertEqual(self.parsed.einvoice.profile, "urn:cen.eu:en16931:2017")
        self.assertNotIn("cen.eu", self.parsed.source_text)
        self.assertEqual(document_identifiers(self.parsed.source_text), {"siren:900000019"})
        self.assertNotIn("factur-x.eu", read(CII_MINIMUM).source_text)

    def test_amounts_are_decimals_never_floats(self):
        for line in self.parsed.lines:
            for value in (line.unit_cost_ht, line.total_ht, line.vat_rate):
                self.assertIsInstance(value, Decimal)
        for rate, base, tax in self.parsed.vat_breakdown:
            self.assertIsInstance(rate, Decimal)
            self.assertIsInstance(base, Decimal)
            self.assertIsInstance(tax, Decimal)


class UblInvoiceTests(SimpleTestCase):
    """The same invoice in the other mandated syntax."""

    def test_ubl_and_cii_read_as_the_same_invoice(self):
        """Two syntaxes, one document. Anything that differs is a reader
        that learnt one dialect and guessed at the other."""
        cii, ubl = read(CII_TWO_RATES), read(UBL_TWO_RATES)
        self.assertEqual(ubl.einvoice.syntax, einvoice.UBL)
        self.assertEqual(ubl.invoice_number, cii.invoice_number)
        self.assertEqual(ubl.invoice_date, cii.invoice_date)
        self.assertEqual(ubl.einvoice.seller_name, cii.einvoice.seller_name)
        self.assertEqual(ubl.printed_total_ttc, cii.printed_total_ttc)
        self.assertEqual(ubl.vat_breakdown, cii.vat_breakdown)
        self.assertEqual(
            [(line.raw_name, line.quantity, line.unit_cost_ht, line.total_ht, line.vat_rate, line.ean)
             for line in ubl.lines],
            [(line.raw_name, line.quantity, line.unit_cost_ht, line.total_ht, line.vat_rate, line.ean)
             for line in cii.lines],
        )

    def test_the_syntax_is_told_by_the_root_element_not_the_file_name(self):
        self.assertTrue(einvoice.looks_like_an_invoice(CII_TWO_RATES.encode()))
        self.assertTrue(einvoice.looks_like_an_invoice(UBL_TWO_RATES.encode()))
        self.assertTrue(einvoice.looks_like_an_invoice(UBL_CREDIT_NOTE.encode()))
        self.assertFalse(einvoice.looks_like_an_invoice(XML_NOT_AN_INVOICE.encode()))
        self.assertFalse(einvoice.looks_like_an_invoice(XML_BARE_INVOICE_ROOT.encode()))
        self.assertFalse(einvoice.looks_like_an_invoice(b"%PDF-1.7 not xml at all"))

    def test_looks_like_an_invoice_never_raises(self):
        """It is asked of every attachment of a PDF, a logo included."""
        for data in (b"", b"\x00\x01\x02", b"<", b"<?xml", XML_WITH_DOCTYPE.encode()):
            self.assertIsInstance(einvoice.looks_like_an_invoice(data), bool)

    def test_ubl_finds_the_seller_siren_through_the_identifiers_module(self):
        self.assertIn("siren:900000019", document_identifiers(read(UBL_TWO_RATES).source_text))


class CreditNoteTests(SimpleTestCase):
    """Type 381 - and every amount in the file is stated POSITIVE.

    This codebase writes money going back as a negative count AND a negative
    amount (CLAUDE.md). Read at face value, a credit note would double a
    purchase instead of cancelling it, and the stock ledger would gain the
    keg that was given back.
    """

    def test_a_cii_credit_note_comes_out_negative(self):
        parsed = read(CII_CREDIT_NOTE)
        self.assertTrue(parsed.einvoice.is_credit_note)
        line, = parsed.lines
        self.assertEqual(line.quantity, -1)
        self.assertEqual(line.total_ht, D("-84.50"))
        # The unit price stays positive: it is what one keg costs, and a
        # negative one beside a negative count would price the return twice.
        self.assertEqual(line.unit_cost_ht, D("84.50"))
        self.assertEqual(parsed.printed_total_ttc, D("-101.40"))
        self.assertEqual(parsed.vat_breakdown, [(D("0.20"), D("-84.50"), D("-16.90"))])

    def test_a_ubl_credit_note_comes_out_the_same_way(self):
        """UBL says it in the root element and in cbc:CreditedQuantity,
        where CII says it in ram:TypeCode."""
        ubl, cii = read(UBL_CREDIT_NOTE), read(CII_CREDIT_NOTE)
        self.assertTrue(ubl.einvoice.is_credit_note)
        self.assertEqual(ubl.printed_total_ttc, cii.printed_total_ttc)
        self.assertEqual(ubl.vat_breakdown, cii.vat_breakdown)
        self.assertEqual(
            [(line.quantity, line.total_ht, line.vat_rate) for line in ubl.lines],
            [(line.quantity, line.total_ht, line.vat_rate) for line in cii.lines],
        )

    def test_its_own_arithmetic_still_has_to_hold(self):
        parsed = read(CII_CREDIT_NOTE)
        self.assertTrue(all(item.passed for item in parsed.checks), parsed.checks)

    def test_a_plain_invoice_is_not_one(self):
        self.assertFalse(read(CII_TWO_RATES).einvoice.is_credit_note)


class NoLinesProfileTests(SimpleTestCase):
    """MINIMUM and BASIC WL carry the totals and the VAT breakdown only.

    A document like that is not a failed reading - it is a valid invoice
    whose lines the sender never put in the file. It has to go down the
    total-only path, and the page has to SAY so, or it reads exactly like a
    document whose lines could not be read.
    """

    def setUp(self):
        self.parsed = read(CII_MINIMUM)

    def test_no_line_is_invented(self):
        self.assertEqual(self.parsed.lines, [])

    def test_the_flag_says_the_document_carried_none(self):
        self.assertTrue(self.parsed.einvoice.carries_no_lines)
        self.assertFalse(read(CII_TWO_RATES).einvoice.carries_no_lines)

    def test_the_profile_it_came_from_is_kept(self):
        self.assertEqual(self.parsed.einvoice.profile, "urn:factur-x.eu:1p0:minimum")

    def test_the_totals_and_the_table_are_still_exact(self):
        self.assertEqual(self.parsed.printed_total_ttc, D("144.00"))
        self.assertEqual(self.parsed.vat_breakdown, [(D("0.20"), D("120.00"), D("24.00"))])

    def test_a_check_says_it_and_passes(self):
        """Passing, not failing. The reading is complete and exact - a
        failed check here would park every MINIMUM invoice in « À vérifier »
        for ever with nothing anybody could do about it, which is precisely
        the noise the review screen exists to avoid."""
        said = check(self.parsed, einvoice.NO_LINES_CHECK)
        self.assertIsNotNone(said)
        self.assertTrue(said.passed)
        self.assertIn("profil", said.detail.lower())

    def test_it_is_not_a_warning_either(self):
        self.assertEqual(self.parsed.warnings, [])

    def test_an_invoice_that_does_carry_lines_says_nothing_about_it(self):
        self.assertIsNone(check(read(CII_TWO_RATES), einvoice.NO_LINES_CHECK))

    def test_a_line_that_states_no_figures_is_not_the_same_thing_at_all(self):
        """The document DID carry two lines and one of them has neither an
        amount nor a quantity. That is a line lost, and it must never read
        like a profile that carries none - one is exact, the other is a
        hole."""
        parsed = read(CII_LINE_WITHOUT_FIGURES)
        self.assertFalse(parsed.einvoice.carries_no_lines)
        self.assertIsNone(check(parsed, einvoice.NO_LINES_CHECK))
        said = check(parsed, einvoice.LINES_READ_CHECK)
        self.assertIsNotNone(said)
        self.assertFalse(said.passed)
        self.assertIn("1 ligne(s) sur 2", said.detail)
        self.assertTrue(parsed.warnings)


class DocumentAllowancesAndChargesTests(SimpleTestCase):
    """BG-20 and BG-21: where duty, eco-participation and discounts live.

    This application already has a place for money the lines do not carry -
    `Invoice.reconciliation_adjustment` - and CLAUDE.md is emphatic that it
    is part of the VAT base. Dropped instead, an invoice would be filed at
    less than it charges and the bank match would never find it.
    """

    def test_a_document_level_charge_becomes_the_adjustment(self):
        parsed = read(CII_DOCUMENT_CHARGE)
        self.assertEqual(parsed.reconciliation_adjustment, D("12.00"))
        line, = parsed.lines
        self.assertEqual(line.total_ht, D("100.00"))
        self.assertEqual(parsed.printed_total_ttc, D("134.40"))
        self.assertTrue(all(item.passed for item in parsed.checks), parsed.checks)

    def test_a_document_level_allowance_is_the_same_field_negative(self):
        parsed = read(CII_DOCUMENT_ALLOWANCE)
        self.assertEqual(parsed.reconciliation_adjustment, D("-5.00"))
        self.assertEqual(parsed.printed_total_ttc, D("114.00"))
        self.assertTrue(all(item.passed for item in parsed.checks), parsed.checks)

    def test_what_the_charge_was_for_is_kept(self):
        """« Droits de circulation » is what the row says on the review
        screen; an adjustment with no reason is a figure nobody can check."""
        self.assertEqual(
            read(CII_DOCUMENT_CHARGE).einvoice.adjustment_reasons, ["Droits de circulation"]
        )

    def test_ubl_states_the_same_charge_in_its_own_elements(self):
        """cbc:ChargeIndicator on a cac:AllowanceCharge of the root, where
        CII nests an udt:Indicator inside ram:SpecifiedTradeAllowanceCharge.
        One document, and the two readings have to agree."""
        ubl, cii = read(UBL_DOCUMENT_CHARGE), read(CII_DOCUMENT_CHARGE)
        self.assertEqual(ubl.reconciliation_adjustment, cii.reconciliation_adjustment)
        self.assertEqual(ubl.einvoice.adjustment_reasons, cii.einvoice.adjustment_reasons)
        self.assertEqual(ubl.printed_total_ttc, cii.printed_total_ttc)
        self.assertTrue(all(item.passed for item in ubl.checks), ubl.checks)

    def test_a_charge_stated_only_in_the_totals_is_still_the_adjustment(self):
        """BT-108 without the BG-21 blocks behind it. Read only from the
        blocks, the adjustment would be 0 and the invoice filed 12,00 € HT
        short of what it charges - with its own total check still passing,
        which is the shape of silently wrong money."""
        parsed = read(CII_CHARGE_TOTAL_ONLY)
        self.assertEqual(parsed.reconciliation_adjustment, D("12.00"))
        self.assertEqual(parsed.einvoice.adjustment_reasons, [])
        self.assertTrue(all(item.passed for item in parsed.checks), parsed.checks)

    def test_the_lines_alone_do_not_have_to_reach_the_vat_base(self):
        """The line sum is 100,00 € and the taxable base 112,00 €. That is
        not a discrepancy - the difference is the charge - and reported as
        one it would flag every invoice carrying duty."""
        said = check(read(CII_DOCUMENT_CHARGE), einvoice.LINES_CHECK)
        self.assertIsNotNone(said)
        self.assertTrue(said.passed, said.detail)


class RatesTests(SimpleTestCase):
    def test_a_rate_of_zero_and_an_exempt_line_are_not_the_same_thing(self):
        """Category Z states a rate of 0.00; category E states no rate at
        all. Both are stored as 0, and both have to survive the reading
        rather than fall back on a default - 5,5 % is the food rate this
        codebase guesses with, and guessed here it would invent tax on a
        postage stamp."""
        parsed = read(CII_ZERO_AND_EXEMPT)
        self.assertEqual([line.vat_rate for line in parsed.lines], [D("0"), D("0")])
        self.assertEqual(parsed.printed_total_ttc, D("50.00"))
        self.assertEqual(
            parsed.vat_breakdown, [(D("0"), D("20.00"), D("0.00")), (D("0"), D("30.00"), D("0.00"))]
        )
        self.assertTrue(all(item.passed for item in parsed.checks), parsed.checks)


class PrecisionTests(SimpleTestCase):
    def test_an_amount_with_more_decimals_is_rounded_where_it_is_stored(self):
        """`InvoiceLine.total_ht` holds two decimals and `unit_cost_ht`
        four, so an invoice stating 12.345 has to land somewhere. It is
        rounded half away from zero, the rule the rest of this codebase
        converts money with."""
        parsed = read(CII_MORE_DECIMALS)
        line, = parsed.lines
        self.assertEqual(str(line.total_ht), "12.35")
        self.assertEqual(str(line.unit_cost_ht), "4.1150")

    def test_the_checks_are_run_on_the_documents_own_figures(self):
        """Rounded first, 12.35 against a taxable base of 12.345 would fail
        a check about the supplier's arithmetic because of this
        application's storage. The check has to see what the file says."""
        parsed = read(CII_MORE_DECIMALS)
        self.assertTrue(all(item.passed for item in parsed.checks), parsed.checks)

    def test_the_total_is_rounded_to_the_cent(self):
        """`Invoice.printed_total_ttc` holds two decimals, and it is what
        the bank match compares against."""
        self.assertEqual(str(read(CII_MORE_DECIMALS).printed_total_ttc), "14.81")


class WhatWasNotReadTests(SimpleTestCase):
    def test_arithmetic_that_does_not_hold_is_reported_with_both_figures(self):
        """The supplier's error, not the reader's. Repairing it silently is
        how this codebase has shipped wrong money before."""
        parsed = read(CII_TOTALS_DISAGREE)
        said = check(parsed, einvoice.TOTAL_CHECK)
        self.assertIsNotNone(said)
        self.assertFalse(said.passed)
        self.assertIn("229.39", said.detail)
        self.assertIn("239.39", said.detail)
        # And the total is still the one the document states: nothing is
        # recomputed behind the supplier's back.
        self.assertEqual(parsed.printed_total_ttc, D("239.39"))

    def test_a_failed_check_is_also_a_warning(self):
        """`Invoice.parse_checks` is what makes a document a receipt
        (`Invoice.is_receipt`), so an importer may well not want to store
        these on a digital invoice. `warnings` is the other channel - it
        lands in `error_message` and holds the document in « À vérifier » -
        and carrying both leaves that decision where it belongs."""
        parsed = read(CII_TOTALS_DISAGREE)
        self.assertTrue(parsed.warnings)
        self.assertTrue(any("229.39" in warning for warning in parsed.warnings))

    def test_the_vat_total_is_taken_in_the_invoices_own_currency(self):
        """A document may state its VAT twice - BT-110 in the invoice's
        currency and BT-111 in the one the seller accounts for tax in - as
        two identical elements in either order. Taking the first put 32,60
        CHF beside French figures and failed the total check on an invoice
        that adds up perfectly."""
        parsed = read(CII_TAX_IN_TWO_CURRENCIES)
        self.assertEqual(parsed.printed_total_ttc, D("229.39"))
        self.assertTrue(all(item.passed for item in parsed.checks), parsed.checks)

    def test_a_seller_id_that_is_not_a_siren_is_not_called_one(self):
        """BT-30 can be a GLN or a foreign register number. « SIREN » in
        front of one is a lie on the review screen, and it invites the
        identifier module to match it as a French company."""
        parsed = read(CII_SELLER_GLN)
        self.assertIn("4012345000009", parsed.source_text)
        self.assertNotIn("SIREN", parsed.source_text)
        self.assertEqual(document_identifiers(parsed.source_text), set())

    def test_a_missing_seller_siren_is_not_an_error(self):
        """Legal for a small or a foreign seller. The invoice is read; there
        is simply nothing in it that can name a supplier."""
        parsed = read(CII_NO_SIREN)
        self.assertEqual(parsed.einvoice.seller_name, "Brasserie du Canal")
        self.assertEqual(document_identifiers(parsed.source_text), set())
        self.assertEqual(parsed.printed_total_ttc, D("229.39"))

    def test_a_missing_number_is_said_rather_than_invented(self):
        """A number made up from the date and the total is what the ticket
        reader falls back on, and `refresh_document_numbers` exists to undo
        those. An e-invoice with no BT-1 gets none at all and says so."""
        parsed = read(CII_NO_NUMBER)
        self.assertEqual(parsed.invoice_number, "")
        said = check(parsed, einvoice.NUMBER_CHECK)
        self.assertIsNotNone(said)
        self.assertFalse(said.passed)
        self.assertTrue(parsed.warnings)


class RefusalTests(SimpleTestCase):
    """The XML comes from outside - a platform, a mailbox, a portal.

    Every refusal is a message in French on the import, never a traceback
    and never a hang. `EInvoiceError` is a ValueError so the batch importer's
    existing "broken file" branch reports it per file instead of failing the
    whole folder.
    """

    def assertRefused(self, data, *words):
        with self.assertRaises(einvoice.EInvoiceError) as caught:
            einvoice.read(data)
        message = str(caught.exception)
        for word in words:
            self.assertIn(word, message.lower())
        return message

    def test_it_is_a_value_error(self):
        self.assertTrue(issubclass(einvoice.EInvoiceError, ValueError))

    def test_a_doctype_is_refused_before_anything_parses_it(self):
        """ElementTree expands internal entities, so a DOCTYPE is the
        billion-laughs door. An EN 16931 instance never carries one."""
        self.assertRefused(XML_WITH_DOCTYPE.encode(), "doctype")

    def test_an_entity_declaration_is_refused(self):
        self.assertRefused(XML_WITH_ENTITY.encode(), "doctype")

    def test_a_currency_other_than_the_euro_is_refused_not_converted(self):
        message = self.assertRefused(CII_IN_POUNDS.encode(), "gbp")
        self.assertIn("euro", message.lower())

    def test_well_formed_xml_that_is_not_an_invoice_is_refused(self):
        self.assertRefused(XML_NOT_AN_INVOICE.encode(), "facture")

    def test_a_root_element_with_the_right_name_and_no_namespace_is_not_ubl(self):
        self.assertRefused(XML_BARE_INVOICE_ROOT.encode(), "facture")

    def test_bytes_that_are_not_xml_are_refused(self):
        self.assertRefused(b"\x89PNG\r\n\x1a\n this is a logo", "xml")

    def test_xml_that_is_not_well_formed_is_refused(self):
        self.assertRefused(b"<?xml version='1.0'?><rsm:CrossIndustryInvoice>", "xml")

    def test_an_empty_file_is_refused(self):
        self.assertRefused(b"", "xml")

    def test_an_xml_over_the_cap_is_refused_rather_than_read(self):
        padding = b"<!-- " + b"x" * (einvoice.MAX_XML_BYTES + 1) + b" -->"
        self.assertRefused(padding + CII_TWO_RATES.encode(), "trop volumineux")

    def test_nothing_reaches_the_network(self):
        """An XML naming an external DTD must not be fetched - there is no
        resolver here at all, since the DOCTYPE carrying it is refused
        first. Pinned because a parser swapped in later could bring one."""
        self.assertRefused(
            b'<?xml version="1.0"?><!DOCTYPE r SYSTEM "http://exemple.invalid/r.dtd"><r/>', "doctype"
        )
