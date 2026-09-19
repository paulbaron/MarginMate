"""What the website scraper decides, without a browser: which dates a row of
an invoice list covers, which link downloads an invoice, which one is
already imported, and where the credentials come from.

Data invented.
"""

import os
import tempfile
from datetime import date
from unittest import mock

from django.test import SimpleTestCase

from invoices.scrapers.website import (
    Candidate,
    WebsiteError,
    WebsiteRecipe,
    choose,
    credentials,
    in_window,
    known_number_in,
    leads_to_invoices,
    link_to_follow,
    looks_like_an_invoice,
    periods,
)

MAY = (date(2026, 5, 1), date(2026, 5, 31))


class PeriodsTests(SimpleTestCase):
    def test_a_date_is_that_day(self):
        self.assertEqual(periods("Facture du 12/05/2026 - 39,99 €"), [(date(2026, 5, 12), date(2026, 5, 12))])
        self.assertEqual(periods("2026-05-12"), [(date(2026, 5, 12), date(2026, 5, 12))])
        self.assertEqual(periods("12.05.26"), [(date(2026, 5, 12), date(2026, 5, 12))])

    def test_a_day_spelled_out(self):
        self.assertEqual(periods("Émise le 1er mai 2026"), [(date(2026, 5, 1), date(2026, 5, 1))])
        self.assertEqual(periods("12 févr. 2026"), [(date(2026, 2, 12), date(2026, 2, 12))])

    def test_a_month_is_the_whole_month(self):
        self.assertEqual(periods("Facture de mai 2026"), [MAY])
        self.assertEqual(periods("Avis d'échéance 05/2026"), [MAY])
        self.assertEqual(periods("Février 2024"), [(date(2024, 2, 1), date(2024, 2, 29))])

    def test_a_days_figures_are_not_read_again_as_a_month(self):
        """"12/05/2026" also contains "05/2026"."""
        self.assertEqual(periods("12/05/2026"), [(date(2026, 5, 12), date(2026, 5, 12))])

    def test_what_is_no_date(self):
        self.assertEqual(periods("Facture n° 2025100000007 - 260,63 €"), [])
        self.assertEqual(periods("31/02/2026"), [])  # no such day
        self.assertEqual(periods(""), [])


class WindowTests(SimpleTestCase):
    def test_a_row_meeting_the_period_belongs_to_it(self):
        self.assertTrue(in_window("Facture de mai 2026", date(2026, 5, 20), date(2026, 6, 30)))
        self.assertTrue(in_window("Période du 01/04/2026 au 30/04/2026, émise le 02/05/2026", *MAY))

    def test_a_row_outside_it_does_not(self):
        self.assertFalse(in_window("Facture du 02/03/2026", *MAY))

    def test_a_row_printing_no_date_is_not_known_to_be_out(self):
        self.assertIsNone(in_window("Facture - 39,99 €", *MAY))


class LinkTests(SimpleTestCase):
    def link(self, **kwargs):
        return Candidate(index=0, **kwargs)

    def test_a_download_link_in_a_row_with_a_date(self):
        self.assertTrue(looks_like_an_invoice(self.link(text="Télécharger", row="Facture du 12/05/2026 39,99 €")))
        self.assertTrue(looks_like_an_invoice(self.link(href="https://x.fr/f/123.pdf", row="Mai 2026")))
        self.assertTrue(looks_like_an_invoice(self.link(label="PDF", row="Facture 260,63 €")))
        self.assertTrue(looks_like_an_invoice(self.link(download=True, row="02/05/2026")))

    def test_the_footers_terms_are_a_pdf_too_but_print_no_date_or_amount(self):
        self.assertFalse(looks_like_an_invoice(self.link(href="https://x.fr/cgv.pdf", text="CGV (PDF)", row="Mentions légales CGV (PDF)")))

    def test_a_navigation_link_is_not_an_invoice(self):
        self.assertFalse(looks_like_an_invoice(self.link(text="Mes factures", href="https://x.fr/factures", row="Accueil Mes factures")))
        self.assertFalse(looks_like_an_invoice(self.link(text="Télécharger", href="mailto:a@b.fr", row="12/05/2026")))


class IconAndViewLinkTests(SimpleTestCase):
    """A control whose only content is a download icon, a link opening the
    invoice (« Voir ma facture »): Free Mobile's list offers nothing else."""

    ROW = "Facture de mai 2026 9,99 € Voir ma facture"

    def test_a_download_icon_says_download(self):
        self.assertTrue(looks_like_an_invoice(Candidate(0, label="icon-download-2-line", row=self.ROW)))

    def test_a_link_opening_the_invoice_is_one(self):
        self.assertTrue(looks_like_an_invoice(Candidate(0, text="Voir ma facture", href="https://x.fr/v/1", row=self.ROW)))
        self.assertFalse(looks_like_an_invoice(Candidate(0, text="Voir ma facture", href="https://x.fr/v/1", row="Aide")))
        # The way to the list is not an invoice, whatever it says.
        self.assertFalse(looks_like_an_invoice(Candidate(0, text="Mes factures", href="https://x.fr/f", row="Accueil")))

    def test_one_link_a_row_the_download_first(self):
        """Both links of one card: the invoice was downloaded twice."""
        view = Candidate(0, text="Voir ma facture", href="https://x.fr/v/1", row=self.ROW)
        icon = Candidate(1, label="icon-download-2-line", row=self.ROW)
        other_month = Candidate(2, label="icon-download-2-line", row="Facture d'avril 2026 9,99 € Voir ma facture")
        decided = choose([view, icon, other_month], date(2026, 4, 1), date(2026, 5, 31))
        self.assertEqual([candidate.index for candidate, _decision in decided], [1, 2])


class NavigationTests(SimpleTestCase):
    def test_the_menu_entry_speaking_of_invoices_is_followed(self):
        self.assertTrue(leads_to_invoices(Candidate(0, text="Mes factures", href="https://x.fr/factures", row="Accueil Mes factures")))
        self.assertTrue(leads_to_invoices(Candidate(0, text="Conso et factures", href="https://x.fr/conso", row="")))

    def test_an_invoice_on_the_page_is_not_the_way_to_the_invoices(self):
        """A home page listing the latest invoices, each a link named after
        its month: clicking one as if it were the menu opened a PDF."""
        self.assertFalse(leads_to_invoices(Candidate(
            0, text="Facture de mai 2026", href="https://x.fr/facture_pdf.pl?no=1", row="Facture de mai 2026 39,99 €"
        )))
        self.assertFalse(leads_to_invoices(Candidate(0, text="Télécharger la facture", href="https://x.fr/f", row="")))

    def test_a_link_not_speaking_of_invoices_is_not_followed(self):
        self.assertFalse(leads_to_invoices(Candidate(0, text="Mon compte", href="https://x.fr/compte", row="")))

    def test_a_menu_clickable_as_a_whole_is_not_the_link_it_holds(self):
        """A side menu drawn as one clickable block says every entry's text
        at once and comes first in the page; clicked, it only closes itself."""
        menu = Candidate(7, text="TABLEAU DE BORD MON CONTRAT MES FACTURES MES ALERTES", holds=(8, 9, 10))
        entry = Candidate(10, text="MES FACTURES", href="https://x.fr/#/facture")
        links = [Candidate(3, text="Accueil", href="https://x.fr/"), menu, Candidate(8, text="TABLEAU DE BORD"), entry]
        self.assertIs(link_to_follow(links), entry)
        self.assertIs(link_to_follow(links, "Mes factures"), entry)

    def test_the_first_link_is_kept_among_links_side_by_side(self):
        """Nesting is read from the page, not from the words: two links side
        by side, one's text inside the other's, are both links."""
        first = Candidate(2, text="Voir mes factures", href="https://x.fr/factures")
        links = [first, Candidate(5, text="Facture", href="https://x.fr/aide/facture")]
        self.assertIs(link_to_follow(links), first)

    def test_a_block_holding_no_matching_link_is_still_followed(self):
        """A card around the invoices' link, with a "Voir" button in it, is
        the way - not a link further down that says "facturation"."""
        card = Candidate(4, text="Mes factures Voir", href="https://x.fr/factures", holds=(5,))
        links = [card, Candidate(5, text="Voir"), Candidate(9, text="Modifier mon adresse de facturation", href="https://x.fr/profil")]
        self.assertIs(link_to_follow(links), card)
        self.assertIsNone(link_to_follow([Candidate(1, text="Mon compte")]))


class VerificationWordingTests(SimpleTestCase):
    def test_a_slider_asking_if_one_is_a_robot_is_a_verification(self):
        from invoices.scrapers.website import VERIFY_RE

        for text in (
            "On s'assure qu'on s'adresse bien à vous, et non pas à un robot.",
            "Faites glisser vers la droite pour sécuriser votre accès",
            "Slide to verify",
            "Verify you are human",
        ):
            self.assertTrue(VERIFY_RE.search(text), text)
        self.assertFalse(VERIFY_RE.search("Mes factures - Télécharger - Mon compte"))
        for text in ("Entrez le code envoyé par SMS", "Un code vous a été envoyé", "Renseignez votre code"):
            self.assertTrue(VERIFY_RE.search(text), text)
        # The legal notice of every page reCAPTCHA protects asks nothing.
        self.assertFalse(VERIFY_RE.search("Ce site est protégé par reCAPTCHA et les règles de Google s'appliquent."))
        self.assertFalse(VERIFY_RE.search("This site is protected by reCAPTCHA and the Google Privacy Policy."))
        self.assertFalse(VERIFY_RE.search("Faites glisser vos fichiers ici pour les envoyer"))


class ClosedWindowTests(SimpleTestCase):
    """The window closed by hand while a person was waited for: the page,
    no longer readable, read as one asking nothing - « vérification
    faite », then twenty seconds looking for a form in a browser gone."""

    def test_it_is_said_not_taken_for_a_check_done(self):
        from selenium.common.exceptions import InvalidSessionIdException

        from invoices.scrapers import website

        class ClosedBrowser:
            def execute_script(self, *args):
                raise InvalidSessionIdException("invalid session id")

            @property
            def current_url(self):
                raise InvalidSessionIdException("invalid session id")

            @property
            def window_handles(self):
                raise InvalidSessionIdException("invalid session id")

            def quit(self):
                pass

        logged = []
        recipe = WebsiteRecipe(
            name="Mobile Exemple", login_url="https://x.fr/login", username_env="A", password_env="B", show_browser=True
        )
        with tempfile.TemporaryDirectory() as folder:
            visit = website._Visit(recipe, folder, logged.append, None, lambda *args: ClosedBrowser(), headless=False)
            with mock.patch.object(website, "POLL_SECONDS", 0.01), self.assertRaises(WebsiteError) as raised:
                visit._hand_over(before_login=True)
        self.assertIn("fenêtre du navigateur a été fermée", str(raised.exception))
        self.assertFalse(any("vérification faite" in line for line in logged), logged)


class KnownNumberTests(SimpleTestCase):
    def test_a_number_already_imported_is_found_as_a_whole_word(self):
        self.assertEqual(known_number_in("Facture 2025100000007 du 12/05/2026", ["2025100000007"]), "2025100000007")
        self.assertIsNone(known_number_in("Facture 20251000000070 du 12/05/2026", ["2025100000007"]))

    def test_a_short_number_names_nothing(self):
        self.assertIsNone(known_number_in("Ligne 1234 du 12/05/2026", ["1234"]))


class ChooseTests(SimpleTestCase):
    def test_each_link_gets_one_decision_and_a_file_is_one_link(self):
        links = [
            Candidate(0, text="Télécharger", href="https://x.fr/f/1.pdf", row="Facture 12/05/2026 39,99 €"),
            Candidate(1, text="PDF", href="https://x.fr/f/1.pdf", row="Facture 12/05/2026 39,99 €"),
            Candidate(2, text="Télécharger", href="https://x.fr/f/2.pdf", row="Facture 12/03/2026 39,99 €"),
            Candidate(3, text="Télécharger", href="https://x.fr/f/3.pdf", row="Facture F-10001 du 12/05/2026"),
            Candidate(4, text="Accueil", href="https://x.fr/", row="Accueil"),
        ]
        decided = [(candidate.index, decision) for candidate, decision in choose(links, *MAY, known_numbers=["F-10001"])]
        self.assertEqual(decided, [(0, "à télécharger"), (2, "hors période"), (3, "déjà importée (F-10001)")])

    def test_buttons_leading_nowhere_but_their_script_are_not_one_file(self):
        """Download buttons drawn as href="#" all read as the page's own
        address: taken for one file, every invoice but the first was left."""
        links = [
            Candidate(0, text="Télécharger", href="https://x.fr/espace#", row="Facture de mai 2026 39,99 €"),
            Candidate(1, text="Télécharger", href="https://x.fr/espace#", row="Facture d'avril 2026 39,99 €"),
        ]
        self.assertEqual([d for _c, d in choose(links, date(2026, 4, 1), date(2026, 5, 31))], ["à télécharger"] * 2)

    def test_a_given_selector_names_the_links_itself(self):
        links = [Candidate(0, text="Voir", href="https://x.fr/show?id=1", row="Mai 2026")]
        self.assertEqual(choose(links, *MAY), [])
        self.assertEqual([d for _c, d in choose(links, *MAY, selector_given=True)], ["à télécharger"])


class CredentialsTests(SimpleTestCase):
    recipe = WebsiteRecipe(name="Box Exemple", login_url="https://x.fr/login", username_env="BOX_LOGIN", password_env="BOX_PASSWORD")

    def test_they_are_read_from_the_env_file_at_each_run(self):
        """A line added to .env counts without restarting the server."""
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("BOX_LOGIN=jean@exemple.fr\nBOX_PASSWORD=secret\n")
            self.assertEqual(credentials(self.recipe, env_file=path, environ={}), ("jean@exemple.fr", "secret"))

    def test_the_environment_answers_otherwise(self):
        environ = {"BOX_LOGIN": "jean", "BOX_PASSWORD": "secret"}
        self.assertEqual(credentials(self.recipe, env_file=None, environ=environ), ("jean", "secret"))

    def test_a_browser_that_cannot_start_is_said_as_this_sites_failure(self):
        """Raised raw, it stopped the whole gather, not just this site."""
        from invoices.scrapers.website import fetch_website_invoices

        def no_browser(*args):
            raise OSError("session not created")

        credentials_set = mock.patch.dict(os.environ, {"BOX_LOGIN": "jean", "BOX_PASSWORD": "secret"})
        with tempfile.TemporaryDirectory() as folder, credentials_set, self.assertRaises(WebsiteError) as raised:
            fetch_website_invoices(
                self.recipe, folder, date(2026, 5, 1), date(2026, 5, 31), log=lambda message: None,
                driver_factory=no_browser, env_file=None,
            )
        self.assertIn("navigateur", str(raised.exception))

    def test_a_missing_one_is_named(self):
        with self.assertRaises(WebsiteError) as raised:
            credentials(self.recipe, env_file=None, environ={"BOX_LOGIN": "jean"})
        self.assertIn("BOX_PASSWORD est absente du fichier .env", str(raised.exception))


class CancelWhileWaitingTests(SimpleTestCase):
    def test_a_cancel_is_heard_while_a_person_is_waited_for(self):
        """The wait - five minutes - never read it: the run went on to the
        invoices after the check."""
        from invoices.scrapers import website

        class OpenBrowser:
            current_url = "https://x.fr/login"

            def execute_script(self, script, *args):
                return "Saisissez le code reçu par SMS" if "innerText" in script else False

            def quit(self):
                pass

        recipe = WebsiteRecipe(
            name="Mobile Exemple", login_url="https://x.fr/login", username_env="A", password_env="B", show_browser=True
        )
        with tempfile.TemporaryDirectory() as folder:
            visit = website._Visit(recipe, folder, lambda message: None, lambda: True, lambda *args: OpenBrowser(), headless=False)
            with mock.patch.object(website, "POLL_SECONDS", 0.01), self.assertRaises(WebsiteError) as raised:
                visit._hand_over(before_login=True)
        self.assertIn("annulée", str(raised.exception))


class DownloadFolderTests(SimpleTestCase):
    """What landed in the folder, without a browser. Eau de Paris names each
    file after its invoice, and Chrome - downloading as told through its
    DevTools - writes over a file of the same name: a run after the first
    found every name already there, and said nothing had arrived while the
    browser showed each download done."""

    def visit(self, folder):
        from invoices.scrapers import website

        visit = website._Visit.__new__(website._Visit)
        visit.download_dir = folder
        return visit

    def test_a_file_written_over_is_seen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "Facture_N°1_du_2026-07-07.pdf")
            with open(path, "wb") as handle:
                handle.write(b"%PDF-1.4 an earlier run's")
            visit = self.visit(folder)
            visit.start_downloads()
            self.assertEqual(visit._new_pdfs(), [])
            with open(path, "wb") as handle:
                handle.write(b"%PDF-1.4 this run's, written over it")
            os.utime(path, None)
            self.assertEqual(visit._new_pdfs(), ["Facture_N°1_du_2026-07-07.pdf"])

    def test_a_file_taken_keeps_a_name_nothing_writes_over(self):
        """Two invoices downloaded under one name: imported after the run,
        both paths held the second."""
        with tempfile.TemporaryDirectory() as folder:
            visit = self.visit(folder)
            visit.start_downloads()
            path = os.path.join(folder, "facture.pdf")
            with open(path, "wb") as handle:
                handle.write(b"%PDF-1.4 first")
            (first,) = visit._take(visit._new_pdfs())
            with open(path, "wb") as handle:
                handle.write(b"%PDF-1.4 second")
            (second,) = visit._take(visit._new_pdfs())
            self.assertNotEqual(first, second)
            self.assertEqual((open(first, "rb").read(), open(second, "rb").read()), (b"%PDF-1.4 first", b"%PDF-1.4 second"))
