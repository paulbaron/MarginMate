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
from urllib.parse import parse_qs, urlsplit

from django.contrib.messages import get_messages
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import FileResponse
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from bank.models import IgnoreRule, OperationRule, StatementFormat
from inventory.models import StockType
from invoices.models import ScrapeJob, Supplier
from returnables.models import ReturnableType, SlipFormat
from tests.runner import log_in_the_browser
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax
from transfer import registry, runner, safety, staging, views
from transfer.archive import ArchiveReader
from transfer.registry import INFO
from transfer.report import RunReport, SectionReport
from transfer.sections.base import Group
from transfer.tests.support import (
    FAKES,
    FakeSection,
    FakeSectionsMixin,
    export_archive,
    fake_row,
    forge,
    new_archive_path,
)
from transfer.tests.test_archive import (
    OLD_BANK,
    OLD_BANK_COUNTS,
    OLD_RETURNABLES,
    OLD_RETURNABLES_COUNTS,
    old_archive,
)

#: « Configuration », in its order: what « Configuration seule » ticks.
CONFIGURATION = [key for key in registry.ordered(INFO) if INFO[key].group == Group.CONFIG]
#: The download's name, the moment it was made aside.
CONFIGURATION_NAME = r'attachment; filename="marginmate-configuration-\d{4}-\d{2}-\d{2}-\d{4}\.zip"'
FULL_NAME = r'attachment; filename="marginmate-\d{4}-\d{2}-\d{2}-\d{4}\.zip"'

BACKUPS = {
    "database": "C:/sauvegardes/2026-09-19_143012_avant-effacement.sqlite3",
    "archive": "C:/sauvegardes/2026-09-19_143012_avant-effacement.zip",
}


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


def row_text(response, key: str) -> str:
    """Everything the picker says under the box of `key`, as read."""
    content = response.content.decode()
    match = re.search(rf'data-row="{key}">(.*?)(?=data-row="|</fieldset>)', content, re.DOTALL)
    return html.unescape(match.group(1)) if match else ""


def archive_counts(response, key: str) -> set[str]:
    """What a stage's row says the archive holds - « archive : 2 bons · … » -
    one count a part."""
    match = re.search(r'<span class="picker-counts">archive : (.*?)(?: — ici : .*?)?</span>', row_text(response, key))
    return set(match.group(1).split(" · ")) if match else set()


def new_database_note(response) -> str:
    """The « Base neuve » note as read; "" when the page has none."""
    match = re.search(r'<div class="message">(Base neuve[^<]*)</div>', response.content.decode())
    return html.unescape(match.group(1)) if match else ""


def stage_of(keys, **payload_changes):
    reader = export_archive(keys)
    reader.close()
    path = forge(reader.path, **payload_changes) if payload_changes else reader.path
    return staging.stage_upload(SimpleUploadedFile("archive.zip", Path(path).read_bytes()))


def staged(path: Path):
    return staging.stage_upload(SimpleUploadedFile("archive.zip", path.read_bytes()))


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

    def test_the_navigation_lights_the_data_tab(self):
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
            {
                "recettes": "Recettes",
                "liens_ventes": "Liens recettes ↔ ventes",
                "ventes": "Ventes",
                "inventaires": "Inventaires",
            },
        )
        clear = self.client.get(reverse("transfer:data_clear"))
        self.assertEqual(forced_by(clear, "associations"), {"fournisseurs": "Enseignes et fournisseurs"})

    def test_the_bank_rules_and_the_returnable_types_tick_what_they_need(self):
        """The rules tick nothing and nothing ticks them; the types go with
        the suppliers, and the pickups with the types."""
        export = self.client.get(reverse("transfer:data_home"))
        self.assertEqual(forced_by(export, "regles_banque"), {})
        self.assertEqual(forced_by(export, "types_consignes"), {"consignes": "Consignes"})
        self.assertEqual(forced_by(export, "fournisseurs")["types_consignes"], "Types et formats de consignes")
        self.assertNotIn("regles_banque", forced_by(export, "fournisseurs"))
        clear = self.client.get(reverse("transfer:data_clear"))
        self.assertEqual(forced_by(clear, "regles_banque"), {})
        self.assertEqual(forced_by(clear, "banque"), {})
        self.assertEqual(
            forced_by(clear, "consignes"),
            {"fournisseurs": "Enseignes et fournisseurs", "types_consignes": "Types et formats de consignes"},
        )

    def test_the_clear_tab_says_the_installed_rows_go_with_the_rules_and_the_types(self):
        """What the migrations installed - the bank's format and rules, the
        returnable types and the UBA slip format - goes with the part that
        holds it now, and is said there, no longer under « Banque » and
        « Consignes ». « Banque » says only what is still its own: the
        treasury's points and adjustments."""
        clear = self.client.get(reverse("transfer:data_clear"))
        self.assertIn(
            "à savoir : les formats et les règles installés d'office partent aussi", row_text(clear, "regles_banque")
        )
        self.assertIn(
            "à savoir : les types de consigne et le format de bon créés à l'installation partent aussi",
            row_text(clear, "types_consignes"),
        )
        self.assertTrue(row_text(clear, "consignes"))
        self.assertNotIn("à savoir", row_text(clear, "consignes"))
        bank = row_text(clear, "banque")
        self.assertIn("à savoir : les points et les ajustements de trésorerie partent aussi", bank)
        for word in ("règle", "format"):
            with self.subTest(word=word):
                self.assertNotIn(word, bank)

    def test_the_tick_parameter_pre_ticks_with_what_it_needs(self):
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
        self.assertContains(
            response,
            "conseillé : Associations produits → articles — sinon les produits de ces factures arrivent « à classer »",
        )

    def test_the_clear_tab_says_what_a_clear_costs_not_what_to_take_along(self):
        """« conseillé : Factures et tickets » under Associations read, on the
        Effacer tab, as advice to clear the invoices too (review, 19/09)."""
        response = self.client.get(reverse("transfer:data_clear"))
        self.assertNotContains(response, "conseillé")
        self.assertContains(response, "à savoir : la banque perd les paiements de ces factures")
        self.assertContains(
            response, "à savoir : la banque perd les noms de payeurs appris pour les fournisseurs effacés"
        )
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
            [
                (
                    "La partie « Recettes » a besoin de : Enseignes et fournisseurs, Associations produits → articles. "
                    "Elles sont maintenant cochées : exportez de nouveau."
                )
            ],
        )
        self.assertEqual(ticked(response), {"recettes", "associations", "fournisseurs"})

    def test_several_parts_needing_more_are_named_in_the_plural(self):
        response = self.client.post(self.url, {"sections": ["recettes", "ventes", "fournisseurs"]})
        self.assertEqual(
            said(response),
            [
                (
                    "Les parties « Recettes » et « Ventes » ont besoin de : Associations produits → articles. "
                    "Elle est maintenant cochée : exportez de nouveau."
                )
            ],
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
        self.assertRegex(response["Content-Disposition"], CONFIGURATION_NAME)
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

    def test_an_archive_holding_data_keeps_its_name(self):
        """« configuration » only where every part is configuration: the
        rules of the bank with its lines are a full archive."""
        for sections in (["fournisseurs", "factures"], ["banque"], ["regles_banque", "banque"]):
            with self.subTest(sections=sections):
                response = self.client.post(self.url, {"sections": sections})
                self.assertIsInstance(response, FileResponse)
                self.assertRegex(response["Content-Disposition"], FULL_NAME)
                response.close()


class ConfigurationOnlyTests(FakeSectionsMixin, TestCase):
    """« Configuration seule » (the owner, 02/10/2026): every setting made by
    hand, without the bar's data, for a new espace - another bar on the same
    bank and suppliers. A link, so the page drawn with it reads the same
    with or without its script."""

    def link(self, response) -> str:
        match = re.search(
            r'<a class="btn btn-secondary" href="([^"]*)" data-configuration-only>Configuration seule</a>',
            response.content.decode(),
        )
        return html.unescape(match.group(1)) if match else ""

    def test_the_link_ticks_the_configuration_and_nothing_else(self):
        self.assertEqual(views.configuration_keys(), CONFIGURATION)
        self.assertIn("regles_banque", CONFIGURATION)
        self.assertIn("types_consignes", CONFIGURATION)
        href = self.link(self.client.get(reverse("transfer:data_home")))
        self.assertEqual(urlsplit(href).path, reverse("transfer:data_home"))
        self.assertEqual(parse_qs(urlsplit(href).query), {"cocher": CONFIGURATION})

        page = self.client.get(href)
        self.assertEqual(ticked(page), set(CONFIGURATION))
        # Closed: no box is ticked for another, and none of « Données ».
        for key in INFO:
            with self.subTest(key=key):
                self.assertNotIn("is-forced", checkbox(page, key))
                self.assertEqual(" checked" in checkbox(page, key), INFO[key].group == Group.CONFIG)

    def test_the_page_never_says_the_archive_holds_what_is_not_ticked(self):
        """An archive of configuration is made to be handed to another bar:
        « contient vos documents, votre banque… : gardez-la pour vous » was
        false of it, and told the owner not to do what it is for. Prices
        still travel with it (the suppliers' known prices, the recipes)."""
        page = self.client.get(self.link(self.client.get(reverse("transfer:data_home"))))
        self.assertNotContains(page, "contient vos documents")
        self.assertNotContains(page, "gardez-la pour vous")
        self.assertContains(page, "ne la confiez qu'à qui peut les voir")

    def test_only_what_can_be_exported_is_ticked(self):
        """A part whose lane is missing, or whose suppliers' is, cannot be
        exported: the link leaves it out rather than be refused."""
        with registry.swap({key: FAKES[key] for key in INFO if key != "fournisseurs"}):
            self.assertEqual(views.configuration_keys(), ["formats_caisse", "regles_banque"])
            href = self.link(self.client.get(reverse("transfer:data_home")))
        self.assertEqual(parse_qs(urlsplit(href).query), {"cocher": ["formats_caisse", "regles_banque"]})

    def test_only_the_export_tab_offers_it(self):
        self.assertTrue(self.link(self.client.get(reverse("transfer:data_home"))))
        for name in ("transfer:data_import", "transfer:data_clear"):
            with self.subTest(page=name):
                self.assertNotContains(self.client.get(reverse(name)), "data-configuration-only")

    def test_its_archive_says_so_in_its_name(self):
        """Beside the full archives it would otherwise look like."""
        fake_row("regles_banque", "Format exemple")
        response = self.client.post(reverse("transfer:data_export"), {"sections": views.configuration_keys()})
        self.assertIsInstance(response, FileResponse)
        self.assertRegex(response["Content-Disposition"], CONFIGURATION_NAME)
        path = new_archive_path("configuration")
        path.write_bytes(b"".join(response.streaming_content))
        response.close()
        with ArchiveReader(path) as reader:
            self.assertEqual(reader.sections, set(CONFIGURATION))
            self.assertEqual(reader.section("regles_banque").payload()["records"][0]["name"], "Format exemple")


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

    def post(self, action, sections=None, preview=None, **strategies):
        """A confirm names the preview its page shows, unless `preview` says
        otherwise."""
        data = {"action": action, "sections": sections if sections is not None else self.all}
        data.update({f"strategie-{key}": value for key, value in strategies.items()})
        if action == "importer":
            data["apercu"] = shown_preview(self.client.get(self.url)) if preview is None else preview
        return self.client.post(self.url, data)

    def test_an_upload_is_staged_then_previewed(self):
        reader = export_archive({"fournisseurs", "banque"})
        reader.close()
        response = self.client.post(
            reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", reader.path.read_bytes())}
        )
        token = response["Location"].rstrip("/").split("/")[-1]
        self.assertRedirects(
            response, reverse("transfer:data_import_stage", args=[token]), fetch_redirect_response=False
        )
        self.assertEqual(staging.get(token).sections, {"fournisseurs", "banque"})

    def test_a_refused_upload_is_said(self):
        response = self.client.post(
            reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", b"bonjour")}
        )
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
        with (
            mock.patch.dict("sys.modules", {"transfer.legacy": fake_legacy}),
            mock.patch("transfer.legacy", fake_legacy, create=True),
        ):
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
        self.assertContains(page, "un article créé prend 10 % de pertes.")

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
            [
                (
                    "Import terminé. Sauvegardes faites avant : 2026-09-19_143012_avant-effacement.sqlite3 et "
                    f"2026-09-19_143012_avant-effacement.zip (dans {views.BACKUPS_PLACE})."
                )
            ],
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
        self.assertEqual(
            views.EXPIRED, "L'aperçu avait plus de 30 minutes : voici le nouvel aperçu, confirmez de nouveau."
        )

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
        self.assertEqual(
            views.MOVED_IMPORT, "La base a changé depuis l'aperçu : rien n'a été importé. Voici le nouvel aperçu."
        )
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
        for posted, expected in (
            (tab_a, views.OTHER_TAB_IMPORT),
            ("0" * 64, views.OTHER_TAB_IMPORT),
            ("", views.OLD_PAGE_IMPORT),
        ):
            with (
                self.subTest(posted=posted),
                mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before,
            ):
                response = self.post("importer", preview=posted, recettes="remplacer")
                before.assert_not_called()
                self.assertRedirects(response, self.url, fetch_redirect_response=False)
                self.assertEqual(said(response), [expected])
                self.assertTrue(StockType.objects.filter(name="Recette arrivée entre deux aperçus").exists())
                self.assertEqual(StockType.objects.get(name="Recette A").loss_percent, 12)
                # The preview shown now is the stored one, still to confirm.
                self.assertEqual(shown_preview(self.client.get(self.url)), tab_b)

        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            response = self.post("importer", preview=tab_b, recettes="remplacer")
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
        posted = {
            "sections": ["fournisseurs", "factures", "banque"],
            "strategie-factures": "remplacer",
            "strategie-banque": "fusionner",
        }
        self.client.post(url, {**posted, "action": "previsualiser"})
        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            self.client.post(url, {**posted, "action": "importer", "apercu": shown_preview(self.client.get(url))})
        before.assert_called_once_with("import", {"banque"})

    def test_an_unclosed_selection_is_refused_and_sent_back_ticked(self):
        response = self.post("previsualiser", sections=["recettes"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            said(response),
            [
                (
                    "La partie « Recettes » a besoin de : Enseignes et fournisseurs, Associations produits → articles. "
                    "Elles sont maintenant cochées : prévisualisez de nouveau."
                )
            ],
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
        self.assertIn(
            "choisissez « Remplacer » pour Enseignes et fournisseurs, Sources de factures, afin",
            new_database_note(self.client.get(self.url)),
        )
        Supplier.objects.create(code="EXEMPLE", name="Fournisseur Exemple")
        self.assertNotContains(self.client.get(self.url), "Base neuve")

    def test_the_new_database_note_names_every_part_installed_in_it(self):
        """The migrations install the bank's format and recognition rules,
        the returnable types and the UBA slip format too: merged into a new
        database, an archive's edited copy is a conflict and the installed
        one stays. An archive of the bank's rules alone - what another bar on
        the same bank takes - is told so as well."""
        stage = stage_of({"regles_banque"})
        url = reverse("transfer:data_import_stage", args=[stage.token])
        self.assertEqual(
            new_database_note(self.client.get(url)),
            "Base neuve : choisissez « Remplacer » pour Règles de la banque, afin que ce qui est installé d'office "
            "prenne les réglages de l'archive.",
        )
        stage = stage_of({"types_consignes", "regles_banque", "recettes", "associations", "fournisseurs"})
        self.assertIn(
            "pour Enseignes et fournisseurs, Règles de la banque, Types et formats de consignes, afin",
            new_database_note(self.client.get(reverse("transfer:data_import_stage", args=[stage.token]))),
        )
        Supplier.objects.create(code="EXEMPLE", name="Fournisseur Exemple")
        self.assertEqual(new_database_note(self.client.get(url)), "")

    def test_the_new_database_note_leaves_out_a_part_holding_rows_of_its_own(self):
        """« Remplacer » deletes what the archive does not name: a part that
        holds a row a person made - an ignore rule (none is installed), a
        format, a recognition rule, a type, a slip format, a source - is not
        « installé d'office », and the note asked to delete it. An espace
        without its first invoice may well have imported statements and
        typed rules already (review, 02/10/2026)."""
        from invoices.models import InvoiceType
        from returnables.tests.support import make_format

        stage = stage_of({"fournisseurs", "sources", "regles_banque", "types_consignes"})
        url = reverse("transfer:data_import_stage", args=[stage.token])
        everything = (
            "pour Enseignes et fournisseurs, Sources de factures, Règles de la banque, Types et formats de consignes,"
        )
        self.assertIn(everything, new_database_note(self.client.get(url)))
        # A seed edited is still the seed: « Remplacer » is what gives it the
        # archive's settings.
        StatementFormat.objects.update(delimiter=",")
        OperationRule.objects.update(is_active=False)
        ReturnableType.objects.update(position=7)
        self.assertIn(everything, new_database_note(self.client.get(url)))

        def renamed():
            rule = OperationRule.objects.first()
            name = rule.name
            OperationRule.objects.filter(pk=rule.pk).update(name="Renommée")
            return lambda: OperationRule.objects.filter(pk=rule.pk).update(name=name)

        def made(row):
            return row.delete

        franprix = Supplier.objects.get(code="FRANPRIX")
        own_rows = {
            "regles_banque": (
                lambda: made(IgnoreRule.objects.create(pattern="PRET IMMO")),
                lambda: made(
                    StatementFormat.objects.create(
                        name="Ma banque (CSV)", date_column=1, label_columns="2", amount_column=3
                    )
                ),
                lambda: made(OperationRule.objects.create(name="Ma règle", meaning="debit", pattern="PRLV MOI")),
                renamed,
            ),
            "types_consignes": (
                lambda: made(ReturnableType.objects.create(name="Fûts 20 L", position=9)),
                lambda: made(make_format(name="Bon du caviste", supplier=franprix)),
            ),
            "sources": (lambda: made(InvoiceType.objects.create(name="Franprix - tickets", supplier=franprix)),),
        }
        for key, makers in own_rows.items():
            for index, make in enumerate(makers):
                with self.subTest(section=key, row=index):
                    undo = make()
                    note = new_database_note(self.client.get(url))
                    undo()
                    self.assertIn("Base neuve", note)
                    self.assertNotIn(INFO[key].label, note)
                    for other in {"fournisseurs", "sources", "regles_banque", "types_consignes"} - {key}:
                        self.assertIn(INFO[other].label, note)
        self.assertIn(everything, new_database_note(self.client.get(url)))

    def test_the_new_database_note_needs_the_suppliers_in_the_archive(self):
        """An old associations file holds no « Enseignes et fournisseurs »:
        the note would ask for a box that is disabled (review, 19/09). Nor
        does this version's « Banque » alone hold anything installed: no
        rule is carved out of it (archive.carved)."""
        stage = stage_of({"banque"})
        self.assertEqual(stage.sections, {"banque"})
        self.assertNotContains(self.client.get(reverse("transfer:data_import_stage", args=[stage.token])), "Base neuve")
        stage = staged(forge({"associations": {"records": [], "files": []}}))
        self.assertNotContains(self.client.get(reverse("transfer:data_import_stage", args=[stage.token])), "Base neuve")

    def test_the_new_database_note_names_only_a_box_that_can_be_ticked(self):
        """A part whose lane is missing is not drawn, and one whose suppliers'
        is drawn disabled: « Remplacer » cannot be chosen for either, and the
        note asked for it (the review of 19/09, again)."""
        stage = stage_of({"fournisseurs", "types_consignes", "regles_banque"})
        url = reverse("transfer:data_import_stage", args=[stage.token])
        with registry.swap({key: FAKES[key] for key in INFO if key != "regles_banque"}):
            page = self.client.get(url)
        self.assertEqual(checkbox(page, "regles_banque"), "")
        self.assertIn("pour Enseignes et fournisseurs, Types et formats de consignes, afin", new_database_note(page))
        with registry.swap({key: FAKES[key] for key in INFO if key != "fournisseurs"}):
            page = self.client.get(url)
        self.assertIn(" disabled", checkbox(page, "types_consignes"))
        self.assertIn("pour Règles de la banque, afin", new_database_note(page))
        with registry.swap({key: FAKES[key] for key in INFO if key not in ("fournisseurs", "regles_banque")}):
            self.assertEqual(new_database_note(self.client.get(url)), "")

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
        for counts in (
            {"articles fictifs": "onze"},
            {"articles fictifs": [1]},
            {"articles fictifs": None},
            {"articles fictifs": True},
            {"articles fictifs": {"n": 1}},
            "onze",
            [1],
        ):
            with self.subTest(counts=counts):

                def manifest(data, counts=counts):
                    data["sections"]["sources"]["counts"] = counts
                    data["sections"]["fournisseurs"]["counts"] = {"articles fictifs": 2, "autre": "deux"}
                    return data

                path = forge(
                    {"fournisseurs": {"records": [], "files": []}, "sources": {"records": [], "files": []}},
                    manifest=manifest,
                )
                stage = staging.stage_upload(SimpleUploadedFile("archive.zip", path.read_bytes()))
                response = self.client.get(reverse("transfer:data_import_stage", args=[stage.token]))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "archive : 2 articles fictifs —")
                self.assertNotContains(response, "onze")

    def test_an_archive_damaged_on_the_way_is_refused_in_french(self):
        from transfer.tests.test_archive import damaged

        response = self.client.post(
            reverse("transfer:data_import"),
            {"archive": SimpleUploadedFile("a.zip", damaged("manifest.json").read_bytes())},
        )
        self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
        self.assertEqual(said(response), ["Fichier altéré dans l'archive : manifest.json"])

        response = self.client.post(
            reverse("transfer:data_import"),
            {"archive": SimpleUploadedFile("a.zip", damaged("sources.json").read_bytes())},
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
                response = self.client.post(
                    reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", path.read_bytes())}
                )
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
        response = self.client.post(
            reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", path.read_bytes())}
        )
        self.assertRedirects(response, reverse("transfer:data_import"), fetch_redirect_response=False)
        self.assertEqual(said(response), ["Archive refusée : manifest.json contient un caractère invalide."])

        path = escaped(
            forge(sources),
            "sources.json",
            lambda payload: {"records": [{"name": "Source \ud800", "unit": "L", "loss_percent": "10.00"}], "files": []},
        )
        response = self.client.post(
            reverse("transfer:data_import"), {"archive": SimpleUploadedFile("a.zip", path.read_bytes())}
        )
        url = response["Location"]
        response = self.client.post(url, {"action": "previsualiser", "sections": ["sources"]})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertEqual(said(response), ["Archive refusée : sources.json contient un caractère invalide."])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertFalse(StockType.objects.filter(category="sources").exclude(name="Source A").exists())

        old = (
            '{"version": 1, "products": [{"supplier": "Grossiste Exemple", "raw_name": "RHUM EXEMPLE \\ud800", '
            '"stock_type_name": "Rhum essai"}]}'
        )
        response = self.client.post(
            reverse("transfer:data_import"),
            {"archive": SimpleUploadedFile("marginmate-associations.json", old.encode("ascii"))},
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
        # Putting a database copy back is done with the server stopped: what
        # only the server's operator can do is asked of him, and no path of
        # the server is shown.
        self.assertContains(response, "Seul l'administrateur peut revenir à une copie")
        self.assertContains(response, "donnez-lui le nom du fichier")
        self.assertNotContains(response, str(safety.backup_path()))
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

    def confirm(self, typed, preview=None):
        """From the page: the confirm names the preview it shows, unless
        `preview` says otherwise."""
        if preview is None:
            preview = shown_preview(self.client.get(self.url))
        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            response = self.client.post(
                self.url, {**self.selection, "action": "effacer", "confirmation": typed, "apercu": preview}
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
        self.assertEqual(
            re.findall(r"<th[^>]*>([^<]*)</th>", report.content.decode()), ["Partie", "Supprimés", "Modifiés"]
        )

    def test_a_database_that_moved_since_the_preview_is_not_cleared(self):
        self.preview()
        fake_row("factures", "Document arrivé après l'aperçu")
        response, before = self.confirm("EFFACER")
        # The backups are taken first, outside the transaction; the run then
        # finds it is not the one previewed, and is undone.
        before.assert_called_once_with("effacement", {"factures", "inventaires"})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(said(response), [views.MOVED_CLEAR])
        self.assertEqual(
            views.MOVED_CLEAR, "La base a changé depuis l'aperçu : rien n'a été effacé. Voici le nouvel aperçu."
        )
        self.assertEqual(StockType.objects.filter(category="factures").count(), 2)
        page = self.client.get(self.url)
        self.assertRegex(
            page.content.decode(), r"Factures et tickets › articles fictifs</td>\s*<td class=\"num\">2</td>"
        )

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
                response, before = self.confirm("EFFACER", preview=posted)
                before.assert_not_called()
                self.assertRedirects(response, self.url, fetch_redirect_response=False)
                self.assertEqual(said(response), [expected])
                self.assertEqual(StockType.objects.filter(category="factures").count(), 2)
                self.assertEqual(shown_preview(self.client.get(self.url)), tab_b)

        response, before = self.confirm("EFFACER", preview=tab_b)
        before.assert_called_once_with("effacement", {"factures", "inventaires"})
        self.assertRedirects(response, self.url + "?rapport=1", fetch_redirect_response=False)
        self.assertEqual(StockType.objects.filter(category="factures").count(), 0)

    def test_an_expired_preview_is_said_as_such(self):
        self.preview()
        session = self.client.session
        session[views.session_key(views.SESSION_CLEAR)]["at"] = (timezone.now() - timedelta(minutes=31)).isoformat()
        session.save()
        response, before = self.confirm("EFFACER")
        before.assert_not_called()
        self.assertEqual(said(response), [views.EXPIRED])
        self.assertEqual(StockType.objects.filter(category="factures").count(), 1)

    def test_without_typing_the_confirmation_word_nothing_is_cleared(self):
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
            [
                (
                    "Effacer « Factures et tickets » efface aussi : Inventaires. Elle est maintenant cochée : "
                    "voyez de nouveau ce qui sera effacé."
                )
            ],
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
        preview = shown_preview(self.client.get(self.url))
        with mock.patch(
            "transfer.views.safety.before",
            side_effect=safety.SafetyError("Sauvegarde impossible (x) : rien n'a été changé."),
        ):
            response = self.client.post(
                self.url, {**self.selection, "action": "effacer", "confirmation": "EFFACER", "apercu": preview}
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


class OldArchiveTests(FakeSectionsMixin, TestCase):
    """An archive written before « Règles de la banque » and « Types et
    formats de consignes » were parts of their own - every safety backup
    taken until then - holds them in « Banque »'s and « Consignes »'s files
    (archive.CARVED): the Importer tab offers them as the parts they are
    now, each with its own share of the archive's counts."""

    def setUp(self):
        super().setUp()
        self.stage = staged(
            old_archive(
                fournisseurs=({"records": [], "files": []}, {"articles fictifs": 0}),
                banque=(OLD_BANK, OLD_BANK_COUNTS),
                consignes=(OLD_RETURNABLES, OLD_RETURNABLES_COUNTS),
            )
        )
        self.url = reverse("transfer:data_import_stage", args=[self.stage.token])
        self.everything = {"fournisseurs", "regles_banque", "types_consignes", "banque", "consignes"}

    def test_the_parts_carved_out_are_offered_and_ticked(self):
        self.assertEqual(staging.get(self.stage.token).sections, self.everything)
        page = self.client.get(self.url)
        self.assertEqual(ticked(page), self.everything)
        for key in self.everything:
            with self.subTest(key=key):
                self.assertNotIn(" disabled", checkbox(page, key))
        # « Archives en attente » names them too.
        self.assertContains(
            self.client.get(reverse("transfer:data_import")),
            "Enseignes et fournisseurs, Règles de la banque, Types et formats de consignes, Banque, Consignes",
        )

    def test_each_part_shows_its_own_share_of_the_counts(self):
        page = self.client.get(self.url)
        self.assertEqual(
            archive_counts(page, "regles_banque"),
            {"1 format de relevé", "3 règles de reconnaissance", "2 règles « sans facture »"},
        )
        self.assertEqual(
            archive_counts(page, "banque"),
            {"1 opération", "0 paiements", "4 noms de payeurs appris", "5 payeurs retenus (entrées d'argent)"},
        )
        self.assertEqual(archive_counts(page, "types_consignes"), {"3 types de consigne", "1 format de bons"})
        self.assertEqual(
            archive_counts(page, "consignes"),
            {"2 reprises", "0 photos", "1 bon", "2 lignes de bons", "0 Mo de fichiers"},
        )

    def test_a_new_database_is_told_to_replace_the_rules_and_types_inside(self):
        self.assertIn(
            "pour Enseignes et fournisseurs, Règles de la banque, Types et formats de consignes, afin",
            new_database_note(self.client.get(self.url)),
        )
        bank_alone = staged(old_archive(banque=(OLD_BANK, OLD_BANK_COUNTS)))
        page = self.client.get(reverse("transfer:data_import_stage", args=[bank_alone.token]))
        self.assertEqual(ticked(page), {"banque", "regles_banque"})
        self.assertIn("pour Règles de la banque, afin", new_database_note(page))


class OldArchiveImportTests(TestCase):
    """An older archive through the Importer tab with the real sections, up
    to the confirm: its « Banque » brings the bank's rules back, and its
    « Consignes » the types and slip formats, as they always did - each now
    under the part it belongs to, whose report holds them, so the safety
    archive of a « Remplacer » takes that part. Offered on the page and
    dropped from what the confirm runs, they would be lost with nothing
    said."""

    def written_by_the_old_code(self, within: str, carved: str, old_labels: dict[str, str]):
        """`within` and `carved` exported by this version, then made into the
        archive the code before 02/10/2026 wrote: one file holding both
        sections' lists, `carved`'s counts under their old labels
        (`old_labels`: today's → then's) among `within`'s, and no `carved`
        in the manifest. Returns (the archive, `carved`'s counts)."""
        reader = export_archive({within, carved}, closed=False)
        self.addCleanup(reader.close)
        old, new = reader.section(within), reader.section(carved)
        counts = {**old.counts, **{old_labels[label]: number for label, number in new.counts.items()}}
        payload = {**old.payload(), **new.payload()}
        # The supplier names both read: one dict of every code either names.
        for name in set(old.payload()) & set(new.payload()):
            payload[name] = {**old.payload()[name], **new.payload()[name]}

        def manifest(data):
            data["sections"][within]["counts"] = counts
            return data

        return forge({within: payload}, manifest=manifest), dict(new.counts)

    def import_through_the_page(self, path: Path, within: str, carved: str):
        """Upload, preview, then confirm with « Remplacer » on `carved`, as
        a browser posts each step. Returns (the preview, the safety call)."""
        response = self.client.post(
            reverse("transfer:data_import"), {"archive": SimpleUploadedFile("ancienne.zip", path.read_bytes())}
        )
        url = response["Location"]
        token = url.rstrip("/").split("/")[-1]
        self.assertEqual(ticked(self.client.get(url)), {within, carved})
        data = {"sections": [within, carved], f"strategie-{within}": "fusionner", f"strategie-{carved}": "remplacer"}
        self.client.post(url, {**data, "action": "previsualiser"})
        preview = RunReport.from_json(staging.get(token).state["preview"])
        with mock.patch("transfer.views.safety.before", return_value=BACKUPS) as before:
            response = self.client.post(
                url, {**data, "action": "importer", "apercu": shown_preview(self.client.get(url))}
            )
        self.assertRedirects(response, reverse("transfer:data_import") + "?rapport=1", fetch_redirect_response=False)
        return preview, before

    def test_the_bank_rules_come_back_from_an_old_bank(self):
        IgnoreRule.objects.create(pattern="URSSAF", description="Cotisations")
        before = registry.get("regles_banque").snapshot()
        path, counts = self.written_by_the_old_code(
            "banque",
            "regles_banque",
            {
                "formats de relevé": "formats de relevé",
                "règles de reconnaissance": "règles de reconnaissance",
                "règles « sans facture »": "règles",
            },
        )
        self.assertTrue(all(counts.values()), counts)
        for model in (IgnoreRule, OperationRule, StatementFormat):
            model.objects.all().delete()

        preview, backup = self.import_through_the_page(path, "banque", "regles_banque")
        created = preview.section("regles_banque").tallies
        self.assertEqual({label: created[label].created for label in counts}, counts)
        self.assertFalse(set(preview.section("banque").tallies) & {"règles", *counts})
        backup.assert_called_once_with("import", {"regles_banque"})
        self.assertEqual(registry.get("regles_banque").snapshot(), before)

    def test_the_types_and_slip_formats_come_back_from_old_returnables(self):
        before = registry.get("types_consignes").snapshot()
        path, counts = self.written_by_the_old_code(
            "consignes",
            "types_consignes",
            {"types de consigne": "types de consigne", "formats de bons": "formats de bons"},
        )
        self.assertTrue(all(counts.values()), counts)
        SlipFormat.objects.all().delete()
        ReturnableType.objects.all().delete()

        preview, backup = self.import_through_the_page(path, "consignes", "types_consignes")
        created = preview.section("types_consignes").tallies
        self.assertEqual({label: created[label].created for label in counts}, counts)
        self.assertFalse(set(preview.section("consignes").tallies) & set(counts))
        backup.assert_called_once_with("import", {"types_consignes"})
        self.assertEqual(registry.get("types_consignes").snapshot(), before)


class SeededSectionsTests(TestCase):
    """`views.SEEDED_SECTIONS` is what the migrations install into every
    database, read off the real sections of a new one (the test database is
    migrated like a new espace): a part missing from it is merged into a new
    database, and the archive's edited copy of its installed rows stays a
    conflict nobody was told to replace; a part named there that installs
    nothing asks for « Remplacer » for no reason."""

    def test_they_are_the_configuration_parts_a_new_database_holds_rows_of(self):
        self.assertTrue(views._fresh_database())
        holding = {key for key in INFO if INFO[key].group == Group.CONFIG and any(registry.get(key).count().values())}
        self.assertEqual(holding, set(views.SEEDED_SECTIONS))

    def test_a_new_database_holds_only_what_they_say_is_installed(self):
        """Each part's « nothing but the seeds » reads the migrations' own
        names: a seed renamed by a later migration, or one added, and a new
        espace would never be told to replace it."""
        for key in views.SEEDED_SECTIONS:
            with self.subTest(section=key):
                self.assertTrue(views.holds_only_seeds(key))


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
            # The bank's « Entrées d'argent »: the parenthesis is a complement.
            "payeurs retenus (entrées d'argent)": "1 payeur retenu (entrées d'argent)",
            "mouvements d'achat": "1 mouvement d'achat",
            "ventes saisies à la main": "1 vente saisie à la main",
            "règles « sans facture »": "1 règle « sans facture »",
            "règles de reconnaissance": "1 règle de reconnaissance",
            "formats de relevé": "1 format de relevé",
            # « Trésorerie »'s, in the bank's counts.
            "points de trésorerie": "1 point de trésorerie",
            "ajustements de trésorerie": "1 ajustement de trésorerie",
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
        self.assertEqual(
            views._counts_text({"fournisseurs": 1234, "prix connus": 0}), "1\xa0234 fournisseurs · 0 prix connus"
        )

    def test_what_is_not_a_count_is_left_out(self):
        self.assertEqual(
            views._counts_text({"sources": "onze", "lignes": [1], "fichiers": True, "documents": 2}), "2 documents"
        )
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
        self.assertIn(
            "Sauvegarde faite avant : <code>C:/sauvegardes/2026-09-19_155653_avant-import.sqlite3</code>.", one
        )
        two = self.render(safety=BACKUPS)
        self.assertIn("Sauvegardes faites avant : <code>", two)
        self.assertIn("_avant-effacement.zip</code>.", two)

    def test_a_clear_draws_what_it_deletes_first(self):
        html = self.render(mode="clear", preview=True)
        self.assertEqual(re.findall(r"<th[^>]*>([^<]*)</th>", html), ["Partie", "À supprimer", "À modifier"])
        self.assertRegex(
            html, r"Factures et tickets › documents</td> <td class=\"num\">2</td> <td class=\"num\">0</td> </tr>"
        )
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

    def setUp(self):
        super().setUp()
        # Every page wants a login: the test tenant's owner.
        log_in_the_browser(self.driver, self.live_server_url)

    def ticked(self) -> set[str]:
        return set(
            self.driver.execute_script(
                "return Array.from(document.querySelectorAll('input[name=sections]:checked')).map(b => b.value);"
            )
        )

    def click(self, selector):
        self.driver.execute_script("document.querySelector(arguments[0]).click();", selector)

    def test_untick_all_on_a_full_archive(self):
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
        forced = self.driver.execute_script(
            "return document.querySelector('input[value=fournisseurs]').getAttribute('aria-disabled');"
        )
        self.assertEqual(forced, "true")

    def test_untick_all_after_tick_all_on_export(self):
        from transfer.registry import INFO

        self.driver.get(self.live_server_url + reverse("transfer:data_home"))
        self.click("[data-tick-all]")
        self.assertEqual(len(self.ticked()), len(INFO))
        self.click("[data-untick-all]")
        self.assertEqual(self.ticked(), set())


class CsrfTests(FakeSectionsMixin, TestCase):
    def test_every_post_needs_its_token(self):
        stage = stage_of({"fournisseurs"})
        # Logged in as the tenant's owner (tests/runner.py): the token is
        # all that is missing.
        client = self.client_class(enforce_csrf_checks=True)
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
