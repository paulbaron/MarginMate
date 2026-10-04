"""Reading an OFX statement (bank/ofx.py), SGML 1.x and XML 2.x alike, through
the one pipeline every statement goes through (`statements.parse_statement`,
`statements.finish`): what each operation is is the recognition rules', and
its fingerprint the CSV's.

Each test holds one promise - a field read where the standard puts it, a
file refused whole and in French where it cannot be read whole - and one
test runs mutated files through the reader to hold the last promise for
every file: whatever the bytes, a refusal is one of the reader's own French
sentences, never a library's English nor a 500.

Fixtures: bank/tests/ofx_files.py (the standard's structure, invented data).
"""

from __future__ import annotations

import hashlib
import random
import time
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from bank import ofx, reconcile, statements
from bank.models import BankTransaction
from bank.statements import parse_statement
from bank.tests import ofx_files
from bank.tests.ofx_files import ACCOUNT, OPERATIONS, changed, sgml, statement_block, xml
from bank.tests.ofx_files import ofx as ofx_file
from bank.tests.support import make_format, rule, rules_of

#: An OFX format, as « Format du relevé » stores one.
OFX_FORMAT = SimpleNamespace(
    name="Relevé OFX",
    file_type="ofx",
    encoding="auto",
    delimiter=";",
    date_format="dd/mm/yyyy",
    decimal_mark=",",
    date_column=None,
    label_columns="",
    amount_column=None,
    debit_column=None,
    credit_column=None,
    value_date_column=None,
    bank_type_column=None,
    account_pattern="",
)


def read(content: bytes, rules=None, fmt=OFX_FORMAT):
    return parse_statement(content, rules_of() if rules is None else rules, fmt)


def refusal(content: bytes, fmt=OFX_FORMAT) -> str:
    try:
        read(content, fmt=fmt)
    except ValueError as error:
        return str(error)
    raise AssertionError("the file was read")


def brief(statement) -> list[tuple]:
    return [(line.operation_date, line.value_date, line.bank_type, line.label, line.amount) for line in statement.lines]


#: What the fixture reads, line by line.
READ = [
    (date(2026, 8, 3), None, "POS", "CB EPICERIE EXEMPLE FACTURE CARTE DU 010826", Decimal("-4.10")),
    (date(2026, 8, 3), None, "POS", "CB EPICERIE EXEMPLE FACTURE CARTE DU 010826", Decimal("-4.10")),
    (date(2026, 8, 9), None, "DIRECTDEBIT", "PRLV SEPA FOURNISSEUR EXEMPLE ECH/090826 REF/000123", Decimal("-120.35")),
    (date(2026, 8, 5), date(2026, 8, 6), "XFER", "VIR SEPA RECU CLIENT EXEMPLE", Decimal("250.00")),
    (date(2026, 8, 31), None, "SRVCHG", "FRAIS CAFÉ ÉTOILE & FILS COTISATION", Decimal("-8.50")),
]


class ReadingTests(SimpleTestCase):
    def test_an_sgml_statement_is_read_where_the_standard_puts_each_datum(self):
        statement = read(sgml())
        self.assertEqual(statement.account, ACCOUNT)
        self.assertEqual(brief(statement), READ)
        # Digit for digit, as printed: « 250.00 », never « 250 ».
        self.assertEqual(str(statement.lines[3].amount), "250.00")

    def test_the_same_statement_in_xml_gives_the_same_fingerprints(self):
        sgml_lines, xml_lines = read(sgml()).lines, read(xml()).lines
        self.assertEqual(brief(read(xml())), READ)
        self.assertEqual([line.fingerprint for line in sgml_lines], [line.fingerprint for line in xml_lines])
        # Two identical operations are two lines, two fingerprints.
        self.assertEqual(len({line.fingerprint for line in sgml_lines}), len(OPERATIONS))

    def test_the_fingerprint_is_the_csv_s_composition(self):
        """account|day|value date|label|amount|occurrence, the amount as
        printed: an OFX line is known again as a CSV line would be."""
        line = read(sgml()).lines[3]
        key = f"{ACCOUNT}|2026-08-05|2026-08-06|VIR SEPA RECU CLIENT EXEMPLE|250.00|0"
        self.assertEqual(line.fingerprint, hashlib.sha256(key.encode()).hexdigest())

    def test_windows_1252_accents_and_utf8_both_read_in_auto(self):
        for content in (sgml(), xml(), sgml().decode("cp1252").encode("utf-8")):
            with self.subTest(content=content[:20]):
                self.assertEqual(read(content).lines[4].label, "FRAIS CAFÉ ÉTOILE & FILS COTISATION")

    def test_a_card_statement_is_read_by_its_card_account(self):
        statement = read(sgml(card=True))
        self.assertEqual((statement.account, len(statement.lines)), (ACCOUNT, len(OPERATIONS)))

    def test_the_account_of_a_transfer_s_beneficiary_is_not_the_statement_s(self):
        transfer = changed(
            OPERATIONS[3],
            extra=("<BANKACCTTO>", "<BANKID>11111", "<ACCTID>99999999999", "<ACCTTYPE>CHECKING", "</BANKACCTTO>"),
        )
        self.assertEqual(read(sgml((transfer,))).account, ACCOUNT)

    def test_payee_name_stands_for_a_missing_name(self):
        payee = changed(name=None, memo=None, extra=("<PAYEE>", "<NAME>BOULANGERIE EXEMPLE", "<CITY>PARIS", "</PAYEE>"))
        self.assertEqual(read(sgml((payee,))).lines[0].label, "BOULANGERIE EXEMPLE")

    def test_an_empty_leaf_or_an_unknown_one_never_hides_what_follows(self):
        operation = changed(memo="", extra=("<CHECKNUM>", "<SIC>5411"))
        (line,) = read(sgml((operation,))).lines
        self.assertEqual((line.label, line.amount), ("CB EPICERIE EXEMPLE", Decimal("-4.10")))

    def test_an_empty_xml_element_is_read_as_absent(self):
        (line,) = read(xml((changed(memo=None, extra=("<MEMO/>", "<CHECKNUM />")),))).lines
        self.assertEqual(line.label, "CB EPICERIE EXEMPLE")

    def test_dates_are_their_first_eight_digits_whatever_the_time_and_zone(self):
        for printed in ("20260803", "20260803120000", "20260803120000.000[+1:CET]", "20260803235959[-5:EST]"):
            with self.subTest(printed=printed):
                self.assertEqual(read(sgml((changed(dtposted=printed),))).lines[0].operation_date, date(2026, 8, 3))

    def test_dtavail_is_the_value_date_and_dtuser_is_never_read(self):
        (line,) = read(sgml((changed(dtavail="20260804", dtuser="20260701"),))).lines
        self.assertEqual((line.value_date, line.card_date), (date(2026, 8, 4), None))

    def test_amounts_are_read_digit_for_digit(self):
        for printed, amount in (
            ("-12.50", "-12.50"),
            ("-12,5", "-12.5"),
            ("+7", "7"),
            ("-.50", "-0.50"),
            ("9999999999.99", "9999999999.99"),
        ):
            with self.subTest(printed=printed):
                self.assertEqual(str(read(sgml((changed(trnamt=printed),))).lines[0].amount), amount)

    def test_the_pending_list_is_left_out(self):
        statement = read(sgml(OPERATIONS[:1], pending=(changed(trnamt="-99.00", fitid="P1"),)))
        self.assertEqual([line.amount for line in statement.lines], [Decimal("-4.10")])

    def test_two_statements_of_one_account_are_one(self):
        content = ofx_file(
            statement_block(OPERATIONS[:1], xml=False), statement_block(OPERATIONS[2:3], xml=False), xml=False
        )
        self.assertEqual(len(read(content).lines), 2)

    def test_numeric_references_and_the_five_entities_are_decoded_others_left(self):
        name = "CAF&#201; &#xC9;TOILE &lt;2&gt; &quot;A&quot; &apos;B&apos; &nbsp;TAB&#9;FIN"
        (line,) = read(sgml((changed(name=name, memo=None),))).lines
        self.assertEqual(line.label, "CAFÉ ÉTOILE <2> \"A\" 'B' &nbsp;TAB FIN")

    def test_the_type_is_cut_to_its_column(self):
        (line,) = read(sgml((changed(trntype="X" * 100),))).lines
        self.assertEqual(line.bank_type, "X" * statements.BANK_TYPE_MAX)

    def test_the_rules_read_the_type(self):
        rules = rules_of(rule("Carte (OFX : POS)", "card_payment", "^POS$", searched="bank_type"))
        kinds = [line.kind for line in read(sgml(), rules).lines]
        self.assertEqual(kinds, ["CARD", "CARD", "OTHER", "OTHER", "OTHER"])


class RefusalTests(SimpleTestCase):
    def test_another_currency_refuses_the_file(self):
        self.assertEqual(
            refusal(sgml(currency="USD")), "Ce relevé est en USD : seuls les relevés en euros s'importent."
        )
        own = changed(extra=("<CURRENCY>", "<CURRATE>1.1", "<CURSYM>CHF", "</CURRENCY>"))
        self.assertEqual(refusal(sgml((own,))), "Ce relevé est en CHF : seuls les relevés en euros s'importent.")
        # Converted already: the amount is in the statement's currency.
        converted = changed(extra=("<ORIGCURRENCY>", "<CURRATE>1.1", "<CURSYM>CHF", "</ORIGCURRENCY>"))
        self.assertEqual(len(read(sgml((converted,))).lines), 1)

    def test_a_statement_that_says_no_currency(self):
        self.assertEqual(refusal(sgml(currency=None)), ofx.NO_CURRENCY)

    def test_two_accounts_refuse_the_file(self):
        content = ofx_file(
            statement_block(OPERATIONS[:1], xml=False),
            statement_block(OPERATIONS[:1], xml=False, account="00098765432"),
            xml=False,
        )
        self.assertEqual(refusal(content), "Ce relevé contient plusieurs comptes (2) : exportez-les un par un.")

    def test_a_date_that_is_no_day_or_no_day_a_statement_holds(self):
        for printed in ("20261340", "19991231", "2026-08-03", "2026080", "\N{ARABIC-INDIC DIGIT TWO}0260803"):
            with self.subTest(printed=printed):
                said = refusal(xml((changed(dtposted=printed),)))
                self.assertEqual(said, f"Date illisible dans le relevé : {printed!r}")
        self.assertEqual(
            refusal(xml((changed(dtposted=""),))),
            f"{ofx.INCOMPLETE} (sans DTPOSTED) : exportez-le à nouveau au format OFX (Money).",
        )
        self.assertEqual(refusal(sgml((changed(dtavail="20260231"),))), "Date illisible dans le relevé : '20260231'")

    def test_an_amount_that_cannot_be_read_whole(self):
        for printed in (
            "1e5",
            "12.345,67",
            "1.234,56",
            "1 234.56",
            "10000000000.00",
            "--4",
            "4.",
            "NaN",
            "\N{ARABIC-INDIC DIGIT FOUR}",
        ):
            with self.subTest(printed=printed):
                self.assertEqual(
                    refusal(xml((changed(trnamt=printed),))), f"Montant illisible dans le relevé : {printed!r}"
                )

    def test_an_echoed_value_is_cut_to_80_characters(self):
        said = refusal(sgml((changed(trnamt="9" * 1_000_000),)))
        self.assertEqual(said, f"Montant illisible dans le relevé : {'9' * 80!r}")

    def test_a_reference_to_no_character_a_label_holds(self):
        for reference in (
            "&#0;",
            "&#x0;",
            "&#10;",
            "&#x7F;",
            "&#x85;",
            "&#xD800;",
            "&#55296;",
            "&#x110000;",
            "&#12345678;",
        ):
            with self.subTest(reference=reference):
                said = refusal(sgml((changed(name=f"A{reference}B"),)))
                self.assertEqual(said, f"{ofx.BAD_REFERENCE} : « {reference} ».")
        # Digits past what any character needs, refused before `int` reads them.
        reference = "&#" + "1" * 5000 + ";"
        said = refusal(sgml((changed(name=reference),)))
        self.assertEqual(said, f"{ofx.BAD_REFERENCE} : « {reference[:80]} ».")

    def test_a_doctype_an_entity_or_a_nul_refuses_the_file_unread(self):
        doctype = xml().replace(b"<OFX>", b'<!DOCTYPE OFX [<!ENTITY lol "lol">]>\n<OFX>', 1)
        self.assertEqual(refusal(doctype), ofx.UNSAFE)
        entity = sgml().replace(b"<OFX>", b'<!ENTITY x "y">\n<OFX>', 1)
        self.assertEqual(refusal(entity), ofx.UNSAFE)
        self.assertEqual(refusal(sgml().replace(b"EPICERIE", b"EPI\x00CERIE")), ofx.NOT_OFX)

    def test_a_file_cut_short_never_loses_its_last_operation_in_silence(self):
        content = sgml()
        for cut in (content.index(b"<TRNAMT>-8.50"), content.index(b"</BANKTRANLIST>"), len(content) - 8):
            with self.subTest(cut=cut):
                self.assertEqual(refusal(content[:cut]), ofx.NOT_OFX)

    def test_what_is_no_ofx(self):
        for content in (b"", b"Date;Montant\n03/08/2026;-4,10\n", b"<OFX><STMTRS <BAD>", b"<OFX>\n<A attr='1'>x"):
            with self.subTest(content=content):
                self.assertEqual(refusal(content), ofx.NOT_OFX)

    def test_elements_nested_past_any_statement_are_refused_at_once(self):
        content = sgml().replace(b"<BANKTRANLIST>", b"<BANKTRANLIST>" + b"<A>" * 200_000, 1)
        started = time.perf_counter()
        self.assertEqual(refusal(content), ofx.NOT_OFX)
        self.assertLess(time.perf_counter() - started, 2)

    def test_a_file_with_no_operation_names_its_format(self):
        self.assertEqual(refusal(sgml(())), "Aucune opération trouvée dans ce relevé OFX (format « Relevé OFX »).")

    def test_a_file_too_heavy_is_refused_unread(self):
        with mock.patch.object(statements, "STRUCTURED_MAX_BYTES", 1000):
            self.assertEqual(refusal(sgml()), "Ce relevé dépasse 1 Ko : exportez une période plus courte.")

    def test_an_account_wider_than_its_column(self):
        self.assertEqual(refusal(sgml(account="1" * 41)), statements.ACCOUNT_TOO_LONG)


#: Every sentence a refusal of an OFX file may begin with - the reader's own
#: and the pipeline's.
FRENCH = (*ofx.REFUSALS, *statements.REFUSALS)
#: What a mutation writes into a file: the characters a tokenizer, a
#: reference or a number reads.
MUTATIONS = ("<", ">", "/", "&", "#", "x", ";", "0", "9", ".", ",", "-", "\x00", "\x85", "é", "퟿", "&#x", "<!--")


class MutationTests(SimpleTestCase):
    """Whatever the bytes, a refusal is one of these sentences: a mutated
    file is read, or refused in French - never a library's English, never
    another exception (a 500 on the import)."""

    def test_every_mutated_file_is_read_or_refused_in_french(self):
        generator = random.Random(20261004)
        seeds = [sgml(), xml(), sgml(card=True), xml(OPERATIONS[:2], pending=(OPERATIONS[3],))]
        refused = read_whole = 0
        for _ in range(1500):
            content = bytearray(generator.choice(seeds))
            for _change in range(generator.randint(1, 4)):
                at = generator.randrange(len(content))
                action = generator.random()
                if action < 0.3:
                    del content[at : at + generator.randint(1, 12)]
                elif action < 0.8:
                    content[at:at] = generator.choice(MUTATIONS).encode("utf-8", "surrogatepass")
                else:
                    content = content[:at]
                    break
            try:
                read(bytes(content))
            except ValueError as error:
                said = str(error)
                self.assertTrue(said.startswith(FRENCH), said)
                refused += 1
            else:
                read_whole += 1
        # The mutations do both, often: a test that refused everything
        # would prove nothing about what is read.
        self.assertGreater(refused, 300)
        self.assertGreater(read_whole, 100)


class ImportTests(TestCase):
    """Through the real import: a format in the database, the lines written,
    and the same file imported again writes nothing."""

    def setUp(self):
        super().setUp()
        self.fmt = make_format("Relevé OFX", file_type="ofx", date_column=None, label_columns="", account_pattern="")

    def test_imported_then_imported_again(self):
        first = reconcile.import_statement(sgml(), rules_of(), self.fmt)
        self.assertEqual((first.lines, first.created), (len(OPERATIONS), len(OPERATIONS)))
        self.assertEqual(set(BankTransaction.objects.values_list("account", flat=True)), {ACCOUNT})
        # The same month exported in OFX 2 is known again, line for line.
        again = reconcile.import_statement(xml(), rules_of(), self.fmt)
        self.assertEqual((again.lines, again.created), (len(OPERATIONS), 0))

    def test_a_refused_file_writes_nothing(self):
        with self.assertRaises(ValueError):
            reconcile.import_statement(sgml((*OPERATIONS, changed(trnamt="1e5"))), rules_of(), self.fmt)
        self.assertFalse(BankTransaction.objects.exists())


class FixtureTests(SimpleTestCase):
    def test_the_fixtures_follow_the_standard_s_shapes(self):
        self.assertTrue(sgml().startswith(b"OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n"))
        self.assertTrue(xml().startswith(b'<?xml version="1.0"'))
        self.assertIn(b'<?OFX OFXHEADER="200" VERSION="211"', xml())
        self.assertIn(b"<TRNAMT>-4.10\n", sgml())
        self.assertIn(b"<TRNAMT>-4.10</TRNAMT>", xml())
        self.assertIn(b"<CREDITCARDMSGSRSV1>\n<CCSTMTTRNRS>", ofx_files.sgml(card=True))
