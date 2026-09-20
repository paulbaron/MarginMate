"""The archive (§4) and the field encoding (§4.3).

An archive comes from outside, so most of this is about what it may NOT
do: point outside where it is written, claim a size it does not have,
inflate into gigabytes. And the encoding is what makes importing a fresh
export of the same data say « inchangé » for every record - the check that
catches a Decimal's places or a time zone.
"""

import hashlib
import io
import json
import os
import tracemalloc
import zipfile
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import SimpleTestCase, TestCase

from inventory.models import StockType
from invoices.models import Invoice, InvoiceLine
from transfer import archive, codec
from transfer.archive import (
    ArchiveError,
    ArchiveReader,
    ArchiveWriter,
    safe_member_name,
    storage_name_problem,
)
from transfer.report import SectionReport
from transfer.tests.support import forge, new_archive_path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_zip(members: dict, manifest=None, *, raw_names: dict | None = None) -> Path:
    """A zip written by hand. `raw_names` maps a placeholder to the name
    actually written - zipfile would clean a NUL or a backslash otherwise."""
    path = new_archive_path("hand")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as target:
        for name, data in members.items():
            info = zipfile.ZipInfo(name)
            if raw_names and name in raw_names:
                info.filename = raw_names[name]
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, data)
        if manifest is not None:
            target.writestr("manifest.json", json.dumps(manifest))
    return path


def manifest_for(sections=(), files=()):
    return {
        "format": "marginmate-archive",
        "version": 1,
        "created_at": "2026-09-19T14:30:12+02:00",
        "sections": {key: {"file": f"{key}.json", "counts": {}, "requires": []} for key in sections},
        "files": list(files),
    }


def stored(name: str, data: bytes) -> str:
    """A file in the test MEDIA_ROOT, under exactly this name."""
    if default_storage.exists(name):
        default_storage.delete(name)
    return default_storage.save(name, ContentFile(data))


class WritingTests(TestCase):
    def build(self, **files):
        path = new_archive_path("written")
        with mock.patch("transfer.archive.app_revision", return_value="abc1234"), \
                ArchiveWriter(path, reason="export") as writer:
            section = writer.section("factures")
            refs = [section.add_file(name) for name in files]
            section.write({"invoices": [], "refs": refs}, {"documents": 0, "fichiers": len(files)})
            writer.section("fournisseurs").write({"suppliers": []}, {"fournisseurs": 0})
            manifest = writer.close()
        return path, manifest, refs

    def test_the_manifest_says_what_the_archive_holds(self):
        name = stored("invoices/2026/09/facture-exemple.pdf", b"%PDF-1.4 exemple")
        path, manifest, refs = self.build(**{name: None})
        self.assertEqual(manifest["format"], "marginmate-archive")
        self.assertEqual(manifest["version"], 1)
        self.assertIsNotNone(datetime.fromisoformat(manifest["created_at"]).tzinfo)
        self.assertEqual(manifest["app_revision"], "abc1234")
        self.assertEqual(manifest["reason"], "export")
        self.assertEqual(
            manifest["sections"]["factures"],
            {"file": "factures.json", "counts": {"documents": 0, "fichiers": 1}, "requires": ["fournisseurs"]},
        )
        self.assertEqual(
            manifest["files"],
            [{"member": f"files/{name}", "size": 16, "sha256": sha(b"%PDF-1.4 exemple")}],
        )
        self.assertEqual(refs[0], {"member": f"files/{name}", "name": name, "size": 16, "sha256": sha(b"%PDF-1.4 exemple")})
        with zipfile.ZipFile(path) as written:
            # Written last: an archive cut short has no manifest.
            self.assertEqual(written.namelist()[-1], "manifest.json")
            self.assertEqual(json.loads(written.read("manifest.json")), manifest)

    def test_documents_are_stored_and_json_deflated(self):
        pdf = stored("invoices/2026/09/a.pdf", b"%PDF" + b"x" * 200)
        photo = stored("receipts/2026/09/b.jpg", b"\xff\xd8" + b"y" * 200)
        text = stored("invoices/2026/09/c.txt", b"z" * 200)
        path, _manifest, _refs = self.build(**{pdf: None, photo: None, text: None})
        with zipfile.ZipFile(path) as written:
            self.assertEqual(written.getinfo(f"files/{pdf}").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(written.getinfo(f"files/{photo}").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(written.getinfo(f"files/{text}").compress_type, zipfile.ZIP_DEFLATED)
            self.assertEqual(written.getinfo("factures.json").compress_type, zipfile.ZIP_DEFLATED)
            self.assertEqual(written.getinfo("manifest.json").compress_type, zipfile.ZIP_DEFLATED)

    def test_a_file_named_twice_is_stored_once_and_a_missing_one_is_said(self):
        name = stored("invoices/2026/09/deux-fois.pdf", b"%PDF deux")
        path = new_archive_path("twice")
        with ArchiveWriter(path, reason="export") as writer:
            section = writer.section("factures")
            first, second = section.add_file(name), section.add_file(name)
            missing = section.add_file("invoices/2026/09/disparu.pdf")
            empty = section.add_file("")
            section.write({}, {})
            manifest = writer.close()
        self.assertEqual(first, second)
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(missing, {"name": "invoices/2026/09/disparu.pdf", "missing": True})
        self.assertIsNone(empty)
        self.assertEqual(manifest["notes"], ["fichier absent du disque : invoices/2026/09/disparu.pdf"])

    def test_a_failed_export_leaves_no_file(self):
        path = new_archive_path("failed")
        with self.assertRaises(RuntimeError), ArchiveWriter(path, reason="export") as writer:
            writer.section("fournisseurs").write({}, {})
            raise RuntimeError("disque plein")
        self.assertFalse(path.exists())

    def test_nan_is_never_written(self):
        path = new_archive_path("nan")
        with self.assertRaises(ValueError), ArchiveWriter(path, reason="export") as writer:
            writer.section("fournisseurs").write({"x": float("nan")}, {})

    def test_a_section_opened_and_never_written_is_a_bug(self):
        path = new_archive_path("unwritten")
        with self.assertRaises(RuntimeError), ArchiveWriter(path, reason="export") as writer:
            writer.section("fournisseurs")
            writer.close()


class StreamingTests(TestCase):
    """A full export is ~420 MB of files: none is ever read whole."""

    def test_a_30_mb_file_goes_in_and_out_in_chunks(self):
        name = "invoices/2026/09/gros-fichier.pdf"
        path = Path(default_storage.path(name))
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with open(path, "wb") as handle:
            for _ in range(30):
                block = os.urandom(1024 * 1024)
                digest.update(block)
                handle.write(block)
        archive_path = new_archive_path("big")
        # 90 MB between them: not left in temp by every run.
        self.addCleanup(path.unlink, missing_ok=True)
        self.addCleanup(archive_path.unlink, missing_ok=True)

        tracemalloc.start()
        try:
            with ArchiveWriter(archive_path, reason="export") as writer:
                section = writer.section("factures")
                ref = section.add_file(name)
                section.write({"ref": ref}, {})
                writer.close()
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 10 * 1024 * 1024)
        self.assertEqual(ref["sha256"], digest.hexdigest())

        tracemalloc.start()
        try:
            with ArchiveReader(archive_path) as reader, reader.open_file(ref) as stream:
                copy = default_storage.save("invoices/2026/09/copie.pdf", File(stream, name="copie.pdf"))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.addCleanup(default_storage.delete, copy)
        self.assertLess(peak, 10 * 1024 * 1024)
        with default_storage.open(copy, "rb") as handle:
            copied = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                copied.update(chunk)
        self.assertEqual(copied.hexdigest(), digest.hexdigest())


class RefusalTests(SimpleTestCase):
    def assertRefused(self, path, message):
        with self.assertRaises(ArchiveError) as caught:
            ArchiveReader(path)
        self.assertEqual(str(caught.exception), message)

    def test_not_a_zip(self):
        path = new_archive_path("text")
        path.write_bytes(b"bonjour")
        self.assertRefused(path, archive.NOT_ZIP_NOR_JSON)

    def test_a_broken_zip(self):
        path = new_archive_path("broken")
        path.write_bytes(b"PK\x03\x04" + b"\x00" * 50)
        self.assertRefused(path, archive.NOT_ARCHIVE)

    def test_no_manifest(self):
        self.assertRefused(write_zip({"fournisseurs.json": "{}"}), "Ce fichier n'est pas une archive MarginMate.")

    def test_a_manifest_of_another_format(self):
        manifest = {**manifest_for(), "format": "autre-chose"}
        self.assertRefused(write_zip({}, manifest), "Ce fichier n'est pas une archive MarginMate.")

    def test_a_newer_version(self):
        manifest = {**manifest_for(), "version": 2}
        self.assertRefused(
            write_zip({}, manifest),
            "Archive faite par une version plus récente de MarginMate (format 2) : mettez l'application à jour "
            "avant de l'importer.",
        )

    def test_a_version_that_is_not_an_int(self):
        for version in ("1", True, None, 1.0):
            with self.subTest(version=version):
                self.assertRefused(write_zip({}, {**manifest_for(), "version": version}), archive.NOT_ARCHIVE)

    def test_zip_slip(self):
        for name in ("../x", "/abs", "C:/x", "a\\..\\b", "files/../../x", "files/a\x00.pdf", "files//x", "files/./x", ""):
            with self.subTest(name=name):
                path = write_zip({"placeholder": b"x"}, manifest_for(), raw_names={"placeholder": name})
                with self.assertRaises(ArchiveError) as caught:
                    ArchiveReader(path)
                self.assertTrue(str(caught.exception).startswith("Archive refusée : nom de fichier dangereux ("))

    def test_safe_names(self):
        self.assertTrue(safe_member_name("files/invoices/2026/09/Monoprix_8EUR44_26_02_2025.pdf"))
        self.assertTrue(safe_member_name("manifest.json"))
        for name in ("", "a" * 256, "a\tb", "a\x7fb", "x\\y", "C:x", "/x", "a/", "a//b", "./a", "a/.."):
            with self.subTest(name=name):
                self.assertFalse(safe_member_name(name))

    def test_too_many_members(self):
        path = write_zip({"a.txt": "1", "b.txt": "2"}, manifest_for())
        with mock.patch.object(archive, "MAX_MEMBERS", 2):
            self.assertRefused(path, "Archive refusée : trop de fichiers.")

    def test_a_file_too_big(self):
        def archive_declaring(size):
            return write_zip(
                {"files/invoices/a.pdf": b"%PDF"},
                manifest_for(files=[{"member": "files/invoices/a.pdf", "size": size, "sha256": sha(b"%PDF")}]),
            )

        self.assertRefused(archive_declaring(300 * 1024**2), "Archive refusée : un fichier dépasse 200 Mo.")
        with mock.patch.object(archive, "MAX_FILE_BYTES", 1024**2):
            self.assertRefused(archive_declaring(2 * 1024**2), "Archive refusée : un fichier dépasse 1 Mo.")
        ArchiveReader(archive_declaring(4)).close()

    def test_too_big_once_uncompressed(self):
        path = write_zip({"a.txt": "x" * 1000}, manifest_for())
        with mock.patch.object(archive, "MAX_TOTAL_BYTES", 500):
            self.assertRefused(path, "Archive refusée : trop volumineuse une fois décompressée.")

    def test_a_member_compressed_like_a_bomb(self):
        path = write_zip({"a.txt": b"\x00" * 200_000}, manifest_for())
        with mock.patch.object(archive, "RATIO_MIN_BYTES", 1000):
            self.assertRefused(path, "Archive refusée : un fichier est anormalement compressé.")

    def test_a_section_file_too_big(self):
        path = write_zip({"fournisseurs.json": json.dumps({"suppliers": ["x" * 5000]})}, manifest_for(["fournisseurs"]))
        with mock.patch.object(archive, "MAX_JSON_BYTES", 1000):
            self.assertRefused(path, "Archive refusée : fournisseurs.json est trop gros.")

    def test_a_declared_section_missing(self):
        self.assertRefused(write_zip({}, manifest_for(["fournisseurs"])), "Archive refusée : fournisseurs.json manque.")


class ReadingTests(TestCase):
    def test_nan_in_a_section_is_refused(self):
        path = write_zip({"fournisseurs.json": '{"suppliers": [NaN]}'}, manifest_for(["fournisseurs"]))
        with ArchiveReader(path) as reader, self.assertRaises(ArchiveError) as caught:
            reader.section("fournisseurs").payload()
        self.assertIn("n'est pas un nombre", str(caught.exception))

    def test_a_section_that_is_not_an_object_is_refused(self):
        path = write_zip({"fournisseurs.json": "[1, 2]"}, manifest_for(["fournisseurs"]))
        with ArchiveReader(path) as reader, self.assertRaises(ArchiveError) as caught:
            reader.section("fournisseurs").payload()
        self.assertEqual(str(caught.exception), "Archive refusée : fournisseurs.json est illisible.")

    def test_an_undeclared_member_is_ignored_and_said(self):
        path = write_zip({"files/invoices/cache.pdf": b"secret", "notes.txt": b"x"}, manifest_for())
        with ArchiveReader(path) as reader:
            self.assertEqual(reader.notes, ["2 fichier(s) de l'archive qu'elle ne déclare pas : ignoré(s)."])
            self.assertFalse(reader.has_file({"member": "files/invoices/cache.pdf"}))
            with self.assertRaises(ArchiveError):
                reader.open_file({"member": "files/invoices/cache.pdf", "name": "invoices/cache.pdf"})

    def test_an_unknown_section_is_ignored_and_said(self):
        path = write_zip({"cocktails.json": "{}"}, manifest_for(["cocktails"]))
        with ArchiveReader(path) as reader:
            self.assertEqual(reader.sections, frozenset())
            self.assertIn("partie inconnue ignorée : cocktails", reader.notes)

    def read_member(self, content: bytes, declared_size: int, declared_sha: str):
        member = "files/invoices/2026/09/x.pdf"
        path = write_zip(
            {member: content},
            manifest_for(files=[{"member": member, "size": declared_size, "sha256": declared_sha}]),
        )
        ref = {"member": member, "name": "invoices/2026/09/x.pdf", "size": declared_size, "sha256": declared_sha}
        with ArchiveReader(path) as reader, reader.open_file(ref) as stream:
            return stream.read()

    def test_a_member_read_as_declared(self):
        self.assertEqual(self.read_member(b"%PDF ok", 7, sha(b"%PDF ok")), b"%PDF ok")

    def test_a_member_larger_than_declared(self):
        with self.assertRaises(ArchiveError) as caught:
            self.read_member(b"%PDF plus long que dit", 5, sha(b"%PDF "))
        self.assertEqual(str(caught.exception), "Fichier altéré dans l'archive : invoices/2026/09/x.pdf")

    def test_a_member_whose_sha_differs(self):
        with self.assertRaises(ArchiveError) as caught:
            self.read_member(b"%PDF autre", 10, sha(b"%PDF sien!"))
        self.assertEqual(str(caught.exception), "Fichier altéré dans l'archive : invoices/2026/09/x.pdf")

    def test_the_forge_helper_edits_a_section(self):
        path = forge({"fournisseurs": {"suppliers": []}})
        changed = forge(path, fournisseurs=lambda payload: {**payload, "suppliers": ["x"]})
        with ArchiveReader(changed) as reader:
            self.assertEqual(reader.section("fournisseurs").payload(), {"suppliers": ["x"]})


DOCUMENT = "files/invoices/2026/09/essai.pdf"
DOCUMENT_DATA = b"%PDF-1.4 essai abcdefgh"


def damaged(member: str, *, header: bool = False, compress_type=zipfile.ZIP_STORED) -> Path:
    """An archive changed on the way: bytes in the middle of `member`'s data
    (its CRC no longer matches, or its deflate stream no longer inflates),
    or - `header` - the signature of its local header."""
    manifest = manifest_for(["sources"], files=[{"member": DOCUMENT, "size": len(DOCUMENT_DATA), "sha256": sha(DOCUMENT_DATA)}])
    members = {
        "sources.json": json.dumps({"sources": [], "marque": "abcdefgh" * 40}).encode(),
        DOCUMENT: DOCUMENT_DATA,
        "manifest.json": json.dumps({**manifest, "reason": "export"}).encode(),
    }
    path = new_archive_path("damaged")
    with zipfile.ZipFile(path, "w") as target:
        for name, data in members.items():
            target.writestr(zipfile.ZipInfo(name), data, compress_type=compress_type)
    with zipfile.ZipFile(path) as source:
        info = source.getinfo(member)
    raw = bytearray(path.read_bytes())
    if header:
        raw[info.header_offset] ^= 0x01
    else:
        # The local header: 30 bytes, then the name and the extra field.
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        middle = start + info.compress_size // 2
        for offset in range(middle, middle + 4):
            raw[offset] ^= 0xFF
    path.write_bytes(bytes(raw))
    return path


class DamagedArchiveTests(TestCase):
    """An archive damaged in transit (a CRC that fails, a deflate stream cut)
    is refused in French, like any other refusal - never a 500."""

    def test_a_damaged_manifest_is_refused(self):
        with self.assertRaises(ArchiveError) as caught:
            ArchiveReader(damaged("manifest.json"))
        self.assertEqual(str(caught.exception), "Fichier altéré dans l'archive : manifest.json")

    def test_a_damaged_section_is_refused_when_it_is_read(self):
        with ArchiveReader(damaged("sources.json")) as reader, self.assertRaises(ArchiveError) as caught:
            reader.section("sources").payload()
        self.assertEqual(str(caught.exception), "Fichier altéré dans l'archive : sources.json")

    def test_a_spoilt_deflate_stream_is_refused(self):
        with ArchiveReader(damaged("sources.json", compress_type=zipfile.ZIP_DEFLATED)) as reader, \
                self.assertRaises(ArchiveError) as caught:
            reader.section("sources").payload()
        self.assertEqual(str(caught.exception), "Fichier altéré dans l'archive : sources.json")

    def test_a_damaged_file_header_is_refused_when_it_is_opened(self):
        ref = {"member": DOCUMENT, "name": "invoices/2026/09/essai.pdf", "size": len(DOCUMENT_DATA), "sha256": sha(DOCUMENT_DATA)}
        with ArchiveReader(damaged(DOCUMENT, header=True)) as reader, self.assertRaises(ArchiveError) as caught, \
                reader.open_file(ref) as stream:
            stream.read()
        self.assertEqual(str(caught.exception), "Fichier altéré dans l'archive : invoices/2026/09/essai.pdf")

    def test_an_undamaged_archive_reads(self):
        """The helper spoils only what it is asked to."""
        path = new_archive_path("sound")
        with zipfile.ZipFile(damaged("sources.json")) as source, zipfile.ZipFile(path, "w") as target:
            for info in source.infolist():
                if info.filename != "sources.json":
                    target.writestr(info, source.read(info.filename))
            target.writestr("sources.json", json.dumps({"sources": []}))
        ref = {"member": DOCUMENT, "name": "invoices/2026/09/essai.pdf", "size": len(DOCUMENT_DATA), "sha256": sha(DOCUMENT_DATA)}
        with ArchiveReader(path) as reader:
            self.assertEqual(reader.section("sources").payload(), {"sources": []})
            with reader.open_file(ref) as stream:
                self.assertEqual(stream.read(), DOCUMENT_DATA)


class StorageNameTests(TestCase):
    """The name an import writes a file under comes from the archive's
    record: it may not leave the two folders documents live in."""

    def test_names_that_are_refused(self):
        for name in ("../x.pdf", "invoices/../../x.pdf", "/etc/passwd", "C:/x.pdf", "config/settings.py",
                     "invoices/" + "a" * 100 + ".pdf", "", None):
            with self.subTest(name=name):
                self.assertIsNotNone(storage_name_problem(name))

    def test_a_name_safe_join_refuses(self):
        with mock.patch.object(default_storage, "path", side_effect=SuspiciousFileOperation("dehors")):
            self.assertEqual(
                storage_name_problem("invoices/2026/09/x.pdf"),
                "nom de fichier hors du dossier des fichiers (« invoices/2026/09/x.pdf »)",
            )

    def test_a_document_name_is_accepted(self):
        self.assertIsNone(storage_name_problem("invoices/2026/09/Monoprix_8EUR44_26_02_2025.pdf"))
        self.assertIsNone(storage_name_problem("receipts/2026/09/0149.jpg"))


class AppRevisionTests(SimpleTestCase):
    def tearDown(self):
        archive.app_revision.cache_clear()

    def test_read_from_a_branch_ref(self):
        root = Path(settings.DATA_STAGING_DIR) / "fake-repo"
        (root / ".git" / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (root / ".git" / "refs" / "heads" / "main").write_text("3771d9d" + "0" * 33 + "\n")
        archive.app_revision.cache_clear()
        with self.settings(BASE_DIR=root):
            self.assertEqual(archive.app_revision(), "3771d9d")

    def test_read_from_packed_refs(self):
        root = Path(settings.DATA_STAGING_DIR) / "fake-repo-packed"
        (root / ".git").mkdir(parents=True, exist_ok=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (root / ".git" / "packed-refs").write_text("# pack-refs\nabcdef1" + "1" * 33 + " refs/heads/main\n")
        archive.app_revision.cache_clear()
        with self.settings(BASE_DIR=root):
            self.assertEqual(archive.app_revision(), "abcdef1")

    def test_none_without_a_repository(self):
        archive.app_revision.cache_clear()
        with self.settings(BASE_DIR=Path(settings.DATA_STAGING_DIR) / "no-repo-here"):
            self.assertIsNone(archive.app_revision())


class CodecTests(TestCase):
    def test_decimals_as_stored(self):
        line = InvoiceLine(quantity=Decimal("0.35"), total_ht=Decimal(-15), unit_cost_ht=Decimal("0.7"))
        self.assertEqual(codec.dump(line, "quantity"), "0.350")
        self.assertEqual(codec.dump(line, "total_ht"), "-15.00")
        self.assertEqual(codec.dump(line, "unit_cost_ht"), "0.7000")
        self.assertEqual(codec.load(InvoiceLine, "quantity", "0.35"), Decimal("0.350"))
        self.assertEqual(codec.load(InvoiceLine, "total_ht", "-2.76"), Decimal("-2.76"))
        self.assertEqual(codec.load(InvoiceLine, "total_ht", "0"), Decimal("0.00"))

    def test_a_decimal_that_does_not_fit_is_refused_not_rounded(self):
        for value in ("1.234", "1e20", "123456789012.00", "NaN", "Infinity", "abc", 1.5, True, [1]):
            with self.subTest(value=value), self.assertRaises(codec.FieldValueError):
                codec.load(InvoiceLine, "total_ht", value)

    def test_dates_and_moments(self):
        self.assertEqual(codec.dump(Invoice(invoice_date=date(2026, 9, 19)), "invoice_date"), "2026-09-19")
        self.assertEqual(codec.load(Invoice, "invoice_date", "2026-09-19"), date(2026, 9, 19))
        moment = datetime(2026, 9, 19, 10, 2, 16, 915895, tzinfo=dt_timezone.utc)
        self.assertEqual(codec.dump(Invoice(imported_at=moment), "imported_at"), "2026-09-19T10:02:16.915895+00:00")
        self.assertEqual(codec.load(Invoice, "imported_at", "2026-09-19T12:02:16.915895+02:00"), moment)
        for bad in ("19/09/2026", 20260919, None):
            with self.subTest(value=bad), self.assertRaises(codec.FieldValueError):
                codec.load(Invoice, "imported_at", bad)

    def test_a_naive_moment_is_refused(self):
        with self.assertRaises(codec.FieldValueError) as caught:
            codec.load(Invoice, "imported_at", "2026-09-19T10:02:16")
        self.assertIn("sans fuseau horaire", str(caught.exception))
        with self.assertRaises(codec.FieldValueError):
            codec.dump(Invoice(imported_at=datetime(2026, 9, 19, 10, 0)), "imported_at")  # noqa: DTZ001 - naive on purpose

    def test_a_moment_off_the_calendar_is_refused(self):
        """A moment that cannot be shown here is refused where it is read,
        so the record is skipped with its reason. Kept, it would be a 500
        on every page drawing it: a page's date filter says the local time,
        and there is none for the first or the last day of the calendar."""
        for value in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:30:00+00:00", "9999-12-31T23:59:59-14:00"):
            with self.subTest(value=value), self.assertRaises(codec.FieldValueError) as caught:
                codec.load(Invoice, "imported_at", value)
            self.assertIn("hors calendrier", str(caught.exception))

    def test_null_where_allowed_and_refused_where_not(self):
        self.assertIsNone(codec.load(Invoice, "invoice_date", None))
        self.assertIsNone(codec.load(Invoice, "printed_total_ttc", None))
        with self.assertRaises(codec.FieldValueError):
            codec.load(InvoiceLine, "total_ht", None)

    def test_json_booleans_integers_and_text(self):
        self.assertEqual(codec.load(Invoice, "parse_checks", [{"label": "x", "passed": True}]), [{"label": "x", "passed": True}])
        self.assertEqual(codec.load(InvoiceLine, "colisage", 6), 6)
        with self.assertRaises(codec.FieldValueError):
            codec.load(InvoiceLine, "colisage", True)  # a bool is not an int
        with self.assertRaises(codec.FieldValueError):
            codec.load(InvoiceLine, "colisage", "6")
        with self.assertRaises(codec.FieldValueError):
            codec.load(StockType, "category", 12)
        with self.assertRaises(codec.FieldValueError):
            codec.load(StockType, "category", "x" * 256)
        self.assertEqual(codec.load(StockType, "category", "  Spiritueux "), "  Spiritueux ")  # no trimming

    def test_an_unknown_choice_is_refused(self):
        self.assertEqual(codec.load(StockType, "unit", "L"), "L")
        with self.assertRaises(codec.FieldValueError) as caught:
            codec.load(StockType, "unit", "LITRES")
        self.assertIn("valeur inconnue", str(caught.exception))

    def test_relations_and_files_are_the_sections_business(self):
        with self.assertRaises(TypeError):
            codec.load(Invoice, "supplier", "METRO")
        with self.assertRaises(TypeError):
            codec.dump(Invoice(), "source_file")

    def test_an_absent_field_is_never_compared_nor_written(self):
        article = StockType(name="Rhum", unit="L", loss_percent=Decimal("10.00"))
        self.assertEqual(codec.differences(article, {}, ("unit", "loss_percent")), [])
        changed = codec.assign(article, {"unit": "KG"}, ("unit", "loss_percent"))
        self.assertEqual(changed, ["unit"])
        self.assertEqual(article.loss_percent, Decimal("10.00"))

    def test_equal_values_written_differently_are_the_same(self):
        """What makes a fresh export of the same data « inchangé »."""
        article = StockType(name="Rhum", unit="L", loss_percent=Decimal(10), category="")
        data = {"loss_percent": "10.00", "category": "", "unit": "L"}
        self.assertEqual(codec.differences(article, data, ("loss_percent", "category", "unit")), [])
        invoice = Invoice(
            imported_at=datetime(2026, 9, 19, 12, 0, tzinfo=dt_timezone.utc),
            parse_checks=[{"passed": True, "label": "x"}],
        )
        self.assertEqual(
            codec.differences(
                invoice,
                {"imported_at": "2026-09-19T14:00:00+02:00", "parse_checks": [{"label": "x", "passed": True}]},
                ("imported_at", "parse_checks"),
            ),
            [],
        )
        self.assertEqual(codec.differences(article, {"loss_percent": "12.50"}, ("loss_percent",)), ["loss_percent"])

    def test_a_bad_value_leaves_the_object_as_it_was(self):
        article = StockType(name="Rhum", unit="L", loss_percent=Decimal("10.00"))
        with self.assertRaises(codec.FieldValueError):
            codec.assign(article, {"unit": "KG", "loss_percent": "abc"}, ("unit", "loss_percent"))
        self.assertEqual(article.unit, "L")

    def test_an_unknown_field_is_said_once(self):
        report = SectionReport(key="fournisseurs", label="Enseignes et fournisseurs")
        for _ in range(3):
            codec.note_unknown(report, {"name": "x", "couleur": "bleu"}, ("name",))
        self.assertEqual(report.notes, ["champ inconnu ignoré : couleur"])
        self.assertEqual(codec.unknown_fields({"a": 1, "b": 2}, ("a",)), {"b"})

    def test_what_is_blank(self):
        for value in ("", [], {}, None, ()):
            self.assertTrue(codec.is_blank(value))
        for value in (0, False, "0", " ", [0], Decimal(0)):
            self.assertFalse(codec.is_blank(value))

    def test_a_record(self):
        article = StockType(name="Rhum", unit="L", loss_percent=Decimal(5))
        self.assertEqual(codec.record(article, ("unit", "loss_percent")), {"unit": "L", "loss_percent": "5.00"})


class ReportTests(SimpleTestCase):
    def test_round_trips_through_json(self):
        from transfer.report import RunReport

        section = SectionReport(key="factures", label="Factures et tickets")
        section.created("documents", 3)
        section.updated("lignes")
        section.conflict("Facture Metro n° 1 : différente")
        report = RunReport(mode="import", preview=True, sections=[section], rebuilt={"mouvements de stock": 2})
        again = RunReport.from_json(json.loads(json.dumps(report.to_json())))
        self.assertEqual(again.to_json(), report.to_json())
        self.assertEqual(again.affected(), {"factures"})

    def test_lists_are_capped_and_counted(self):
        section = SectionReport(key="factures", label="Factures et tickets")
        for number in range(250):
            section.skip(f"ignorée {number}")
        self.assertEqual(len(section.skipped), 200)
        self.assertEqual(section.overflow, {"skipped": 50})
        shown = section.display_lists[0]
        self.assertEqual((shown["title"], len(shown["items"]), shown["more"], shown["total"]), ("Ignorés", 20, 230, 250))

    def test_changes_and_destructive(self):
        section = SectionReport(key="banque", label="Banque")
        self.assertFalse(section.changes)
        section.unchanged("opérations", 5)
        self.assertFalse(section.changes)
        section.created("opérations")
        self.assertTrue(section.changes)
        self.assertFalse(section.destructive)
        section.deleted("paiements")
        self.assertTrue(section.destructive)

    def test_the_fingerprint_names_what_a_run_does(self):
        """What a confirm form posts back to say which preview it shows: the
        outcome only - not when, how long, nor the flag a confirm turns off -
        and the same once read back from the stage's state.json."""
        from transfer.report import RunReport

        def report(documents: int, **fields) -> RunReport:
            section = SectionReport(key="factures", label="Factures et tickets")
            section.deleted("documents", documents)
            section.unchanged("lignes", 4)
            section.note("Facture Metro n° 1 : gardée")
            return RunReport(**{"mode": "import", "preview": True, "sections": [section], "rebuilt": {"mouvements de stock": 2}, **fields})

        shown = report(1)
        self.assertRegex(shown.fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(report(1, preview=False, duration_s=9.4, safety={"database": "x", "archive": ""}).fingerprint, shown.fingerprint)
        self.assertEqual(RunReport.from_json(json.loads(json.dumps(shown.to_json()))).fingerprint, shown.fingerprint)
        self.assertNotEqual(report(2).fingerprint, shown.fingerprint)
        self.assertNotEqual(report(1, mode="clear").fingerprint, shown.fingerprint)

    def test_a_note_about_what_the_run_leaves_alone_is_not_the_run(self):
        """« 4 fichiers que plus rien ne cite dans media/ restent tels
        quels » counts files the run never touches, and it changed under the
        owner between the page's preview and the confirm: every « Effacer »
        was then refused for ever, the page announcing the same deletions
        (20/09). What a run DOES - its tallies, its conflicts, what it skips
        and keeps - is what a preview and its confirm are held to."""
        from transfer.report import RunReport

        def report(note: str) -> RunReport:
            section = SectionReport(key="factures", label="Factures et tickets")
            section.deleted("documents", 893)
            section.note(note)
            return RunReport(mode="clear", preview=True, sections=[section], rebuilt={}, notes=[note])

        four = report("4 fichiers que plus rien ne cite dans media/ restent tels quels.")
        seven = report("7 fichiers que plus rien ne cite dans media/ restent tels quels.")
        self.assertEqual(four.fingerprint, seven.fingerprint)
        self.assertTrue(four.same_outcome(seven))
        # What it does still decides.
        differs = report("4 fichiers que plus rien ne cite dans media/ restent tels quels.")
        differs.sections[0].deleted("documents")
        self.assertNotEqual(differs.fingerprint, four.fingerprint)
        self.assertFalse(differs.same_outcome(four))


class LoneSurrogateTests(SimpleTestCase):
    """Half a UTF-16 pair written as a JSON escape ("\\ud800") is valid JSON,
    parsed into text no UTF-8 write takes: the stage's state.json, an archive
    written from it, the page. Refused as it is read, in French - it was a
    500 at the upload or at « Prévisualiser » (review, 19/09)."""

    def test_in_the_manifest(self):
        path = write_zip({"sources.json": "{}"}, {**manifest_for(["sources"]), "reason": "export \ud800"})
        with self.assertRaises(ArchiveError) as caught:
            ArchiveReader(path)
        self.assertEqual(str(caught.exception), "Archive refusée : manifest.json contient un caractère invalide.")

    def test_in_a_section_value_or_key(self):
        for payload in ({"sources": [{"name": "Portail \ud800"}]}, {"sources": [], "\udc00": 1}):
            with self.subTest(payload=payload):
                path = write_zip({"sources.json": json.dumps(payload)}, manifest_for(["sources"]))
                with ArchiveReader(path) as reader, self.assertRaises(ArchiveError) as caught:
                    reader.section("sources").payload()
                self.assertEqual(str(caught.exception), "Archive refusée : sources.json contient un caractère invalide.")

    def test_a_whole_pair_escaped_is_one_character(self):
        """An emoji written as its two escapes, as an ASCII-only editor
        saves it, reads as the emoji."""
        path = write_zip({"sources.json": json.dumps({"sources": [{"name": "Portail 🍺"}]})}, manifest_for(["sources"]))
        self.assertIn("\\ud83c\\udf7a", json.dumps({"name": "🍺"}))
        with ArchiveReader(path) as reader:
            self.assertEqual(reader.section("sources").payload(), {"sources": [{"name": "Portail 🍺"}]})

    def test_an_escaped_backslash_before_u_is_text(self):
        path = write_zip({"sources.json": json.dumps({"sources": [{"name": "C:\\ud800"}]})}, manifest_for(["sources"]))
        with ArchiveReader(path) as reader:
            self.assertEqual(reader.section("sources").payload(), {"sources": [{"name": "C:\\ud800"}]})


class ShownMomentTests(SimpleTestCase):
    """The manifest's created_at, as the page can show it. An aware moment at
    the edge of the calendar parses, then overflowed in the page's |date
    filter (converted to Paris time): the Importer tab and the stage page were
    a 500 until the 24 h sweep (review, 19/09)."""

    def test_what_cannot_be_shown_is_none(self):
        for value in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:30:00-14:00", "pas une date", "", None, 20260919, ["2026"]):
            with self.subTest(value=value):
                self.assertIsNone(archive.shown_moment(value))

    def test_a_moment_is_shown_in_local_time(self):
        moment = archive.shown_moment("2026-09-19T12:30:12+00:00")
        self.assertEqual(moment.strftime("%d/%m/%Y %H:%M"), "19/09/2026 14:30")
        self.assertEqual(archive.shown_moment("2026-09-19T14:30:12").strftime("%d/%m/%Y %H:%M"), "19/09/2026 14:30")

    def test_the_reader_says_the_same(self):
        path = write_zip({}, {**manifest_for(), "created_at": "0001-01-01T00:00:00+14:00"})
        with ArchiveReader(path) as reader:
            self.assertIsNone(reader.created_at)
        with ArchiveReader(write_zip({}, manifest_for())) as reader:
            self.assertEqual(reader.created_at.strftime("%d/%m/%Y %H:%M"), "19/09/2026 14:30")


class IoHelpersTests(SimpleTestCase):
    def test_delete_on_close_removes_its_file(self):
        path = new_archive_path("temp")
        path.write_bytes(b"x")
        handle = archive.DeleteOnClose(path)
        self.assertEqual(handle.read(), b"x")
        handle.close()
        self.assertFalse(path.exists())
        self.assertIsInstance(handle, io.FileIO)
