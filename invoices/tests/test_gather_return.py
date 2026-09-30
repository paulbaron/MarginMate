"""« Récupérer » asked from another page comes back to it.

The Consignes page's « Récupérer les bons » posts to Achats' own gather view
(invoices:gather) with `retour=/consignes/`: every rule of the gather stays
that view's - the integrations refusal, the reaper, one gather at a time,
nothing ticked - and only where it answers changes. A `retour` naming another
site is never followed (an open redirect is a link anybody can send the
owner). Data invented.
"""

from unittest import mock

from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from common import local_return
from invoices.models import ScrapeJob


class LocalReturnTests(SimpleTestCase):
    def returned(self, method="post", **data):
        request = getattr(RequestFactory(), method)("/achats/recuperer/", data)
        return local_return(request)

    def test_a_path_of_this_site_is_kept(self):
        self.assertEqual(self.returned(retour="/consignes/"), "/consignes/")
        self.assertEqual(self.returned(retour="/consignes/?enregistree=1"), "/consignes/?enregistree=1")
        self.assertEqual(self.returned("get", retour="/consignes/"), "/consignes/")

    def test_anything_else_is_nothing(self):
        for target in ("", "https://example.invalid/", "//example.invalid/x", "consignes/", "/\\example.invalid",
                       "javascript:alert(1)", "http://testserver/consignes/"):
            with self.subTest(target=target):
                self.assertEqual(self.returned(retour=target), "")
        self.assertEqual(self.returned(), "")


class GatherReturnTests(TestCase):
    def post(self, **data):
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.client.post(reverse("invoices:gather"), {
                "start_date": "2026-06-01", "end_date": "2026-09-20", **data,
            })
        return response, thread

    def achats(self):
        return f"{reverse('invoices:invoice_list')}?ajouter=recuperer"

    def test_a_gather_asked_from_consignes_comes_back_there(self):
        response, thread = self.post(sources=["bons-1"], retour="/consignes/")
        self.assertEqual(response["Location"], "/consignes/")
        thread.return_value.start.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["args"][3], {"bons-1"})

    def test_a_foreign_return_goes_to_achats(self):
        response, thread = self.post(sources=["bons-1"], retour="https://example.invalid/consignes/")
        self.assertEqual(response["Location"], self.achats())
        thread.return_value.start.assert_called_once()

    def test_without_one_it_is_achats_as_before(self):
        response, _thread = self.post(sources=["type-1"])
        self.assertEqual(response["Location"], self.achats())

    def test_one_already_running_is_said_where_it_was_asked(self):
        ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now())
        response, thread = self.post(sources=["bons-1"], retour="/consignes/")
        self.assertEqual(response["Location"], "/consignes/")
        thread.assert_not_called()
        self.assertEqual(ScrapeJob.objects.count(), 1)
        page = self.client.get(self.achats())
        self.assertContains(page, "Une récupération est déjà en cours")

    def test_nothing_ticked_is_refused_there_too(self):
        response, thread = self.post(retour="/consignes/")
        self.assertEqual(response["Location"], "/consignes/")
        thread.assert_not_called()
        self.assertFalse(ScrapeJob.objects.exists())

    def test_a_get_starts_nothing_and_goes_to_achats(self):
        response = self.client.get(reverse("invoices:gather") + "?retour=/consignes/")
        self.assertRedirects(response, reverse("invoices:invoice_list"), fetch_redirect_response=False)
        self.assertFalse(ScrapeJob.objects.exists())
