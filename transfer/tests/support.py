"""What the transfer tests share.

Two halves:

* **FakeSections** stand for the nine real sections, so the harness - the
  runner, the views, the safety net - is tested without waiting for the
  lanes and without their data. Each fake keeps its records as articles
  (`StockType`) whose category is its key: real rows, written and rolled
  back like a real section's, which is what a preview that "changes
  nothing" has to be proven on.
* **The helpers of §10.2** every section's tests use: export an archive,
  round-trip a section, import, fingerprint the database and the media
  folder, forge a hand-edited archive for the refusal tests.

Archives written here are made from the tests' own invented rows, in the
test settings' temp folders - never from real data.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import zipfile
from pathlib import Path
from typing import ClassVar

from django.apps import apps
from django.conf import settings
from django.test import TestCase

from inventory.models import StockType
from transfer import codec, registry
from transfer.archive import ArchiveError, ArchiveReader
from transfer.registry import INFO
from transfer.report import RunReport
from transfer.runner import run_clear, run_export, run_import
from transfer.sections.base import FileRefused, Section, Strategy

FIELDS = ("unit", "loss_percent")


class FakeSection(Section):
    """One key's records are the articles whose category is that key.

    Class-level knobs, reset by `reset_fakes()`:
    * `calls` - every (method, key) in the order the harness called them;
    * `files` - key → storage names exported with that key (and deleted on
      commit when it is cleared);
    * `fail_on` - (method, key) pairs that raise;
    * `touch` - key → another key whose report its apply/clear writes into,
      as factures writes the bank's payments;
    * `probe` - key → a callable run at the start of its apply (a test's
      assertion about what exists by then).
    """

    key: ClassVar[str] = ""
    calls: ClassVar[list[tuple[str, str]]] = []
    files: ClassVar[dict[str, list[str]]] = {}
    fail_on: ClassVar[set[tuple[str, str]]] = set()
    touch: ClassVar[dict[str, str]] = {}
    probe: ClassVar[dict[str, object]] = {}

    def _log(self, method: str) -> None:
        FakeSection.calls.append((method, self.key))
        if (method, self.key) in FakeSection.fail_on:
            raise RuntimeError(f"{method} {self.key} échoue (test)")

    def _rows(self):
        return StockType.objects.filter(category=self.key).order_by("name")

    def count(self) -> dict[str, int]:
        return {"articles fictifs": self._rows().count()}

    def snapshot(self):
        return [(row.name, row.unit, codec.dump(row, "loss_percent")) for row in self._rows()]

    def export(self, out) -> None:
        self._log("export")
        refs = [out.add_file(name) for name in FakeSection.files.get(self.key, [])]
        records = [
            {"name": row.name, **codec.record(row, (*FIELDS, "created_at"))}
            for row in self._rows()
        ]
        out.write({"records": records, "files": refs}, {"articles fictifs": len(records)})

    def load(self, src) -> None:
        payload = src.payload()
        if not isinstance(payload.get("records"), list):
            raise ArchiveError(f"Archive refusée : {self.key}.json n'a pas de liste « records ».")
        self.records = payload["records"]
        self.refs = payload.get("files") or []

    def apply(self, ctx, report) -> None:
        self._log("apply")
        if self.key in FakeSection.probe:
            FakeSection.probe[self.key]()
        existing = {row.name: row for row in self._rows()}
        for record in self.records:
            codec.note_unknown(report, record, ("name", *FIELDS, "created_at"))
            try:
                row = existing.get(record["name"])
                if row is None:
                    row = StockType(name=record["name"], category=self.key)
                    codec.assign(row, record, FIELDS)
                    row.save()
                    report.created("articles fictifs")
                    continue
                different = codec.differences(row, record, FIELDS)
            except codec.FieldValueError as exc:
                report.skip(f"« {record.get('name')} » : {exc}")
                continue
            if not different:
                report.unchanged("articles fictifs")
            elif ctx.replacing(self.key):
                codec.assign(row, record, FIELDS)
                row.save()
                report.updated("articles fictifs")
            else:
                report.conflict(f"« {row.name} » : différent dans l'archive ({', '.join(different)}) — gardé tel quel")
        for ref in self.refs:
            if not ref:
                continue
            try:
                ctx.save_file(ref, ref.get("name", ""))
            except FileRefused as exc:
                report.skip(str(exc))
                continue
            report.created("fichiers")
        other = FakeSection.touch.get(self.key)
        if other:
            ctx.report(other).deleted("paiements")

    def prune(self, ctx, report) -> None:
        self._log("prune")
        wanted = {record.get("name") for record in self.records}
        for row in self._rows().exclude(name__in=wanted):
            row.delete()
            report.deleted("articles fictifs")

    def clear(self, ctx, report) -> None:
        self._log("clear")
        deleted, _ = self._rows().delete()
        if deleted:
            report.deleted("articles fictifs", deleted)
        for name in FakeSection.files.get(self.key, []):
            ctx.delete_file_on_commit(name)
        other = FakeSection.touch.get(self.key)
        if other:
            ctx.report(other).deleted("paiements")


FAKES: dict[str, type[Section]] = {
    key: type(f"Fake_{key}", (FakeSection,), {"key": key}) for key in INFO
}


def reset_fakes() -> None:
    FakeSection.calls.clear()
    FakeSection.files.clear()
    FakeSection.fail_on.clear()
    FakeSection.touch.clear()
    FakeSection.probe.clear()


def fake_row(key: str, name: str, unit: str = "L", loss_percent: str = "10.00") -> StockType:
    return StockType.objects.create(name=name, unit=unit, category=key, loss_percent=loss_percent)


class FakeSectionsMixin:
    """Every test of the class runs with the fakes standing for all nine
    sections, whatever lanes have landed."""

    fake_keys: tuple[str, ...] | None = None

    def setUp(self):
        super().setUp()
        reset_fakes()
        keys = self.fake_keys or tuple(INFO)
        swap = registry.swap({key: FAKES[key] for key in keys})
        swap.__enter__()
        self.addCleanup(swap.__exit__, None, None, None)
        self.addCleanup(reset_fakes)


# -- the helpers of §10.2 ---------------------------------------------------------------

def archive_dir() -> Path:
    path = Path(settings.DATA_STAGING_DIR) / "test-archives"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_archive_path(stem: str = "archive") -> Path:
    return archive_dir() / f"{stem}-{secrets.token_hex(6)}.zip"


def export_archive(keys, *, closed: bool = True) -> ArchiveReader:
    """Export these sections to a temp archive and open it. `closed=True`
    wants the export closure, as the page does."""
    path = new_archive_path("export")
    run_export(set(keys), path, closed=closed)
    return ArchiveReader(path)


def import_archive(reader: ArchiveReader, strategies, *, preview: bool = False) -> RunReport:
    """`strategies`: a dict key → Strategy, or one Strategy for every
    section of the archive."""
    if isinstance(strategies, (str, Strategy)):
        strategies = {key: Strategy(strategies) for key in reader.sections}
    return run_import(reader, {key: Strategy(value) for key, value in strategies.items()}, preview=preview)


def round_trip(keys, strategy=Strategy.MERGE, *, after_clear=None):
    """snapshot → export (the export closure of `keys`) → clear `keys` and
    whatever depends on them among what was exported → import everything
    exported with `strategy` → snapshot. Returns (before, after), each
    {key: snapshot()} over the exported sections. `after_clear()` runs
    between the clear and the import (to assert the section is empty).
    File deletions of the clear run, so the import has to bring the files
    back."""
    keys = set(keys)
    exported = registry.closure(keys, "export")
    before = {key: registry.get(key).snapshot() for key in registry.ordered(exported)}
    reader = export_archive(exported)
    try:
        clearing = registry.closure(keys, "clear") & exported
        with TestCase.captureOnCommitCallbacks(execute=True):
            run_clear(clearing, preview=False, closed=False)
        if after_clear is not None:
            after_clear()
        import_archive(reader, {key: Strategy(strategy) for key in exported})
    finally:
        reader.close()
    after = {key: registry.get(key).snapshot() for key in registry.ordered(exported)}
    return before, after


def db_fingerprint() -> str:
    """Every row of every model of the app's own apps, ordered: equal before
    and after means nothing was written."""
    digest = hashlib.sha256()
    for app_label in ("inventory", "invoices", "recipes", "bank"):
        for model in sorted(apps.get_app_config(app_label).get_models(), key=lambda m: m._meta.label):
            names = [field.attname for field in model._meta.concrete_fields]
            digest.update(model._meta.label.encode())
            for row in model._default_manager.order_by("pk").values_list(*names):
                digest.update(repr(row).encode())
    return digest.hexdigest()


def media_listing() -> dict[str, str]:
    """name → sha256 of every file under MEDIA_ROOT."""
    root = Path(settings.MEDIA_ROOT)
    listing = {}
    for folder, _dirs, files in os.walk(root):
        for filename in files:
            path = Path(folder) / filename
            listing[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return listing


def forge(reader_or_payloads, *, manifest=None, **changes) -> Path:
    """A hand-edited archive, for the refusal tests.

    From an archive (an ArchiveReader or a path): every member copied, the
    section files named in `changes` replaced - by a payload dict, or by a
    callable taking the parsed payload and returning the new one (or raw
    bytes, written as they are). From payloads (a dict key → payload): a new
    archive holding just those sections, no files.
    `manifest`: a dict merged into the manifest, or a callable returning it."""
    path = new_archive_path("forged")
    if isinstance(reader_or_payloads, dict):
        payloads = dict(reader_or_payloads)
        payloads.update(changes)
        base_manifest = {
            "format": "marginmate-archive",
            "version": 1,
            "created_at": "2026-09-19T14:30:12+02:00",
            "app_revision": None,
            "migrations": {},
            "reason": "export",
            "sections": {
                key: {"file": f"{key}.json", "counts": {}, "requires": list(INFO[key].requires) if key in INFO else []}
                for key in payloads
            },
            "files": [],
        }
        members = {f"{key}.json": _encode(payload) for key, payload in payloads.items()}
        sources = {}
    else:
        source = reader_or_payloads.path if isinstance(reader_or_payloads, ArchiveReader) else Path(reader_or_payloads)
        with zipfile.ZipFile(source) as original:
            base_manifest = json.loads(original.read("manifest.json"))
            members = {}
            sources = {}
            for info in original.infolist():
                if info.filename == "manifest.json":
                    continue
                key = info.filename[: -len(".json")] if info.filename.endswith(".json") else None
                if key in changes:
                    change = changes[key]
                    if callable(change):
                        change = change(json.loads(original.read(info.filename)))
                    members[info.filename] = _encode(change)
                else:
                    sources[info.filename] = original.read(info.filename)
    if callable(manifest):
        base_manifest = manifest(base_manifest)
    elif manifest:
        base_manifest.update(manifest)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, data in {**sources, **members}.items():
            target.writestr(name, data)
        target.writestr("manifest.json", json.dumps(base_manifest, ensure_ascii=False))
    return path


def _encode(payload) -> bytes:
    if isinstance(payload, bytes):
        return payload
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")
