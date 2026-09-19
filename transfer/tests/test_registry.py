"""The one table of what depends on what (§2.1), and the closures drawn
from it. Locked: a change to the table has to be deliberate, since both the
page's ticks and the server's refusals follow it."""

from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from transfer import registry
from transfer.registry import INFO
from transfer.sections.base import Group

ALL = set(INFO)

# key: (label, group, order, requires, recommends) - exactly the spec's table.
TABLE = {
    "fournisseurs": ("Enseignes et fournisseurs", Group.CONFIG, 10, (), ()),
    "sources": ("Sources de factures", Group.CONFIG, 20, ("fournisseurs",), ()),
    "associations": ("Associations produits → articles", Group.CONFIG, 30, ("fournisseurs",), ("factures",)),
    "recettes": ("Recettes", Group.CONFIG, 40, ("associations",), ()),
    "liens_ventes": ("Liens recettes ↔ ventes", Group.CONFIG, 50, ("recettes",), ("ventes",)),
    "factures": ("Factures et tickets", Group.DATA, 60, ("fournisseurs",), ("associations",)),
    "banque": ("Banque", Group.DATA, 70, (), ("factures", "fournisseurs")),
    "ventes": ("Ventes", Group.DATA, 80, ("recettes",), ("liens_ventes",)),
    "inventaires": ("Inventaires", Group.DATA, 90, ("factures", "associations"), ()),
}


class TableTests(SimpleTestCase):
    def test_the_table_is_the_specs(self):
        self.assertEqual(
            {key: (i.label, i.group, i.order, i.requires, i.recommends) for key, i in INFO.items()},
            TABLE,
        )

    def test_every_section_says_what_it_holds_and_why_a_hint(self):
        for key, info in INFO.items():
            with self.subTest(key=key):
                self.assertTrue(info.description)
                self.assertEqual(info.key, key)
                # Every recommendation the page shows has its reason.
                self.assertLessEqual(set(info.recommend_reason), set(info.recommends))
        self.assertIn("à classer", INFO["factures"].recommend_reason["associations"])

    def test_the_picker_says_what_an_import_does_not_bring_back_on_its_own(self):
        """A portal from an archive arrives inactive (sections/sources.py),
        and the bank's links come back with their invoices only in one
        import: in two, the bank takes the lines for links a person undid
        (bank.UNDONE_NOTE)."""
        self.assertIn("un portail importé arrive inactif", INFO["sources"].description)
        self.assertIn("sur un autre ordinateur", INFO["sources"].description)
        self.assertIn(
            "importées ensemble, les factures effacées reviennent avec leurs liens",
            INFO["banque"].recommend_reason["factures"],
        )

    def test_order_is_a_topological_order_so_the_graph_has_no_cycle(self):
        for key, info in INFO.items():
            for required in info.requires:
                with self.subTest(key=key, requires=required):
                    self.assertLess(INFO[required].order, info.order)

    def test_the_bank_requires_nothing(self):
        """A hard link to the invoices would make « Effacer les factures »
        wipe the bank too."""
        self.assertEqual(INFO["banque"].requires, ())
        self.assertNotIn("banque", registry.closure({"factures"}, "clear"))


class ClosureTests(SimpleTestCase):
    def test_export_takes_what_a_section_requires(self):
        self.assertEqual(registry.closure({"recettes"}, "export"), {"recettes", "associations", "fournisseurs"})
        self.assertEqual(registry.closure({"banque"}, "export"), {"banque"})
        self.assertEqual(
            registry.closure({"inventaires"}, "export"), {"inventaires", "factures", "associations", "fournisseurs"}
        )

    def test_clear_takes_what_requires_a_section(self):
        self.assertEqual(registry.closure({"fournisseurs"}, "clear"), ALL - {"banque"})
        self.assertEqual(registry.closure({"factures"}, "clear"), {"factures", "inventaires"})
        self.assertEqual(
            registry.closure({"associations"}, "clear"),
            {"associations", "recettes", "liens_ventes", "ventes", "inventaires"},
        )
        self.assertEqual(registry.closure({"recettes"}, "clear"), {"recettes", "liens_ventes", "ventes"})
        for alone in ("sources", "liens_ventes", "banque", "ventes", "inventaires"):
            with self.subTest(key=alone):
                self.assertEqual(registry.closure({alone}, "clear"), {alone})

    def test_import_takes_only_what_the_archive_has(self):
        self.assertEqual(
            registry.closure({"inventaires"}, "import", available={"inventaires", "factures"}),
            {"inventaires", "factures"},
        )
        self.assertEqual(registry.closure({"recettes"}, "import"), {"recettes", "associations", "fournisseurs"})

    def test_empty_is_empty(self):
        for mode in ("export", "import", "clear"):
            self.assertEqual(registry.closure(set(), mode), set())
            self.assertEqual(registry.missing(set(), mode), set())

    def test_needs_and_dependents(self):
        self.assertEqual(registry.needs({"ventes"}), {"ventes", "recettes", "associations", "fournisseurs"})
        self.assertEqual(registry.dependents({"inventaires"}), {"inventaires"})

    def test_missing_is_what_the_server_refuses(self):
        self.assertEqual(registry.missing({"recettes"}, "export"), {"associations", "fournisseurs"})
        self.assertEqual(registry.missing({"recettes", "associations", "fournisseurs"}, "export"), set())
        self.assertEqual(registry.missing({"factures"}, "clear"), {"inventaires"})
        self.assertEqual(registry.missing({"inventaires"}, "import", available={"inventaires"}), set())

    def test_an_unknown_mode_is_a_bug(self):
        with self.assertRaises(ValueError):
            registry.closure({"banque"}, "sync")

    def test_ordered_follows_the_table(self):
        self.assertEqual(
            registry.ordered({"inventaires", "fournisseurs", "banque", "recettes"}),
            ["fournisseurs", "recettes", "banque", "inventaires"],
        )


class ForcingTests(SimpleTestCase):
    """What the page's script is handed: for each box, every box that ticks
    it - transitively, so the script needs no graph."""

    def test_to_export_associations_are_needed_by_recipes_and_stock_takes(self):
        forcing = registry.forcing("export")
        self.assertEqual(forcing["associations"], ["recettes", "liens_ventes", "ventes", "inventaires"])
        self.assertEqual(forcing["fournisseurs"], [key for key in registry.ordered(ALL) if key not in ("fournisseurs", "banque")])
        self.assertEqual(forcing["banque"], [])

    def test_to_clear_the_arrows_are_reversed(self):
        forcing = registry.forcing("clear")
        self.assertEqual(forcing["recettes"], ["fournisseurs", "associations"])
        self.assertEqual(forcing["inventaires"], ["fournisseurs", "associations", "factures"])
        self.assertEqual(forcing["fournisseurs"], [])

    def test_import_forcing_is_exports(self):
        self.assertEqual(registry.forcing("import"), registry.forcing("export"))


class RegistrationTests(SimpleTestCase):
    def test_a_key_outside_the_table_is_refused(self):
        class Stray:
            key = "cocktails"

        with registry.swap({}), self.assertRaises(AssertionError):
            registry.register(Stray)

    def test_a_key_registered_twice_by_two_classes_is_refused(self):
        class One:
            key = "banque"

        class Two:
            key = "banque"

        with registry.swap({}):
            registry.register(One)
            registry.register(One)  # the same class again: a module imported twice
            with self.assertRaises(AssertionError):
                registry.register(Two)

    def test_get_gives_a_fresh_instance(self):
        class Bank:
            key = "banque"

        with registry.swap({"banque": Bank}):
            self.assertIsNot(registry.get("banque"), registry.get("banque"))
            with self.assertRaises(KeyError):
                registry.get("factures")

    def test_a_missing_or_broken_lane_module_is_skipped_with_a_log(self):
        """The page draws whatever sections are there: a lane not landed yet,
        or one that fails to import, is logged and left out."""
        with registry.swap({}), mock.patch.object(registry, "_loaded", False), \
                mock.patch.object(registry, "SECTION_MODULES", ("pas_encore_la",)):
            with self.assertLogs("transfer.registry", level="INFO") as logs:
                registry.load_sections()
            self.assertIn("pas_encore_la", "\n".join(logs.output))
        with registry.swap({}), mock.patch.object(registry, "_loaded", False), \
                mock.patch.object(registry, "SECTION_MODULES", ("casse",)), \
                mock.patch("importlib.import_module", side_effect=SyntaxError("invalid syntax")):
            with self.assertLogs("transfer.registry", level="ERROR") as logs:
                registry.load_sections()
            self.assertIn("casse", "\n".join(logs.output))

    def test_every_key_has_a_registered_section(self):
        """Runs once every lane has landed; until then it says which are
        missing and skips. A module that exists but registers nothing (or
        fails to import) fails here."""
        folder = Path(registry.__file__).resolve().parent / "sections"
        absent = [name for name in registry.SECTION_MODULES if not (folder / f"{name}.py").exists()]
        if absent:
            self.skipTest(f"sections not landed yet: {', '.join(absent)}")
        self.assertEqual(set(registry.registered()), ALL)
