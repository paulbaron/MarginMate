"""What depends on what: the one table (§2.1), and nothing else holds it.

`requires` is hard: records of a section point at records of the one it
requires (a foreign key, or a natural key that must resolve). To export or
import a section is to take what it requires with it; to clear one is to
clear what requires it - the arrows reversed. The page's script only mirrors
this (it is handed `forcing()`); the server refuses a selection that is not
closed, and never completes one in silence.

`recommends` never ticks anything: it is a hint on the page.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from transfer.sections.base import Group, Section

logger = logging.getLogger(__name__)

Mode = Literal["export", "import", "clear"]


@dataclass(frozen=True)
class SectionInfo:
    key: str
    label: str
    group: Group
    order: int
    requires: tuple[str, ...]
    recommends: tuple[str, ...]
    description: str                 # §3.4
    recommend_reason: dict[str, str]  # recommended key → the hint text of §3.4
    #: What clearing it costs outside what « effacé avec » already ticks -
    #: shown on the Effacer tab instead of the hints, which there read as
    #: advice to clear the recommended section too (review, 19/09).
    clear_note: str = ""


def _info(key, label, group, order, requires=(), recommends=(), description="", reasons=None, clear_note="") -> SectionInfo:
    return SectionInfo(
        key, label, group, order, tuple(requires), tuple(recommends), description, dict(reasons or {}), clear_note
    )


INFO: dict[str, SectionInfo] = {
    info.key: info
    for info in (
        _info(
            "fournisseurs", "Enseignes et fournisseurs", Group.CONFIG, 10,
            # « prix connus »: what the review page calls them (« Prix connus
            # chez … »); an « article » is a StockType everywhere else.
            description=(
                "Noms, en-têtes, identifiants appris, nature (produits ou charges) et prix connus (pour les "
                "tickets qui ne nomment pas l'article). Jamais l'état de connexion à Metro."
            ),
            clear_note="la banque perd les noms de payeurs appris pour les fournisseurs effacés",
        ),
        _info(
            "sources", "Sources de factures", Group.CONFIG, 20, requires=["fournisseurs"],
            # An import never switches a portal on (sections/sources.py): the
            # next gather would type the .env variables it names into its page.
            description=(
                "Recherches dans la boîte mail et portails clients. Les identifiants des portails restent "
                "dans le fichier .env (à recopier à la main sur un autre ordinateur) ; un portail importé "
                "arrive inactif, à activer après vérification."
            ),
        ),
        _info(
            "associations", "Associations produits → articles", Group.CONFIG, 30,
            requires=["fournisseurs"], recommends=["factures"],
            description=(
                "Les articles et, pour chaque produit acheté, l'article qu'il remplit et sa conversion "
                "(0,7 L par bouteille…). Un produit appartient à un fournisseur."
            ),
            reasons={"factures": "Factures et tickets — pour que les achats comptent"},
            clear_note="les produits des factures redeviennent « à classer » et leurs achats ne comptent plus en stock",
        ),
        _info(
            "recettes", "Recettes", Group.CONFIG, 40, requires=["associations"],
            description="Prix, TVA et ingrédients (articles et sous-recettes).",
        ),
        _info(
            "liens_ventes", "Liens recettes ↔ ventes", Group.CONFIG, 50,
            requires=["recettes"], recommends=["ventes"],
            description=(
                "Quel produit de la caisse est vendu comme quelle recette, les produits ignorés et les noms "
                "« happy hour ». Le prochain import de la caisse relie de nouveau d'office les produits qui "
                "portent le nom d'une recette."
            ),
            reasons={"ventes": "Ventes"},
            clear_note="les recettes perdent leurs ventes venues de la caisse ; les ventes par jour de la caisse restent",
        ),
        _info(
            "factures", "Factures et tickets", Group.DATA, 60,
            requires=["fournisseurs"], recommends=["associations"],
            description="Chaque document, ses lignes, ce qui a été lu et vérifié, et son fichier (PDF ou photo).",
            reasons={
                "associations": (
                    "Associations produits → articles — sinon les produits de ces factures arrivent « à classer »"
                ),
            },
            clear_note="la banque perd les paiements de ces factures",
        ),
        _info(
            "banque", "Banque", Group.DATA, 70, recommends=["factures", "fournisseurs"],
            description=(
                "Opérations importées, leurs liens aux factures, règles « sans facture » et noms de payeurs "
                "appris."
            ),
            # In two imports the bank cannot tell a line whose payment went
            # with its invoice from one a person unlinked (bank.UNDONE_NOTE).
            reasons={
                "factures": (
                    "Factures et tickets — un lien vers une facture absente est ignoré ; importées ensemble, "
                    "les factures effacées reviennent avec leurs liens"
                ),
                "fournisseurs": "Enseignes et fournisseurs — un nom de payeur appris pour un fournisseur absent est ignoré",
            },
        ),
        _info(
            "ventes", "Ventes", Group.DATA, 80, requires=["recettes"], recommends=["liens_ventes"],
            description=(
                "Quantités vendues par produit de la caisse et par jour, ventes saisies à la main, bons de "
                "vente. Les ventes par recette sont recalculées."
            ),
            reasons={"liens_ventes": "Liens recettes ↔ ventes — sinon aucune vente par recette n'est recalculée"},
        ),
        _info(
            "inventaires", "Inventaires", Group.DATA, 90, requires=["factures", "associations"],
            description=(
                "Chaque comptage avec sa valeur figée et les lignes de factures qui l'ont valorisé, et les "
                "pertes saisies."
            ),
        ),
    )
}

GROUP_LABELS = {Group.CONFIG: "Configuration", Group.DATA: "Données"}

#: Each lane's module registers its section on import. A module that is not
#: there yet (the lanes land one by one) or that fails to import is skipped
#: with a log, so the page still draws the sections that are.
SECTION_MODULES = (
    "suppliers", "sources", "invoices", "associations", "stock_takes", "recipes", "till_links", "sales", "bank",
)

_SECTIONS: dict[str, type[Section]] = {}
_loaded = False


def register(cls: type[Section]) -> type[Section]:
    """Class decorator: `@register class RecipesSection(Section): key = "recettes"`."""
    key = getattr(cls, "key", None)
    assert key in INFO, f"section key {key!r} is not in registry.INFO"
    existing = _SECTIONS.get(key)
    assert existing is None or existing.__qualname__ == cls.__qualname__, (
        f"section {key!r} registered twice ({existing!r} and {cls!r})"
    )
    _SECTIONS[key] = cls
    return cls


def load_sections() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for name in SECTION_MODULES:
        module = f"transfer.sections.{name}"
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name == module:
                logger.info("Partie non installée : %s", module)
            else:
                logger.exception("Partie ignorée : %s ne s'importe pas", module)
        except Exception:  # one broken lane must not take the page down
            logger.exception("Partie ignorée : %s ne s'importe pas", module)


def registered() -> dict[str, type[Section]]:
    load_sections()
    return dict(_SECTIONS)


def is_registered(key: str) -> bool:
    return key in registered()


def get(key: str) -> Section:
    """A fresh instance: a section keeps what load() parsed on itself."""
    classes = registered()
    if key not in classes:
        raise KeyError(f"section {key!r} is not registered")
    return classes[key]()


@contextlib.contextmanager
def swap(classes: dict[str, type[Section]]):
    """For tests: these classes stand for their keys - and only these are
    registered - for the duration."""
    load_sections()
    saved = dict(_SECTIONS)
    _SECTIONS.clear()
    _SECTIONS.update(classes)
    try:
        yield
    finally:
        _SECTIONS.clear()
        _SECTIONS.update(saved)


# -- the graph -----------------------------------------------------------------------

def ordered(keys: Iterable[str]) -> list[str]:
    return sorted(set(keys), key=lambda key: INFO[key].order)


def needs(keys: Iterable[str]) -> set[str]:
    """keys ∪ everything they require, transitively."""
    result: set[str] = set()
    stack = list(keys)
    while stack:
        key = stack.pop()
        if key not in result:
            result.add(key)
            stack.extend(INFO[key].requires)
    return result


def dependents(keys: Iterable[str]) -> set[str]:
    """keys ∪ everything that requires them, transitively."""
    result: set[str] = set()
    stack = list(keys)
    while stack:
        key = stack.pop()
        if key not in result:
            result.add(key)
            stack.extend(other for other, info in INFO.items() if key in info.requires)
    return result


def closure(keys: Iterable[str], mode: Mode, available: Iterable[str] | None = None) -> set[str]:
    keys = set(keys)
    if mode == "clear":
        return dependents(keys)
    if mode == "export":
        return needs(keys)
    if mode == "import":
        return needs(keys) & (set(INFO) if available is None else set(available))
    raise ValueError(f"unknown mode {mode!r}")


def forcing(mode: Mode) -> dict[str, list[str]]:
    """key → the keys that force it when ticked (for the « nécessaire pour … »
    and « effacé avec … » notes). Transitive already, so the page's script
    needs no graph of its own."""
    reach = dependents if mode == "clear" else needs
    return {
        key: [other for other in ordered(INFO) if other != key and key in reach({other})]
        for key in ordered(INFO)
    }


def missing(selected: Iterable[str], mode: Mode, available: Iterable[str] | None = None) -> set[str]:
    """closure − selected: non-empty means the server refuses (§5.7)."""
    selected = set(selected)
    return closure(selected, mode, available) - selected


def labels(keys: Iterable[str]) -> list[str]:
    return [INFO[key].label for key in ordered(keys)]
