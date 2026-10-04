"""The AI reading (« Autre (analyse IA) », parsers/llm_fallback.py), open to
every bar with its own key: what keeps it from holding the server, and what
the bar is told.

- One call is bounded (TIMEOUT_SECONDS, no retry) and two run at once on the
  server, a third refused at once (AI_BUSY) - never waited for.
- Every answer Anthropic can give is a fixed French sentence
  (AiReadingRefused), said as it is on the page, never « Erreur inattendue ».
- Outside the platform owner's espace « Analyse IA » is offered once a key
  is on « Identifiants », and refused before the upload without one.

Nothing reaches Anthropic: the SDK is a stand-in module holding the same
names. Data invented.
"""

from __future__ import annotations

import sys
import threading
import types
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts import vault
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from invoices import integrations
from invoices.integrations import AiReadingRefused
from invoices.models import Invoice, Supplier
from invoices.parsers import LLM_PARSER_KEY, llm_fallback
from invoices.parsers.llm_fallback import LLMFallbackParser


def in_another_thread(work) -> None:
    """`work()` in a thread of its own, as another request is: one thread
    binds one espace at a time. Its exception, if any, is raised here."""
    failed = []

    def run():
        try:
            work()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            failed.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    if failed:
        raise failed[0]


def fake_anthropic(raised=None):
    """A stand-in for the SDK: its error classes by their names, and a client
    whose call raises `raised` (an instance) - or records and answers."""
    module = types.ModuleType("anthropic")

    class AnthropicError(Exception):
        pass

    class APIError(AnthropicError):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(APIError):
        status_code = 0

    names = {
        "AnthropicError": AnthropicError,
        "APIError": APIError,
        "APIConnectionError": APIConnectionError,
        "APITimeoutError": APITimeoutError,
        "APIStatusError": APIStatusError,
    }
    for name, status in (
        ("BadRequestError", 400),
        ("AuthenticationError", 401),
        ("PermissionDeniedError", 403),
        ("NotFoundError", 404),
        ("RateLimitError", 429),
        ("InternalServerError", 500),
        ("ServiceUnavailableError", 503),
        ("OverloadedError", 529),
    ):
        names[name] = type(name, (APIStatusError,), {"status_code": status})
    for name, kind in names.items():
        setattr(module, name, kind)
    module.calls = []

    class Anthropic:
        def __init__(self, **kwargs):
            module.calls.append(kwargs)
            self.messages = self

        def create(self, **kwargs):
            module.calls.append(kwargs)
            if raised is not None:
                raise raised(module) if callable(raised) and not isinstance(raised, BaseException) else raised
            block = types.SimpleNamespace(type="tool_use", input={"invoice_number": "F-1", "lines": []})
            return types.SimpleNamespace(content=[block])

    module.Anthropic = Anthropic
    return module


@override_settings(ANTHROPIC_API_KEY="cle-essai")
class ParserTests(SimpleTestCase):
    """In the test espace, the platform owner's: the server's key stands in."""

    def parse(self, module):
        with (
            mock.patch.dict(sys.modules, {"anthropic": module}),
            mock.patch("invoices.parsers.llm_fallback._extract_text", return_value="FACTURE ESSAI"),
        ):
            return LLMFallbackParser().parse("facture.pdf")

    def test_one_call_is_bounded_and_never_retried(self):
        module = fake_anthropic()
        self.parse(module)
        self.assertEqual(module.calls[0], {"api_key": "cle-essai", "timeout": 60.0, "max_retries": 0})
        # The model and the forced tool are unchanged.
        self.assertEqual(module.calls[1]["model"], "claude-sonnet-5")
        self.assertEqual(module.calls[1]["tool_choice"], {"type": "tool", "name": "record_invoice"})

    def test_every_answer_of_anthropic_is_a_sentence(self):
        for name, sentence in (
            ("AuthenticationError", integrations.AI_KEY_REFUSED),
            ("PermissionDeniedError", integrations.AI_KEY_REFUSED),
            ("RateLimitError", integrations.AI_RATE_LIMITED),
            ("BadRequestError", integrations.AI_BAD_REQUEST),
            ("NotFoundError", integrations.AI_MODEL_GONE),
            ("InternalServerError", integrations.AI_UNAVAILABLE),
            ("ServiceUnavailableError", integrations.AI_UNAVAILABLE),
            ("OverloadedError", integrations.AI_UNAVAILABLE),
            ("APIConnectionError", integrations.AI_NO_ANSWER),
            ("APITimeoutError", integrations.AI_NO_ANSWER),
        ):
            with self.subTest(error=name):
                module = fake_anthropic(lambda module, name=name: getattr(module, name)("in English"))
                with self.assertRaises(AiReadingRefused) as refused:
                    self.parse(module)
                self.assertEqual(str(refused.exception), sentence)
        # A status the SDK has no class for: by its code.
        for status, sentence in ((502, integrations.AI_UNAVAILABLE), (418, integrations.AI_BAD_REQUEST)):
            with self.subTest(status=status):

                def other(module, status=status):
                    error = module.APIStatusError("in English")
                    error.status_code = status
                    return error

                with self.assertRaises(AiReadingRefused) as refused:
                    self.parse(fake_anthropic(other))
                self.assertEqual(str(refused.exception), sentence)

    def test_anything_else_is_raised_as_it_is(self):
        with self.assertRaises(ValueError):
            self.parse(fake_anthropic(ValueError("autre chose")))

    def test_two_at_a_time_a_third_is_refused_at_once(self):
        taken = []
        try:
            for _slot in range(llm_fallback.AI_SLOTS):
                self.assertTrue(llm_fallback._SLOTS.acquire(blocking=False))
                taken.append(True)
            module = fake_anthropic()
            with self.assertRaises(AiReadingRefused) as refused:
                self.parse(module)
            self.assertEqual(str(refused.exception), integrations.AI_BUSY)
            self.assertEqual(module.calls, [])
        finally:
            for _slot in taken:
                llm_fallback._SLOTS.release()
        # Given back after a call, refused or not.
        self.parse(fake_anthropic())
        with self.assertRaises(AiReadingRefused):
            self.parse(fake_anthropic(lambda module: module.RateLimitError("x")))
        self.assertEqual(llm_fallback._SLOTS._value, llm_fallback.AI_SLOTS)


class OwnerUploadTests(TestCase):
    """The platform owner's espace (the test one) is offered « Analyse IA » as
    before, key or not - and an upload without one now says why."""

    def test_no_key_is_said_not_an_unexpected_error(self):
        ai = Supplier.objects.get(parser_key=LLM_PARSER_KEY)
        self.assertContains(self.client.get(reverse("invoices:invoice_list")), '<optgroup label="Analyse IA">')
        upload = SimpleUploadedFile("facture.pdf", b"%PDF-1.4 essai", content_type="application/pdf")
        with (
            mock.patch("invoices.ocr.check_page_count"),
            mock.patch("invoices.receipts.route_to_returnables"),
            mock.patch("invoices.parsers.llm_fallback._extract_text", return_value="FACTURE"),
        ):
            response = self.client.post(
                reverse("invoices:invoice_upload"), {"supplier": str(ai.pk), "source_file": upload}, follow=True
            )
        self.assertContains(response, "L&#x27;analyse IA demande une clé d&#x27;API Anthropic")
        self.assertFalse(Invoice.objects.exists())


class HostedUploadTests(TwoTenantsTestCase):
    """Bar Beta, another bar: « Analyse IA » once its key is on its page."""

    owner_a = True

    def parse(self, tenant, module):
        with (
            bound_tenant(tenant),
            mock.patch.dict(sys.modules, {"anthropic": module}),
            mock.patch("invoices.parsers.llm_fallback._extract_text", return_value="FACTURE ESSAI"),
        ):
            return LLMFallbackParser().parse("facture.pdf")

    def test_one_reading_at_a_time_for_another_bar_never_both_slots(self):
        """Two uploads of one bar took both of the server's slots, and every
        other bar was told the server was busy."""
        self.bar_c = self.make_tenant("Bar Gamma")
        for tenant, key in ((self.bar_b, "cle-beta"), (self.bar_c, "cle-gamma")):
            with bound_tenant(tenant):
                vault.save({"ANTHROPIC_API_KEY": key})
        inner = {}

        def readings():
            try:
                self.parse(self.bar_b, fake_anthropic())
            except AiReadingRefused as refused:
                inner["beta"] = str(refused)
            inner["gamma"] = self.parse(self.bar_c, fake_anthropic()).invoice_number

        def second_reading_inside(module):
            # While Beta's first reading runs, from other request threads:
            # Beta's second is refused, Gamma's goes through.
            in_another_thread(readings)
            return module.RateLimitError("x")

        with self.assertRaises(AiReadingRefused):
            self.parse(self.bar_b, fake_anthropic(second_reading_inside))
        self.assertEqual(inner, {"beta": integrations.AI_BUSY_HERE, "gamma": "F-1"})
        # Every slot is given back, refused or not.
        self.assertEqual(llm_fallback._RUNNING, {})
        self.assertEqual(llm_fallback._SLOTS._value, llm_fallback.AI_SLOTS)
        self.assertEqual(self.parse(self.bar_b, fake_anthropic()).invoice_number, "F-1")

    def test_the_owner_s_readings_are_not_counted_per_espace(self):
        inner = {}

        def reading():
            inner["owner"] = self.parse(self.bar_a, fake_anthropic()).invoice_number

        def second_reading_inside(module):
            in_another_thread(reading)
            return module.RateLimitError("x")

        with override_settings(ANTHROPIC_API_KEY="cle-essai"), self.assertRaises(AiReadingRefused):
            self.parse(self.bar_a, fake_anthropic(second_reading_inside))
        self.assertEqual(inner, {"owner": "F-1"})

    def test_offered_once_a_key_is_typed_and_refused_before_the_upload_without(self):
        with bound_tenant(self.bar_b):
            ai = Supplier.objects.get(parser_key=LLM_PARSER_KEY)
        self.client.force_login(self.user_b)
        page = self.client.get(reverse("invoices:invoice_list"))
        self.assertNotContains(page, '<optgroup label="Analyse IA">')
        self.assertContains(page, "renseignez-la sur la page Identifiants")
        upload = SimpleUploadedFile("facture.pdf", b"%PDF-1.4 essai", content_type="application/pdf")
        with mock.patch("invoices.views.parse_and_import") as parse:
            response = self.client.post(
                reverse("invoices:invoice_upload"), {"supplier": str(ai.pk), "source_file": upload}
            )
        parse.assert_not_called()
        self.assertContains(response, "L&#x27;analyse IA demande une clé d&#x27;API Anthropic")
        with bound_tenant(self.bar_b):
            vault.save({"ANTHROPIC_API_KEY": "cle-beta"})
        self.assertContains(self.client.get(reverse("invoices:invoice_list")), '<optgroup label="Analyse IA">')
