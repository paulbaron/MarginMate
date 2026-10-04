"""The jobs' logs as the espace reading them may read them (common.job_line).

A gather's, a source test's and the till import's logs are drawn on the
espace's own pages. In the platform owner's espace they keep everything -
the server's tracebacks and paths are his own server's. In any other
espace a traceback, Selenium's stack and a path of the server are never
drawn: the line is cut, the espace's own file is named by its name, any
other path is « [fichier du serveur] », and the line as written goes to the
server's log. Real tenants in temporary files (accounts/tests/support.py);
every path and name invented.
"""

from __future__ import annotations

from accounts import paths
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from common import SERVER_FILE, job_line
from invoices.models import ScrapeJob
from recipes.models import SalesImportJob

TRACEBACK = (
    "Échec de l'import de facture.pdf : illisible\n"
    "Traceback (most recent call last):\n"
    '  File "C:\\Serveur\\app\\invoices\\tasks.py", line 181, in _import_downloaded_file\n'
    "ValueError: illisible"
)
SELENIUM = (
    "Box Exemple : WebDriverException - Message: unknown error: cannot find Chrome binary\n"
    "Stacktrace:\n"
    "\tGetHandleVerifier [0x00007FF6 C:\\Program Files\\Google\\Chrome\\chrome.exe]"
)


class JobLineTests(TwoTenantsTestCase):
    """Bar Alpha is the platform owner's espace, Bar Beta another bar."""

    owner_a = True

    def own_file(self, tenant, *parts) -> str:
        return str(paths.tenant_dir(tenant).joinpath("downloads", *parts))

    def test_a_traceback_is_cut_in_another_bar_and_kept_for_the_owner(self):
        with bound_tenant(self.bar_b), self.assertLogs("marginmate.jobs", "WARNING") as logged:
            self.assertEqual(job_line(TRACEBACK), "Échec de l'import de facture.pdf : illisible")
        # The server's log keeps the line whole, with the espace's folder.
        self.assertIn("Traceback (most recent call last):", logged.output[0])
        self.assertIn(self.bar_b.dir_name, logged.output[0])
        with bound_tenant(self.bar_a):
            self.assertEqual(job_line(TRACEBACK), TRACEBACK)

    def test_selenium_s_stack_and_chrome_s_path_are_cut(self):
        with bound_tenant(self.bar_b):
            cleaned = job_line(SELENIUM)
        self.assertEqual(
            cleaned, "Box Exemple : WebDriverException - Message: unknown error: cannot find Chrome binary"
        )

    def test_a_file_of_the_espace_is_named_by_its_name_any_other_path_is_the_server_s(self):
        with bound_tenant(self.bar_b):
            own = self.own_file(self.bar_b, "type-1", "Facture 01-2026 (2).pdf")
            self.assertEqual(
                job_line(f"Ignoré : {own} (déjà importé)"), "Ignoré : Facture 01-2026 (2).pdf (déjà importé)"
            )
            # The same, written with forward slashes.
            posix = paths.tenant_dir(self.bar_b).joinpath("downloads", "metro", "f.pdf").as_posix()
            self.assertEqual(job_line(f"Échec de l'import de {posix} : x"), "Échec de l'import de f.pdf : x")
            # Another espace's folder is a path of the server, as any other.
            theirs = self.own_file(self.bar_a, "type-1", "facture.pdf")
            self.assertEqual(job_line(f"Ignoré : {theirs}"), f"Ignoré : {SERVER_FILE}")
            for path in (
                "C:\\Program Files\\Python311\\Lib\\imaplib.py",
                "C:\\Users\\Exemple\\Bureau\\Mon dossier avec espaces\\app\\x.py",
                "\\\\serveur\\partage\\fichier.txt",
                "/usr/lib/python3/imaplib.py",
            ):
                with self.subTest(path=path):
                    self.assertEqual(job_line(f"Erreur dans {path} ici"), f"Erreur dans {SERVER_FILE} ici")

    def test_urls_dates_and_plain_words_are_left_alone(self):
        line = (
            "Recherche du 01/02/2026 au 28/02/2026 sur https://box.exemple.invalid/a/b?x=1 : 3 factures, "
            "Recettes & ventes › Ventes, 1/2 et TVA 20 %"
        )
        with bound_tenant(self.bar_b), self.assertNoLogs("marginmate.jobs"):
            self.assertEqual(job_line(line), line)

    def test_the_owner_s_paths_stay_and_unbound_nothing_changes(self):
        line = "Échec de l'import de C:\\Serveur\\donnees\\facture.pdf : x"
        with bound_tenant(self.bar_a):
            self.assertEqual(job_line(line), line)
        self.assertEqual(job_line(line), line)

    def test_a_gather_s_log_and_progress_are_cleaned_in_another_bar(self):
        with bound_tenant(self.bar_b):
            job = ScrapeJob.objects.create()
            job.append_log(TRACEBACK)
            # A line that was only a traceback is not written at all.
            job.append_log("Traceback (most recent call last):\n  File x")
            job.update_progress("type-1", label="Grossiste", error="Boîte mail : C:\\Serveur\\x.py a échoué", found=2)
            job.refresh_from_db()
        self.assertIn("Échec de l'import de facture.pdf : illisible", job.log)
        self.assertNotIn("Traceback", job.log)
        self.assertNotIn("tasks.py", job.log)
        self.assertEqual(len(job.log.splitlines()), 1)
        self.assertEqual(job.progress["type-1"]["error"], f"Boîte mail : {SERVER_FILE} a échoué")
        self.assertEqual(job.progress["type-1"]["found"], 2)
        with bound_tenant(self.bar_a):
            mine = ScrapeJob.objects.create()
            mine.append_log(TRACEBACK)
            mine.refresh_from_db()
        self.assertIn("Traceback (most recent call last):", mine.log)

    def test_the_till_import_s_log_is_cleaned_in_another_bar(self):
        with bound_tenant(self.bar_b):
            job = SalesImportJob.objects.create()
            job.append_log(f"Échec : boom\n{TRACEBACK}")
            job.refresh_from_db()
        self.assertIn("Échec : boom", job.log)
        self.assertNotIn("Traceback", job.log)
