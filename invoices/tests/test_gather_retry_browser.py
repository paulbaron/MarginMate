"""The gather button, in a real (headless) Chrome: disabled while a gather
runs, usable again the moment the status card says it is over - without
reloading the page, which also put back the default start date.

The gather itself never runs: the job is flipped by the test, and the task
a click starts is replaced (never a real mailbox, Metro or portal).

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Data invented.
"""

import tempfile
from unittest import mock

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse
from django.utils import timezone

from invoices.models import InvoiceType, ScrapeJob
from invoices.scrapers import website
from tests.factories import make_invoice_type, make_supplier

WAIT_SECONDS = 10


@tag("browser")
class GatherRetryInBrowserTests(StaticLiveServerTestCase):
    # Its flush then fires no post_migrate: recreated content types broke
    # every later class restoring its snapshot (tests/test_transaction_cases.py).
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.driver = website.build_chrome(tempfile.mkdtemp(), True)
        except Exception as exc:  # noqa: BLE001
            cls.tearDownClass()
            raise cls.skipTest(cls, f"Chrome indisponible : {exc}")

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        super().tearDownClass()

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, WAIT_SECONDS).until(lambda driver: condition())

    def state(self):
        return self.driver.execute_script(
            "const button = document.querySelector('#import-recuperer button[type=submit]');"
            "const dot = document.querySelector('#ajouter .live-dot');"
            "return {disabled: button.disabled, dot: !!dot && !dot.hidden,"
            " status: (document.querySelector('#gather-status .status-pill') || {}).textContent};"
        )

    def setUp(self):
        # A source of its own to gather: the seeded ones are gone after any
        # live-server test ran before this one (each empties the database),
        # and the form then had nothing to send.
        if not InvoiceType.objects.filter(is_active=True).exists():
            make_invoice_type(
                supplier=make_supplier(code="GROSSISTE_X", name="Grossiste Exemple", parser_key=""),
                name="Grossiste Exemple - Factures",
                sender_pattern="factures@grossiste",
            )

    def test_the_button_comes_back_when_the_gather_ends(self):
        job = ScrapeJob.objects.create(
            kind=ScrapeJob.Kind.GATHER, status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now()
        )
        self.driver.get(self.live_server_url + reverse("invoices:invoice_list") + "?ajouter=recuperer")
        self.assertEqual(self.state()["disabled"], True)
        self.assertEqual(self.state()["dot"], True)

        ScrapeJob.objects.filter(pk=job.pk).update(status=ScrapeJob.Status.FAILED, finished_at=timezone.now())
        self.wait_for(lambda: self.state()["disabled"] is False)
        self.assertEqual(self.state()["dot"], False)

        with mock.patch("invoices.views.gather_invoices_task"), mock.patch("invoices.tasks.gather_invoices_task"):
            self.driver.find_element("css selector", "#import-recuperer button[type=submit]").click()
            self.wait_for(lambda: ScrapeJob.objects.count() == 2)
