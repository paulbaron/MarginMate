"""config.language.ActiveLanguageMiddleware: a request runs with the
language it already had made ACTIVE - for speed only, so nothing a page says
may change."""

import re
from datetime import date

from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import translation
from django.utils.translation import trans_real

from config.language import ActiveLanguageMiddleware
from tests.factories import make_invoice_line


def _seen(request):
    """What a view sees: the language, and the catalogue gettext reads."""
    return HttpResponse(f"{translation.get_language()}|{id(trans_real.catalog())}")


class ActiveLanguageTests(SimpleTestCase):
    def setUp(self):
        self.addCleanup(translation.deactivate)
        translation.deactivate()

    def run_request(self):
        return ActiveLanguageMiddleware(_seen)(RequestFactory().get("/")).content.decode()

    def test_the_view_sees_the_language_and_catalogue_it_had(self):
        default = trans_real.translation(settings.LANGUAGE_CODE)
        self.assertIs(trans_real.catalog(), default)
        self.assertEqual(self.run_request(), f"{settings.LANGUAGE_CODE}|{id(default)}")

    def test_a_language_already_active_stays(self):
        with translation.override("fr"):
            self.assertEqual(self.run_request(), f"fr|{id(trans_real.translation('fr'))}")
            self.assertEqual(translation.get_language(), "fr")

    def test_a_french_override_inside_still_nests(self):
        """accounts.pages renders the login in French inside the request."""

        def french(request):
            with translation.override("fr"):
                inside = translation.get_language()
            return HttpResponse(f"{inside}|{translation.get_language()}")

        answer = ActiveLanguageMiddleware(french)(RequestFactory().get("/")).content.decode()
        self.assertEqual(answer, f"fr|{settings.LANGUAGE_CODE}")

    def test_after_the_request_the_same_language(self):
        self.run_request()
        self.assertEqual(translation.get_language(), settings.LANGUAGE_CODE)
        self.assertIs(trans_real.catalog(), trans_real.translation(settings.LANGUAGE_CODE))

    def test_it_is_installed(self):
        self.assertIn("config.language.ActiveLanguageMiddleware", settings.MIDDLEWARE)


CSRF = re.compile(r'name="csrfmiddlewaretoken" value="[^"]+"')


class PagesReadTheSameTests(TestCase):
    """The same page, byte for byte, with and without the middleware: dates,
    amounts, integers and links included."""

    def page(self, url):
        return CSRF.sub("csrf", self.client.get(url).content.decode())

    def test_a_page_of_documents(self):
        for n in range(3):
            line = make_invoice_line(total_ht=f"{1234 + n}.5", quantity=n + 2)
            line.invoice.invoice_date = date(2026, 3, 9 + n)
            line.invoice.save()
        for url in (reverse("invoices:invoice_list"), reverse("inventory:stock_list")):
            with self.subTest(url=url):
                self.page(url)  # what a first visit writes (the review panel's suggestions)
                active = self.page(url)
                with self.modify_settings(MIDDLEWARE={"remove": ["config.language.ActiveLanguageMiddleware"]}):
                    translation.deactivate()
                    defaulted = self.page(url)
                self.assertIn("09/03/2026", active)
                self.assertEqual(active, defaulted)
