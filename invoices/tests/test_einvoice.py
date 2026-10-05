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

import codecs
import dataclasses
import hashlib
import json
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from invoices import einvoice
from invoices.identifiers import document_identifiers
from invoices.tests import einvoice_files
from invoices.tests.einvoice_files import (
    CII_CHARGE_TOTAL_ONLY,
    CII_CREDIT_NOTE,
    CII_CREDIT_NOTE_STATED_NEGATIVE,
    CII_DOCUMENT_ALLOWANCE,
    CII_DOCUMENT_CHARGE,
    CII_IN_POUNDS,
    CII_LINE_WITHOUT_FIGURES,
    CII_MINIMUM,
    CII_MORE_DECIMALS,
    CII_NO_NUMBER,
    CII_NO_SIREN,
    CII_ROUNDING,
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
            [
                (line.raw_name, line.quantity, line.unit_cost_ht, line.total_ht, line.vat_rate, line.ean)
                for line in ubl.lines
            ],
            [
                (line.raw_name, line.quantity, line.unit_cost_ht, line.total_ht, line.vat_rate, line.ean)
                for line in cii.lines
            ],
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
        (line,) = parsed.lines
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

    def test_a_credit_note_stating_its_amounts_negative_is_signed_once(self):
        """OVH states its credit notes negative (AFR1176742, -3,92 €): signed
        again, it was filed at +3,27 €, a refund read as a purchase. The
        stated total says which way the document already points."""
        parsed = read(CII_CREDIT_NOTE_STATED_NEGATIVE)
        self.assertTrue(parsed.einvoice.is_credit_note)
        self.assertEqual(parsed.printed_total_ttc, D("-101.40"))
        self.assertEqual(parsed.vat_breakdown, [(D("0.20"), D("-84.50"), D("-16.90"))])
        (line,) = parsed.lines
        self.assertEqual(line.total_ht, D("-84.50"))
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
        (line,) = parsed.lines
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
        self.assertEqual(read(CII_DOCUMENT_CHARGE).einvoice.adjustment_reasons, ["Droits de circulation"])

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
        self.assertEqual(parsed.vat_breakdown, [(D("0"), D("20.00"), D("0.00")), (D("0"), D("30.00"), D("0.00"))])
        self.assertTrue(all(item.passed for item in parsed.checks), parsed.checks)


class PrecisionTests(SimpleTestCase):
    def test_an_amount_with_more_decimals_is_rounded_where_it_is_stored(self):
        """`InvoiceLine.total_ht` holds two decimals and `unit_cost_ht`
        four, so an invoice stating 12.345 has to land somewhere. It is
        rounded half away from zero, the rule the rest of this codebase
        converts money with."""
        parsed = read(CII_MORE_DECIMALS)
        (line,) = parsed.lines
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

    def test_an_encoding_expat_cannot_use_is_a_refusal_never_a_crash(self):
        """expat raises LookupError for an unknown encoding and a plain
        ValueError for a multi-byte one: neither escaped as an exception of
        another kind - « + Facture de vente » answered 500, Achats a generic
        sentence - and `looks_like_an_invoice` never raises."""
        for name in ("UCS2", "utf_16", "utf_32", "bogus", "UCS-4BE"):
            data = einvoice_files.CII_TWO_RATES.replace('encoding="UTF-8"', f'encoding="{name}"', 1).encode()
            with self.subTest(encoding=name):
                self.assertFalse(einvoice.looks_like_an_invoice(data))
                with self.assertRaises(einvoice.EInvoiceError):
                    einvoice.root_syntax(data)
                with self.assertRaises(einvoice.EInvoiceError):
                    einvoice.read(data)

    def test_a_wide_encoding_spelt_with_an_underscore_says_utf_8(self):
        for name in ("utf_16", "UTF_32LE", "UCS2", "ucs4"):
            data = einvoice_files.CII_TWO_RATES.replace('encoding="UTF-8"', f'encoding="{name}"', 1).encode()
            with self.subTest(encoding=name):
                self.assertRefused(data, "utf-8")

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
        self.assertRefused(b'<?xml version="1.0"?><!DOCTYPE r SYSTEM "http://exemple.invalid/r.dtd"><r/>', "doctype")


# What every fixture of einvoice_files.py read as BEFORE the sales side read
# the buyer and BT-25 / BT-72 / BG-14 (recipes/sale_einvoice.py): a digest of
# the whole ParsedInvoice but those new facts, or of the refusal - computed
# on the reader as it was. The proof that Achats does not move (`_outcome`).
OUTCOMES_BEFORE_THE_SALES_SIDE = {
    "CII_ADJUSTMENT_TOO_WIDE": "refused:1ded529e30e6a123",
    "CII_ALLOWANCE_AT_ITS_OWN_RATE": "read:9099b9b5d036230d",
    "CII_AMOUNT_TOO_WIDE": "refused:1d3e220573c92d15",
    "CII_CHARGE_AT_ITS_OWN_RATE": "read:bf66724a03a620b8",
    "CII_CHARGE_TOTAL_ONLY": "read:33603dd9d28b0c3a",
    "CII_CREDIT_NOTE": "read:0f9f5137490449d2",
    "CII_CREDIT_NOTE_STATED_NEGATIVE": "read:4111c5fbf0dd45f8",
    "CII_DATE_FAR_FUTURE": "read:5505a1c9b0152d04",
    "CII_DATE_YEAR_ONE": "read:569c29f3d2b39217",
    "CII_DISCOUNT_LINE": "read:eb5a6f8a859c8eb9",
    "CII_DOCUMENT_ALLOWANCE": "read:8cb890c51ec6047a",
    "CII_DOCUMENT_CHARGE": "read:f2aca25c035b86cf",
    "CII_ENDLESS_NAME": "read:9c0ca509e38ba7a4",
    "CII_EXPONENT_LINE": "refused:df36bcff35bbea53",
    "CII_EXPONENT_TOTAL": "refused:0a40a841397bdb3d",
    "CII_IN_POUNDS": "refused:c5bc55051ba32c1a",
    "CII_LINE_WITHOUT_FIGURES": "read:4ba1658ef316c051",
    "CII_MINIMUM": "read:408fe6aca523b7e3",
    "CII_MORE_DECIMALS": "read:1fe0c9c0978c24c7",
    "CII_NEGATIVE_QUANTITY": "read:3cc91fce5aea7bae",
    "CII_NO_NUMBER": "read:aa9aba95ad0fbd8c",
    "CII_NO_SIREN": "read:06081c845fc83e5e",
    "CII_PREPAID": "read:e625663ade588d04",
    "CII_QUANTITY_TOO_WIDE": "refused:186b3137ef6d34e6",
    "CII_RATE_TOO_WIDE": "refused:35ce4521b70fe089",
    "CII_ROUNDING": "read:4096a7de22d15d78",
    "CII_SELLER_GLN": "read:3a77f2d36a1e0157",
    "CII_TAX_IN_TWO_CURRENCIES": "read:7859d26c6352bb05",
    "CII_TOTALS_DISAGREE": "read:b1a3dcef2bb519c8",
    "CII_TWO_RATES": "read:7859d26c6352bb05",
    "CII_UNIT_PRICE_TOO_WIDE": "refused:41c7adde420e16ff",
    "CII_ZERO_AND_EXEMPT": "read:28748eb4f9e9fa91",
    "UBL_CHARGE_AT_ITS_OWN_RATE": "read:53fa63f92e8e9fa1",
    "UBL_CREDIT_NOTE": "read:d57d830ffe87dc13",
    "UBL_DOCUMENT_CHARGE": "read:288243c4ca0c755a",
    "UBL_ROUNDING": "read:cecb796b6daa1148",
    "UBL_TWO_RATES": "read:f02da61fef5f138d",
    "XML_BARE_INVOICE_ROOT": "refused:cdac48f19387ad83",
    "XML_NOT_AN_INVOICE": "refused:cdac48f19387ad83",
    "XML_UTF16_DOCTYPE": "refused:9611048a8fcff161",
    "XML_WITH_DOCTYPE": "refused:138705bae26f0528",
    "XML_WITH_ENTITY": "refused:138705bae26f0528",
}

#: The facts EInvoiceFacts had before the sales side - what `_outcome` hashes.
FACTS_BEFORE_THE_SALES_SIDE = (
    "syntax",
    "profile",
    "seller_name",
    "is_credit_note",
    "document_type_code",
    "carries_no_lines",
    "currency",
    "adjustment_reasons",
    "adjustment_vat_rate",
)

_DUE = "<ram:DuePayableAmount>229.39</ram:DuePayableAmount>"


def _outcome(data: bytes) -> str:
    """What Achats gets out of `data`: a digest of the whole ParsedInvoice -
    lines, source_text, checks, warnings, totals, VAT table and the facts it
    already had - or of the refusal's sentence."""
    try:
        parsed = einvoice.read(data)
    except einvoice.EInvoiceError as error:
        return "refused:" + hashlib.sha256(f"refused:{error}".encode()).hexdigest()[:16]
    shape = dataclasses.asdict(parsed)
    facts = shape.pop("einvoice")
    shape["facts"] = {name: facts[name] for name in FACTS_BEFORE_THE_SALES_SIDE}
    text = json.dumps(shape, default=str, sort_keys=True, ensure_ascii=False)
    return "read:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _delivered_on(fixture: str, stamp: str, code: str = "102") -> str:
    """`fixture` (CII) with BT-72 stated as `stamp`."""
    return fixture.replace(
        "<ram:ApplicableHeaderTradeDelivery/>",
        "<ram:ApplicableHeaderTradeDelivery><ram:ActualDeliverySupplyChainEvent><ram:OccurrenceDateTime>"
        f'<udt:DateTimeString format="{code}">{stamp}</udt:DateTimeString>'
        "</ram:OccurrenceDateTime></ram:ActualDeliverySupplyChainEvent></ram:ApplicableHeaderTradeDelivery>",
    )


class SalesSideFactsTests(SimpleTestCase):
    """What the sales side reads into `EInvoiceFacts` (recipes/sale_einvoice.py):
    the buyer, the totals as stated, BT-25 / BT-72 / BG-14 - every one a field
    nothing in invoices/ reads, never in `source_text`, never a new refusal."""

    def test_the_buyer_is_read_and_stays_out_of_the_text(self):
        for name, before in OUTCOMES_BEFORE_THE_SALES_SIDE.items():
            with self.subTest(fixture=name):
                self.assertEqual(_outcome(getattr(einvoice_files, name).encode("utf-8")), before)
        for fixture in (CII_TWO_RATES, UBL_TWO_RATES):
            parsed = read(fixture)
            facts = parsed.einvoice
            self.assertEqual(facts.buyer_name, "Le Comptoir Exemple")
            self.assertEqual(facts.buyer_siren, "800000002")
            self.assertEqual(facts.buyer_vat, "")
            self.assertEqual(facts.seller_siren, "900000019")
            self.assertEqual(facts.seller_vat, "FR25900000019")
            self.assertEqual(facts.taxable_total, D("194.20"))
            self.assertEqual(facts.payable, D("229.39"))
            self.assertIsNone(facts.prepaid)
            self.assertIsNone(facts.rounding)
            self.assertEqual(facts.preceding_number, "")
            self.assertIsNone(facts.delivered)
            self.assertIsNone(facts.period_start)
            self.assertNotIn("800000002", parsed.source_text)
            self.assertNotIn("Comptoir", parsed.source_text)

    def test_the_stated_totals_are_signed_and_unrounded(self):
        facts = read(CII_CREDIT_NOTE).einvoice
        self.assertLess(facts.taxable_total, 0)
        self.assertLess(facts.payable, 0)
        prepaid = read(
            CII_TWO_RATES.replace(_DUE, "<ram:TotalPrepaidAmount>100.005</ram:TotalPrepaidAmount>\n        " + _DUE)
        )
        self.assertEqual(prepaid.einvoice.prepaid, D("100.005"))
        rounding = read(CII_ROUNDING).einvoice
        self.assertEqual(rounding.rounding, D("0.01"))

    def test_a_hostile_prepaid_is_no_new_refusal(self):
        """BT-113 / BT-115 are copied with their sign (`copy_negate`, no
        arithmetic): an absurd figure reads - or is refused - exactly as
        before the sales side read it."""
        hostile = CII_TWO_RATES.replace(
            _DUE, "<ram:TotalPrepaidAmount>1E+999999999</ram:TotalPrepaidAmount>\n        " + _DUE
        )
        wide = CII_TWO_RATES.replace(
            _DUE, "<ram:TotalPrepaidAmount>99999999999999999999.99</ram:TotalPrepaidAmount>\n        " + _DUE
        )
        payable = CII_TWO_RATES.replace(_DUE, "<ram:DuePayableAmount>1E+999999999</ram:DuePayableAmount>")
        self.assertEqual(_outcome(hostile.encode()), "refused:0a40a841397bdb3d")
        self.assertEqual(_outcome(wide.encode()), "read:4050b0de8b21f13d")
        self.assertEqual(_outcome(payable.encode()), "read:7859d26c6352bb05")
        self.assertEqual(read(payable).einvoice.payable, D("1E+999999999"))
        # Signed all the same: a credit note states its BT-115 positive.
        self.assertLess(read(UBL_CREDIT_NOTE).einvoice.payable, 0)

    def test_an_unreadable_delivery_date_is_none(self):
        cii = _delivered_on(CII_TWO_RATES, "20261345")
        coded = _delivered_on(CII_TWO_RATES, "202609", code="610")
        ubl = UBL_TWO_RATES.replace(
            "  <cac:TaxTotal>",
            "  <cac:Delivery><cbc:ActualDeliveryDate>2026-13-45</cbc:ActualDeliveryDate></cac:Delivery>\n"
            "  <cac:InvoicePeriod><cbc:StartDate>hier</cbc:StartDate></cac:InvoicePeriod>\n"
            "  <cac:TaxTotal>",
            1,
        )
        for fixture in (cii, coded, ubl):
            with self.subTest(fixture=fixture[-200:]):
                parsed = read(fixture)
                self.assertIsNone(parsed.einvoice.delivered)
                self.assertIsNone(parsed.einvoice.period_start)
                self.assertEqual(parsed.invoice_date, date(2026, 9, 3))
                self.assertTrue(all(item.passed for item in parsed.checks))

    def test_the_references_are_read_in_both_syntaxes(self):
        cii = (
            _delivered_on(CII_TWO_RATES, "20260831")
            .replace(
                "      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>",
                "      <ram:BillingSpecifiedPeriod><ram:StartDateTime>"
                '<udt:DateTimeString format="102">20260815</udt:DateTimeString>'
                "</ram:StartDateTime></ram:BillingSpecifiedPeriod>\n"
                "      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>",
            )
            .replace(
                "    </ram:ApplicableHeaderTradeSettlement>",
                "      <ram:InvoiceReferencedDocument><ram:IssuerAssignedID> FA-2026-0001 </ram:IssuerAssignedID>"
                "</ram:InvoiceReferencedDocument>\n    </ram:ApplicableHeaderTradeSettlement>",
            )
        )
        ubl = UBL_TWO_RATES.replace(
            "  <cac:AccountingSupplierParty>",
            "  <cac:InvoicePeriod><cbc:StartDate>2026-08-15</cbc:StartDate></cac:InvoicePeriod>\n"
            "  <cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>FA-2026-0001</cbc:ID>"
            "</cac:InvoiceDocumentReference></cac:BillingReference>\n"
            "  <cac:AccountingSupplierParty>",
            1,
        ).replace(
            "  <cac:TaxTotal>",
            "  <cac:Delivery><cbc:ActualDeliveryDate>2026-08-31</cbc:ActualDeliveryDate></cac:Delivery>\n"
            "  <cac:TaxTotal>",
            1,
        )
        for fixture, plain in ((cii, CII_TWO_RATES), (ubl, UBL_TWO_RATES)):
            with self.subTest(fixture=plain[40:90]):
                parsed = read(fixture)
                self.assertEqual(parsed.einvoice.preceding_number, "FA-2026-0001")
                self.assertEqual(parsed.einvoice.delivered, date(2026, 8, 31))
                self.assertEqual(parsed.einvoice.period_start, date(2026, 8, 15))
                # Read, never written: Achats gets what it got without them.
                self.assertEqual(_outcome(fixture.encode()), _outcome(plain.encode()))

    def test_the_buyers_vat_number_is_read_in_both_syntaxes(self):
        cii = CII_TWO_RATES.replace(
            "        </ram:SpecifiedLegalOrganization>\n      </ram:BuyerTradeParty>",
            "        </ram:SpecifiedLegalOrganization>\n"
            '        <ram:SpecifiedTaxRegistration><ram:ID schemeID="FC">12345</ram:ID></ram:SpecifiedTaxRegistration>\n'
            "        <ram:SpecifiedTaxRegistration>"
            '<ram:ID schemeID="VA">FR22800000002</ram:ID></ram:SpecifiedTaxRegistration>\n'
            "      </ram:BuyerTradeParty>",
        )
        ubl = UBL_TWO_RATES.replace(
            "  <cac:AccountingCustomerParty>\n    <cac:Party>\n",
            "  <cac:AccountingCustomerParty>\n    <cac:Party>\n"
            "      <cac:PartyName><cbc:Name>Comptoir</cbc:Name></cac:PartyName>\n"
            "      <cac:PartyTaxScheme><cbc:CompanyID></cbc:CompanyID></cac:PartyTaxScheme>\n"
            "      <cac:PartyTaxScheme><cbc:CompanyID>FR22800000002</cbc:CompanyID></cac:PartyTaxScheme>\n",
        )
        self.assertEqual(read(cii).einvoice.buyer_vat, "FR22800000002")
        self.assertEqual(read(ubl).einvoice.buyer_vat, "FR22800000002")
        # The registered name wins over the trading name, as for the seller.
        self.assertEqual(read(ubl).einvoice.buyer_name, "Le Comptoir Exemple")
        self.assertEqual(_outcome(cii.encode()), _outcome(CII_TWO_RATES.encode()))
        self.assertEqual(_outcome(ubl.encode()), _outcome(UBL_TWO_RATES.encode()))

    def test_an_identifier_is_cut_to_forty_characters(self):
        parsed = read(CII_TWO_RATES.replace('schemeID="0002">800000002<', 'schemeID="0002">' + "8" * 90 + "<"))
        self.assertEqual(parsed.einvoice.buyer_siren, "8" * einvoice.MAX_IDENTIFIER)
        self.assertEqual(einvoice.MAX_IDENTIFIER, 40)

    def test_party_siren(self):
        self.assertEqual(einvoice.party_siren("900000019", ""), "900000019")
        self.assertEqual(einvoice.party_siren(" 900 000 019 ", ""), "900000019")
        # A SIRET: its first nine digits.
        self.assertEqual(einvoice.party_siren("90000001900001", ""), "900000019")
        # The VAT number alone, and both naming one company.
        self.assertEqual(einvoice.party_siren("", "FR25900000019"), "900000019")
        self.assertEqual(einvoice.party_siren("900000019", "FR25900000019"), "900000019")
        # A GLN, nothing, a VAT key that does not match, nine digits failing Luhn.
        self.assertEqual(einvoice.party_siren("3560070000012", ""), "")
        self.assertEqual(einvoice.party_siren("", ""), "")
        self.assertEqual(einvoice.party_siren("", "FR99900000019"), "")
        self.assertEqual(einvoice.party_siren("900000018", ""), "")
        # Two companies name neither.
        self.assertEqual(einvoice.party_siren("800000002", "FR25900000019"), "")
        self.assertEqual(einvoice.party_siren("800000002 700000003", ""), "")

    def test_root_syntax_refuses_what_root_tag_refuses(self):
        self.assertEqual(einvoice.root_syntax(CII_TWO_RATES.encode()), einvoice.CII)
        self.assertEqual(einvoice.root_syntax(bytearray(UBL_TWO_RATES.encode())), einvoice.UBL)
        self.assertIsNone(einvoice.root_syntax(XML_NOT_AN_INVOICE.encode()))
        self.assertIsNone(einvoice.root_syntax(XML_BARE_INVOICE_ROOT.encode()))
        refused = (
            "pas une facture",
            None,
            b"",
            b"%PDF-1.4",
            b"<?xml version='1.0'?><r",
            XML_WITH_DOCTYPE.encode(),
            XML_WITH_ENTITY.encode(),
            CII_TWO_RATES.replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16"),
            CII_TWO_RATES.replace('encoding="UTF-8"', 'encoding="UTF-32"').encode(),
            b"<!-- " + b"x" * einvoice.MAX_XML_BYTES + b" -->" + CII_TWO_RATES.encode(),
        )
        for data in refused:
            with self.subTest(data=repr(data)[:40]):
                with self.assertRaises(einvoice.EInvoiceError) as caught:
                    einvoice.root_syntax(data)
                with self.assertRaises(einvoice.EInvoiceError) as root_tag:
                    einvoice._root_tag(data)
                self.assertEqual(str(caught.exception), str(root_tag.exception))

    def test_looks_like_an_invoice_answers_as_before(self):
        for name in OUTCOMES_BEFORE_THE_SALES_SIDE:
            with self.subTest(fixture=name):
                self.assertIs(
                    einvoice.looks_like_an_invoice(getattr(einvoice_files, name).encode("utf-8")),
                    name.startswith(("CII_", "UBL_")),
                )
        answers = (
            (b"", False),
            (b"<", False),
            (b"not xml", False),
            ("str", False),
            (None, False),
            (b"<r/>", False),
            (b"<Invoice/>", False),
            (CII_TWO_RATES.encode()[:400], True),
            (codecs.BOM_UTF8 + CII_TWO_RATES.encode(), True),
            (CII_TWO_RATES.encode("utf-16"), False),
            (bytearray(UBL_TWO_RATES.encode()), True),
        )
        for data, answer in answers:
            with self.subTest(data=repr(data)[:40]):
                self.assertIs(einvoice.looks_like_an_invoice(data), answer)
