"""An espace's folders (accounts/paths.py), the default storage
(accounts/storage.py) and the logged-in file view (accounts/views.py)."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.urls import reverse

from accounts import paths
from accounts.models import Tenant
from accounts.tenancy import NoTenantBound, bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from tests.factories import make_invoice

#: Every folder an espace keeps its files in.
FOLDERS = (
    paths.media_root,
    paths.private_dir,
    paths.downloads_dir,
    paths.backups_dir,
    paths.staging_dir,
    paths.imports_dir,
)


class MultiModePathsTests(TwoTenantsTestCase):
    def test_a_new_espace_has_every_folder(self):
        folder = paths.tenant_dir(self.bar_a)
        self.assertTrue((folder / "db.sqlite3").is_file())
        for name in paths.FOLDERS:
            self.assertTrue((folder / name).is_dir(), name)

    def test_each_folder_is_the_bound_espace_s_own(self):
        for function in FOLDERS:
            with self.subTest(function=function.__name__):
                with bound_tenant(self.bar_a):
                    alpha = function()
                with bound_tenant(self.bar_b):
                    beta = function()
                self.assertNotEqual(alpha, beta)
                self.assertEqual(alpha.parent, paths.tenant_dir(self.bar_a))
                self.assertEqual(beta.parent, paths.tenant_dir(self.bar_b))
                self.assertTrue(alpha.is_dir())

    def test_a_folder_missing_is_made_on_demand(self):
        (paths.tenant_dir(self.bar_a) / "downloads").rmdir()
        with bound_tenant(self.bar_a):
            self.assertTrue(paths.downloads_dir().is_dir())

    def test_unbound_there_is_no_folder(self):
        for function in FOLDERS:
            with self.subTest(function=function.__name__), self.assertRaises(NoTenantBound):
                function()

    def test_a_folder_name_never_climbs_out(self):
        for name in ("../evil", "..", "a/b", "_template", "AB", ""):
            with self.subTest(name=name), self.assertRaises(ImproperlyConfigured):
                paths.tenant_dir(Tenant(name="x", dir_name=name))


class MultiModeStorageTests(TwoTenantsTestCase):
    def test_a_stored_file_lands_in_the_bound_espace_s_media(self):
        with bound_tenant(self.bar_a):
            invoice = make_invoice()
            invoice.source_file.save("facture-alpha.pdf", ContentFile(b"%PDF-alpha"))
            stored = Path(invoice.source_file.path)
            url = invoice.source_file.url
        self.assertTrue(stored.is_file())
        media_a = paths.tenant_dir(self.bar_a) / "media"
        self.assertIn(media_a.resolve(), stored.resolve().parents)
        self.assertFalse(invoice.source_file.name.startswith(self.bar_a.dir_name))
        self.assertEqual(url, reverse("accounts:media", args=[invoice.source_file.name]))
        self.assertEqual(list((paths.tenant_dir(self.bar_b) / "media").rglob("*.pdf")), [])

    def test_unbound_the_storage_has_nowhere_to_go(self):
        with self.assertRaises(NoTenantBound):
            default_storage.save("invoices/orphelin.pdf", ContentFile(b"x"))
        with self.assertRaises(NoTenantBound):
            default_storage.location  # noqa: B018


class FileViewTests(TwoTenantsTestCase):
    def setUp(self):
        super().setUp()
        with bound_tenant(self.bar_a):
            self.name_a = default_storage.save("invoices/2026/09/facture-alpha.pdf", ContentFile(b"%PDF-alpha"))
            self.page_a = default_storage.save("invoices/2026/09/page.html", ContentFile(b"<script>alert(1)</script>"))
        with bound_tenant(self.bar_b):
            self.name_b = default_storage.save("invoices/2026/09/facture-beta.pdf", ContentFile(b"%PDF-beta"))

    def get(self, name):
        return self.client.get("/fichiers/" + name)

    def test_a_bar_gets_its_own_file_framed_by_its_own_pages(self):
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("accounts:media", args=[self.name_a]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-alpha")
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response["Content-Disposition"].startswith("inline"))
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_another_bar_s_file_is_not_found_by_its_name(self):
        self.client.force_login(self.user_a)
        self.assertEqual(self.get(self.name_b).status_code, 404)
        self.client.force_login(self.user_b)
        self.assertEqual(self.get(self.name_a).status_code, 404)
        self.assertEqual(self.get(self.name_b).status_code, 200)

    def test_no_climbing_out_of_the_media_folder(self):
        self.client.force_login(self.user_a)
        beta = self.bar_b.dir_name
        absolute = str(paths.tenant_dir(self.bar_b) / "media" / self.name_b).replace("\\", "/")
        for name in (
            f"../../{beta}/media/{self.name_b}",
            f"invoices/../../../{beta}/media/{self.name_b}",
            f"%2e%2e/%2e%2e/{beta}/media/{self.name_b}",
            f"..\\..\\{beta}\\media\\{self.name_b}",
            f"../../{beta}/db.sqlite3",
            "../db.sqlite3",
            absolute,
            "/" + absolute,
        ):
            with self.subTest(name=name):
                self.assertEqual(self.get(name).status_code, 404)

    def test_anonymous_is_sent_to_the_login_page(self):
        response = self.get(self.name_a)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/connexion/"))

    def test_a_page_a_user_stored_is_downloaded_sandboxed_never_shown(self):
        self.client.force_login(self.user_a)
        response = self.get(self.page_a)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        self.assertEqual(response["Content-Security-Policy"], "sandbox")

    def test_a_folder_is_not_a_file(self):
        self.client.force_login(self.user_a)
        self.assertEqual(self.get("invoices/2026/09/").status_code, 404)
        self.assertEqual(self.get("invoices/2026/09/absente.pdf").status_code, 404)

    def test_a_link_out_of_media_is_refused(self):
        with bound_tenant(self.bar_a):
            link = paths.media_root() / "invoices" / "lien.pdf"
        target = paths.tenant_dir(self.bar_b) / "media" / self.name_b
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("This system does not let a test make a symbolic link.")
        self.client.force_login(self.user_a)
        self.assertEqual(self.get("invoices/lien.pdf").status_code, 404)

    def test_only_reading_is_answered(self):
        self.client.force_login(self.user_a)
        self.assertEqual(self.client.post("/fichiers/" + self.name_a).status_code, 405)
