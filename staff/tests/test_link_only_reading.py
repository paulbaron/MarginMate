"""Why « Voir le PDF » (document/) needs the link and not the code
(security audit ANON-6, its page half - decided NOT to gate it, 29/09).

The audit offered, as optional hardening, to require the verified code for
the frozen PDF as well. Two facts, each pinned here, say it would protect
nothing and break the flow:

* the month's PAGE, which the link opens without a code by design, already
  shows everything the PDF holds - the employee's name, the establishment,
  its address, every day's hours and notes (it is drawn from the same
  snapshot: « what his phone shows is what he signs »);
* once he has signed, no code can be issued any more (`issue_code` wants a
  waiting request), yet the page still offers « Voir le PDF » and his copy
  until the link expires: a code gate would lock him out of the document
  he signed, from any browser but the one he signed in.

The log half (the token in server logs) is the logging filter's
(config, H2). Names invented."""

import io
import re
from html import unescape

import pdfplumber
from django.test import Client

from staff import signature_requests as requests_
from staff.tests.test_sign_public import IP, PHONE, PublicCase


def pdf_text(data: bytes) -> str:
    with pdfplumber.open(io.BytesIO(data)) as document:
        return " ".join(" ".join(page.extract_text() or "" for page in document.pages).split())


class LinkOnlyReadingTests(PublicCase):
    def test_the_page_opened_without_a_code_already_shows_what_the_pdf_holds(self):
        page_text = self.text(self.get())
        pdf = pdf_text(self.get(self.route("staff:sign_document")).content)
        for fact in ("DUPONT Jeanne", "BAR EXEMPLE", "12 rue Imaginaire", "inventaire", "juin 2026"):
            with self.subTest(fact=fact):
                self.assertIn(fact, pdf)
                self.assertIn(fact, page_text)
        self.assertNotIn("Code vérifié", page_text)

    def test_once_signed_another_browser_reads_the_pdf_and_no_code_can_be_had(self):
        self.sign(self.identify())
        self.refresh()
        self.assertEqual(self.request.status, requests_.Status.EMPLOYEE_SIGNED)
        other = Client(enforce_csrf_checks=True, REMOTE_ADDR=IP, HTTP_USER_AGENT=PHONE)
        page = other.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'href="{self.route("staff:sign_document")}"', page.content.decode())
        self.assertIn("Voir le PDF", " ".join(unescape(re.sub(r"<[^>]+>", " ", page.content.decode())).split()))
        self.assertEqual(other.get(self.route("staff:sign_document")).status_code, 200)
        with self.assertRaises(requests_.RequestStateError):
            requests_.issue_code(self.request, requests_.Identification.CODE_HANDED_OVER)
