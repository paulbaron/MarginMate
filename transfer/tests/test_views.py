"""The « Données » page (§3, §5.7), with FakeSections.

The server is authoritative: the script ticks what a section requires, but
a selection posted without it is refused and sent back ticked - never
completed in silence, never downloaded or imported half. And an import or a
clear is confirmed only for what was previewed.
"""

import html
import json
import re
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.messages import get_messages
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import FileResponse
from django.test import Client, SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from inventory.models import StockType
from invoices.models import ScrapeJob, Supplier
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax
from transfer import runner, safety, staging, views
from transfer.archive import ArchiveReader
from transfer.report import RunReport, SectionReport
from transfer.tests.support import (
    FakeSection,
    FakeSectionsMixin,
    export_archive,
    fake_row,
    forge,
    new_archive_path,
)

BACKUPS = {"database": "C:/sauvegardes/2026-09-19_143012_avant-effacement.sqlite3",
           "archive": "C:/sauvegardes/2026-09-19_143012_avant-effacement.zip"}


def said(response) -> list[str]:
    """The messages of this response - and only them: a redirect not
    followed leaves them in the client's cookie for the next request."""
    messages = [str(message) for message in get_messages(response.wsgi_request)]
    response.client.cookies.pop("messages", None)
    return messages


def checkbox(response, key: str) -> str:
    content = response.content.decode()
    match = re.search(rf'<input type="checkbox" name="sections" value="{key}"[^>]*>', content)
    return match.group(0) if match else ""


def forced_by(response, key: str) -> dict:
    return json.loads(html.unescape(re.search(r'data-forced-by="([^"]*)"', checkbox(response, key)).group(1)))


def ticked(response) -> set[str]:
    content = response.content.decode()
    return {
        match.group(1)
        for match in re.finditer(r'<input type="checkbox" name="sections" value="(\w+)"[^>]*>', content)
        if " checked" in match.group(0)
    }


def stage_of(keys, **payload_changes):
    reader = export_archive(keys)
    reader.close()
    path = forge(reader.path, **payload_changes) if payload_changes else reader.path
    return staging.stage_upload(SimpleUploadedFile("archive.zip", Path(path).read_bytes()))


def shown_preview(page) -> str:
    """The preview a page's confirm form names, as a browser posts it back
    (views.SHOWN_PREVIEW); "" when the page offers no confirm."""
    match = re.search(r'name="apercu" value="([^"]*)"', page.content.decode())
    return match.group(1) if match else ""


def escaped(path: Path, member: str, change) -> Path:
    """The archive at `path`, `member` rewritten by change(parsed) as ASCII
    JSON: a lone surrogate ("\\ud800") goes in as the six characters of its
    escape, as a hand-edited file carries it."""
    target = new_archive_path("escaped")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(target, "w") as out:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == member:
                data = json.dumps(change(json.loads(data))).encode("ascii")
            out.writestr(info.filename, data)
    return target


class SmokeTests(FakeSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        fake_row("fournisseurs", "Fournisseur A")
        fake_row("recettes", "Recette A")

    def assertPage(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        assertNoUnrenderedTemplateSyntax(self, response, url)
        return response

    def test_the_three_tabs(self):
        for name in ("transfer:data_home", "transfer:data_import", "transfer:data_clear"):
            with self.subTest(page=name):
                response = self.assertPage(reverse(name))
                self.assertContains(response, "<h1>Données</h1>")
                self.assertContains(response, 'aria-current="page"')
        for name in ("transfer:data_home", "transfer:data_clear"):
            with self.subTest(picker=name):
                response = self.client.get(reverse(name))
                self.assertContains(response, "<legend>Configuration</legend>")
                self.assertContains(response, "<legend>Données</legend>")
                self.assertContains(response, "1 article fictif")
        self.assertContains(self.client.get(reverse("transfer:data_import")), "Envoyer l'archive")

    def test_a_stage_page(self):
        stage = stage_of({"fournisseurs", "recettes", "associations"})
        response = self.assertPage(reverse("transfer:data_import_stage", args=[stage.token]))
        self.assertContains(response, "Archive du ")
        self.assertContains(response, 'name="strategie-recettes" value="remplacer"')
        self.assertContains(response, "absent de l&#x27;archive")

    def test_post_only_urls_redirect_on_get(self):
        for name in ("transfer:data_export", "transfer:data_import_backup"):
            with self.subTest(url=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 302)

    def test_the_navigation_lights_donnees(self):
        response = self.client.get(reverse("transfer:data_home"))
        self.assertRegex(response.content.decode(), r'<a href="/donnees/" class="active">Données</a>')

    def test_a_lane_missing_does_not_take_the_page_down(self):
        """Only the registered sections are drawn; one requiring a section
        not there yet is drawn disabled."""
        from transfer import registry
        from transfer.tests.support import FAKES

        with registry.swap({key: FAKES[key] for key in ("recettes", "banque")}):
            response = self.assertPage(reverse("transfer:data_home"))
        self.assertNotIn("Enseignes et fournisseurs", response.content.decode())
        self.assertIn(" disabled", checkbox(response, "recettes"))
        self.assertNotIn(" disabled", checkbox(response, "banque"))
        self.assertContains(response, "indisponible")


class PickerTests(FakeSectionsMixin, TestCase):
    def test_what_ticks_what_is_drawn_for_the_script(self):
        export = self.client.get(reverse("transfer:data_home"))
        self.assertEqual(
            forced_by(export, "associations"),
            {"recettes": "Recettes", "liens_ventes": "Liens recettes ↔ ventes", "ventes": "Ventes", "inventaires": "Inventaires"},
        )
        clear = self.client.get(reverse("transfer:data_clear"))
        self.assertEqual(forced_by(clear, "associations"), {"fournisseurs": "Enseignes et fournisseurs"})

    def test_cocher_pre_ticks_with_what_it_needs(self):
        response = self.client.get(reverse("transfer:data_home") + "?cocher=associations&cocher=inconnu")
        self.assertEqual(ticked(response), {"associations", "fournisseurs"})
        self.assertIn('class="is-forced"', checkbox(response, "fournisseurs"))
        self.assertNotIn("is-forced", checkbox(response, "associations"))
        self.assertContains(response, "nécessaire pour Associations produits → articles")

    def test_a_forced_box_stays_enabled(self):
        """A disabled input is not posted, and the server wants the closure."""
        response = self.client.get(reverse("transfer:data_home") + "?cocher=recettes")
        box = checkbox(response, "fournisseurs")
        self.assertIn('aria-disabled="true"', box)
        self.assertNotIn(" disabled", box)

    def test_the_hints_are_drawn(self):
        response = self.client.get(reverse("transfer:data_home"))
        self.assertContains(response, "conseillé : Associations produits → articles — sinon les produits de ces factures arrivent « à classer »")

    def test_the_clear_tab_says_what_a_clear_costs_not_what_to_take_along(self):
        """« conseillé : Factures et tickets » under Associations read, on the
        Effacer tab, as advice to clear the invoices too (review, 19/09)."""
        response = self.client.get(reverse("transfer:data_clear"))
        self.assertNotContains(response, "conseillé")
        self.assertContains(response, "à savoir : la banque perd les paiements de ces factures")
        self.assertContains(response, "à savoir : la banque perd les noms de payeurs appris pour les fournisseurs effacés")
        self.assertNotContains(self.client.get(reverse("transfer:data_home")), "à savoir : la banque")

    def test_known_prices_are_called_what_the_app_calls_them(self):
        """An « article » is a StockType everywhere else; these are the prices
        the review page calls « Prix connus »."""
        response = self.client.get(reverse("transfer:data_home"))
        self.assertContains(response, "prix connus")
        self.assertNotContains(response, "articles sans nom")

    def test_the_script_can_untick_everything(self):
        self.assertContains(self.client.get(reverse("transfer:data_home")), "data-untick-all")

    def test_the_estimated_size_shows_with_invoices(self):
        with mock.patch.object(FakeSection, "count", return_value={"documents": 3, "Mo de fichiers": 420}):
            hidden = self.client.get(reverse("transfer:data_home"))
            shown = self.client.get(reverse("transfer:data_home") + "?cocher=factures")
        self.assertRegex(hidden.content.decode(), r'data-shown-with="factures" hidden>≈ 420 Mo avec les fichiers')
        self.assertRegex(shown.content.decode(), r'data-shown-with="factures">≈ 420 Mo avec les fichiers')


class ExportTests(FakeSectionsMixin, TestCase):
    url = reverse("transfer:data_export")

    def test_an_unclosed_selection_is_refused_and_sent_back_ticked(self):
        response = self.client.post(self.url, {"sections": ["recettes"]})
        self.assertEqual(response.status_code, 200)
        self.assertNotIsInstance(response, FileResponse)
        self.assertNotIn("Content-Disposition", response)
        self.assertEqual(
            said(response),
            [("La partie « Recettes » a besoin de : Enseignes et fournisseurs, Associations produits → articles. "
              "Elles sont maintenant cochées : exportez de nouveau.")],
        )
        self.assertEqual(ticked(response), {"recettes", "associations", "fournisseurs"})

    def test_several_parts_needing_more_are_named_in_the_plural(self):
        response = self.client.post(self.url, {"sections": ["recettes", "ventes", "fournisseurs"]})
        self.assertEqual(
            said(response),
            [("Les parties « Recettes » et « Ventes » ont besoin de : Associations produits → articles. "
              "Elle est maintenant cochée : exportez de nouveau.")],
        )

    def test_an_empty_selection(self):
        for data in ({}, {"sections": ["inconnu"]}):
            with self.subTest(data=data):
                response = self.client.post(self.url, data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(said(response), ["Cochez au moins une partie."])

    def test_the_download_is_streamed_and_its_temp_file_removed(self):
        fake_row("fournisseurs", "Fournisseur A")
        before = set(staging.exports_dir().iterdir())
        response = self.client.post(self.url, {"sections": ["recettes", "associations", "fournisseurs"]})
        self.assertIsInstance(response, FileResponse)
        self.assertTrue(response.streaming)
        self.assertRegex(response["Content-Disposition"], r'attachment; filename="marginmate-\d{4}-\d{2}-\d{2}-\d{4}\.zip"')
        temp = set(staging.exports_dir().iterdir()) - before
        self.assertEqual(len(temp), 1)
        path = new_archive_path("downloaded")
        path.write_bytes(b"".join(response.streaming_content))
        response.close()
        self.assertFalse(next(iter(temp)).exists())
        with ArchiveReader(path) as reader:
            self.assertEqual(reader.sections, {"recettes", "associations", "fournisseurs"})
            self.assertEqual(reader.section("fournisseurs").payload()["records"][0]["name"], "Fournisseur A")

    def test_refused_while_a_job_runs(self):
        ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now())
        response = self.client.post(self.url, {"sections": ["banque"]})
        self.assertRedirects(response, reverse("transfer:data_home"), fetch_redirect_response=False)
        self.assertEqual(said(response), [runner.BUSY])
        self.assertContains(self.client.get(reverse("transfer:data_home")), "est en cours")


class ImportTests(FakeSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        fake_row("fournisseurs", "Fournisseur A")
        fake_row("sources", "Source A")
        fake_row("associations", "Article A")
        fake_row("recettes", "Recette A")
        self.stage = stage_of({"fournisseurs", "sources", "associations", "recettes"})
        self.url = reverse("transfer:data_import_stage", args=[self.stage.token])
        self.all = ["fournisseurs", "sources", "associations", "recettes"]

    def post(self, action, sections=None, apercu=None, **strategies):
        """A confirm names the preview its page shows, unless `apercu` says
        otherwise."""
        data = {"action": action, "sections": sections if sections is not None else self.all}
        data.update({f"strategie-{key}": value for key, value in strategies.items()})
        if action == "importer":
            data["apercu"] = shown_preview(self.client.get(self.url)) if apercu is None else apercu
        return self.client.post(self.url, data)

    def test_an_upload_is_staged_then_previewed(self):
        reader = export_archive({"fournisseurs", "banque"})
        reader.close()
        response = self.client.post(
            reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", reader.path.read_bytes())}
        )
        token = response["Location"].rstrip("/").split("/")[-1]
        self.assertRedirects(response, reverse("transfer:data_import_stage", args=[token]), fetch_redirect_response=False)
        self.assertEqual(staging.get(token).sections, {"fournisseurs", "banque"})

    def test_a_refused_upload_is_said(self):
        response = self.client.post(reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", b"bonjour")})
        self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
        self.assertEqual(
            said(response),
            ["Ce fichier n'est ni une archive MarginMate (.zip) ni un export d'associations (.json)."],
        )
        response = self.client.post(reverse("transfer:data_import"))
        self.assertEqual(said(response), ["Choisissez un fichier à importer."])

    def test_the_old_associations_file_is_staged_with_only_associations(self):
        def to_archive(payload, dest):
            Path(dest).write_bytes(forge({"associations": {"records": [], "files": []}}).read_bytes())

        fake_legacy = mock.Mock(to_archive=to_archive)
        payload = {"version": 1, "products": []}
        with mock.patch.dict("sys.modules", {"transfer.legacy": fake_legacy}), \
                mock.patch("transfer.legacy", fake_legacy, create=True):
            response = self.client.post(
                reverse("transfer:data_import"),
                {"archive": SimpleUploadedFile("marginmate-associations.json", json.dumps(payload).encode())},
            )
        token = response["Location"].rstrip("/").split("/")[-1]
        stage = staging.get(token)
        self.assertEqual(stage.sections, {"associations"})
        page = self.client.get(response["Location"])
        self.assertContains(page, "Ancien fichier « marginmate-associations.json »")
        # The old file never carried an article's losses: one it creates
        # takes the default, and an article at 0 % would come back at 10 %.
        self.assertContains(page, "Les pertes (%) n'y figurent pas : un article créé prend 10 %.")

    def test_preview_then_import(self):
        StockType.objects.filter(name="Recette A").update(loss_percent="12.00")
        response = self.post("previsualiser", recettes="remplacer")
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        page = self.client.get(self.url)
        self.assertContains(page, "À créer")
        self.assertContains(page, "Recettes › articles fictifs")
        self.assertContains(page, 'value="importer"')
        self.assertContains(page, "ainsi qu'une archive de ce qui change")
        self.assertEqual(StockType.objects.get(name="Recette A").loss_percent, 12)  # a preview writes nothing

        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            response = self.post("importer", recettes="remplacer")
        before.assert_called_once_with("import", {"recettes"})
        self.assertRedirects(response, reverse("transfer:data_import") + "?rapport=1", fetch_redirect_response=False)
        self.assertEqual(StockType.objects.get(name="Recette A").loss_percent, 10)
        self.assertIsNone(staging.get(self.stage.token))
        self.assertEqual(
            said(response),
            [("Import terminé. Sauvegardes faites avant : 2026-09-19_143012_avant-effacement.sqlite3 et "
              f"2026-09-19_143012_avant-effacement.zip (dans {safety.backup_path()}).")],
        )
        report = self.client.get(reverse("transfer:data_import") + "?rapport=1")
        self.assertContains(report, "Import terminé")
        self.assertContains(report, "Modifiés")
        self.assertContains(report, "Aucun document n&#x27;a été relu ; rien n&#x27;a été appris.")
        again = self.client.get(reverse("transfer:data_import") + "?rapport=1")
        self.assertNotContains(again, "Modifiés")  # shown once

    def test_a_merge_backs_up_no_archive(self):
        self.post("previsualiser")
        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            self.post("importer")
        before.assert_called_once_with("import", set())

    def test_a_confirm_other_than_the_preview_previews_again(self):
        self.post("previsualiser")
        StockType.objects.filter(name="Recette A").update(loss_percent="12.00")
        with mock.patch("transfer.views.safety.before") as before:
            response = self.post("importer", recettes="remplacer")
        before.assert_not_called()
        self.assertEqual(said(response), [views.CHANGED])
        self.assertEqual(StockType.objects.get(name="Recette A").loss_percent, 12)
        self.assertEqual(staging.get(self.stage.token).state["sections"]["recettes"], "remplacer")

        # A preview more than 30 minutes old is previewed again too - and
        # said as such: the selection did not change, the preview expired.
        stage = staging.get(self.stage.token)
        stage.state["preview_at"] = (timezone.now() - timedelta(minutes=31)).isoformat()
        staging.save(stage)
        self.assertContains(self.client.get(self.url), "Cet aperçu a plus de 30 minutes")
        with mock.patch("transfer.views.safety.before") as before:
            response = self.post("importer", recettes="remplacer")
        before.assert_not_called()
        self.assertEqual(said(response), [views.EXPIRED])
        self.assertEqual(views.EXPIRED, "L'aperçu avait plus de 30 minutes : voici le nouvel aperçu, confirmez de nouveau.")

    def test_a_database_that_moved_since_the_preview_is_not_imported(self):
        """The preview can be half an hour old: the confirm proves it is the
        run that was previewed, or imports nothing and shows what it would
        do now (review, 19/09)."""
        StockType.objects.filter(name="Recette A").update(loss_percent="12.00")
        self.post("previsualiser", recettes="remplacer")
        fake_row("recettes", "Recette arrivée après l'aperçu")
        with mock.patch("transfer.views.safety.before", return_value=BACKUPS):
            response = self.post("importer", recettes="remplacer")
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(said(response), [views.MOVED_IMPORT])
        self.assertEqual(views.MOVED_IMPORT, "La base a changé depuis l'aperçu : rien n'a été importé. Voici le nouvel aperçu.")
        self.assertTrue(StockType.objects.filter(name="Recette arrivée après l'aperçu").exists())
        self.assertEqual(StockType.objects.get(name="Recette A").loss_percent, 12)
        now = RunReport.from_json(staging.get(self.stage.token).state["preview"])
        self.assertTrue(now.preview)
        self.assertEqual(now.section("recettes").tallies["articles fictifs"].deleted, 1)

        # Confirmed on the new preview, it runs.
        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            response = self.post("importer", recettes="remplacer")
        before.assert_called_once_with("import", {"recettes"})
        self.assertRedirects(response, reverse("transfer:data_import") + "?rapport=1", fetch_redirect_response=False)
        self.assertFalse(StockType.objects.filter(name="Recette arrivée après l'aperçu").exists())

    def test_a_confirm_is_held_to_the_preview_on_its_own_page(self):
        """Two tabs on one stage: tab A's « Importer » ran what tab B had
        previewed since - the confirm was held to the preview stored last,
        not to the one on the screen it was clicked from (review, 19/09)."""
        StockType.objects.filter(name="Recette A").update(loss_percent="12.00")
        self.post("previsualiser", recettes="remplacer")
        tab_a = shown_preview(self.client.get(self.url))
        self.assertRegex(tab_a, r"^[0-9a-f]{64}$")
        fake_row("recettes", "Recette arrivée entre deux aperçus")
        self.post("previsualiser", recettes="remplacer")
        tab_b = shown_preview(self.client.get(self.url))
        self.assertNotIn(tab_b, ("", tab_a))

        # A form naming no preview was drawn before this version: say so,
        # rather than « la base a changé », which sent the owner looking for
        # a change nobody made (20/09, the owner's « Effacer » did nothing).
        for posted, expected in ((tab_a, views.OTHER_TAB_IMPORT), ("0" * 64, views.OTHER_TAB_IMPORT), ("", views.OLD_PAGE_IMPORT)):
            with self.subTest(posted=posted), mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
                response = self.post("importer", apercu=posted, recettes="remplacer")
                before.assert_not_called()
                self.assertRedirects(response, self.url, fetch_redirect_response=False)
                self.assertEqual(said(response), [expected])
                self.assertTrue(StockType.objects.filter(name="Recette arrivée entre deux aperçus").exists())
                self.assertEqual(StockType.objects.get(name="Recette A").loss_percent, 12)
                # The preview shown now is the stored one, still to confirm.
                self.assertEqual(shown_preview(self.client.get(self.url)), tab_b)

        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            response = self.post("importer", apercu=tab_b, recettes="remplacer")
        before.assert_called_once_with("import", {"recettes"})
        self.assertRedirects(response, reverse("transfer:data_import") + "?rapport=1", fetch_redirect_response=False)
        self.assertFalse(StockType.objects.filter(name="Recette arrivée entre deux aperçus").exists())

    def test_the_backup_covers_what_the_run_touches_without_importing_it(self):
        """The invoices' prune deletes the bank's payments: the bank is not
        imported, yet its report tells the owner to take them back from the
        safety archive - so it is in it (review, 19/09)."""
        stage = stage_of({"fournisseurs", "factures"})
        url = reverse("transfer:data_import_stage", args=[stage.token])
        FakeSection.touch["factures"] = "banque"
        posted = {"sections": ["fournisseurs", "factures"], "strategie-factures": "remplacer"}
        self.client.post(url, {**posted, "action": "previsualiser"})
        page = self.client.get(url)
        self.assertContains(page, "ainsi qu'une archive de ce qui change")
        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            self.client.post(url, {**posted, "action": "importer", "apercu": shown_preview(page)})
        before.assert_called_once_with("import", {"banque"})

    def test_the_backup_covers_a_merged_section_that_loses_rows(self):
        stage = stage_of({"fournisseurs", "factures", "banque"})
        url = reverse("transfer:data_import_stage", args=[stage.token])
        FakeSection.touch["factures"] = "banque"
        posted = {"sections": ["fournisseurs", "factures", "banque"], "strategie-factures": "remplacer", "strategie-banque": "fusionner"}
        self.client.post(url, {**posted, "action": "previsualiser"})
        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            self.client.post(url, {**posted, "action": "importer", "apercu": shown_preview(self.client.get(url))})
        before.assert_called_once_with("import", {"banque"})

    def test_an_unclosed_selection_is_refused_and_sent_back_ticked(self):
        response = self.post("previsualiser", sections=["recettes"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            said(response),
            [("La partie « Recettes » a besoin de : Enseignes et fournisseurs, Associations produits → articles. "
              "Elles sont maintenant cochées : prévisualisez de nouveau.")],
        )
        self.assertEqual(ticked(response), {"recettes", "associations", "fournisseurs"})
        self.assertIsNone(staging.get(self.stage.token).state.get("preview"))
        response = self.post("previsualiser", sections=[])
        self.assertEqual(said(response), ["Cochez au moins une partie."])

    def test_a_section_absent_from_the_archive_cannot_be_ticked(self):
        page = self.client.get(self.url)
        self.assertIn(" disabled", checkbox(page, "banque"))
        self.post("previsualiser", sections=[*self.all, "banque"])
        self.assertEqual(set(staging.get(self.stage.token).state["sections"]), set(self.all))

    def test_an_unknown_strategy_is_a_merge(self):
        self.post("previsualiser", recettes="ecraser")
        self.assertEqual(staging.get(self.stage.token).state["sections"]["recettes"], "fusionner")

    def test_cancel_removes_the_stage(self):
        response = self.post("annuler")
        self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
        self.assertIsNone(staging.get(self.stage.token))

    def test_an_unknown_token_redirects_with_a_message(self):
        for token in ("A" * 22, "pas-un-jeton", "..", "%2E%2E"):
            with self.subTest(token=token):
                response = self.client.get(f"/donnees/importer/{token}/")
                self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
                self.assertEqual(said(response), [views.GONE])

    def test_a_new_database_is_told_to_replace(self):
        self.assertContains(self.client.get(self.url), "Base neuve")
        Supplier.objects.create(code="EXEMPLE", name="Fournisseur Exemple")
        self.assertNotContains(self.client.get(self.url), "Base neuve")

    def test_the_new_database_note_needs_the_suppliers_in_the_archive(self):
        """An old associations file holds no « Enseignes et fournisseurs »:
        the note would ask for a box that is disabled (review, 19/09)."""
        stage = stage_of({"banque"})
        self.assertNotContains(self.client.get(reverse("transfer:data_import_stage", args=[stage.token])), "Base neuve")

    def test_a_hint_names_only_what_the_archive_holds(self):
        stage = stage_of({"fournisseurs", "associations"})
        page = self.client.get(reverse("transfer:data_import_stage", args=[stage.token]))
        self.assertNotContains(page, "conseillé : Factures et tickets")
        stage = stage_of({"fournisseurs", "associations", "factures"})
        page = self.client.get(reverse("transfer:data_import_stage", args=[stage.token]))
        self.assertContains(page, "conseillé : Factures et tickets")

    def test_counts_that_are_not_numbers_are_left_out(self):
        """The manifest comes from outside: a count that is not a number took
        every GET of the stage down with a 500, its « Annuler » with it."""
        for counts in ({"articles fictifs": "onze"}, {"articles fictifs": [1]}, {"articles fictifs": None},
                       {"articles fictifs": True}, {"articles fictifs": {"n": 1}}, "onze", [1]):
            with self.subTest(counts=counts):
                def manifest(data, counts=counts):
                    data["sections"]["sources"]["counts"] = counts
                    data["sections"]["fournisseurs"]["counts"] = {"articles fictifs": 2, "autre": "deux"}
                    return data

                path = forge({"fournisseurs": {"records": [], "files": []}, "sources": {"records": [], "files": []}},
                             manifest=manifest)
                stage = staging.stage_upload(SimpleUploadedFile("archive.zip", path.read_bytes()))
                response = self.client.get(reverse("transfer:data_import_stage", args=[stage.token]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "archive : 2 articles fictifs —")
                self.assertNotContains(response, "onze")

    def test_an_archive_damaged_on_the_way_is_refused_in_french(self):
        from transfer.tests.test_archive import damaged

        response = self.client.post(
            reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", damaged("manifest.json").read_bytes())}
        )
        self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
        self.assertEqual(said(response), ["Fichier altéré dans l'archive : manifest.json"])

        response = self.client.post(
            reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", damaged("sources.json").read_bytes())}
        )
        url = response["Location"]
        response = self.client.post(url, {"action": "previsualiser", "sections": ["sources"]})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(said(response), ["Fichier altéré dans l'archive : sources.json"])

    def test_a_moment_the_page_cannot_show(self):
        """A manifest's created_at at the edge of the calendar parses, then
        overflowed once converted to Paris time: the Importer tab - the
        restore path, and the only « Annuler » of that stage - and the stage
        page were a 500 until the 24 h sweep (review, 19/09)."""
        from transfer.tests.test_staging import no_stages

        self.addCleanup(no_stages)
        for moment in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:30:00-14:00"):
            with self.subTest(moment=moment):
                no_stages()
                path = forge({"sources": {"records": [], "files": []}}, manifest={"created_at": moment})
                response = self.client.post(reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", path.read_bytes())})
                url = response["Location"]
                tab = self.client.get(reverse("transfer:data_import"))
                self.assertEqual(tab.status_code, 200)
                self.assertContains(tab, f'href="{url}">Reprendre</a>')
                self.assertContains(tab, "Archive (date illisible)")
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, "<strong>Archive (date illisible)</strong>", html=True)

    def test_half_a_character_is_refused_in_french(self):
        """A lone surrogate escape ("\\ud800") is valid JSON that no UTF-8 write
        takes: in the manifest it was a 500 at the upload, in a section at
        « Prévisualiser », in an old associations file at the upload
        (review, 19/09)."""
        sources = {"sources": {"records": [], "files": []}}
        path = escaped(forge(sources), "manifest.json", lambda manifest: {**manifest, "reason": "export \ud800"})
        response = self.client.post(reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", path.read_bytes())})
        self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
        self.assertEqual(said(response), ["Archive refusée : manifest.json contient un caractère invalide."])

        path = escaped(forge(sources), "sources.json", lambda payload: {
            "records": [{"name": "Source \ud800", "unit": "L", "loss_percent": "10.00"}], "files": []})
        response = self.client.post(reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", path.read_bytes())})
        url = response["Location"]
        response = self.client.post(url, {"action": "previsualiser", "sections": ["sources"]})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(said(response), ["Archive refusée : sources.json contient un caractère invalide."])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertFalse(StockType.objects.filter(category="sources").exclude(name="Source A").exists())

        old = ('{"version": 1, "products": [{"supplier": "Grossiste Exemple", "raw_name": "RHUM EXEMPLE \\ud800", '
               '"stock_type_name": "Rhum essai"}]}')
        response = self.client.post(
            reverse("transfer:data_import"), {"archive": SimpleUploadedFile("marginmate-associations.json", old.encode("ascii"))}
        )
        self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
        self.assertEqual(said(response), ["Export d'associations refusé : il contient un caractère invalide."])

    def test_a_structural_problem_is_said(self):
        stage = stage_of({"fournisseurs", "sources"}, sources={"pas_de_records": True})
        url = reverse("transfer:data_import_stage", args=[stage.token])
        response = self.client.post(url, {"action": "previsualiser", "sections": ["fournisseurs", "sources"]})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(said(response), ["Archive refusée : sources.json n'a pas de liste « records »."])


class BackupImportTests(FakeSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        folder = safety.backup_dir()
        for path in folder.iterdir():
            path.unlink()
        reader = export_archive({"fournisseurs"})
        reader.close()
        self.name = "2026-09-19_143012_avant-effacement.zip"
        (folder / self.name).write_bytes(reader.path.read_bytes())
        (folder / "2026-09-19_143012_avant-effacement.sqlite3").write_bytes(b"SQLite")
        self.addCleanup(lambda: [path.unlink() for path in folder.iterdir()])

    def test_the_backups_are_listed(self):
        response = self.client.get(reverse("transfer:data_import"))
        self.assertContains(response, self.name)
        self.assertContains(response, "19/09/2026 à 14:30")
        self.assertContains(response, "remplacez db.sqlite3 par ce fichier")
        # In WAL mode a db.sqlite3-wal left by a server stopped hard is read
        # back into whatever file is named db.sqlite3: the copy put back came
        # out holding the newer rows (scratch probe, 19/09).
        self.assertContains(response, "supprimez db.sqlite3-wal et db.sqlite3-shm s'ils sont là")
        self.assertContains(response, 'name="nom" value="2026-09-19_143012_avant-effacement.zip"')
        self.assertNotContains(response, 'name="nom" value="2026-09-19_143012_avant-effacement.sqlite3"')

    def test_a_backup_is_staged_by_its_listed_name_only(self):
        response = self.client.post(reverse("transfer:data_import_backup"), {"nom": self.name})
        token = response["Location"].rstrip("/").split("/")[-1]
        self.assertEqual(staging.get(token).backup, self.name)
        for name in ("../../db.sqlite3", "2026-09-19_143012_avant-effacement.sqlite3", ""):
            with self.subTest(name=name):
                response = self.client.post(reverse("transfer:data_import_backup"), {"nom": name})
                self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
                self.assertEqual(said(response), ["Cette sauvegarde n'existe pas (ou plus)."])


class ClearTests(FakeSectionsMixin, TestCase):
    url = reverse("transfer:data_clear")

    def setUp(self):
        super().setUp()
        fake_row("factures", "Document A")
        fake_row("inventaires", "Comptage A")
        self.selection = {"sections": ["factures", "inventaires"]}

    def preview(self):
        return self.client.post(self.url, {**self.selection, "action": "previsualiser"})

    def confirm(self, typed, apercu=None):
        """From the page: the confirm names the preview it shows, unless
        `apercu` says otherwise."""
        if apercu is None:
            apercu = shown_preview(self.client.get(self.url))
        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            response = self.client.post(
                self.url, {**self.selection, "action": "effacer", "confirmation": typed, "apercu": apercu}
            )
        return response, before

    def test_preview_then_clear(self):
        response = self.preview()
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(StockType.objects.filter(category="factures").count(), 1)
        page = self.client.get(self.url)
        self.assertContains(page, "Ce qui sera effacé")
        self.assertContains(page, "Tapez EFFACER pour confirmer")
        self.assertEqual(ticked(page), {"factures", "inventaires"})
        # What a clear does is delete: that column first, and no column that
        # is always 0 in a clear - on a phone, it was scrolled out of sight.
        headings = re.findall(r"<th[^>]*>([^<]*)</th>", page.content.decode())
        self.assertEqual(headings, ["Partie", "À supprimer", "À modifier"])

        response, before = self.confirm("  effacer ")  # any case, spaces forgiven
        before.assert_called_once_with("effacement", {"factures", "inventaires"})
        self.assertRedirects(response, self.url + "?rapport=1", fetch_redirect_response=False)
        self.assertEqual(StockType.objects.filter(category__in=["factures", "inventaires"]).count(), 0)
        report = self.client.get(self.url + "?rapport=1")
        self.assertContains(report, "Effacement terminé")
        self.assertEqual(re.findall(r"<th[^>]*>([^<]*)</th>", report.content.decode()), ["Partie", "Supprimés", "Modifiés"])

    def test_a_database_that_moved_since_the_preview_is_not_cleared(self):
        self.preview()
        fake_row("factures", "Document arrivé après l'aperçu")
        response, before = self.confirm("EFFACER")
        # The backups are taken first, outside the transaction; the run then
        # finds it is not the one previewed, and is undone.
        before.assert_called_once_with("effacement", {"factures", "inventaires"})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(said(response), [views.MOVED_CLEAR])
        self.assertEqual(views.MOVED_CLEAR, "La base a changé depuis l'aperçu : rien n'a été effacé. Voici le nouvel aperçu.")
        self.assertEqual(StockType.objects.filter(category="factures").count(), 2)
        page = self.client.get(self.url)
        self.assertRegex(page.content.decode(), r"Factures et tickets › articles fictifs</td>\s*<td class=\"num\">2</td>")

        response, _before = self.confirm("EFFACER")
        self.assertRedirects(response, self.url + "?rapport=1", fetch_redirect_response=False)
        self.assertEqual(StockType.objects.filter(category="factures").count(), 0)

    def test_a_confirm_is_held_to_the_preview_on_its_own_page(self):
        """Two tabs of one browser share the session: « Effacer » clicked on
        the tab showing the older preview cleared what the other had
        previewed since (review, 19/09)."""
        self.preview()
        tab_a = shown_preview(self.client.get(self.url))
        self.assertRegex(tab_a, r"^[0-9a-f]{64}$")
        fake_row("factures", "Document arrivé entre deux aperçus")
        self.preview()
        tab_b = shown_preview(self.client.get(self.url))
        self.assertNotIn(tab_b, ("", tab_a))

        # « Effacer » from a page drawn before this version names no preview:
        # what the owner met on 20/09, told « la base a changé ».
        for posted, expected in ((tab_a, views.OTHER_TAB_CLEAR), ("", views.OLD_PAGE_CLEAR)):
            with self.subTest(posted=posted):
                response, before = self.confirm("EFFACER", apercu=posted)
                before.assert_not_called()
                self.assertRedirects(response, self.url, fetch_redirect_response=False)
                self.assertEqual(said(response), [expected])
                self.assertEqual(StockType.objects.filter(category="factures").count(), 2)
                self.assertEqual(shown_preview(self.client.get(self.url)), tab_b)

        response, before = self.confirm("EFFACER", apercu=tab_b)
        before.assert_called_once_with("effacement", {"factures", "inventaires"})
        self.assertRedirects(response, self.url + "?rapport=1", fetch_redirect_response=False)
        self.assertEqual(StockType.objects.filter(category="factures").count(), 0)

    def test_an_expired_preview_is_said_as_such(self):
        self.preview()
        session = self.client.session
        session[views.SESSION_CLEAR]["at"] = (timezone.now() - timedelta(minutes=31)).isoformat()
        session.save()
        response, before = self.confirm("EFFACER")
        before.assert_not_called()
        self.assertEqual(said(response), [views.EXPIRED])
        self.assertEqual(StockType.objects.filter(category="factures").count(), 1)

    def test_without_effacer_nothing_is_cleared(self):
        self.preview()
        for typed in ("", "EFFACE", "oui"):
            with self.subTest(typed=typed):
                response, before = self.confirm(typed)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(said(response), ["Tapez EFFACER pour confirmer."])
                before.assert_not_called()
                self.assertEqual(StockType.objects.filter(category="factures").count(), 1)

    def test_a_confirm_without_a_preview_previews(self):
        response, before = self.confirm("EFFACER")
        before.assert_not_called()
        self.assertEqual(said(response), [views.CHANGED])
        self.assertEqual(StockType.objects.filter(category="factures").count(), 1)

    def test_a_confirm_for_another_selection_previews_again(self):
        fake_row("recettes", "Recette A")
        self.preview()
        self.selection = {"sections": ["recettes", "liens_ventes", "ventes"]}
        response, before = self.confirm("EFFACER")
        before.assert_not_called()
        self.assertEqual(said(response), [views.CHANGED])
        self.assertEqual(StockType.objects.filter(category="recettes").count(), 1)

    def test_an_unclosed_selection_is_refused_and_sent_back_ticked(self):
        response = self.client.post(self.url, {"sections": ["factures"], "action": "previsualiser"})
        self.assertEqual(response.status_code, 200)
        # The Effacer tab has no « Prévisualiser »: its button is « Voir ce
        # qui sera effacé ».
        self.assertEqual(
            said(response),
            [("Effacer « Factures et tickets » efface aussi : Inventaires. Elle est maintenant cochée : "
              "voyez de nouveau ce qui sera effacé.")],
        )
        self.assertEqual(ticked(response), {"factures", "inventaires"})
        self.assertContains(response, "effacé avec Factures et tickets")
        response = self.client.post(self.url, {"action": "previsualiser"})
        self.assertEqual(said(response), ["Cochez au moins une partie."])

    def test_the_backup_covers_what_the_preview_shows_touched(self):
        """Clearing invoices deletes the bank's payments: the bank goes into
        the safety archive too."""
        FakeSection.touch["factures"] = "banque"
        self.preview()
        _response, before = self.confirm("EFFACER")
        before.assert_called_once_with("effacement", {"factures", "inventaires", "banque"})

    def test_a_failed_backup_clears_nothing(self):
        self.preview()
        apercu = shown_preview(self.client.get(self.url))
        with mock.patch("transfer.views.safety.before", side_effect=safety.SafetyError("Sauvegarde impossible (x) : rien n'a été changé.")):
            response = self.client.post(
                self.url, {**self.selection, "action": "effacer", "confirmation": "EFFACER", "apercu": apercu}
            )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(said(response), ["Sauvegarde impossible (x) : rien n'a été changé."])
        self.assertEqual(StockType.objects.filter(category="factures").count(), 1)

    def test_refused_while_a_job_runs(self):
        self.preview()
        ScrapeJob.objects.create(status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now())
        response, before = self.confirm("EFFACER")
        before.assert_not_called()
        self.assertEqual(said(response), [runner.BUSY])
        self.assertEqual(StockType.objects.filter(category="factures").count(), 1)


class PendingStageTests(FakeSectionsMixin, TestCase):
    """An archive sent (possibly 422 MB) and left for a moment could only be
    found again through the browser's history until the 24 h sweep: the
    Importer tab lists what waits (review, 19/09)."""

    def setUp(self):
        super().setUp()
        from transfer.tests.test_staging import no_stages

        no_stages()
        self.addCleanup(no_stages)

    def test_a_staged_archive_is_listed_with_resume_and_cancel(self):
        fake_row("fournisseurs", "Fournisseur A")
        stage = stage_of({"fournisseurs", "recettes", "associations"})
        url = reverse("transfer:data_import_stage", args=[stage.token])
        page = self.client.get(reverse("transfer:data_import"))
        self.assertContains(page, "Archives en attente")
        self.assertContains(page, f'<a class="btn btn-secondary btn-small" href="{url}">Reprendre</a>', html=True)
        self.assertRegex(
            page.content.decode(),
            re.compile(rf'<form method="post" action="{re.escape(url)}">.*?name="action" value="annuler"', re.DOTALL),
        )
        self.assertContains(page, "Enseignes et fournisseurs, Associations produits → articles, Recettes")

        self.client.post(url, {"action": "annuler"})
        self.assertNotContains(self.client.get(reverse("transfer:data_import")), "Archives en attente")

    def test_nothing_waiting_nothing_listed(self):
        self.assertNotContains(self.client.get(reverse("transfer:data_import")), "Archives en attente")

    def test_the_stage_page_can_untick_everything(self):
        stage = stage_of({"fournisseurs", "recettes", "associations"})
        page = self.client.get(reverse("transfer:data_import_stage", args=[stage.token]))
        self.assertContains(page, "Tout décocher")
        self.assertContains(page, "data-untick-all")


class CountsTextTests(SimpleTestCase):
    """« ici : 1 sources » - the labels are plural; one of a thing is said
    in the singular, as far as it can be without guessing."""

    def test_one_is_singular(self):
        cases = {
            "sources": "1 source",
            "articles fictifs": "1 article fictif",
            "prix connus": "1 prix connu",
            "produits caisse liés": "1 produit caisse lié",
            "jours de vente (caisse)": "1 jour de vente (caisse)",
            "noms de payeurs appris": "1 nom de payeurs appris",
            "mouvements d'achat": "1 mouvement d'achat",
            "ventes saisies à la main": "1 vente saisie à la main",
            "règles « sans facture »": "1 règle « sans facture »",
            "Mo de fichiers": "1 Mo de fichiers",
            "alias": "1 alias",
            # Two things counted together stay as they are.
            "pertes et corrections": "1 pertes et corrections",
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                self.assertEqual(views._counts_text({label: 1}), text)

    def test_others_are_plural_and_grouped(self):
        # A no-break space: « 1 234 » is never cut at the end of a line.
        self.assertEqual(views._counts_text({"fournisseurs": 1234, "prix connus": 0}), "1\xa0234 fournisseurs · 0 prix connus")

    def test_what_is_not_a_count_is_left_out(self):
        self.assertEqual(views._counts_text({"sources": "onze", "lignes": [1], "fichiers": True, "documents": 2}), "2 documents")
        self.assertEqual(views._counts_text(None), "")


class ReportTextTests(SimpleTestCase):
    def render(self, **fields):
        from django.template.loader import render_to_string

        section = SectionReport(key="factures", label="Factures et tickets")
        section.deleted("documents", 2)
        report = RunReport(**{"mode": "import", "preview": False, "sections": [section], "rebuilt": {}, **fields})
        return " ".join(render_to_string("transfer/_report.html", {"report": report}).split())

    def test_the_duration_is_written_the_french_way(self):
        self.assertIn("Durée : 10,3 s", self.render(duration_s=10.28))
        self.assertIn("Durée : 0,2 s", self.render(duration_s=0.17))

    def test_one_backup_is_singular(self):
        one = self.render(safety={"database": "C:/sauvegardes/2026-09-19_155653_avant-import.sqlite3", "archive": ""})
        self.assertIn("Sauvegarde faite avant : <code>C:/sauvegardes/2026-09-19_155653_avant-import.sqlite3</code>.", one)
        two = self.render(safety=BACKUPS)
        self.assertIn("Sauvegardes faites avant : <code>", two)
        self.assertIn("_avant-effacement.zip</code>.", two)

    def test_a_clear_draws_what_it_deletes_first(self):
        html = self.render(mode="clear", preview=True)
        self.assertEqual(re.findall(r"<th[^>]*>([^<]*)</th>", html), ["Partie", "À supprimer", "À modifier"])
        self.assertRegex(html, r"Factures et tickets › documents</td> <td class=\"num\">2</td> <td class=\"num\">0</td> </tr>")
        self.assertRegex(html, r'<td>Recalculé</td> <td colspan="2">')
        # An import keeps its four columns.
        self.assertEqual(
            re.findall(r"<th[^>]*>([^<]*)</th>", self.render(preview=True)),
            ["Partie", "À créer", "À modifier", "À supprimer", "Inchangés"],
        )


@tag("browser")
class PickerInBrowserTests(FakeSectionsMixin, StaticLiveServerTestCase):
    """transfer.js, in a real (headless) Chrome: what only the page's script
    does. Tagged "browser"; skipped where Chrome or its driver is missing."""

    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from invoices.scrapers import website

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

    def ticked(self) -> set[str]:
        return set(self.driver.execute_script(
            "return Array.from(document.querySelectorAll('input[name=sections]:checked')).map(b => b.value);"
        ))

    def click(self, selector):
        self.driver.execute_script("document.querySelector(arguments[0]).click();", selector)

    def test_tout_decocher_on_a_full_archive(self):
        """A full archive starts all ticked: importing the bank alone meant
        unticking eight boxes in dependency order, the first click on a
        forced one doing nothing."""
        from transfer.registry import INFO

        stage = stage_of(set(INFO))
        self.driver.get(self.live_server_url + reverse("transfer:data_import_stage", args=[stage.token]))
        self.assertEqual(self.ticked(), set(INFO))
        self.click("[data-untick-all]")
        self.assertEqual(self.ticked(), set())
        self.click('input[value="banque"]')
        self.assertEqual(self.ticked(), {"banque"})
        self.click('input[value="recettes"]')
        self.assertEqual(self.ticked(), {"banque", "recettes", "associations", "fournisseurs"})
        forced = self.driver.execute_script("return document.querySelector('input[value=fournisseurs]').getAttribute('aria-disabled');")
        self.assertEqual(forced, "true")

    def test_tout_decocher_after_tout_cocher_on_export(self):
        self.driver.get(self.live_server_url + reverse("transfer:data_home"))
        self.click("[data-tick-all]")
        self.assertEqual(len(self.ticked()), 9)
        self.click("[data-untick-all]")
        self.assertEqual(self.ticked(), set())


class CsrfTests(FakeSectionsMixin, TestCase):
    def test_every_post_needs_its_token(self):
        stage = stage_of({"fournisseurs"})
        client = Client(enforce_csrf_checks=True)
        for url in (
            reverse("transfer:data_export"),
            reverse("transfer:data_import"),
            reverse("transfer:data_import_backup"),
            reverse("transfer:data_import_stage", args=[stage.token]),
            reverse("transfer:data_clear"),
        ):
            with self.subTest(url=url):
                self.assertEqual(client.post(url, {"sections": ["banque"], "action": "previsualiser"}).status_code, 403)

    def test_the_forms_carry_it(self):
        for name in ("transfer:data_home", "transfer:data_import", "transfer:data_clear"):
            with self.subTest(page=name):
                self.assertContains(self.client.get(reverse(name)), "csrfmiddlewaretoken")


class OldAddressTests(TestCase):
    def test_the_old_associations_addresses_lead_here(self):
        response = self.client.get(reverse("inventory:export_associations"))
        self.assertRedirects(response, "/donnees/?cocher=associations", fetch_redirect_response=False)
        for method in ("get", "post"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(reverse("inventory:import_associations"))
                self.assertRedirects(response, "/donnees/importer/", fetch_redirect_response=False)
