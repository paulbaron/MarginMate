"""The server's logs (config/logs.py): the signing links' tokens cut short in
every record (security audit ANON-6, its log half), a file made only when a
line is written, and the levels of DEPLOY-5. How the configuration behaves
once Django applies it is in accounts/tests/test_production_settings.py (a
child process: this one keeps the test settings' logging)."""

import logging
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from config import logs

TOKEN = "Zq3vK8mPw2xR7tLc9bN4hJ6yF1dS5gA0eU-_oiQWERTY"
LINK = f"/personnel/signer/{TOKEN}/"


def record(msg, *args, exc_info=None, name="django.request", level=logging.ERROR):
    return logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)


class RedactTests(SimpleTestCase):
    def test_a_link_keeps_four_characters_of_its_token(self):
        self.assertEqual(
            logs.redact_signing_links(f"Not Found: {LINK}exemplaire/"),
            "Not Found: /personnel/signer/Zq3v\N{HORIZONTAL ELLIPSIS}/exemplaire/",
        )

    def test_every_link_of_a_line_and_its_encoded_form(self):
        text = f"GET https://gestion.example.com{LINK} puis ?next=%2Fpersonnel%2Fsigner%2F{TOKEN}%2Fcode%2F"
        redacted = logs.redact_signing_links(text)
        self.assertNotIn(TOKEN, redacted)
        self.assertNotIn(TOKEN[4:12], redacted)
        self.assertEqual(redacted.count("Zq3v\N{HORIZONTAL ELLIPSIS}"), 2)

    def test_nothing_else_changes(self):
        for text in ("/personnel/12/2026-06/", "/invoices/signer/abc/", "signer/abc", ""):
            self.assertEqual(logs.redact_signing_links(text), text)


class FilterTests(SimpleTestCase):
    def test_the_message_with_its_arguments(self):
        """Django writes « Not Found: %s » with the path as an argument."""
        entry = record("Not Found: %s", f"{LINK}document/")
        self.assertTrue(logs.SigningLinkFilter().filter(entry))
        self.assertNotIn(TOKEN, entry.getMessage())
        self.assertIn("/personnel/signer/Zq3v\N{HORIZONTAL ELLIPSIS}/document/", entry.getMessage())

    def test_the_traceback(self):
        try:
            raise ValueError(f"lien {LINK} refusé")
        except ValueError:
            entry = record("Internal Server Error: %s", "/boom/", exc_info=sys.exc_info())
        logs.SigningLinkFilter().filter(entry)
        written = logging.Formatter("{message}", style="{").format(entry)
        self.assertIn("ValueError", written)
        self.assertNotIn(TOKEN, written)

    def test_a_record_that_does_not_format_is_kept_as_it_is(self):
        entry = record("%s et %s", "un seul argument")
        self.assertTrue(logs.SigningLinkFilter().filter(entry))
        self.assertEqual(entry.msg, "%s et %s")


class HandlerTests(SimpleTestCase):
    def test_the_folder_is_made_at_the_first_line_not_before(self):
        folder = Path(tempfile.mkdtemp(prefix="marginmate-tests-logs-")) / "journal" / "serveur"
        handler = logs.FolderRotatingFileHandler(folder / logs.LOG_FILE, delay=True, encoding="utf-8")
        self.addCleanup(handler.close)
        self.assertFalse(folder.exists())
        handler.emit(record("Une erreur"))
        handler.flush()
        self.assertIn("Une erreur", (folder / logs.LOG_FILE).read_text(encoding="utf-8"))


class ConfigTests(SimpleTestCase):
    def test_only_the_production_server_writes_the_file(self):
        """PROD-5: every process wrote the same marginmate.log, and on
        Windows a second one holding it made the rotation fail - every
        record past the size was lost. The others log to their console."""
        self.assertEqual(list(logs.logging_config("C:/journal")["handlers"]), ["console"])
        self.assertEqual(list(logs.logging_config("C:/journal", server=False)["handlers"]), ["console"])
        self.assertEqual(sorted(logs.logging_config("C:/journal", server=True)["handlers"]), ["console", "file"])
        for argv, server in (
            (["manage.py", "serve"], True),
            (["manage.py", "serve", "--port", "8002"], True),
            (["manage.py", "serve", "--verifier"], True),
            (["C:/AdminMate/manage.py", "serve"], True),
            (["manage.py", "runserver"], False),
            (["manage.py", "runserver", "8765"], False),
            (["manage.py", "migrate_tenants"], False),
            (["manage.py", "tenant", "bar", "serve"], False),
            (["manage.py", "test"], False),
            (["manage.py"], False),
            ([], False),
        ):
            with self.subTest(argv=argv):
                self.assertIs(logs.is_the_server(argv), server)

    def test_the_levels_and_the_filter_on_every_handler(self):
        config = logs.logging_config("C:/journal", server=True)
        self.assertEqual(config["loggers"]["django.request"]["level"], "ERROR")
        self.assertEqual(config["loggers"]["django.security"]["level"], "WARNING")
        self.assertEqual(config["root"]["level"], "WARNING")
        self.assertEqual(sorted(config["root"]["handlers"]), ["console", "file"])
        self.assertFalse(config["disable_existing_loggers"])
        for name, handler in config["handlers"].items():
            with self.subTest(handler=name):
                self.assertEqual(handler["filters"], ["signing_links"])
        file = config["handlers"]["file"]
        self.assertEqual(Path(file["filename"]), Path("C:/journal") / "marginmate.log")
        self.assertIs(file["delay"], True)
        self.assertEqual(file["level"], "WARNING")
        self.assertEqual((file["maxBytes"], file["backupCount"]), (5 * 1024 * 1024, 5))
        # runserver's lines: the console only, filtered.
        self.assertEqual(config["loggers"]["django.server"]["handlers"], ["console"])

    def test_no_folder_no_file(self):
        self.assertEqual(list(logs.logging_config(None, server=True)["handlers"]), ["console"])
