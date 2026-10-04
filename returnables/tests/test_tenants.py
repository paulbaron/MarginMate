"""« Consignes » with one database per bar (multi mode, accounts/tenancy.py).

Two real tenants in temporary files (accounts.tests.support.TwoTenantsTestCase):
Bar Alpha is the platform owner's tenant, Bar Beta another bar - each
fetches its slips from its own mailbox (its « Identifiants »). Both databases number their rows from 1, so A's pickup and B's are
both pk 1, their slips and photos too - exactly what a page reading the wrong
database, or a photo served from the wrong folder, would mix up without a
word. Every name, number and count INVENTED; no mail server, no SMTP: the
gather's thread is patched, and a client class reaching for either fails.
"""

from __future__ import annotations

from datetime import date
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts import paths
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from invoices import integrations
from invoices.models import ScrapeJob
from invoices.tasks import gather_invoices_task
from returnables.models import Pickup, PickupPhoto, Slip
from returnables.tests.support import make_pickup, make_slip, make_supplier, seeded_format, tiny_jpeg
from staff.tests.page_forms import as_post, form_posting_to, forms_of
from tests.support import _Forbidden

HOME = "/consignes/"


class TenantsCase(TwoTenantsTestCase):
    owner_a = True

    def setUp(self):
        super().setUp()
        for target, label in (
            ("imaplib.IMAP4_SSL", "IMAP"),
            ("imaplib.IMAP4", "IMAP"),
            ("smtplib.SMTP", "SMTP"),
            ("smtplib.SMTP_SSL", "SMTP"),
        ):
            patcher = mock.patch(target, new=_Forbidden(label))
            patcher.start()
            self.addCleanup(patcher.stop)

    def fill(self, tenant, word, day):
        """A supplier, a pickup with a photo, a slip - named after `word`."""
        with bound_tenant(tenant):
            supplier = make_supplier(f"Grossiste {word}")
            pickup = make_pickup(date=day, supplier=supplier, counts={"Fûts": 4}, note=f"Vides {word}", photos=1)
            slip = make_slip(number=f"{len(word)}0{day.day:02d}", delivery_date=day)
            photo = pickup.photos.get()
            return {"pickup": pickup, "slip": slip, "photo": photo, "image": photo.image.name, "url": photo.image.url}

    def media_of(self, tenant):
        return paths.tenant_dir(tenant) / "media"


class EachBarSeesItsOwnTests(TenantsCase):
    def setUp(self):
        super().setUp()
        self.alpha = self.fill(self.bar_a, "Alpha", date(2026, 2, 10))
        self.beta = self.fill(self.bar_b, "Beta", date(2026, 2, 11))

    def test_both_databases_number_from_one(self):
        """What every test below relies on: the same pks on both sides."""
        for kind in ("pickup", "slip", "photo"):
            with self.subTest(row=kind):
                self.assertEqual(self.alpha[kind].pk, self.beta[kind].pk)

    def test_each_bar_s_pages_show_its_own_rows(self):
        pk = self.alpha["pickup"].pk
        for user, mine, other in ((self.user_a, "Alpha", "Beta"), (self.user_b, "Beta", "Alpha")):
            self.client.force_login(user)
            for url in (
                HOME,
                reverse("returnables:pickup_detail", args=[pk]),
                reverse("returnables:slip_detail", args=[self.alpha["slip"].pk]),
            ):
                with self.subTest(bar=mine, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotContains(response, f"Grossiste {other}")
                    self.assertNotContains(response, f"Vides {other}")
            self.assertContains(self.client.get(reverse("returnables:pickup_detail", args=[pk])), f"Vides {mine}")

    def test_a_photo_is_served_to_its_own_bar_only(self):
        """B's photo is named after B's day, a name A holds nothing under:
        A asking for it is a 404, not B's picture."""
        self.assertNotEqual(self.alpha["image"], self.beta["image"])
        self.client.force_login(self.user_a)
        self.assertEqual(self.client.get(self.beta["url"]).status_code, 404)
        own = self.client.get(self.alpha["url"])
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own["Content-Type"], "image/jpeg")
        self.client.force_login(self.user_b)
        self.assertEqual(self.client.get(self.alpha["url"]).status_code, 404)
        self.assertEqual(self.client.get(self.beta["url"]).status_code, 200)

    def test_the_files_are_in_each_tenant_s_media(self):
        for tenant, mine, other in ((self.bar_a, self.alpha, self.beta), (self.bar_b, self.beta, self.alpha)):
            with self.subTest(tenant=tenant.name):
                media = self.media_of(tenant)
                self.assertTrue((media / mine["image"]).is_file())
                self.assertFalse((media / other["image"]).exists())
                with bound_tenant(tenant):
                    slip = Slip.objects.get(pk=mine["slip"].pk)
                self.assertTrue((media / slip.file.name).is_file())


class PhotosThroughThePageTests(TenantsCase):
    def test_a_photo_sent_lands_in_its_bar_s_media_and_a_deletion_leaves_the_other_s(self):
        """Both bars send a pickup of the same day: the same file name in
        both media folders. Deleting A's leaves B's."""
        names = {}
        for tenant, user in ((self.bar_a, self.user_a), (self.bar_b, self.user_b)):
            self.client.force_login(user)
            html = self.client.get(HOME).content.decode()
            form = form_posting_to(html, HOME)
            data = as_post(form.submission(values={"date": "2026-02-10"}))
            data["photos"] = [SimpleUploadedFile("IMG_0001.jpg", tiny_jpeg(), content_type="image/jpeg")]
            response = self.client.post(HOME, data)
            self.assertEqual(response.status_code, 302)
            with bound_tenant(tenant):
                names[tenant.pk] = PickupPhoto.objects.get().image.name
            self.assertTrue((self.media_of(tenant) / names[tenant.pk]).is_file())
        self.assertEqual(names[self.bar_a.pk], names[self.bar_b.pk])

        self.client.force_login(self.user_a)
        with bound_tenant(self.bar_a):
            pickup = Pickup.objects.get()
        html = self.client.get(reverse("returnables:pickup_detail", args=[pickup.pk])).content.decode()
        self.client.post(
            reverse("returnables:pickup_delete", args=[pickup.pk]),
            as_post(form_posting_to(html, reverse("returnables:pickup_delete", args=[pickup.pk])).submission()),
        )
        self.assertFalse((self.media_of(self.bar_a) / names[self.bar_a.pk]).exists())
        self.assertTrue((self.media_of(self.bar_b) / names[self.bar_b.pk]).is_file())
        with bound_tenant(self.bar_b):
            self.assertEqual(Pickup.objects.count(), 1)


class GatherPerTenantTests(TenantsCase):
    """« Récupérer les bons » searches each bar's own mailbox: offered and
    bound to its own tenant in A and in B once B's mailbox is typed on its
    « Identifiants »."""

    def setUp(self):
        super().setUp()
        for tenant in (self.bar_a, self.bar_b):
            with bound_tenant(tenant):
                make_pickup()
                self.format_pk = seeded_format().pk

    def test_bar_b_s_gather_runs_in_a_thread_bound_to_bar_b(self):
        from accounts import vault

        with bound_tenant(self.bar_b):
            vault.save(
                {"INVOICE_EMAIL_ADDRESS": "beta@exemple.invalid", "INVOICE_EMAIL_APP_PASSWORD": "secret-beta"},
                bindings={"INVOICE_EMAIL_APP_PASSWORD": "imap.beta.invalid"},
            )
        self.client.force_login(self.user_b)
        html = self.client.get(HOME).content.decode()
        self.assertNotIn(integrations.SLIPS, html)
        form = next(form for form in forms_of(html) if form.action == reverse("invoices:gather"))
        with mock.patch("invoices.views.threading.Thread") as thread:
            self.client.post(form.action, as_post(form.submission()))
        target = thread.call_args.kwargs["target"]
        self.assertIs(target.__wrapped__, gather_invoices_task)
        self.assertEqual(target.tenant.pk, self.bar_b.pk)
        with bound_tenant(self.bar_b):
            self.assertEqual(ScrapeJob.objects.count(), 1)
        with bound_tenant(self.bar_a):
            self.assertFalse(ScrapeJob.objects.exists())

    def test_bar_a_s_gather_runs_in_a_thread_bound_to_bar_a(self):
        self.client.force_login(self.user_a)
        html = self.client.get(HOME).content.decode()
        form = next(form for form in forms_of(html) if form.action == reverse("invoices:gather"))
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.client.post(form.action, as_post(form.submission()))
        target = thread.call_args.kwargs["target"]
        self.assertIs(target.__wrapped__, gather_invoices_task)
        self.assertEqual(target.tenant.pk, self.bar_a.pk)
        self.assertEqual(set(thread.call_args.kwargs["args"][3]), {f"bons-{self.format_pk}"})
        self.assertEqual(response["Location"], HOME)
        with bound_tenant(self.bar_a):
            self.assertEqual(ScrapeJob.objects.count(), 1)
        with bound_tenant(self.bar_b):
            self.assertFalse(ScrapeJob.objects.exists())
