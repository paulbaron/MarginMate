"""The signatures' files live in a private folder of their own
(`settings.STAFF_PRIVATE_DIR`), never under media/ - which config/urls.py
serves whole when DEBUG is on."""

import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from staff import private_files


class PrivateFolderTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="marginmate-private-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_a_folder_inside_media_is_refused(self):
        media = self.root / "media"
        for inside in (media, media / "private", media / "a" / ".." / "private"):
            with self.subTest(folder=str(inside)):
                with override_settings(MEDIA_ROOT=str(media), STAFF_PRIVATE_DIR=inside):
                    with self.assertRaises(ImproperlyConfigured) as caught:
                        private_files.private_dir()
                    self.assertIn("MEDIA_ROOT", str(caught.exception))

    def test_media_inside_the_private_folder_is_refused_too(self):
        """Serving media would serve the private folder's children."""
        with override_settings(MEDIA_ROOT=str(self.root / "private" / "media"), STAFF_PRIVATE_DIR=self.root / "private"):
            with self.assertRaises(ImproperlyConfigured):
                private_files.private_dir()

    def test_created_on_demand_beside_media(self):
        folder = self.root / "private"
        with override_settings(MEDIA_ROOT=str(self.root / "media"), STAFF_PRIVATE_DIR=folder):
            self.assertFalse(folder.exists())
            self.assertEqual(private_files.private_dir(), folder.resolve())
            self.assertTrue(folder.is_dir())


class RequestFilesTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="marginmate-private-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        override = override_settings(STAFF_PRIVATE_DIR=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.id = uuid.uuid4()

    def test_written_under_signatures_uuid_and_read_back(self):
        digest = private_files.write(self.id, private_files.DOCUMENT, b"%PDF-1.4 essai")
        self.assertEqual(digest, hashlib.sha256(b"%PDF-1.4 essai").hexdigest())
        self.assertTrue((self.root / "signatures" / str(self.id) / "document.pdf").is_file())
        self.assertEqual(private_files.read(self.id, private_files.DOCUMENT), b"%PDF-1.4 essai")
        self.assertTrue(private_files.exists(self.id, private_files.DOCUMENT))
        self.assertFalse(private_files.exists(self.id, private_files.PROOF))

    def test_rewriting_replaces_the_file_whole(self):
        private_files.write(self.id, private_files.PROOF, b"premier")
        private_files.write(self.id, private_files.PROOF, b"second")
        self.assertEqual(private_files.read(self.id, private_files.PROOF), b"second")
        leftovers = [path.name for path in (self.root / "signatures" / str(self.id)).iterdir()]
        self.assertEqual(leftovers, ["preuve.pdf"])

    def test_a_file_whose_hash_changed_is_refused(self):
        digest = private_files.write(self.id, private_files.DOCUMENT, b"original")
        self.assertEqual(private_files.read_checked(self.id, private_files.DOCUMENT, digest), b"original")
        (self.root / "signatures" / str(self.id) / "document.pdf").write_bytes(b"modifie")
        with self.assertRaises(private_files.AlteredFileError) as caught:
            private_files.read_checked(self.id, private_files.DOCUMENT, digest)
        self.assertIn("document.pdf", str(caught.exception))
        self.assertIn("ne correspond plus", str(caught.exception))

    def test_only_the_known_names_and_real_uuids(self):
        for name in ("../../settings.py", "autre.pdf", "", "keys/authority.key.pem"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                private_files.write(self.id, name, b"x")
        for bad in ("..", "../keys", "abc", "", None):
            with self.subTest(request=bad), self.assertRaises(ValueError):
                private_files.request_dir(bad)

    def test_deleting_a_request_takes_its_folder_and_nothing_else(self):
        other = uuid.uuid4()
        private_files.write(self.id, private_files.DOCUMENT, b"a")
        private_files.write(self.id, private_files.PROOF, b"b")
        private_files.write(other, private_files.DOCUMENT, b"c")
        self.assertEqual(sorted(private_files.delete_request_files(self.id)), ["document.pdf", "preuve.pdf"])
        self.assertFalse((self.root / "signatures" / str(self.id)).exists())
        self.assertEqual(private_files.read(other, private_files.DOCUMENT), b"c")
        self.assertEqual(private_files.delete_request_files(self.id), [])

    def test_a_key_file_is_written_whole(self):
        path = private_files.keys_dir() / "essai.key.pem"
        private_files.write_private(path, b"-----BEGIN-----")
        self.assertEqual(path.read_bytes(), b"-----BEGIN-----")
        self.assertEqual(path.parent, (self.root / "keys").resolve())
