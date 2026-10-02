"""Who may import or clear on « Données » (security review of 01/10/2026).

An import replaces the espace's invoices, bank, prices and portals - a
portal from an archive decides where a stored password is typed - and a
clear deletes them: the espace's owner's alone, his MarginMate password
confirmed (accounts/sudo.py), as « Identifiants » and a portal's source are.
Every POST that stages an archive (sent, or one of the backups), previews,
confirms or cancels a staged one, or previews or confirms a clear asks
both; the confirmation then lasts while the page is in use. The export and
every tab stay open to a member: what is shown and downloaded there is the
espace's own, as on every page.
"""

from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import FileResponse
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from accounts import sudo
from accounts.models import Membership
from inventory.models import StockType
from tests.runner import TEST_TENANT_PK, confirm_password, test_user
from transfer import safety, staging, views
from transfer.report import RunReport
from transfer.tests.support import FakeSectionsMixin, export_archive, fake_row
from transfer.tests.test_views import BACKUPS, said, shown_preview, stage_of

IMPORT = reverse("transfer:data_import")
BACKUP = reverse("transfer:data_import_backup")
CLEAR = reverse("transfer:data_clear")
EXPORT = reverse("transfer:data_export")
BACKUP_NAME = "2026-09-19_143012_avant-effacement.zip"


class ProtectionCase(FakeSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        fake_row("fournisseurs", "Fournisseur A")
        fake_row("banque", "Ligne A")
        self.stage = stage_of({"fournisseurs"})
        self.addCleanup(lambda: staging.get(self.stage.token) and staging.discard(self.stage))
        self.stage_url = reverse("transfer:data_import_stage", args=[self.stage.token])
        folder = safety.backup_dir()
        reader = export_archive({"fournisseurs"})
        reader.close()
        (folder / BACKUP_NAME).write_bytes(reader.path.read_bytes())
        self.addCleanup((folder / BACKUP_NAME).unlink, missing_ok=True)
        # The test espace's staging folder is the whole run's.
        self.waiting = {stage.token for stage in staging.pending()}
        self.untouched = staging.get(self.stage.token).state

    @staticmethod
    def archive():
        reader = export_archive({"fournisseurs"})
        reader.close()
        return SimpleUploadedFile("archive.zip", reader.path.read_bytes())

    def protected_posts(self):
        """Every POST that imports or clears, with the page it comes from."""
        stage = {"sections": ["fournisseurs"], "strategie-fournisseurs": "fusionner"}
        return [
            ("envoyer", IMPORT, lambda: {"archive": self.archive()}, IMPORT),
            ("sauvegarde", BACKUP, lambda: {"nom": BACKUP_NAME}, IMPORT),
            ("previsualiser", self.stage_url, lambda: {**stage, "action": "previsualiser"}, self.stage_url),
            ("importer", self.stage_url, lambda: {**stage, "action": "importer", "apercu": "x"}, self.stage_url),
            ("annuler", self.stage_url, lambda: {"action": "annuler"}, self.stage_url),
            ("apercu effacer", CLEAR, lambda: {"sections": ["banque"], "action": "previsualiser"}, CLEAR),
            (
                "effacer",
                CLEAR,
                lambda: {"sections": ["banque"], "action": "effacer", "confirmation": "EFFACER", "apercu": "x"},
                CLEAR,
            ),
        ]

    def assertNothingDone(self):
        self.assertEqual({stage.token for stage in staging.pending()}, self.waiting)
        self.assertEqual(staging.get(self.stage.token).state, self.untouched)
        self.assertIsNone(self.client.session.get(views.session_key(views.SESSION_CLEAR)))
        self.assertEqual(StockType.objects.filter(category="banque").count(), 1)

    def assertExportsAndReads(self):
        # « Configuration seule » too: an export, the espace's own.
        for sections in (["fournisseurs"], views.configuration_keys()):
            with self.subTest(sections=sections):
                response = self.client.post(EXPORT, {"sections": sections})
                self.assertIsInstance(response, FileResponse)
                response.close()
        for url in (reverse("transfer:data_home"), IMPORT, CLEAR, self.stage_url):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class AMemberTests(ProtectionCase):
    def setUp(self):
        super().setUp()
        Membership.objects.filter(user=test_user(), tenant_id=TEST_TENANT_PK).update(role=Membership.Role.MEMBER)

    def test_every_import_and_clear_is_refused_and_said(self):
        # His password confirmed (the client's implicit login): it is not
        # enough.
        for name, url, data, _page in self.protected_posts():
            with self.subTest(post=name):
                response = self.client.post(url, data())
                self.assertContains(response, escape(views.OWNER_ONLY), status_code=403)
                self.assertNothingDone()

    def test_he_still_exports_and_reads_every_tab(self):
        self.assertExportsAndReads()


class TheOwnerUnconfirmedTests(ProtectionCase):
    def setUp(self):
        super().setUp()
        self.client.confirms_password = False

    def test_every_import_and_clear_asks_for_the_password_and_does_nothing(self):
        for name, url, data, page in self.protected_posts():
            with self.subTest(post=name):
                response = self.client.post(url, data())
                self.assertRedirects(response, sudo.confirm_url(page), fetch_redirect_response=False)
                self.assertNothingDone()

    def test_a_confirmation_that_ended_is_asked_again(self):
        confirm_password(self.client, seconds=-1)
        response = self.client.post(CLEAR, {"sections": ["banque"], "action": "previsualiser"})
        self.assertRedirects(response, sudo.confirm_url(CLEAR), fetch_redirect_response=False)
        self.assertNothingDone()

    def test_he_still_exports_and_reads_every_tab(self):
        self.assertExportsAndReads()


class TheOwnerConfirmedTests(ProtectionCase):
    def setUp(self):
        super().setUp()
        confirm_password(self.client, seconds=60)

    def until(self):
        return self.client.session[sudo.SESSION_KEY]["until"]

    def test_he_stages_previews_and_confirms_and_the_confirmation_is_kept_alive(self):
        before = self.until()
        response = self.client.post(IMPORT, {"archive": self.archive()})
        token = response["Location"].rstrip("/").split("/")[-1]
        self.assertRedirects(
            response, reverse("transfer:data_import_stage", args=[token]), fetch_redirect_response=False
        )
        self.assertGreater(self.until(), before + 300)
        self.addCleanup(lambda: staging.get(token) and staging.discard(staging.get(token)))

        posted = {"sections": ["fournisseurs"], "strategie-fournisseurs": "fusionner"}
        self.client.post(self.stage_url, {**posted, "action": "previsualiser"})
        self.assertIsInstance(RunReport.from_json(staging.get(self.stage.token).state["preview"]), RunReport)
        page = self.client.get(self.stage_url)
        # The backups are safety's own tests' (a TestCase holds a transaction).
        with mock.patch("transfer.views.safety.before", return_value=BACKUPS):
            response = self.client.post(self.stage_url, {**posted, "action": "importer", "apercu": shown_preview(page)})
        self.assertRedirects(response, IMPORT + "?rapport=1", fetch_redirect_response=False)
        self.assertIsNone(staging.get(self.stage.token))
        said(response)

    def test_he_stages_a_backup(self):
        response = self.client.post(BACKUP, {"nom": BACKUP_NAME})
        token = response["Location"].rstrip("/").split("/")[-1]
        self.addCleanup(lambda: staging.get(token) and staging.discard(staging.get(token)))
        self.assertEqual(staging.get(token).backup, BACKUP_NAME)

    def test_he_cancels_a_staged_archive(self):
        response = self.client.post(self.stage_url, {"action": "annuler"})
        self.assertRedirects(response, IMPORT, fetch_redirect_response=False)
        self.assertIsNone(staging.get(self.stage.token))

    def test_he_previews_a_clear(self):
        response = self.client.post(CLEAR, {"sections": ["banque"], "action": "previsualiser"})
        self.assertRedirects(response, CLEAR, fetch_redirect_response=False)
        self.assertIsNotNone(self.client.session.get(views.session_key(views.SESSION_CLEAR)))
