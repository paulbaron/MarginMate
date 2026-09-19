"""An archive waiting between its upload and its import (§5.11): outside
media, streamed in, gone when refused, confirmed or cancelled - and a backup
staged by reference is never deleted with its stage."""

import json
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile, TemporaryUploadedFile
from django.test import TestCase

from transfer import archive, safety, staging
from transfer.archive import ArchiveError
from transfer.tests.support import FakeSectionsMixin, export_archive, fake_row, forge


def upload_of(path: Path, name="archive.zip") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, path.read_bytes())


class StageUploadTests(FakeSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        fake_row("fournisseurs", "Fournisseur A")
        reader = export_archive({"fournisseurs", "sources"})
        reader.close()
        self.archive_path = reader.path

    def test_an_upload_is_staged_outside_media(self):
        stage = staging.stage_upload(upload_of(self.archive_path))
        self.assertEqual(stage.sections, {"fournisseurs", "sources"})
        self.assertEqual(stage.source, "envoi")
        self.assertTrue(stage.archive_path.is_file())
        self.assertEqual(stage.archive_path.read_bytes(), self.archive_path.read_bytes())
        media = Path(settings.MEDIA_ROOT).resolve()
        self.assertNotIn(media, stage.archive_path.resolve().parents)
        again = staging.get(stage.token)
        self.assertEqual((again.token, again.sections, again.manifest), (stage.token, stage.sections, stage.manifest))

    def test_it_goes_through_chunks_and_a_large_upload_works(self):
        """Django keeps an upload over 2,5 Mo on disk (a TemporaryUploadedFile):
        it is copied in chunks, never read whole."""
        big = TemporaryUploadedFile("archive.zip", "application/zip", self.archive_path.stat().st_size, "utf-8")
        big.write(self.archive_path.read_bytes())
        big.seek(0)
        self.addCleanup(big.close)
        with mock.patch.object(big, "chunks", wraps=big.chunks) as chunks:
            stage = staging.stage_upload(big)
        chunks.assert_called_once_with()
        self.assertEqual(stage.sections, {"fournisseurs", "sources"})
        self.assertEqual(stage.archive_path.read_bytes(), self.archive_path.read_bytes())

    def test_a_refused_archive_leaves_no_stage(self):
        before = set(staging.staging_dir().iterdir())
        broken = forge(self.archive_path, manifest={"version": 2})
        with self.assertRaises(ArchiveError):
            staging.stage_upload(upload_of(broken))
        with self.assertRaises(ArchiveError) as caught:
            staging.stage_upload(SimpleUploadedFile("notes.txt", b"bonjour"))
        self.assertEqual(str(caught.exception), archive.NOT_ZIP_NOR_JSON)
        self.assertEqual(set(staging.staging_dir().iterdir()), before)

    def test_json_that_is_not_the_old_associations_file(self):
        for payload in ({"format": "marginmate-archive"}, {"version": 2, "products": []}, {"version": 1}, [1, 2]):
            with self.subTest(payload=payload), self.assertRaises(ArchiveError) as caught:
                staging.stage_upload(SimpleUploadedFile("x.json", json.dumps(payload).encode()))
            self.assertEqual(str(caught.exception), archive.NOT_ZIP_NOR_JSON)

    def test_the_old_associations_file_goes_through_legacy(self):
        """Converted to an archive holding only the associations (lane B's
        transfer/legacy.py), then staged like any other."""
        payload = {"version": 1, "products": [{"supplier": "Grossiste Exemple", "raw_name": "RHUM EXEMPLE 70CL"}]}
        calls = []

        def to_archive(data, dest):
            calls.append(data)
            forge_path = forge({"associations": {"articles": [], "products": []}})
            Path(dest).write_bytes(forge_path.read_bytes())

        fake_legacy = mock.Mock(to_archive=to_archive)
        with mock.patch.dict("sys.modules", {"transfer.legacy": fake_legacy}), \
                mock.patch("transfer.legacy", fake_legacy, create=True):
            stage = staging.stage_upload(SimpleUploadedFile("marginmate-associations.json", ("﻿  " + json.dumps(payload)).encode()))
        self.assertEqual(calls, [payload])
        self.assertTrue(stage.legacy)
        self.assertEqual(stage.sections, {"associations"})

    def test_an_old_associations_file_holding_half_a_character_is_refused(self):
        """A lone surrogate escape ("\\ud800") is valid JSON; turned into an
        archive, its UTF-8 write failed with a 500 (review, 19/09). Refused
        before the conversion, and nothing is left staged."""
        raw = ('{"version": 1, "products": [{"supplier": "Grossiste Exemple", "raw_name": "RHUM EXEMPLE \\ud800", '
               '"stock_type_name": "Rhum essai"}]}')
        before = set(staging.staging_dir().iterdir())
        with self.assertRaises(ArchiveError) as caught:
            staging.stage_upload(SimpleUploadedFile("marginmate-associations.json", raw.encode("ascii")))
        self.assertEqual(str(caught.exception), "Export d'associations refusé : il contient un caractère invalide.")
        self.assertEqual(set(staging.staging_dir().iterdir()), before)

    def test_too_big(self):
        upload = upload_of(self.archive_path)
        with mock.patch.object(archive, "MAX_ARCHIVE_BYTES", 10), self.assertRaises(ArchiveError) as caught:
            staging.stage_upload(upload)
        self.assertRegex(str(caught.exception), r"^Archive trop grosse \(0,0 Go, 4 Go au plus\)\.$")

    def test_not_enough_room(self):
        with mock.patch("transfer.staging.shutil.disk_usage", return_value=mock.Mock(free=1)), \
                self.assertRaises(ArchiveError) as caught:
            staging.stage_upload(upload_of(self.archive_path))
        self.assertEqual(str(caught.exception), "Pas assez de place sur le disque pour préparer l'import.")

    def test_discard_removes_the_stage(self):
        stage = staging.stage_upload(upload_of(self.archive_path))
        staging.discard(stage)
        self.assertFalse(stage.dir.exists())
        self.assertIsNone(staging.get(stage.token))

    def test_state_is_saved(self):
        stage = staging.stage_upload(upload_of(self.archive_path))
        stage.state = {"sections": {"fournisseurs": "fusionner"}, "preview": None, "preview_at": "2026-09-19T12:00:00+00:00"}
        staging.save(stage)
        self.assertEqual(staging.get(stage.token).state["sections"], {"fournisseurs": "fusionner"})


def no_stages():
    """The staging folder is one temp folder for the whole run: what other
    tests staged goes first."""
    for path in staging.staging_dir().iterdir():
        if path.is_dir() and staging.TOKEN_RE.match(path.name):
            shutil.rmtree(path)


class PendingTests(FakeSectionsMixin, TestCase):
    """What waits, for the Importer tab to list: a stage whose state or
    archive is gone is not one."""

    def setUp(self):
        super().setUp()
        no_stages()
        self.addCleanup(no_stages)
        fake_row("fournisseurs", "Fournisseur A")
        reader = export_archive({"fournisseurs"})
        reader.close()
        self.archive_path = reader.path

    def test_what_waits_newest_first(self):
        first = staging.stage_upload(upload_of(self.archive_path))
        first.created_at -= timedelta(minutes=5)
        staging.save(first)
        second = staging.stage_upload(upload_of(self.archive_path))
        broken = staging.staging_dir() / ("B" * 22)
        broken.mkdir()
        (broken / "state.json").write_text("{", encoding="utf-8")
        emptied = staging.stage_upload(upload_of(self.archive_path))
        emptied.archive_path.unlink()
        (staging.staging_dir() / "pas-un-jeton").mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, staging.staging_dir() / "pas-un-jeton", True)

        self.assertEqual([stage.token for stage in staging.pending()], [second.token, first.token])

    def test_nothing_waits(self):
        self.assertEqual(staging.pending(), [])


class TokenTests(TestCase):
    def test_a_token_of_the_wrong_shape_is_nothing(self):
        for token in ("", "abc", "../../etc", "a" * 21, "a" * 23, "a/" * 11, "é" * 22, None, 42):
            with self.subTest(token=token):
                self.assertIsNone(staging.get(token))

    def test_an_unknown_token_is_nothing(self):
        self.assertIsNone(staging.get("A" * 22))


class BackupStageTests(FakeSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        folder = safety.backup_dir()
        for path in folder.iterdir():
            path.unlink()
        fake_row("fournisseurs", "Fournisseur A")
        reader = export_archive({"fournisseurs"})
        reader.close()
        self.backup = folder / "2026-09-19_143012_avant-import.zip"
        self.backup.write_bytes(reader.path.read_bytes())
        self.addCleanup(self.backup.unlink, missing_ok=True)

    def test_a_backup_is_staged_by_reference_and_kept_on_discard(self):
        stage = staging.stage_backup(self.backup.name)
        self.assertEqual(stage.archive_path, self.backup)
        self.assertEqual(stage.source, f"sauvegarde {self.backup.name}")
        self.assertEqual(staging.get(stage.token).archive_path, self.backup)
        staging.discard(stage)
        self.assertTrue(self.backup.exists())
        self.assertFalse(stage.dir.exists())

    def test_a_name_not_in_the_listing_is_refused(self):
        for name in ("../db.sqlite3", "2026-09-19_143012_avant-import.sqlite3", "autre.zip", ""):
            with self.subTest(name=name), self.assertRaises(ArchiveError):
                staging.stage_backup(name)


class SweepTests(TestCase):
    def test_old_stages_and_stale_exports_go(self):
        old = staging.staging_dir() / ("O" * 22)
        old.mkdir(exist_ok=True)
        (old / "state.json").write_text(json.dumps({"created_at": "2026-01-01T00:00:00+00:00"}))
        fresh = staging.staging_dir() / ("F" * 22)
        fresh.mkdir(exist_ok=True)
        (fresh / "state.json").write_text(json.dumps({"created_at": "2999-01-01T00:00:00+00:00"}))
        foreign = staging.staging_dir() / "pas-un-jeton"
        foreign.mkdir(exist_ok=True)
        stale_export = staging.exports_dir() / "vieux.zip"
        stale_export.write_bytes(b"x")
        two_hours_ago = time.time() - 7200
        os.utime(stale_export, (two_hours_ago, two_hours_ago))
        recent_export = staging.exports_dir() / "en-cours.zip"
        recent_export.write_bytes(b"x")

        self.assertEqual(staging.sweep(max_age=timedelta(hours=24)), 2)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(foreign.exists())
        self.assertFalse(stale_export.exists())
        self.assertTrue(recent_export.exists())
        for path in (fresh, foreign):
            shutil.rmtree(path)
        recent_export.unlink()
