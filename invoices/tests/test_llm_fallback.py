"""« Autre (analyse IA) »: what the AI's answer is turned into. Its tool's
schema is a request, not a guarantee - an answer cut off at its length
limit, a count of 0,5 or « 2.5 », a line without a name - and none of it may
become a purchase filed silently wrong or a failure nobody can read. The
API is replaced (never called). Data invented."""

import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from invoices.parsers.llm_fallback import AIReadingRefused, LLMFallbackParser
from invoices.receipt_batches import READING_REFUSALS

D = Decimal


def answer(lines=(), stop_reason="tool_use", content=None, **fields):
    data = {"invoice_number": "F-0001", "invoice_date": "2026-09-04", "lines": list(lines), **fields}
    blocks = [SimpleNamespace(type="tool_use", input=data)] if content is None else content
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


@override_settings(ANTHROPIC_API_KEY="cle-essai")
class AIAnswerTests(SimpleTestCase):
    def read(self, response):
        anthropic = mock.Mock()
        anthropic.Anthropic.return_value.messages.create.return_value = response
        with (
            mock.patch.dict(sys.modules, {"anthropic": anthropic}),
            mock.patch("accounts.tenancy.integrations_allowed", return_value=True),
            mock.patch("invoices.parsers.llm_fallback._extract_text", return_value="FACTURE ESSAI"),
        ):
            return LLMFallbackParser().parse("facture.pdf")

    def test_an_answer_cut_off_files_nothing_and_says_why(self):
        """At its length limit the tool's input is still a valid object - the
        lines before the cut. Filed, a long invoice lost its last lines with
        nothing said."""
        line = {"name": "VIN ROUGE", "quantity": 6, "total_price_ht": 42.0}
        with self.assertRaises(AIReadingRefused) as refused:
            self.read(answer([line, {"name": "VIN BL"}], stop_reason="max_tokens"))
        self.assertIn("trop longue", str(refused.exception))
        # Said on the page in these words, not as « Erreur inattendue ».
        self.assertIsInstance(refused.exception, READING_REFUSALS)

    def test_an_answer_without_the_invoice_is_said(self):
        """No tool call in it (a refusal): StopIteration came out of the
        reading."""
        with self.assertRaises(AIReadingRefused):
            self.read(answer(content=[SimpleNamespace(type="text", text="Je ne peux pas.")], stop_reason="refusal"))
        with self.assertRaises(AIReadingRefused):
            self.read(answer([{"name": "VIN ROUGE", "quantity": 6, "total_price_ht": 42.0}], stop_reason="refusal"))

    def test_a_count_that_is_a_fraction_is_kept(self):
        """0,5 kg divided the price by int(0.5) = 0; « 2.5 » raised; 2.5 came
        out as 2, the unit cost a quarter too high."""
        invoice = self.read(
            answer(
                [
                    {"name": "FROMAGE", "quantity": 0.5, "total_price_ht": 4},
                    {"name": "CITRONS", "quantity": "2.5", "total_price_ht": "10"},
                    {"name": "OLIVES", "quantity": 2.5, "total_price_ht": 5},
                ]
            )
        )
        self.assertEqual(
            [(line.quantity, line.unit_cost_ht) for line in invoice.lines],
            [(D("0.5"), D("8")), (D("2.5"), D("4")), (D("2.5"), D("2"))],
        )
        self.assertEqual(invoice.warnings, [])

    def test_a_whole_count_stays_a_count(self):
        invoice = self.read(answer([{"name": "VIN ROUGE", "quantity": 3, "total_price_ht": 42.0}]))
        (line,) = invoice.lines
        self.assertEqual((line.quantity, line.unit_cost_ht, line.total_ht), (3, D("14"), D("42.0")))
        self.assertIsInstance(line.quantity, int)
        self.assertEqual((invoice.invoice_number, str(invoice.invoice_date)), ("F-0001", "2026-09-04"))

    def test_a_line_without_a_name_or_a_price_is_said(self):
        """A nameless line raised KeyError; one without a price was filed at
        0,00 €, its stock at no cost. Both are said, and the invoice waits in
        « À vérifier »."""
        invoice = self.read(
            answer(
                [
                    {"quantity": 1, "total_price_ht": 3},
                    {"name": "VIN BLANC", "quantity": 2},
                    {"name": "BIERE", "quantity": "deux", "total_price_ht": 8},
                    "BIERE 8,00",
                ]
            )
        )
        self.assertEqual([line.raw_name for line in invoice.lines], ["VIN BLANC", "BIERE"])
        self.assertEqual(len(invoice.warnings), 2)
        self.assertIn("2 ligne(s)", invoice.warnings[0])
        self.assertIn("VIN BLANC, BIERE", invoice.warnings[1])

    def test_a_price_that_does_not_read_is_said(self):
        """« 12,50 » or « N/A » read as 0: the line was filed at 0,00 € with
        nothing said, like a missing price. A price of 0 given as 0 is one."""
        invoice = self.read(
            answer(
                [
                    {"name": "VIN BLANC", "quantity": 2, "total_price_ht": "12,50"},
                    {"name": "BIERE", "quantity": 1, "total_price_ht": "N/A"},
                    {"name": "GOBELETS", "quantity": 1, "total_price_ht": 0},
                    {"name": "PAILLES", "quantity": 1, "total_price_ht": "0.00"},
                ]
            )
        )
        self.assertEqual(len(invoice.lines), 4)
        self.assertEqual(invoice.warnings, ["Analyse IA : quantité ou prix illisible pour VIN BLANC, BIERE."])

    def test_a_date_that_is_no_text_falls_back_on_the_hint(self):
        invoice = self.read(answer([], invoice_date=20260904))
        self.assertIsNone(invoice.invoice_date)
