"""The contract every section implements, and the contexts the harness
hands them (§5.2 of the spec - frozen: the lanes code against it).

A section is one checkbox. What it depends on, its label and its
description live in `registry.INFO`, not here, so the graph is one table.
"""

from __future__ import annotations

import enum
import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from django.core.exceptions import SuspiciousFileOperation
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction

from transfer import archive
from transfer.report import SectionReport

if TYPE_CHECKING:  # pragma: no cover - typing only
    from transfer.archive import ArchiveReader, SectionReader, SectionWriter
    from transfer.keys import (
        ArticleResolver,
        InvoiceIndex,
        ProductResolver,
        RecipeResolver,
        SupplierResolver,
    )

logger = logging.getLogger(__name__)


class Group(enum.StrEnum):
    CONFIG = "configuration"
    DATA = "donnees"


class Strategy(enum.StrEnum):
    MERGE = "fusionner"
    REPLACE = "remplacer"


class FileRefused(ValueError):
    """A file ref that cannot be stored (a dangerous or foreign name, a
    member the archive does not declare). The message is French and says
    why; the section skips the record with it (§4.5)."""


@dataclass
class Dirty:
    """What the sections changed that derived data depends on; rebuilt once, at the end (rebuild.py)."""
    lines: set[int] = field(default_factory=set)            # invoice lines whose PURCHASE movement must be rebuilt
    products: set[int] = field(default_factory=set)         # classification changed: all their lines' movements + invoice statuses
    status_products: set[int] = field(default_factory=set)  # invoice statuses only
    recipes: set[int] = field(default_factory=set)          # "laddition" RecipeSale rows to resync
    pos_products: set[int] = field(default_factory=set)     # total_quantity / first_seen / last_seen to recount


class Section(ABC):
    """One checkbox. Metadata (label, group, order, requires, recommends,
    description) lives in registry.INFO[key], not here."""

    key: ClassVar[str]

    # -- what this database holds ------------------------------------------
    @abstractmethod
    def count(self) -> dict[str, int]:
        """French plural label → number, in display order, e.g.
        {"documents": 893, "lignes": 4213, "fichiers": 1520, "Mo de fichiers": 421}.
        Cheap: aggregate queries only (it is drawn on every visit)."""

    @abstractmethod
    def snapshot(self) -> Any:
        """Everything this section covers, keyed by natural keys, pk-free,
        canonical (sorted, Decimals as strings, '' and None folded where the
        format folds them), plus the derived data rebuilt from it
        (e.g. each invoice line's PURCHASE movement). Used by the round-trip
        tests and the real-data rehearsal; never by the page."""

    # -- export ----------------------------------------------------------------
    @abstractmethod
    def export(self, out: SectionWriter) -> None:
        """Write this section: out.write(payload, counts) once, and
        out.add_file(field_file) for each file (returns the file ref)."""

    # -- import (the harness calls load for all, then apply in order, then prune in reverse) --
    @abstractmethod
    def load(self, src: SectionReader) -> None:
        """Parse and check the structure of src.payload(); keep it on self.
        A structural problem (a list that is not a list, a required key
        missing) raises ArchiveError(fr message): the whole import is refused
        before anything is written. A bad *record* is not structural: apply()
        skips it with a reason."""

    @abstractmethod
    def apply(self, ctx: ImportContext, report: SectionReport) -> None:
        """Create and update, per ctx.strategy(self.key) (§6.1, §6.2).
        Never deletes a record of its own section (that is prune), except
        children replaced as a whole (lines, ingredients, a take's lines)."""

    def prune(self, ctx: ImportContext, report: SectionReport) -> None:
        """REPLACE only: delete what the file does not have, except what kept
        data still needs (kept, and said). Called in REVERSE order after
        every apply. Default: nothing."""

    # -- clear -----------------------------------------------------------------
    @abstractmethod
    def clear(self, ctx: ClearContext, report: SectionReport) -> None:
        """Remove everything this section covers, with the app's own deletion
        rules. Called in reverse order, after every section that depends on
        it has been cleared (the closure guarantees they are all in)."""


def _report_for(reports: dict[str, SectionReport], key: str) -> SectionReport:
    """A section may write into another's report (factures deleting the
    bank's payments), including one of a section not in this run: it is
    created on first use, so what it says is not lost."""
    if key not in reports:
        reports[key] = SectionReport.for_key(key)
    return reports[key]


def _delete_if_unreferenced(name: str) -> None:
    """Runs after commit: remove a stored file no row names any more. Asked
    of every FileField of every model rather than of the two known today, so
    a file field added later cannot lose a file another row still shows."""
    from django.apps import apps
    from django.db import models

    for model in apps.get_models():
        for model_field in model._meta.get_fields():
            if isinstance(model_field, models.FileField) and model._default_manager.filter(
                **{model_field.name: name}
            ).exists():
                return
    try:
        default_storage.delete(name)
    except OSError:  # locked on Windows, or already gone: left, never fatal after a commit
        logger.warning("Fichier non supprimé après l'import : %s", name, exc_info=True)


@dataclass
class ImportContext:
    preview: bool
    strategies: dict[str, Strategy]          # ticked sections of this run (closure applied)
    archive_sections: frozenset[str]
    reader: ArchiveReader
    dirty: Dirty
    reports: dict[str, SectionReport]        # one per section; a section may write into another's (§6.5)
    suppliers: SupplierResolver
    invoices: InvoiceIndex
    # Files this run stored, removed by the runner if the run fails.
    stored_files: list[str] = field(default_factory=list, repr=False)
    _resolvers: dict[str, Any] = field(default_factory=dict, repr=False)
    _target_shas: dict[str, str] = field(default_factory=dict, repr=False)

    def strategy(self, key: str) -> Strategy:
        return self.strategies[key]

    def importing(self, key: str) -> bool:
        return key in self.strategies

    def replacing(self, key: str) -> bool:
        return self.strategies.get(key) == Strategy.REPLACE

    def report(self, key: str) -> SectionReport:
        return _report_for(self.reports, key)

    # Rebuilt fresh on each call to a section's apply/prune (cheap: ≤ 1 000
    # rows); within one call, the same resolver, so what a section add()s
    # it finds again. The runner calls refresh() between sections.
    def refresh(self) -> None:
        self._resolvers.clear()

    def articles(self) -> ArticleResolver:
        from transfer.keys import ArticleResolver

        return self._resolver("articles", ArticleResolver)

    def products(self) -> ProductResolver:
        """Told what the archive names (keys.archive_product_keys) for every
        section, not only the ones that thought to ask: the invoices' own
        resolver folded an unclassified twin (« … FÛT » / « … Fût ») onto
        the product created just before it. Supplier keys are resolved again
        each time, since a supplier the fournisseurs section creates in this
        run only resolves after it; the section files are parsed once."""
        from transfer.keys import ProductResolver, archive_product_keys

        if "products" not in self._resolvers:
            self._resolvers["products"] = ProductResolver(archive_product_keys(self.reader, self.suppliers))
        return self._resolvers["products"]

    def recipes(self) -> RecipeResolver:
        from transfer.keys import RecipeResolver

        return self._resolver("recipes", RecipeResolver)

    def _resolver(self, name, cls):
        if name not in self._resolvers:
            self._resolvers[name] = cls()
        return self._resolvers[name]

    # -- files -------------------------------------------------------------------
    def check_file(self, ref: dict | None, desired_name: str) -> None:
        """Raise FileRefused unless `ref` is a member this archive declares
        and `desired_name` is a storage name an import may write. Runs in
        the preview too, so the preview skips exactly what the confirm
        skips."""
        if not self.reader.has_file(ref):
            raise FileRefused(f"fichier absent de l'archive (« {desired_name} »)")
        problem = archive.storage_name_problem(desired_name)
        if problem:
            raise FileRefused(problem)

    def save_file(self, ref: dict, desired_name: str) -> str:
        """Preview: returns desired_name, writes nothing. Apply: if storage
        already holds desired_name with the same sha256, reuses it; else
        streams the member into default_storage.save(desired or an available
        name, max_length=100), records the name for cleanup if the run fails,
        and returns the name actually stored."""
        self.check_file(ref, desired_name)
        if self.preview:
            return desired_name
        if self.file_matches(ref, desired_name):
            return desired_name
        try:
            # On Windows the alternative name comes back joined with a
            # backslash ("invoices/2026/09\\x_Ab12Cd3.pdf"); Django turns it
            # into a slash when it saves, and so does this, before checking.
            name = default_storage.get_available_name(desired_name, max_length=100).replace("\\", "/")
        except SuspiciousFileOperation as exc:
            raise FileRefused(f"nom de fichier refusé (« {desired_name} ») : {exc}") from exc
        problem = archive.storage_name_problem(name)
        if problem:
            raise FileRefused(problem)
        try:
            with self.reader.open_file(ref) as stream:
                stored = default_storage.save(name, File(stream, name=Path(name).name), max_length=100)
        except BaseException:
            # A half-written file (a member altered mid-way) must not stay:
            # nothing names it, and nothing would ever remove it.
            if default_storage.exists(name) and name not in self.stored_files:
                try:
                    default_storage.delete(name)
                except OSError:
                    logger.warning("Fichier partiel non supprimé : %s", name, exc_info=True)
            raise
        self.stored_files.append(stored)
        return stored

    def file_matches(self, ref: dict | None, name: str) -> bool:
        """Whether storage already holds `name` with the ref's content: by
        size first, and the sha256 only when the sizes are equal (§6.4)."""
        if not ref or not name or "sha256" not in ref:
            return False
        try:
            if not default_storage.exists(name) or default_storage.size(name) != ref.get("size"):
                return False
        except (OSError, SuspiciousFileOperation):
            return False
        if name not in self._target_shas:
            digest = hashlib.sha256()
            with default_storage.open(name, "rb") as handle:
                for chunk in iter(lambda: handle.read(archive.CHUNK), b""):
                    digest.update(chunk)
            self._target_shas[name] = digest.hexdigest()
        return self._target_shas[name] == ref["sha256"]

    def delete_file_on_commit(self, name: str) -> None:
        """Only if no row still names it at commit time. Never in a preview:
        its callbacks are dropped with the rollback anyway, and this makes
        sure of it."""
        if self.preview or not name:
            return
        transaction.on_commit(lambda: _delete_if_unreferenced(name), robust=True)


@dataclass
class ClearContext:
    preview: bool
    clearing: frozenset[str]                 # closed under dependents
    dirty: Dirty
    reports: dict[str, SectionReport]

    def report(self, key: str) -> SectionReport:
        return _report_for(self.reports, key)

    def delete_file_on_commit(self, name: str) -> None:
        if self.preview or not name:
            return
        transaction.on_commit(lambda: _delete_if_unreferenced(name), robust=True)
