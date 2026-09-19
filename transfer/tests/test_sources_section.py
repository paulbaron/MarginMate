"""« Sources de factures » (§7.2, §10.2): each invoice type with its mailbox
search or its customer portal.

A portal's settings name the .env variables holding its login - never the
login - and the archive must carry only those names: the test sets a value
in the environment and looks for it in the archive's bytes.

Every name, address and pattern below is invented.
"""

import os
from datetime import datetime
from datetime import timezone as dt_timezone
from unittest import mock

from django.test import TestCase

from invoices.models import EmailInvoiceSource, InvoiceType, WebsiteInvoiceSource
from tests.factories import make_supplier
from transfer import registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.runner import run_clear
from transfer.sections import sources as section
from transfer.sections.base import Strategy
from transfer.tests.support import (
    db_fingerprint,
    export_archive,
    forge,
    import_archive,
    round_trip,
)

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
UTC = dt_timezone.utc
SECRET = "mot-de-passe-du-portail-essai"


def email_source(supplier, name, sender, *, subject="", active=True, created=None) -> InvoiceType:
    invoice_type = InvoiceType.objects.create(supplier=supplier, name=name, is_active=active)
    EmailInvoiceSource.objects.create(
        invoice_type=invoice_type, sender_pattern=sender, subject_pattern=subject, attachment_pattern=r"(?i)\.pdf$"
    )
    InvoiceType.objects.filter(pk=invoice_type.pk).update(created_at=created or datetime(2026, 5, 4, 8, 30, tzinfo=UTC))
    return invoice_type


def portal_source(supplier, name, prefix, created=None) -> InvoiceType:
    invoice_type = InvoiceType.objects.create(supplier=supplier, name=name, source_kind=InvoiceType.SourceKind.WEBSITE)
    WebsiteInvoiceSource.objects.create(
        invoice_type=invoice_type,
        login_url="https://portail.eau.example/connexion",
        username_env=f"{prefix}_LOGIN",
        password_env=f"{prefix}_PASSWORD",
        invoices_url="https://portail.eau.example/factures",
        navigation="Mon espace\nMes factures",
        link_selector="a.telecharger",
        show_browser=True,
    )
    InvoiceType.objects.filter(pk=invoice_type.pk).update(created_at=created or datetime(2026, 6, 1, 9, 0, tzinfo=UTC))
    return invoice_type


def build_sources():
    """The seeded « UBA - Factures », a shop's mailbox search (one paused),
    and a portal."""
    cave = make_supplier(code="CAVE_ESSAI", name="Cave Essai")
    water = make_supplier(code="EAU_ESSAI", name="Eau Essai", expenses_only=True)
    email_source(cave, "Cave Essai - Factures", r"(?i)factures@cave\.example", subject="(?i)facture")
    email_source(cave, "Cave Essai - Avoirs", r"(?i)avoirs@cave\.example", active=False)
    portal_source(water, "Eau Essai", "EAU_ESSAI")
    return cave, water


def as_restored(snapshot: list) -> list:
    """The sources' snapshot as an import brings it back: every portal
    inactive, since an import never switches one on - where a portal signs
    in, and with which .env variables, is the owner's to check first."""
    return [
        {**record, "is_active": False} if record["source_kind"] == section.WEBSITE else record for record in snapshot
    ]


ENV_NOTE = (
    "Les identifiants des portails (EAU_ESSAI_LOGIN, EAU_ESSAI_PASSWORD) sont lus dans le fichier .env : s'il "
    "s'agit d'un autre ordinateur, recopiez-les à la main."
)


def portal_note(how: str, name: str = "Eau Essai", supplier: str = "Eau Essai") -> str:
    return (
        f"Source « {name} » ({supplier}) : {how} — elle se connecte à https://portail.eau.example/connexion avec "
        "EAU_ESSAI_LOGIN et EAU_ESSAI_PASSWORD du fichier .env ; vérifiez l'adresse et les variables, puis cochez "
        "« Active » sur sa page (Achats → Sources)."
    )


class GuardTests(TestCase):
    def test_every_field_is_exported_or_said_why(self):
        for model, exported, left_out in (
            (InvoiceType, section.TYPE_FIELDS, section.TYPE_NOT_EXPORTED),
            (EmailInvoiceSource, section.EMAIL_FIELDS, section.ROW_NOT_EXPORTED),
            (WebsiteInvoiceSource, section.WEBSITE_FIELDS, section.ROW_NOT_EXPORTED),
        ):
            names = {model_field.name for model_field in model._meta.concrete_fields}
            self.assertEqual(names, set(exported) | set(left_out), model.__name__)

    def test_a_portals_env_names_go_in_the_archive_never_their_values(self):
        build_sources()
        with mock.patch.dict(os.environ, {"EAU_ESSAI_PASSWORD": SECRET, "EAU_ESSAI_LOGIN": "identifiant-essai"}):
            reader = export_archive({"sources"}, closed=False)
        self.addCleanup(reader.close)
        data = reader.path.read_bytes()
        self.assertNotIn(SECRET.encode(), data)
        self.assertNotIn(b"identifiant-essai", data)
        records = {record["name"]: record for record in reader.section("sources").payload()["sources"]}
        website = records["Eau Essai"]["website"]
        self.assertEqual((website["username_env"], website["password_env"]), ("EAU_ESSAI_LOGIN", "EAU_ESSAI_PASSWORD"))
        self.assertIsNone(records["Eau Essai"]["email"])

    def test_count(self):
        build_sources()
        self.assertEqual(registry.get("sources").count(), {"sources": InvoiceType.objects.count()})
        self.assertEqual(InvoiceType.objects.count(), 4)


class RoundTripTests(TestCase):
    def setUp(self):
        build_sources()

    def _after_clear(self):
        self.assertFalse(InvoiceType.objects.exists())
        self.assertFalse(EmailInvoiceSource.objects.exists())
        self.assertFalse(WebsiteInvoiceSource.objects.exists())

    def test_merge(self):
        created = dict(InvoiceType.objects.values_list("name", "created_at"))
        before, after = round_trip({"sources"}, MERGE, after_clear=self._after_clear)
        self.assertEqual(after, {**before, "sources": as_restored(before["sources"])})
        self.assertEqual(len(after["sources"]), 4)
        # Not in the snapshot (a seeded source already here keeps its own),
        # but restored on every source the import creates.
        self.assertEqual(dict(InvoiceType.objects.values_list("name", "created_at")), created)

    def test_replace(self):
        before, after = round_trip({"sources"}, REPLACE, after_clear=self._after_clear)
        self.assertEqual(after, {**before, "sources": as_restored(before["sources"])})

    def test_only_the_portal_comes_back_inactive(self):
        before, after = round_trip({"sources"}, MERGE)
        changed = [record["name"] for record, again in zip(before["sources"], after["sources"]) if record != again]
        self.assertEqual(changed, ["Eau Essai"])
        self.assertTrue(InvoiceType.objects.get(name="Cave Essai - Factures").is_active)


class IdempotenceTests(TestCase):
    def setUp(self):
        build_sources()
        self.reader = export_archive({"sources"}, closed=False)
        self.addCleanup(self.reader.close)

    def test_merge_says_everything_is_unchanged(self):
        report = import_archive(self.reader, MERGE).section("sources")
        self.assertEqual(report.tallies["sources"].unchanged, 4)
        self.assertEqual((report.conflicts, report.skipped, report.notes), ([], [], []))
        self.assertFalse(report.changes)

    def test_replace_changes_nothing(self):
        before = db_fingerprint()
        report = import_archive(self.reader, REPLACE).section("sources")
        self.assertFalse(report.changes)
        self.assertEqual(db_fingerprint(), before)


class MergeAndReplaceTests(TestCase):
    """One source changed, one only here, one only in the archive."""

    def setUp(self):
        self.cave, self.water = build_sources()
        self.reader = export_archive({"sources"}, closed=False)
        self.addCleanup(self.reader.close)
        EmailInvoiceSource.objects.filter(invoice_type__name="Cave Essai - Factures").update(subject_pattern="(?i)note")
        InvoiceType.objects.filter(name="Eau Essai").delete()
        email_source(self.cave, "Cave Essai - Relances", r"(?i)relances@cave\.example")

    def test_merge(self):
        report = import_archive(self.reader, MERGE).section("sources")
        self.assertEqual(
            report.conflicts,
            ["Source « Cave Essai - Factures » (Cave Essai) : différente dans l'archive (objet) — gardée telle quelle"],
        )
        self.assertEqual(EmailInvoiceSource.objects.get(invoice_type__name="Cave Essai - Factures").subject_pattern, "(?i)note")
        self.assertTrue(InvoiceType.objects.filter(name="Cave Essai - Relances").exists())
        self.assertEqual(report.tallies["sources"].created, 1)
        portal = WebsiteInvoiceSource.objects.get(invoice_type__name="Eau Essai")
        self.assertEqual(portal.navigation, "Mon espace\nMes factures")
        self.assertFalse(portal.invoice_type.is_active)
        self.assertEqual(report.notes, [portal_note("créée inactive"), ENV_NOTE])

    def test_replace(self):
        report = import_archive(self.reader, REPLACE).section("sources")
        self.assertEqual(
            EmailInvoiceSource.objects.get(invoice_type__name="Cave Essai - Factures").subject_pattern, "(?i)facture"
        )
        self.assertFalse(InvoiceType.objects.filter(name="Cave Essai - Relances").exists())
        self.assertFalse(EmailInvoiceSource.objects.filter(sender_pattern__contains="relances").exists())
        tally = report.tallies["sources"]
        self.assertEqual((tally.created, tally.updated, tally.deleted, tally.unchanged), (1, 1, 1, 2))
        self.assertEqual(
            InvoiceType.objects.get(name="Eau Essai").created_at, datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        )
        self.assertFalse(InvoiceType.objects.get(name="Eau Essai").is_active)
        self.assertEqual(report.notes, [portal_note("créée inactive"), ENV_NOTE])

    def test_the_seeded_uba_source_is_like_any_other(self):
        InvoiceType.objects.filter(name="UBA - Factures").delete()
        report = import_archive(self.reader, MERGE).section("sources")
        self.assertTrue(InvoiceType.objects.filter(name="UBA - Factures", supplier__code="UBA").exists())
        self.assertEqual(report.tallies["sources"].created, 2)

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before = db_fingerprint()
        preview = import_archive(self.reader, REPLACE, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(preview.outcome(), import_archive(self.reader, REPLACE).outcome())


class KindChangeTests(TestCase):
    def test_email_to_website_under_replace_leaves_exactly_one_row(self):
        water = make_supplier(code="EAU_ESSAI", name="Eau Essai", expenses_only=True)
        portal_source(water, "Eau Essai", "EAU_ESSAI")
        reader = export_archive({"sources"}, closed=False)
        self.addCleanup(reader.close)
        InvoiceType.objects.filter(name="Eau Essai").delete()
        email_source(water, "Eau Essai", r"(?i)factures@eau\.example")

        report = import_archive(reader, REPLACE).section("sources")
        invoice_type = InvoiceType.objects.get(name="Eau Essai")
        self.assertEqual(invoice_type.source_kind, InvoiceType.SourceKind.WEBSITE)
        self.assertEqual(WebsiteInvoiceSource.objects.filter(invoice_type=invoice_type).count(), 1)
        self.assertFalse(EmailInvoiceSource.objects.filter(invoice_type=invoice_type).exists())
        self.assertEqual(report.tallies["sources"].updated, 1)
        # A mailbox search turned into a portal signs in somewhere new.
        self.assertFalse(invoice_type.is_active)
        self.assertEqual(report.notes, [portal_note("désactivée"), ENV_NOTE])

    def test_under_merge_it_is_a_conflict(self):
        water = make_supplier(code="EAU_ESSAI", name="Eau Essai", expenses_only=True)
        portal_source(water, "Eau Essai", "EAU_ESSAI")
        reader = export_archive({"sources"}, closed=False)
        self.addCleanup(reader.close)
        InvoiceType.objects.filter(name="Eau Essai").delete()
        email_source(water, "Eau Essai", r"(?i)factures@eau\.example")
        report = import_archive(reader, MERGE).section("sources")
        self.assertEqual(
            report.conflicts,
            ["Source « Eau Essai » (Eau Essai) : différente dans l'archive (type de source) — gardée telle quelle"],
        )
        self.assertTrue(EmailInvoiceSource.objects.filter(invoice_type__name="Eau Essai").exists())


def forged_portal(**website) -> ArchiveReader:
    """An archive holding one portal for UBA - the probe's: an archive from
    « another bar » the page ticks every section of."""
    settings = {
        "login_url": "https://portail-inconnu.example/connexion",
        "username_env": "METRO_EMAIL",
        "password_env": "METRO_PASSWORD",
        "invoices_url": "",
        "navigation": "",
        "username_selector": "",
        "password_selector": "",
        "submit_selector": "",
        "link_selector": "",
        "next_selector": "",
        "show_browser": False,
        **website,
    }
    payload = {
        "supplier_names": {"UBA": "UBA"},
        "sources": [
            {
                "supplier": "UBA",
                "name": "Portail essai",
                "parser_key": "",
                "source_kind": "WEBSITE",
                "is_active": True,
                "email": None,
                "website": settings,
            }
        ],
    }
    return ArchiveReader(forge({"sources": payload}))


class PortalTrustTests(TestCase):
    """Where a portal signs in and which .env variables it types there come
    from the archive; the next gather reads those variables and types them
    into that page. An archive naming the application's own secrets (Metro's,
    the mailbox's, the till's, the AI's) had them typed into a site nobody
    chose."""

    def _import(self, reader, strategy=MERGE):
        self.addCleanup(reader.close)
        return import_archive(reader, {"sources": strategy}).section("sources")

    def test_a_portal_naming_the_apps_own_variables_is_refused(self):
        report = self._import(forged_portal())
        self.assertFalse(InvoiceType.objects.filter(name="Portail essai").exists())
        self.assertEqual(
            report.skipped,
            [
                (
                    "Source « Portail essai » (UBA) : variable de l'identifiant : « METRO_EMAIL » est une variable de "
                    "l'application elle-même (Metro, la boîte mail, la caisse, l'IA) : jamais celle d'un portail ; "
                    "variable du mot de passe : « METRO_PASSWORD » est une variable de l'application elle-même "
                    "(Metro, la boîte mail, la caisse, l'IA) : jamais celle d'un portail"
                )
            ],
        )
        self.assertEqual(report.notes, [])

    def test_every_family_of_the_apps_variables_is_refused(self):
        for name in (
            "INVOICE_EMAIL_APP_PASSWORD", "UBA_EMAIL_APP_PASSWORD", "LADDITION_PASSWORD", "ANTHROPIC_API_KEY",
            "DJANGO_SECRET_KEY", "METRO_PASSWORD",
        ):
            with self.subTest(name=name):
                report = self._import(forged_portal(username_env="PORTAIL_ESSAI_LOGIN", password_env=name))
                self.assertEqual(len(report.skipped), 1, report.skipped)
                self.assertIn(f"« {name} » est une variable de l'application elle-même", report.skipped[0])
                self.assertFalse(InvoiceType.objects.filter(name="Portail essai").exists())

    def test_the_list_holds_every_variable_the_settings_read(self):
        """One list, checked against config/settings.py: a setting added
        later cannot be left out of it in silence. And the source form
        refuses the same names (invoices.models), not a list of its own."""
        import re
        from pathlib import Path

        from django.conf import settings

        from invoices import models

        self.assertIs(section.app_env_name, models.app_env_name)
        text =(Path(settings.BASE_DIR) / "config" / "settings.py").read_text(encoding="utf-8")
        names = set(re.findall(r"""(?:environ\.get|env_bool)\(\s*["']([A-Z][A-Z0-9_]*)["']""", text))
        self.assertIn("METRO_PASSWORD", names)
        self.assertEqual({name for name in names if not section.app_env_name(name)}, set())

    def test_names_that_only_look_alike_are_a_portals(self):
        for name in ("METROPOLE_LOGIN", "EAU_ESSAI_PASSWORD", "FREEBOX_LOGIN", "UBA_LOGIN"):
            with self.subTest(name=name):
                self.assertFalse(section.app_env_name(name))

    def test_replace_never_hands_an_existing_portal_the_apps_variables(self):
        water = make_supplier(code="EAU_ESSAI", name="Eau Essai", expenses_only=True)
        portal_source(water, "Eau Essai", "EAU_ESSAI")
        reader = export_archive({"sources"}, closed=False)
        self.addCleanup(reader.close)

        def change(payload):
            for record in payload["sources"]:
                if record["name"] == "Eau Essai":
                    record["website"]["password_env"] = "LADDITION_PASSWORD"
            return payload

        report = self._import(ArchiveReader(forge(reader, sources=change)), REPLACE)
        portal = WebsiteInvoiceSource.objects.get(invoice_type__name="Eau Essai")
        self.assertEqual((portal.password_env, portal.invoice_type.is_active), ("EAU_ESSAI_PASSWORD", True))
        self.assertEqual(len(report.skipped), 1)
        self.assertIn("« LADDITION_PASSWORD » est une variable de l'application elle-même", report.skipped[0])

    def test_a_new_portal_is_created_inactive_and_said(self):
        report = self._import(forged_portal(username_env="PORTAIL_ESSAI_LOGIN", password_env="PORTAIL_ESSAI_PASSWORD"))
        invoice_type = InvoiceType.objects.get(name="Portail essai")
        self.assertFalse(invoice_type.is_active)
        self.assertEqual(
            report.notes,
            [
                (
                    "Source « Portail essai » (UBA) : créée inactive — elle se connecte à "
                    "https://portail-inconnu.example/connexion avec PORTAIL_ESSAI_LOGIN et PORTAIL_ESSAI_PASSWORD du "
                    "fichier .env ; vérifiez l'adresse et les variables, puis cochez « Active » sur sa page (Achats "
                    "→ Sources)."
                ),
                (
                    "Les identifiants des portails (PORTAIL_ESSAI_LOGIN, PORTAIL_ESSAI_PASSWORD) sont lus dans le "
                    "fichier .env : s'il s'agit d'un autre ordinateur, recopiez-les à la main."
                ),
            ],
        )

    def test_a_new_portal_the_archive_holds_inactive_says_nothing_more(self):
        def inactive(payload):
            payload["sources"][0]["is_active"] = False
            return payload

        with forged_portal(username_env="PORTAIL_ESSAI_LOGIN", password_env="PORTAIL_ESSAI_PASSWORD") as base:
            report = self._import(ArchiveReader(forge(base, sources=inactive)))
        self.assertFalse(InvoiceType.objects.get(name="Portail essai").is_active)
        self.assertEqual(len(report.notes), 1)
        self.assertTrue(report.notes[0].startswith("Les identifiants des portails"))


class PortalReplaceTests(TestCase):
    """« Remplacer » on a portal this database has: the archive may change
    where it signs in, or only how it finds the invoices."""

    def setUp(self):
        self.water = make_supplier(code="EAU_ESSAI", name="Eau Essai", expenses_only=True)
        self.portal = portal_source(self.water, "Eau Essai", "EAU_ESSAI")
        self.reader = export_archive({"sources"}, closed=False)
        self.addCleanup(self.reader.close)

    def _replace(self, **here):
        WebsiteInvoiceSource.objects.filter(invoice_type=self.portal).update(**here)
        report = import_archive(self.reader, REPLACE).section("sources")
        self.portal.refresh_from_db()
        return report

    def test_another_address_switches_it_off(self):
        report = self._replace(login_url="https://ancien-portail.eau.example/connexion")
        self.assertEqual(self.portal.website_source.login_url, "https://portail.eau.example/connexion")
        self.assertFalse(self.portal.is_active)
        self.assertEqual(report.notes, [portal_note("désactivée"), ENV_NOTE])

    def test_another_variable_switches_it_off(self):
        report = self._replace(username_env="EAU_ANCIEN_LOGIN")
        self.assertEqual(self.portal.website_source.username_env, "EAU_ESSAI_LOGIN")
        self.assertFalse(self.portal.is_active)
        self.assertEqual(report.notes[0], portal_note("désactivée"))

    def test_the_same_address_and_variables_keep_it_on(self):
        report = self._replace(navigation="Autre chemin")
        self.assertEqual(self.portal.website_source.navigation, "Mon espace\nMes factures")
        self.assertTrue(self.portal.is_active)
        self.assertEqual(report.notes, [ENV_NOTE])

    def test_an_import_never_switches_a_portal_on(self):
        """Imported once, a forged portal is inactive; the same archive
        replaced again finds its own address here and would have switched it
        on with nobody having looked at it."""
        InvoiceType.objects.filter(pk=self.portal.pk).update(is_active=False)
        report = self._replace(navigation="Autre chemin")
        self.assertFalse(self.portal.is_active)
        self.assertEqual(report.notes, [portal_note(LEFT_OFF), ENV_NOTE])


#: Why a portal the archive has active stays off, as both strategies say it.
LEFT_OFF = "laissée inactive (un import n'active jamais un portail)"


class RestoredPortalTests(TestCase):
    """A restore brings a portal back inactive. Merging the same archive
    again then said « différente dans l'archive (active) — gardée telle
    quelle »: a conflict, which reads as « Remplacer » would settle it - and
    it would not, since no import switches a portal on. It is said why,
    with what to do, and the portal is otherwise « inchangée »."""

    def setUp(self):
        self.water = make_supplier(code="EAU_ESSAI", name="Eau Essai", expenses_only=True)
        self.portal = portal_source(self.water, "Eau Essai", "EAU_ESSAI")
        self.reader = export_archive({"sources"}, closed=False)
        self.addCleanup(self.reader.close)
        run_clear({"sources"}, preview=False, closed=False)
        import_archive(self.reader, REPLACE)
        self.assertFalse(InvoiceType.objects.get(name="Eau Essai").is_active)

    def test_merging_again_says_why_it_is_off_and_nothing_else(self):
        report = import_archive(self.reader, MERGE).section("sources")
        self.assertEqual(report.conflicts, [])
        self.assertEqual(report.tallies["sources"].unchanged, InvoiceType.objects.count())
        self.assertFalse(report.changes)
        self.assertEqual(report.notes, [portal_note(LEFT_OFF)])
        self.assertFalse(InvoiceType.objects.get(name="Eau Essai").is_active)

    def test_replacing_again_says_the_same(self):
        merged = import_archive(self.reader, MERGE).section("sources")
        replaced = import_archive(self.reader, REPLACE).section("sources")
        self.assertEqual(replaced.to_json(), merged.to_json())

    def test_what_else_differs_is_still_a_conflict(self):
        WebsiteInvoiceSource.objects.filter(invoice_type__name="Eau Essai").update(navigation="Autre chemin")
        report = import_archive(self.reader, MERGE).section("sources")
        self.assertEqual(
            report.conflicts, ["Source « Eau Essai » (Eau Essai) : différente dans l'archive (navigation) — gardée telle quelle"]
        )
        self.assertEqual(report.notes, [portal_note(LEFT_OFF)])

    def test_a_portal_on_here_that_the_archive_has_off_is_still_a_conflict(self):
        """That one « Remplacer » does settle: it switches the portal off."""
        InvoiceType.objects.filter(name="Eau Essai").update(is_active=True)
        with ArchiveReader(forge(self.reader, sources=lambda payload: {
            **payload,
            "sources": [
                {**record, "is_active": False} if record["name"] == "Eau Essai" else record for record in payload["sources"]
            ],
        })) as off:
            report = import_archive(off, MERGE).section("sources")
        self.assertEqual(
            report.conflicts, ["Source « Eau Essai » (Eau Essai) : différente dans l'archive (active) — gardée telle quelle"]
        )
        self.assertEqual(report.notes, [])
        self.assertTrue(InvoiceType.objects.get(name="Eau Essai").is_active)


class RefusalTests(TestCase):
    def setUp(self):
        build_sources()
        self.reader = export_archive({"sources"}, closed=False)
        self.addCleanup(self.reader.close)
        InvoiceType.objects.all().delete()

    def _import(self, change, strategy=MERGE):
        reader = ArchiveReader(forge(self.reader, sources=change))
        self.addCleanup(reader.close)
        return import_archive(reader, strategy).section("sources")

    @staticmethod
    def _edit(name, **row):
        def change(payload):
            for record in payload["sources"]:
                if record["name"] == name:
                    for key, value in row.items():
                        if key in ("email", "website"):
                            record[key] = value if value is None else {**(record[key] or {}), **value}
                        else:
                            record[key] = value
            return payload

        return change

    def test_a_file_without_its_list_is_refused_whole(self):
        reader = ArchiveReader(forge(self.reader, sources={"supplier_names": {}}))
        self.addCleanup(reader.close)
        with self.assertRaisesMessage(ArchiveError, "sources.json n'a pas de liste « sources »"):
            import_archive(reader, MERGE)

    def test_a_bad_regex_is_skipped_with_the_forms_message(self):
        report = self._import(self._edit("Cave Essai - Factures", email={"sender_pattern": "(?i)factures@(cave"}))
        self.assertFalse(InvoiceType.objects.filter(name="Cave Essai - Factures").exists())
        self.assertEqual(len(report.skipped), 1)
        self.assertTrue(
            report.skipped[0].startswith(
                "Source « Cave Essai - Factures » (Cave Essai) : expéditeur : Expression régulière invalide :"
            ),
            report.skipped[0],
        )
        self.assertEqual(report.tallies["sources"].created, 3)

    def test_a_credential_in_place_of_an_env_name_is_skipped(self):
        report = self._import(self._edit("Eau Essai", website={"password_env": "hunter2"}))
        self.assertFalse(InvoiceType.objects.filter(name="Eau Essai").exists())
        self.assertIn("variable du mot de passe : Le nom d'une variable du fichier .env", report.skipped[0])

    def test_settings_of_the_wrong_kind_are_skipped(self):
        report = self._import(self._edit("Eau Essai", website=None))
        self.assertEqual(report.skipped, ["Source « Eau Essai » (Eau Essai) : ses réglages (portail) manquent"])

    def test_an_unknown_supplier_is_skipped(self):
        report = self._import(self._edit("Cave Essai - Avoirs", supplier="INCONNU_ESSAI"))
        self.assertIn("Source « Cave Essai - Avoirs » : fournisseur inconnu « INCONNU_ESSAI »", report.skipped)

    def test_an_unknown_kind_is_skipped(self):
        report = self._import(self._edit("Cave Essai - Avoirs", source_kind="FAX"))
        self.assertIn("Source « Cave Essai - Avoirs » (Cave Essai) : « source_kind » : valeur inconnue (« FAX »)", report.skipped)

    def test_an_unknown_field_is_noted_once(self):
        def change(payload):
            for record in payload["sources"]:
                record["couleur"] = "bleu"
            return payload

        report = self._import(change)
        self.assertEqual(report.notes.count("champ inconnu ignoré : couleur"), 1)
        self.assertEqual(report.tallies["sources"].created, 4)


class ClearTests(TestCase):
    def test_clear_deletes_every_source_and_its_settings(self):
        build_sources()
        before = db_fingerprint()
        run_clear({"sources"}, preview=True)
        self.assertEqual(db_fingerprint(), before)
        run = run_clear({"sources"}, preview=False)
        self.assertFalse(InvoiceType.objects.exists())
        self.assertFalse(EmailInvoiceSource.objects.exists())
        self.assertFalse(WebsiteInvoiceSource.objects.exists())
        self.assertEqual(run.section("sources").tallies["sources"].deleted, 4)
        self.assertEqual(registry.get("sources").count(), {"sources": 0})
