"""The signatures' files live in a private folder of their own - the
tenant's `private/` (accounts.paths.private_dir), beside its media/ and
never inside a folder the site serves."""

import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from accounts import paths
from accounts.tenancy import require_tenant
from staff import private_files


class PrivateFolderTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="marginmate-private-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_a_folder_inside_a_served_one_is_refused(self):
        static = self.root / "static"
        for tenants in (static, static / "espaces", static / "a" / ".." / "espaces"):
            with self.subTest(tenants=str(tenants)):
                with override_settings(STATIC_ROOT=str(static), TENANTS_ROOT=tenants):
                    with self.assertRaises(ImproperlyConfigured) as caught:
                        private_files.private_dir()
                    self.assertIn("STATIC_ROOT", str(caught.exception))

    def test_a_served_folder_inside_the_private_folder_is_refused_too(self):
        """Serving it would serve the private folder's children."""
        with override_settings(TENANTS_ROOT=self.root):
            private = paths.private_dir()
            with override_settings(STATIC_ROOT=str(private / "static")):
                with self.assertRaises(ImproperlyConfigured):
                    private_files.private_dir()

    def test_media_is_no_served_folder_any_more(self):
        """Only the old single mode served media (a public /media/ route
        while DEBUG was on, gone since 29/09/2026): a MEDIA_ROOT around the
        tenants refuses nothing - nothing serves it."""
        with override_settings(MEDIA_ROOT=str(self.root), TENANTS_ROOT=self.root / "espaces"):
            folder = private_files.private_dir()
        self.assertTrue(folder.is_dir())
        self.assertTrue(folder.is_relative_to(self.root.resolve()))

    def test_created_on_demand_beside_the_tenant_s_media(self):
        with override_settings(TENANTS_ROOT=self.root):
            folder = paths.tenant_dir(require_tenant()) / paths.PRIVATE
            self.assertFalse(folder.exists())
            self.assertEqual(private_files.private_dir(), folder.resolve())
            self.assertTrue(folder.is_dir())
            self.assertEqual(folder.parent, paths.media_root().parent)


class RequestFilesTests(SimpleTestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="marginmate-private-test-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        override = override_settings(TENANTS_ROOT=root)
        override.enable()
        self.addCleanup(override.disable)
        self.root = paths.private_dir()
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
