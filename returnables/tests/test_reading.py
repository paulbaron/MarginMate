"""returnables/reading.py: a slip's PDF text, and what a format's patterns read
in it - every quirk of spec §1 on the invented tickets of texts.py.

Timeouts are SIMULATED (a stand-in pattern whose search raises
TimeoutError); a pattern the guard must refuse is read with regex.compile
replaced by a sentinel. PDFs are a few hundred bytes, written by
invoices/tests/pdf_files.write_pdf into a temporary folder.
"""

import io
import os
import tempfile
import zlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pdfplumber
import regex
from django.test import SimpleTestCase
from pdfminer import pdftypes

from invoices.tests.pdf_files import write_pdf
from returnables import patterns, reading
from returnables.patterns import Budget, aware_datetime
from returnables.reading import (
    Check,
    ReadLine,
    SlipError,
    SlipReading,
    detect_format,
    pdf_text,
    read_slip_text,
    trace,
)
from returnables.tests import texts
from returnables.tests.test_patterns import NeverCompile, SlowPattern

U_HAT = texts.U_HAT


def read(text, rules=None, **kwargs) -> SlipReading:
    return read_slip_text(text, rules or texts.UBA_RULES, **kwargs)


def labels(result: SlipReading) -> list:
    return [check.label for check in result.checks]


def check(result: SlipReading, label: str) -> Check:
    (found,) = [item for item in result.checks if item.label == label]
    return found


class TheInventedTicketsTests(SimpleTestCase):
    """texts.py is shared by every package's tests: its tickets must print
    with write_pdf, and its labels have the ticket's shape."""

    def test_every_ticket_is_latin_1_for_write_pdf(self):
        for text in [slip.text for slip in texts.ALL] + [texts.JUNK]:
            text.encode("latin-1")

    def test_the_labels_are_at_most_twenty_characters_and_start_their_full_name(self):
        for label, full in texts.FULL_NAMES.items():
            with self.subTest(label=label):
                self.assertLessEqual(len(label), 20)
                self.assertEqual(full[:20], label)
                self.assertIn(label, texts.EXPECTED_TYPE)

    def test_each_label_s_expected_type_is_what_the_seeded_patterns_say(self):
        compiled = [
            (name, patterns.compile_field(patterns.TYPE_FIELD, slip_patterns))
            for name, _position, slip_patterns in texts.SEED_TYPES
        ]
        for label, expected in texts.EXPECTED_TYPE.items():
            with self.subTest(label=label):
                found = next(
                    (name for name, type_regexes in compiled if any(pattern.search(label) for pattern in type_regexes)),
                    None,
                )
                self.assertEqual(found, expected)

    def test_the_seeded_patterns_are_the_specs(self):
        self.assertEqual(texts.UBA_RULES.name, "UBA \N{EM DASH} bon du livreur")
        self.assertEqual(
            texts.SEED_TYPES[0],
            ("F\N{LATIN SMALL LETTER U WITH CIRCUMFLEX}ts", 1, "F[\N{LATIN CAPITAL LETTER U WITH CIRCUMFLEX}U]TS?\\b"),
        )
        self.assertEqual([item.pk for item in texts.seed_types()], [1, 2, 3])
        self.assertEqual(texts.uba_rules(section_start="").section_start, "")
        self.assertEqual(texts.UBA_RULES.section_start, r"^REPRISE VIDE\s*$")


class EveryTicketIsReadTests(SimpleTestCase):
    """Each invented ticket, read with the seeded UBA patterns, gives exactly
    what texts.py says it holds."""

    def test_every_ticket(self):
        for slip in texts.ALL:
            with self.subTest(slip=slip.name):
                result = read(slip.text)
                self.assertIsNone(result.error)
                self.assertEqual([line.as_tuple() for line in result.lines], slip.lines)
                self.assertEqual(result.unread, slip.unread)
                self.assertEqual(result.number, slip.number)
                self.assertEqual(result.delivery_date, slip.delivery_date)
                self.assertEqual(result.printed_at, aware_datetime(*slip.printed))
                self.assertEqual(result.references, slip.references)
                self.assertEqual(result.printed_total, slip.printed_total)
                self.assertEqual(result.replaces, slip.replaces)
                self.assertEqual(result.remarks, slip.remarks)

    def test_the_clean_tickets_pass_every_check(self):
        for slip in texts.CLEAN:
            with self.subTest(slip=slip.name):
                result = read(slip.text)
                self.assertTrue(result.all_passed, [(item.label, item.detail) for item in result.failed_checks])
                self.assertIs(result.section_found, True)

    def test_the_checks_come_in_order(self):
        self.assertEqual(
            labels(read(texts.NORMAL.text)),
            [
                "Partie des consignes trouvée",
                "Fin de la partie trouvée",
                "2 lignes lues",
                "Aucune ligne ignorée",
                "quantité × prix = montant",
                "Total des lignes = total imprimé",
                "Date de livraison lue",
                "Numéro lu",
            ],
        )
        result = read(texts.NORMAL.text)
        self.assertEqual(check(result, "Date de livraison lue").detail, "14/05/2025")
        self.assertEqual(check(result, "Numéro lu").detail, "1001")
        self.assertEqual(
            check(result, "Total des lignes = total imprimé").detail, "lignes : 175,00 · total imprimé : -175,00"
        )

    def test_amounts_are_decimals_to_their_column(self):
        (keg, _co2) = read(texts.NORMAL.text).lines
        self.assertIsInstance(keg.amount, Decimal)
        self.assertEqual(keg.amount.as_tuple().exponent, -2)
        self.assertEqual(keg.unit_amount.as_tuple().exponent, -4)
        self.assertEqual(keg, ReadLine(texts.KEG, 3, Decimal("30.00"), Decimal("90.00")))


class QuirksTests(SimpleTestCase):
    def test_the_u_circumflex_is_kept(self):
        (keg, _co2) = read(texts.NORMAL.text).lines
        self.assertEqual(keg.designation[1], "\N{LATIN CAPITAL LETTER U WITH CIRCUMFLEX}")

    def test_labels_cut_at_twenty_characters_with_digits_are_read_whole(self):
        result = read(texts.MIXED.text)
        designations = [line.designation for line in result.lines]
        self.assertEqual(designations, [texts.KEG_LONG, texts.CRATE, texts.WATER, texts.JUICE, texts.CO2, texts.PALLET])
        for cut in (texts.KEG_LONG, texts.CRATE, texts.CO2, texts.JUICE):
            self.assertEqual(len(cut), 20)
            self.assertTrue(texts.FULL_NAMES[cut].startswith(cut))

    def test_an_empty_part_reads_no_line_and_agrees_with_a_zero_total(self):
        result = read(texts.EMPTY.text)
        self.assertEqual(result.lines, [])
        self.assertIn("0 ligne lue", labels(result))
        self.assertNotIn("quantité × prix = montant", labels(result))
        self.assertTrue(check(result, "Total des lignes = total imprimé").passed)

    def test_two_delivery_notes_are_two_references_and_the_delivery_note_date_is_the_delivery(self):
        result = read(texts.TWO_DELIVERY_NOTES.text)
        self.assertEqual(result.references, ["610003", "610004"])
        self.assertEqual(result.delivery_date, date(2025, 5, 20))

    def test_a_delivery_note_without_an_amount_reads_the_same(self):
        self.assertEqual(read(texts.DELIVERY_NOTE_WITHOUT_AMOUNT.text).references, ["610005"])

    def test_a_resend_keeps_the_delivery_note_date_and_its_own_print_time(self):
        original, resend = read(texts.NORMAL.text), read(texts.RESEND.text)
        self.assertEqual(resend.delivery_date, original.delivery_date)
        self.assertEqual(resend.number, original.number)
        self.assertNotEqual(resend.printed_at, original.printed_at)
        self.assertEqual(resend.printed_at.date(), date(2025, 5, 15))

    def test_the_cancels_and_replaces_mark_is_read(self):
        self.assertTrue(read(texts.REPLACEMENT.text).replaces)
        self.assertFalse(read(texts.ORIGINAL.text).replaces)
        self.assertEqual(read(texts.REPLACEMENT.text).references, read(texts.ORIGINAL.text).references)

    def test_the_anomalies_block_is_the_remarks_without_its_separators(self):
        result = read(texts.ANOMALIES.text)
        self.assertEqual(result.remarks.split("\n"), texts.ANOMALY_LINES)
        self.assertNotIn("+", result.remarks)
        self.assertEqual(read(texts.NORMAL.text).remarks, "")

    def test_an_unread_line_is_listed(self):
        result = read(texts.UNREAD.text)
        self.assertEqual(result.unread, texts.UNREAD_LINES)
        failed = check(result, "Aucune ligne ignorée")
        self.assertFalse(failed.passed)
        self.assertEqual(failed.detail, f"2 lignes non lues : « CASIER DIVERS » ; « {texts.KEG} 1 x 30.00 »")

    def test_quantity_times_price_not_amount_is_said(self):
        failed = check(read(texts.WRONG_PRODUCT.text), "quantité × prix = montant")
        self.assertFalse(failed.passed)
        self.assertEqual(failed.detail, f"« {texts.KEG} » : 3 × 30,00 = 90,00, le bon imprime 95,00")

    def test_lines_not_adding_up_to_the_total_are_said(self):
        failed = check(read(texts.WRONG_TOTAL.text), "Total des lignes = total imprimé")
        self.assertFalse(failed.passed)
        self.assertEqual(failed.detail, "lignes : 90,00 · total imprimé : -120,00")

    def test_no_section(self):
        result = read(texts.NO_SECTION.text)
        self.assertIs(result.section_found, False)
        self.assertEqual(result.lines, [])
        self.assertEqual(
            check(result, "Partie des consignes trouvée").detail, "aucune ligne ne correspond au motif de début"
        )
        self.assertFalse(check(result, "Fin de la partie trouvée").passed)
        self.assertEqual(result.delivery_date, date(2025, 6, 16))

    def test_a_part_printed_twice_is_read_once_and_said(self):
        result = read(texts.DOUBLE_SECTION.text)
        self.assertEqual(len(result.lines), 1)
        found = check(result, "Partie des consignes trouvée")
        self.assertFalse(found.passed)
        self.assertEqual(found.detail, "trouvée 2 fois : seule la première est lue")

    def test_junk(self):
        result = read(texts.JUNK)
        self.assertIsNone(result.error)
        self.assertIs(result.section_found, False)
        self.assertIsNone(result.delivery_date)
        self.assertEqual((result.number, result.references, result.lines), ("", [], []))
        self.assertEqual(check(result, "Date de livraison lue").detail, "aucun motif de date n'a trouvé de date")

    def test_a_pasted_text_with_windows_line_ends_reads_the_same(self):
        pasted = read(texts.NORMAL.text.replace("\n", "\r\n"))
        self.assertEqual([line.as_tuple() for line in pasted.lines], texts.NORMAL.lines)
        self.assertTrue(pasted.all_passed)

    def test_not_a_text_is_read_as_nothing(self):
        for value in (None, b"bytes", 12):
            with self.subTest(value=value):
                result = read(value)
                self.assertIsNone(result.error)
                self.assertEqual(result.lines, [])


class LineRulesTests(SimpleTestCase):
    def part(self, *rows) -> str:
        return "\n".join(["REPRISE VIDE", texts.SEPARATOR, *rows, "FACTURE(S)/BL DU JOUR"])

    def test_a_designation_needs_two_letters_or_digits(self):
        result = read(self.part("* 1 x 2.00 = 2.00", "A1 1 x 2.00 = 2.00", "-- 1 x 2.00 = 2.00"))
        self.assertEqual([line.designation for line in result.lines], ["A1"])
        self.assertEqual(result.unread, ["* 1 x 2.00 = 2.00", "-- 1 x 2.00 = 2.00"])

    def test_a_figure_wider_than_its_column_makes_the_line_unread(self):
        rows = [
            f"{texts.KEG} 123456 x 30.00 = 90.00",
            f"{texts.KEG} 1 x 30.00 = 12345678901.00",
            f"{texts.KEG} 1 x 123456789.00 = 30.00",
            f"{texts.KEG} 1 x 30.00 = 30.001",
            f"{texts.KEG} 99999 x 1.00 = 99999.00",
        ]
        result = read(self.part(*rows))
        self.assertEqual([line.quantity for line in result.lines], [99_999])
        self.assertEqual(result.unread, rows[:4])

    def test_signed_and_zero_quantities_are_read_as_printed(self):
        result = read(self.part(f"{texts.KEG} -1 x 30.00 = -30.00", f"{texts.KEG} 0 x 30.00 = 0.00"))
        self.assertEqual(
            [line.as_tuple() for line in result.lines],
            [(texts.KEG, -1, Decimal("30.00"), Decimal("-30.00")), (texts.KEG, 0, Decimal("30.00"), Decimal("0.00"))],
        )

    def test_optional_groups_left_out_are_none(self):
        rules = texts.uba_rules(line_pattern=r"^(?P<designation>\D+?)\s+(?P<quantite>\d+)(?:\s+x\s+(?P<prix>[\d.]+))?$")
        result = read(self.part("FUT BLONDE 3", "CAISSE 2 x 7.50"), rules)
        self.assertEqual(
            [line.as_tuple() for line in result.lines],
            [("FUT BLONDE", 3, None, None), ("CAISSE", 2, Decimal("7.50"), None)],
        )
        self.assertNotIn("quantité × prix = montant", labels(result))

    def test_a_line_longer_than_500_characters_is_never_matched(self):
        long_row = f"{texts.KEG} 1 x 30.00 = " + "1" * 500
        result = read(self.part(long_row))
        self.assertEqual(result.lines, [])
        self.assertEqual(result.unread, [long_row[:120] + "…"])

    def test_a_header_line_longer_than_500_characters_is_not_read_either(self):
        text = texts.NORMAL.text.replace("Ticket No : 0000001001", "Ticket No : 0000001001" + " " * 480 + "x")
        self.assertEqual(read(text).number, "")

    def test_texts_are_cut_to_their_column_and_cleaned(self):
        rules = texts.uba_rules(number_patterns=r"N\s*:\s*(?P<numero>.+)")
        long_label = "A" * 250
        text = (
            self.part(f"{long_label} 1 x 2.00 = 2.00", "BOUT\N{CHARACTER TABULATION}EILLE   X 1 x 2.00 = 2.00")
            + "\nN : "
            + "7" * 60
        )
        result = read(text, rules)
        self.assertEqual(result.lines[0].designation, "A" * 200)
        self.assertEqual(result.lines[1].designation, "BOUT EILLE X")
        self.assertEqual(result.number, "7" * 40)


class SectionRulesTests(SimpleTestCase):
    def test_without_a_start_pattern_the_whole_text_is_the_part(self):
        rules = texts.uba_rules(section_start="", section_end="")
        text = "\n".join(
            [texts.SEPARATOR, texts.row(texts.KEG, 2, "30.00", "60.00"), "", texts.row(texts.CO2, 1, "85.00", "85.00")]
        )
        result = read(text, rules)
        self.assertIsNone(result.section_found)
        self.assertEqual(len(result.lines), 2)
        self.assertEqual(result.unread, [])
        self.assertNotIn("Partie des consignes trouvée", labels(result))
        self.assertNotIn("Fin de la partie trouvée", labels(result))

    def test_an_end_never_found_reads_to_the_end_and_says_so(self):
        result = read(texts.NORMAL.text, texts.uba_rules(section_end="^NULLE PART"))
        found = check(result, "Fin de la partie trouvée")
        self.assertFalse(found.passed)
        self.assertEqual(found.detail, "la partie est lue jusqu'à la fin du document")
        self.assertIn("FACTURE(S)/BL DU JOUR", result.unread)
        self.assertEqual(len(result.lines), 2)

    def test_an_end_before_the_start_does_not_end_it(self):
        # The end is the first LATER line: « Tournee » comes before the part.
        result = read(texts.NORMAL.text, texts.uba_rules(section_end="^(?:Tournee|FACTURE)"))
        self.assertEqual(len(result.lines), 2)
        self.assertTrue(check(result, "Fin de la partie trouvée").passed)

    def test_a_part_over_300_lines_is_an_error(self):
        rows = [texts.row(texts.KEG, 1, "30.00", "30.00")] * 301
        result = read("\n".join(["REPRISE VIDE", *rows, "FACTURE(S)/BL DU JOUR"]))
        self.assertEqual(result.error, reading.SECTION_TOO_LONG)
        self.assertEqual(result.lines, [])
        self.assertEqual(
            [item.as_dict() for item in result.checks],
            [{"label": "Lecture impossible", "passed": False, "detail": reading.SECTION_TOO_LONG}],
        )
        ok = read("\n".join(["REPRISE VIDE", *rows[:300], "FACTURE(S)/BL DU JOUR"]))
        self.assertEqual(len(ok.lines), 300)


class HeaderRulesTests(SimpleTestCase):
    def test_header_patterns_never_cross_a_line(self):
        rules = texts.uba_rules(number_patterns=r"Ticket No\s*:\s*(?P<numero>\d+)")
        self.assertEqual(read("Ticket No :\n1234", rules).number, "")

    def test_pattern_order_first_then_line_order(self):
        # The print date first: « Le … » wins although the delivery-note line is read too.
        dates = "\n".join(reversed(texts.UBA_PATTERNS["date_patterns"].split("\n")))
        self.assertEqual(read(texts.RESEND.text, texts.uba_rules(date_patterns=dates)).delivery_date, date(2025, 5, 15))
        self.assertEqual(read(texts.RESEND.text).delivery_date, date(2025, 5, 14))

    def test_a_blank_or_unreadable_capture_tries_the_next_line_then_the_next_pattern(self):
        rules = texts.uba_rules(
            number_patterns=r"Ticket No\s*:(?P<numero>\s*\d*)", date_patterns="D (?P<date>\\S+)\nE (?P<date>\\S+)"
        )
        text = "Ticket No :\nTicket No : 42\nD 31/02/2025\nD 2025\nE 03/03/2025"
        result = read(text, rules)
        self.assertEqual(result.number, "42")
        self.assertEqual(result.delivery_date, date(2025, 3, 3))

    def test_the_print_time_is_optional(self):
        rules = texts.uba_rules(printed_patterns=r"^Le\s+(?P<date>\d{2}/\d{2}/\d{4})(?:\s+(?P<heure>\S+))?")
        self.assertEqual(read("Le 14/05/2025", rules).printed_at, aware_datetime(date(2025, 5, 14)))
        self.assertEqual(read("Le 14/05/2025 25:99", rules).printed_at, aware_datetime(date(2025, 5, 14)))

    def test_references_every_match_distinct_bounded(self):
        rules = texts.uba_rules(reference_patterns=r"REF (?P<reference>[^;]+)")
        text = "\n".join(
            ["REF AB ; REF 12  34 ; REF 1234", "REF 1234", "REF " + "X" * 50]
            + [f"REF R{number:03d}" for number in range(30)]
        )
        result = read(text, rules)
        self.assertEqual(result.references[:3], ["12 34", "1234", "X" * 40])
        self.assertEqual(len(result.references), 20)
        self.assertEqual(result.references[-1], "R016")

    def test_the_total_reads_signed_and_bounded(self):
        rules = texts.uba_rules(total_patterns=r"Deconsigne\s*:\s*(?P<total>\S+)")
        self.assertEqual(read("Deconsigne : 1.234,50-", rules).printed_total, Decimal("-1234.50"))
        self.assertIsNone(read("Deconsigne : 99999999999", rules).printed_total)

    def test_remarks_run_to_the_end_without_an_end_pattern(self):
        result = read(texts.ANOMALIES.text, texts.uba_rules(remarks_end=""))
        self.assertEqual(
            result.remarks.split("\n"), texts.ANOMALY_LINES + ["Merci de Votre Commande", "Signature Client"]
        )
        self.assertEqual(read(texts.ANOMALIES.text, texts.uba_rules(remarks_start="")).remarks, "")

    def test_without_number_patterns_there_is_no_number_check(self):
        result = read(texts.NORMAL.text, texts.uba_rules(number_patterns=""))
        self.assertNotIn("Numéro lu", labels(result))
        self.assertEqual(result.number, "")

    def test_without_date_patterns_the_date_check_says_so(self):
        found = check(read(texts.NORMAL.text, texts.uba_rules(date_patterns="")), "Date de livraison lue")
        self.assertEqual((found.passed, found.detail), (False, "le format n'a pas de motif de date"))


class ReadingNeverRaisesTests(SimpleTestCase):
    def assertFailedWith(self, result: SlipReading, text: str):
        self.assertIsNotNone(result.error)
        self.assertIn(text, result.error)
        self.assertEqual(result.lines, [])
        self.assertEqual(len(result.checks), 1)
        self.assertEqual(result.checks[0].label, "Lecture impossible")
        self.assertFalse(result.checks[0].passed)
        self.assertEqual(result.checks[0].detail, result.error)
        self.assertFalse(result.all_passed)

    def test_a_stored_pattern_that_no_longer_compiles(self):
        self.assertFailedWith(
            read(texts.NORMAL.text, texts.uba_rules(line_pattern="^(?P<designation>.+")),
            "Motif de ligne : parenthèse non fermée",
        )
        self.assertFailedWith(read(texts.NORMAL.text, texts.uba_rules(date_patterns="BL(")), "Motif de date")

    def test_a_blank_line_pattern(self):
        self.assertFailedWith(
            read(texts.NORMAL.text, texts.uba_rules(line_pattern="")), "Motif de ligne : le motif est vide."
        )
        self.assertFailedWith(read(texts.NORMAL.text, SimpleNamespace()), "Motif de ligne : le motif est vide.")

    def test_a_stored_pattern_the_guard_refuses_is_never_compiled(self):
        never = NeverCompile()
        patterns._checked.cache_clear()
        self.addCleanup(patterns._checked.cache_clear)
        with mock.patch.object(regex, "compile", new=never):
            result = read(texts.NORMAL.text, texts.uba_rules(section_start=r"(?:x{65535}){65535}"))
        self.assertEqual(never.calls, [])
        self.assertFailedWith(result, "répétition trop grande")

    def test_a_slow_pattern_simulated(self):
        real = patterns.compile_format

        def slow_lines(fmt, *args, **kwargs):
            regexes = real(fmt, *args, **kwargs)
            regexes["line_pattern"] = [SlowPattern()]
            return regexes

        with mock.patch.object(patterns, "compile_format", side_effect=slow_lines):
            result = read(texts.NORMAL.text)
            traced = trace(texts.NORMAL.text, texts.UBA_RULES)
        self.assertFailedWith(result, "est trop lent")
        self.assertTrue(all(line.tags == [] for line in traced))
        self.assertEqual(traced.reading.error, result.error)

    def test_a_spent_budget(self):
        self.assertFailedWith(read(texts.NORMAL.text, budget=Budget(0)), "est trop lent")

    def test_a_text_too_long(self):
        result = read("x" * (reading.MAX_TEXT_CHARS + 1))
        self.assertFailedWith(result, "trop long")
        self.assertEqual(len(trace("x" * (reading.MAX_TEXT_CHARS + 1), texts.UBA_RULES)), 0)


class DetectFormatTests(SimpleTestCase):
    OTHER = texts.uba_rules(name="Autre fournisseur")

    def test_the_one_format_whose_start_is_found(self):
        self.assertIs(detect_format(texts.NORMAL.text, [texts.UBA_RULES]), texts.UBA_RULES)
        self.assertIs(
            detect_format(texts.EMPTY.text, [texts.uba_rules(section_start="^NULLE PART"), texts.UBA_RULES]),
            texts.UBA_RULES,
        )

    def test_none_recognises_it(self):
        for text in (texts.JUNK, texts.NO_SECTION.text, "", None):
            with self.subTest(text=text):
                self.assertIsNone(detect_format(text, [texts.UBA_RULES]))
        self.assertIsNone(detect_format(texts.NORMAL.text, []))

    def test_an_inactive_format_or_one_without_a_start_never_takes_part(self):
        self.assertIsNone(detect_format(texts.NORMAL.text, [texts.uba_rules(is_active=False)]))
        self.assertIsNone(detect_format(texts.NORMAL.text, [texts.uba_rules(section_start=" ")]))

    def test_several_is_refused_naming_them(self):
        with self.assertRaises(SlipError) as caught:
            detect_format(texts.NORMAL.text, [texts.UBA_RULES, self.OTHER])
        self.assertEqual(
            caught.exception.message,
            f"Plusieurs formats reconnaissent ce document ({texts.UBA_FORMAT_NAME}, Autre fournisseur) : "
            "choisissez-le dans la liste.",
        )

    def test_a_broken_start_pattern_recognises_nothing(self):
        broken = texts.uba_rules(name="Cassé", section_start="^REPRISE (VIDE")
        self.assertIs(detect_format(texts.NORMAL.text, [broken, texts.UBA_RULES]), texts.UBA_RULES)

    def test_a_slow_start_pattern_stops_the_detection_naming_its_format(self):
        with mock.patch.object(patterns, "compile_field", return_value=[SlowPattern()]):
            with self.assertRaises(SlipError) as caught:
                detect_format(texts.NORMAL.text, [texts.UBA_RULES])
        self.assertIn(f"Le format « {texts.UBA_FORMAT_NAME} » n'a pas pu être essayé", caught.exception.message)
        self.assertIn("est trop lent", caught.exception.message)


class TraceTests(SimpleTestCase):
    def tags_of(self, traced, text: str) -> list:
        (line,) = [line for line in traced if line.text == text]
        return [(tag.label, tag.value) for tag in line.tags]

    def test_every_line_is_there_numbered_from_one(self):
        traced = trace(texts.NORMAL.text, texts.UBA_RULES)
        self.assertEqual([line.text for line in traced], texts.NORMAL.text.split("\n"))
        self.assertEqual([line.number for line in traced], list(range(1, len(traced) + 1)))
        self.assertTrue(traced.reading.all_passed)

    def test_what_each_line_was_read_as(self):
        traced = trace(texts.NORMAL.text, texts.UBA_RULES)
        self.assertEqual(self.tags_of(traced, "REPRISE VIDE"), [("début", "")])
        self.assertEqual(self.tags_of(traced, "FACTURE(S)/BL DU JOUR"), [("fin", "")])
        self.assertEqual(
            self.tags_of(traced, texts.row(texts.KEG, 3, "30.00", "90.00")),
            [("ligne lue", f"{texts.KEG} · quantité 3 · prix 30,00 · montant 90,00")],
        )
        self.assertEqual(
            self.tags_of(traced, "BL No: 610001 du 14/05/2025"), [("date", "14/05/2025"), ("réf.", "610001")]
        )
        self.assertEqual(self.tags_of(traced, "Ticket No : 0000001001"), [("n°", "1001")])
        self.assertEqual(self.tags_of(traced, "Le 14/05/2025 08:15:02"), [("impression", "14/05/2025 08:15")])
        self.assertEqual(self.tags_of(traced, "Deconsigne : -175.00"), [("total", "-175,00")])
        self.assertEqual(self.tags_of(traced, "U.B.A."), [])
        separators = [line for line in traced if line.text == texts.SEPARATOR]
        self.assertEqual([[tag.label for tag in line.tags] for line in separators], [[], ["séparateur"], [], []])
        (read_line,) = [line for line in traced if line.read is not None and line.read.quantity == 3]
        self.assertEqual(read_line.read.as_tuple(), texts.NORMAL.lines[0])

    def test_replaces_remarks_and_unread_lines(self):
        self.assertEqual(
            self.tags_of(trace(texts.REPLACEMENT.text, texts.UBA_RULES), "***ANNULE ET REMPLACE***"), [("remplace", "")]
        )
        anomalies = trace(texts.ANOMALIES.text, texts.UBA_RULES)
        self.assertEqual(sum(1 for line in anomalies if any(tag.label == "remarque" for tag in line.tags)), 3)
        unread = trace(texts.UNREAD.text, texts.UBA_RULES)
        self.assertEqual(self.tags_of(unread, "CASIER DIVERS"), [("non lue", "ne correspond pas au motif de ligne")])

    def test_a_long_line_is_shown_cut(self):
        long_row = f"{texts.KEG} 1 x 30.00 = " + "1" * 500
        traced = trace(f"REPRISE VIDE\n{long_row}", texts.UBA_RULES)
        self.assertEqual(traced.lines[1].text, long_row[:120] + "…")
        self.assertEqual(traced.lines[1].tags[0].label, "non lue")

    def test_a_pattern_that_does_not_compile_draws_the_text_untagged(self):
        traced = trace(texts.NORMAL.text, texts.uba_rules(line_pattern="("))
        self.assertTrue(traced.reading.error)
        self.assertEqual(len(traced), len(texts.NORMAL.text.split("\n")))
        self.assertTrue(all(line.tags == [] for line in traced))


class PdfTextTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def pdf(self, lines, **kwargs) -> bytes:
        path = write_pdf(os.path.join(self.folder, "bon.pdf"), lines, **kwargs)
        with open(path, "rb") as handle:
            return handle.read()

    def test_a_slip_printed_to_pdf_reads_back_as_the_ticket(self):
        for slip in (texts.NORMAL, texts.MIXED, texts.ANOMALIES, texts.REPLACEMENT):
            with self.subTest(slip=slip.name):
                text = pdf_text(self.pdf(texts.pdf_lines(slip.text)))
                result = read_slip_text(text, texts.UBA_RULES)
                self.assertEqual([line.as_tuple() for line in result.lines], slip.lines)
                self.assertEqual(
                    (result.number, result.references, result.replaces), (slip.number, slip.references, slip.replaces)
                )
                self.assertTrue(result.all_passed, result.failed_checks)
        self.assertIn(f"F{U_HAT}T", pdf_text(self.pdf(texts.pdf_lines(texts.NORMAL.text))))

    def test_not_a_pdf(self):
        for content in (b"bonjour", b"%PDF-1.4\nrien de plus", b"", "texte", None):
            with self.subTest(content=content):
                with self.assertRaises(SlipError) as caught:
                    pdf_text(content)
                self.assertEqual(caught.exception.message, "Ce fichier n'est pas un PDF lisible.")

    def test_a_pdf_without_text_is_a_scan(self):
        for kwargs in ({}, {"logo": True}):
            with self.subTest(**kwargs):
                with self.assertRaises(SlipError) as caught:
                    pdf_text(self.pdf([], **kwargs))
                self.assertEqual(
                    caught.exception.message,
                    "Ce PDF n'a pas de texte lisible (scan ou photo) : il ne peut pas être lu comme un bon.",
                )

    def test_too_heavy_is_refused_before_opening(self):
        content = self.pdf(["REPRISE VIDE"])
        with mock.patch.object(reading, "MAX_PDF_BYTES", len(content) - 1), mock.patch("pdfplumber.open") as opened:
            with self.assertRaises(SlipError) as caught:
                pdf_text(content)
        opened.assert_not_called()
        self.assertEqual(caught.exception.message, reading.TOO_HEAVY)
        with mock.patch.object(reading, "MAX_PDF_BYTES", len(content)):
            self.assertEqual(pdf_text(content), "REPRISE VIDE")

    def test_too_many_pages_or_characters_is_too_long(self):
        content = self.pdf(texts.pdf_lines(texts.NORMAL.text))
        with mock.patch.object(reading, "MAX_PDF_PAGES", 0):
            with self.assertRaises(SlipError) as caught:
                pdf_text(content)
        self.assertEqual(caught.exception.message, "Ce document est trop long pour un bon.")
        with mock.patch.object(reading, "MAX_TEXT_CHARS", 50):
            with self.assertRaises(SlipError) as caught:
                pdf_text(content)
        self.assertEqual(caught.exception.message, "Ce document est trop long pour un bon.")

    def test_the_pages_are_counted_before_pdfplumber_opens_the_file(self):
        """`len(pdf.pages)` is pdfplumber making every page of the file - and
        closing the document makes the rest - so a slip of 5 MB carrying some
        33 000 light pages cost about 30 s and 100 MB to refuse, in Achats'
        guard and the Consignes upload alike. Now pdfminer's own walk of the
        page tree stops one page past the cap, and pdfplumber never opens
        the file (review of the HARDEN-01 fix)."""
        from invoices.tests.test_pdf_page_cap import PagesMadeCounter, pdf_of_pages

        content = pdf_of_pages(reading.MAX_PDF_PAGES + 3)
        with mock.patch("pdfplumber.open", side_effect=AssertionError("pdfplumber a ouvert le fichier")) as opened:
            with PagesMadeCounter() as pages, self.assertRaises(SlipError) as caught:
                pdf_text(content)
        opened.assert_not_called()
        self.assertEqual(caught.exception.message, reading.TOO_LONG)
        self.assertEqual(pages.made, reading.MAX_PDF_PAGES + 1)
        # A file declaring fewer pages than its tree holds is walked all the same.
        with mock.patch("pdfplumber.open", side_effect=AssertionError("pdfplumber a ouvert le fichier")):
            with self.assertRaises(SlipError) as caught:
                pdf_text(pdf_of_pages(reading.MAX_PDF_PAGES + 1, declared=1))
        self.assertEqual(caught.exception.message, reading.TOO_LONG)
        # Within the cap, read as before.
        self.assertEqual(pdf_text(pdf_of_pages(reading.MAX_PDF_PAGES)).count("ARTICLE EXEMPLE"), reading.MAX_PDF_PAGES)

    def test_pages_are_read_one_by_one_and_the_cap_stops_the_extraction(self):
        extracted = []

        class Page:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                extracted.append(self.text)
                return self.text

        class Document:
            def __init__(self, pages):
                self.pages = pages

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opened(pages):
            """pdfplumber handing over `pages`, pdfminer's walk counting as many."""
            return mock.patch("pdfplumber.open", return_value=Document(pages)), mock.patch.object(
                reading, "_page_count", return_value=len(pages)
            )

        document, counted = opened([Page("A"), Page(None), Page("B")])
        with document, counted:
            self.assertEqual(pdf_text(b"%PDF-"), "A\n\nB")
        extracted.clear()
        document, counted = opened([Page("x" * 20), Page("never")])
        with mock.patch.object(reading, "MAX_TEXT_CHARS", 10), document, counted:
            with self.assertRaises(SlipError):
                pdf_text(b"%PDF-")
        self.assertEqual(extracted, ["x" * 20])
        # No page at all is a broken file, not a scan - pdfplumber not even opened.
        document, counted = opened([])
        with document as opening, counted:
            with self.assertRaises(SlipError) as caught:
                pdf_text(b"%PDF-")
        self.assertEqual(caught.exception.message, reading.NOT_A_PDF)
        opening.assert_not_called()

    def test_whatever_pdfminer_raises_is_not_readable(self):
        content = self.pdf(["REPRISE VIDE"])
        for error in (KeyError("Root"), RecursionError(), TypeError("x"), ValueError("x")):
            with self.subTest(error=type(error).__name__):
                with mock.patch("pdfplumber.open", side_effect=error):
                    with self.assertRaises(SlipError) as caught:
                        pdf_text(content)
                self.assertEqual(caught.exception.message, reading.NOT_A_PDF)


#: What the hand-built PDFs below print, one line per content stream.
DRAWN = b"BT /F1 10 Tf 40 700 Td (REPRISE VIDE) Tj ET\n"
DRAWN_LOWER = b"BT /F1 10 Tf 40 600 Td (FACTURE) Tj ET\n"


def pdf_with_streams(streams) -> bytes:
    """A one-page PDF built by hand (as invoices/tests/pdf_files does) whose
    page content is `streams`: (filter names, raw bytes) pairs, such as
    (["FlateDecode", "FlateDecode"], zlib.compress(zlib.compress(data)))."""
    font = 4 + len(streams)
    contents = " ".join(f"{4 + index} 0 R" for index in range(len(streams)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents [{contents}] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>"
        ).encode(),
    ]
    for filters, raw in streams:
        names = " ".join(f"/{name}" for name in filters).encode()
        objects.append(
            b"<< /Filter [" + names + b"] /Length " + str(len(raw)).encode() + b" >>\nstream\n" + raw + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(output)


class InflateBoundTests(SimpleTestCase):
    """The 5 MB cap is on the COMPRESSED bytes: deflate reaches about
    1000:1, and two Flate filters chained far more, so a spoofed mail's
    4.9 MB slip inflated to gigabytes inside pdfminer before any page or
    character cap applied. Every pdfminer decode of a compressing filter
    (Flate, its retry of a damaged stream, LZW, RunLength) is bounded
    process-wide - Achats' own pdfplumber pass included - and one slip's
    streams share one budget. MACHINE SAFETY: the bounds are patched DOWN;
    nothing here inflates past a few hundred KB."""

    def test_a_stream_inflating_past_its_bound_is_too_long_and_never_inflated_whole(self):
        bomb = pdf_with_streams(
            [(["FlateDecode", "FlateDecode"], zlib.compress(zlib.compress(DRAWN + b" " * 200_000)))]
        )
        self.assertLess(len(bomb), 2_000)
        with mock.patch.object(reading, "MAX_INFLATE_STAGE", 4_096):
            with self.assertRaises(SlipError) as caught:
                pdf_text(bomb)
        self.assertEqual(caught.exception.message, reading.TOO_LONG)
        # Within its bound, the same file reads: a real slip is never refused.
        self.assertEqual(pdf_text(bomb), "REPRISE VIDE")

    def test_the_bound_holds_for_every_pdfminer_reader_not_only_the_slips(self):
        """Achats' text layer opens PDFs through pdfplumber too."""
        bomb = pdf_with_streams(
            [(["FlateDecode", "FlateDecode"], zlib.compress(zlib.compress(DRAWN + b" " * 200_000)))]
        )
        with mock.patch.object(reading, "MAX_INFLATE_STAGE", 4_096):
            with self.assertRaises(Exception) as caught, pdfplumber.open(io.BytesIO(bomb)) as pdf:
                pdf.pages[0].extract_text()
        # pdfplumber re-raises it as its own PdfminerException.
        self.assertTrue(reading.inflate_refused(caught.exception), repr(caught.exception))
        self.assertFalse(reading.inflate_refused(ValueError("autre chose")))

    def test_the_bounded_inflate_says_what_zlib_says(self):
        data = zlib.compress(b"x" * 10_000)
        self.assertEqual(reading.bounded_decompress(data), b"x" * 10_000)
        with mock.patch.object(reading, "MAX_INFLATE_STAGE", 10_000):
            self.assertEqual(len(reading.bounded_decompress(data)), 10_000)
        with mock.patch.object(reading, "MAX_INFLATE_STAGE", 9_999):
            with self.assertRaises(reading.InflateLimit):
                reading.bounded_decompress(data)
        # Never a zlib.error: pdfminer answers one with a retry of its own.
        self.assertFalse(issubclass(reading.InflateLimit, zlib.error))
        # What zlib.decompress refuses, refused the same way.
        for broken in (b"", data[:-4] + b"\0\0\0\0", data[: len(data) // 2], b"pas du zlib"):
            with self.subTest(broken=broken[:12]), self.assertRaises(zlib.error):
                reading.bounded_decompress(broken)

    def test_a_damaged_stream_is_still_read_through_pdfminer_s_retry_bounded_too(self):
        damaged = zlib.compress(DRAWN)[:-4] + b"\0\0\0\0"  # its checksum wrong
        with self.assertLogs("pdfminer.pdftypes", level="WARNING") as logged:
            self.assertEqual(pdf_text(pdf_with_streams([(["FlateDecode"], damaged)])), "REPRISE VIDE")
        self.assertIn("Data-loss while decompressing corrupted data", logged.output[0])
        # The retry feeds zlib one byte at a time: the bound holds there too.
        with mock.patch.object(reading, "MAX_INFLATE_STAGE", 100):
            retry = pdftypes.zlib.decompressobj()
            with self.assertRaises(reading.InflateLimit):
                for byte in zlib.compress(b"x" * 1_000):
                    retry.decompress(bytes([byte]))

    def test_the_streams_of_one_slip_share_one_budget(self):
        two = pdf_with_streams(
            [
                (["FlateDecode"], zlib.compress(DRAWN + b" " * 3_000)),
                (["FlateDecode"], zlib.compress(DRAWN_LOWER + b" " * 3_000)),
            ]
        )
        with (
            mock.patch.object(reading, "MAX_INFLATE_STAGE", 4_096),
            mock.patch.object(reading, "MAX_INFLATE_TOTAL", 6_000),
        ):
            with self.assertRaises(SlipError) as caught:
                pdf_text(two)
            self.assertEqual(caught.exception.message, reading.TOO_LONG)
        with (
            mock.patch.object(reading, "MAX_INFLATE_STAGE", 4_096),
            mock.patch.object(reading, "MAX_INFLATE_TOTAL", 7_000),
        ):
            # One budget per slip: the second reading starts afresh.
            for _ in range(2):
                self.assertEqual(pdf_text(two), "REPRISE VIDE\nFACTURE")

    def test_lzw_is_bounded(self):
        self.assertEqual(pdftypes.lzwdecode(b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01"), b"-----A---B")

        def run(decoder):
            for _ in range(20):  # 20 000 bytes: finite, whatever the bound
                yield b"x" * 1_000

        with mock.patch("pdfminer.lzw.LZWDecoder.run", run), mock.patch.object(reading, "MAX_INFLATE_STAGE", 10_000):
            with self.assertRaises(reading.InflateLimit):
                pdftypes.lzwdecode(b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01")

    def test_run_length_is_bounded_by_what_it_gives(self):
        runs = bytes([129, ord("x")]) * 20 + b"\x80"  # 41 bytes giving 20 × 128
        self.assertEqual(pdftypes.rldecode(runs), b"x" * 2_560)
        with mock.patch.object(reading, "MAX_INFLATE_STAGE", 2_600):
            self.assertEqual(len(pdftypes.rldecode(runs)), 2_560)
        with mock.patch.object(reading, "MAX_INFLATE_STAGE", 2_559):
            with self.assertRaises(reading.InflateLimit):
                pdftypes.rldecode(runs)
