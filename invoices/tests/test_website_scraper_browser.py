"""The website scraper, end to end, in a real (headless) Chrome - against a
customer portal made up here and served from this machine, never a real
site.

The portal has what the real ones have: a cookie banner covering the page
(refused, never accepted), a login form with no ids the scraper knows, a
"Mes factures" link to follow, two pages of invoices with dates and
amounts, and the terms of sale as a PDF in the footer - which is not an
invoice. Variants: a site asking for a code sent by SMS, a site turning
automated browsers away, a wrong password.

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing.
"""

import functools
import http.server
import os
import tempfile
import threading
import time
from datetime import date
from unittest import mock

from django.test import SimpleTestCase, tag

from invoices.scrapers import website
from invoices.scrapers.website import (
    NeedsAPerson,
    RefusedByTheSite,
    WebsiteError,
    WebsiteRecipe,
    fetch_website_invoices,
    list_website_invoices,
)

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

BANNER = """<div id="banner" style="position:fixed;inset:0;background:#fff;z-index:9">
  Ce site utilise des cookies. <button onclick="document.cookie='consent=all'">Tout accepter</button>
  <button onclick="document.getElementById('banner').remove()">Tout refuser</button></div>"""

LOGIN = """<!doctype html><html><head><meta charset="utf-8"></head><body>""" + BANNER + """
<h1>Espace client</h1>
<form id="f"><label>Adresse e-mail <input name="u1"></label>
<label>Mot de passe <input type="password" name="p1"></label>
<button type="submit">Se connecter</button></form><div id="msg"></div>
<footer><a href="cgv.pdf">Conditions générales (PDF)</a></footer>
<script>
document.getElementById('f').addEventListener('submit', function (event) {
  event.preventDefault();
  if (document.cookie.indexOf('consent=all') >= 0) { document.getElementById('msg').textContent = 'cookies acceptés'; return; }
  if (this.u1.value === 'jean@exemple.fr' && this.p1.value === 'secret') { location = 'NEXT'; }
  else { document.getElementById('msg').innerHTML = '<p role="alert">Identifiants incorrects</p>'; }
});
</script></body></html>"""

HOME = """<!doctype html><html><head><meta charset="utf-8"></head><body><nav><a href="compte.html">Mon compte</a>
<a href="factures.html">Mes factures</a></nav><p>Bienvenue</p></body></html>"""

# A side menu drawn as one clickable block around its entries (as a real
# water board's portal draws it): clicked as a whole, it only closes itself.
MENU_HOME = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<nav role="button" tabindex="0" onclick="document.body.dataset.menu = 'ferme'"><ul>
<li><a href="home.html">Tableau de bord</a></li><li><a href="compte.html">Mon compte</a></li>
<li><a href="factures.html">Mes factures</a></li></ul></nav><p>Bienvenue</p></body></html>"""

# A portal drawing its pages in place, without a reload: "Mes factures" hides
# the menu and draws the list where the welcome was. The menu's links keep
# the marks an earlier read gave them - the same numbers the list's links
# get next - and the list's links lead nowhere but through their script.
SPA_HOME = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<nav id="menu"><a href="#">Mon compte</a> <a href="#">Aide</a>
<a href="#" onclick="document.getElementById('menu').style.display = 'none';
  document.getElementById('main').innerHTML = document.getElementById('list').innerHTML; return false;">Mes factures</a></nav>
<main id="main"><p>Bienvenue</p></main>
<template id="list"><table>
<tr><td>Facture de mai 2026</td><td>39,99 €</td><td><a href="#" onclick="location = 'pdf/2026-05.pdf'; return false;">Télécharger</a></td></tr>
<tr><td>Facture d'avril 2026</td><td>39,99 €</td><td><a href="#" onclick="location = 'pdf/2026-04.pdf'; return false;">Télécharger</a></td></tr>
</table></template></body></html>"""

ROW = '<tr><td>{label}</td><td>{amount} €</td><td><a href="pdf/{file}">Télécharger</a></td></tr>'


def invoices_page(rows, next_page=""):
    body = "".join(ROW.format(**row) for row in rows)
    more = f'<a href="{next_page}">Suivant</a>' if next_page else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'></head><body><h1>Mes factures</h1><table>" + body + "</table>" + more
        + '<footer><a href="cgv.pdf">CGV (PDF)</a></footer></body></html>'
    )


VERIFY = """<!doctype html><html><head><meta charset="utf-8"></head><body><p>Saisissez le code reçu par SMS</p>
<input autocomplete="one-time-code"></body></html>"""

# A check that one is not a robot, before any login form (as TotalEnergies'
# is): a slider to drag - a person's to do, never the scraper's.
SLIDER = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<p>On s'assure qu'on s'adresse bien à vous, et non pas à un robot.</p>
<div>Faites glisser vers la droite pour sécuriser votre accès</div><div role="slider">→</div></body></html>"""

# A login form redrawn while it is filled (as Free Mobile's is): each key typed
# puts a new submit button in place of the old one.
REDRAWN_LOGIN = LOGIN.replace(
    "</script>",
    """document.querySelectorAll('input').forEach(function (field) {
  field.addEventListener('input', function () {
    var old = document.querySelector('button[type=submit]');
    old.replaceWith(old.cloneNode(true));
  });
});
</script>""",
)

# An invoice list drawn as cards, no table (as Free Mobile's is): each card
# prints its month and amount, a « Voir ma facture » link opening the
# invoice, and a button whose only content is a download icon.
CARD = """<div class="flex justify-between"><div><p>Facture {month}</p><p>{amount} €</p></div>
<div class="flex gap-2"><a href="voir-{file}" target="_blank">Voir ma facture</a>
<button type="button" onclick="location = 'pdf/{file}.pdf'"><i class="icon icon-download-2-line text-gray-500 text-20"></i></button></div></div>"""
CARDS = (
    """<!doctype html><html><head><meta charset="utf-8"></head><body><h1>Mes factures</h1><div>"""
    + "".join(
        CARD.format(**card)
        for card in (
            {"month": "de mai 2026", "amount": "9,99", "file": "2026-05"},
            {"month": "d'avril 2026", "amount": "9,99", "file": "2026-04"},
            {"month": "de mars 2026", "amount": "9,99", "file": "2026-03"},
        )
    )
    + "</div></body></html>"
)
# The same list offering only « Voir ma facture », its address naming no
# file type (served as a download).
VIEW_ONLY_CARDS = CARDS.replace('<button type="button"', '<button hidden type="button"')

def scripted_list(rows, more_rows=()):
    """An invoice list whose rows download through a script - never through
    an address: `rows` are (month, file, script) with `{file}` in the
    script. « Voir plus » appends `more_rows` to the same list."""
    def row(month, file, script):
        return (f'<tr><td>Facture {month}</td><td>9,99 €</td><td><a title="Télécharger" '
                f'onclick="{script.format(file=file)}"><img alt="pdf"> PDF</a></td></tr>')
    more = "".join(row(*r) for r in more_rows).replace('"', "&quot;")
    button = (f'<button type="button" onclick="document.getElementById(\'list\').insertAdjacentHTML('
              f"'beforeend', this.dataset.more); this.remove();\" data-more=\"{more}\">Voir plus</button>"
              if more_rows else "")
    return ('<!doctype html><html><head><meta charset="utf-8"></head><body><h1>Mes factures</h1>'
            f'<table id="list">{"".join(row(*r) for r in rows)}</table>{button}</body></html>')


# Opened a moment after the click, by the page's own script (the file
# fetched first, as Eau de Paris' does): a pop-up no person's gesture opened.
OPEN_LATER = "setTimeout(function () {{ window.open('pdf/{file}.pdf'); }}, 300); return false;"
# Fetched by the page's script and saved from memory, one after the other: a
# site downloading several files is held by Chrome after the first.
SAVE_FROM_MEMORY = (
    "fetch('pdf/{file}.pdf').then(function (r) {{ return r.blob(); }}).then(function (b) {{"
    # window.URL: in an inline handler, a bare URL is document.URL, a string.
    " var a = document.createElement('a'); a.href = window.URL.createObjectURL(b); a.download = '{file}.pdf';"
    " document.body.appendChild(a); a.click(); }}); return false;"
)
NOTHING_HAPPENS = "return false;"

NOTHING_HOME = """<!doctype html><html><head><meta charset="utf-8"></head><body><nav><a href="compte.html">Mon compte</a>
<a href="aide.html">Aide</a></nav><p>Bienvenue</p></body></html>"""

REJECTED = """<!doctype html><html><head><meta charset="utf-8"></head><body>The requested URL was rejected. Please consult with your administrator.
</body></html>"""

# A login page explaining, beside its form, that a first sign-in asks for a
# code sent by SMS (as Free Mobile's does): words about a check, not one. Its
# form is a text field, a password, an eye to show it (a button that is not
# the submit one) and the submit button.
EXPLAINED_LOGIN = LOGIN.replace(
    "<h1>Espace client</h1>",
    """<h1>Espace client</h1>
<p>Première connexion ? Pour vérifier que c'est bien vous, vous devrez saisir un code reçu par SMS ou par e-mail.</p>""",
).replace(
    '<label>Mot de passe <input type="password" name="p1"></label>',
    '<label>Mot de passe <input type="password" name="p1"></label><button type="button" aria-label="Afficher">o</button>',
)

# Invisible captchas - a badge scoring the visitor, a challenge frame parked
# off screen - on a page that asks nothing of a person. Served from here.
INVISIBLE_CAPTCHA_LOGIN = LOGIN.replace(
    "</form>",
    """</form>
<iframe src="recaptcha/api2/anchor?k=exemple&size=invisible" style="position:fixed;right:0;bottom:0;width:256px;height:60px;border:0"></iframe>
<iframe src="recaptcha/api2/bframe?k=exemple" style="position:absolute;top:-10000px;width:400px;height:580px"></iframe>""",
)

# A captcha box a person has to tick beside the form: the form is filled and
# sent, the page refuses it until the box is ticked - a person's to do.
TICK_BOX_LOGIN = LOGIN.replace(
    "</form>",
    """</form><iframe src="recaptcha/api2/anchor?k=exemple&size=normal" style="width:304px;height:78px;border:0"></iframe>""",
).replace(
    "if (this.u1.value === 'jean@exemple.fr' && this.p1.value === 'secret') { location = 'NEXT'; }",
    "if (false) {}",
)


def portal(folder: str, after_login: str = "home.html", login_page: str | None = None, home: str = HOME) -> None:
    pages = {
        "login.html": login_page or LOGIN.replace("NEXT", after_login),
        "home.html": home,
        "verify.html": VERIFY,
        "factures.html": invoices_page(
            [
                {"label": "Facture de mai 2026", "amount": "39,99", "file": "2026-05.pdf"},
                {"label": "Facture F-2026-0417 d'avril 2026", "amount": "39,99", "file": "2026-04.pdf"},
                {"label": "Facture de mars 2026", "amount": "39,99", "file": "2026-03.pdf"},
            ],
            next_page="factures-2.html",
        ),
        "factures-2.html": invoices_page(
            [
                {"label": "Facture du 02/04/2026", "amount": "12,00", "file": "2026-04-bis.pdf"},
                {"label": "Facture de février 2026", "amount": "39,99", "file": "2026-02.pdf"},
            ]
        ),
    }
    for name, html in pages.items():
        with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
            handle.write(html)
    os.makedirs(os.path.join(folder, "pdf"), exist_ok=True)
    for name in ("2026-05", "2026-04", "2026-03", "2026-04-bis", "2026-02"):
        with open(os.path.join(folder, "pdf", f"{name}.pdf"), "wb") as handle:
            handle.write(PDF + name.encode())
        # « Voir ma facture »: the invoice at an address naming no file type.
        with open(os.path.join(folder, f"voir-{name}"), "wb") as handle:
            handle.write(PDF + name.encode())
    with open(os.path.join(folder, "cgv.pdf"), "wb") as handle:
        handle.write(PDF)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # Chrome closing a connection it no longer needs


@tag("browser")
class WebsiteScraperInBrowserTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from selenium import webdriver  # noqa: F401

            cls.driver_factory = staticmethod(website.build_chrome)
            probe_dir = tempfile.mkdtemp()
            driver = website.build_chrome(probe_dir, True)
            driver.quit()
        except Exception as exc:  # noqa: BLE001
            raise cls.skipTest(cls, f"Chrome indisponible : {exc}")

    def setUp(self):
        self.site = tempfile.TemporaryDirectory()
        self.downloads = tempfile.TemporaryDirectory()
        self.addCleanup(self.site.cleanup)
        self.addCleanup(self.downloads.cleanup)
        handler = functools.partial(_Quiet, directory=self.site.name)
        self.server = _Server(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # A person's pace is not needed against a page served from here.
        for name, value in (
            ("CLICK_INTERVAL_SECONDS", 0.1),
            ("POLL_SECONDS", 0.1),
            ("LOGIN_WAIT_SECONDS", 8),
            ("PAGE_WAIT_SECONDS", 8),
            ("DOWNLOAD_TIMEOUT_SECONDS", 8),
            ("SETTLE_SECONDS", 2),
        ):
            patcher = mock.patch.object(website, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"PORTAIL_LOGIN": "jean@exemple.fr", "PORTAIL_PASSWORD": "secret"})
        env.start()
        self.addCleanup(env.stop)
        self.logged = []

    def recipe(self, **kwargs):
        return WebsiteRecipe(
            name="Portail Exemple",
            login_url=f"{self.base}/login.html",
            username_env="PORTAIL_LOGIN",
            password_env="PORTAIL_PASSWORD",
            **kwargs,
        )

    def fetch(self, recipe, known=()):
        return fetch_website_invoices(
            recipe, self.downloads.name, date(2026, 4, 1), date(2026, 5, 31),
            known_numbers=known, log=self.logged.append,
        )

    def test_it_signs_in_follows_the_invoices_and_downloads_the_period_only(self):
        """Refuses the cookies (accepted, the page would not sign in), finds
        the unnamed fields, follows "Mes factures", reads two pages: May and
        both April invoices - not March, not February, not the terms of sale."""
        portal(self.site.name)
        files = self.fetch(self.recipe())
        contents = sorted(open(path, "rb").read()[len(PDF):].decode() for path in files)
        self.assertEqual(contents, ["2026-04", "2026-04-bis", "2026-05"], self.logged)

    def test_an_invoice_already_imported_is_not_downloaded_again(self):
        portal(self.site.name)
        files = self.fetch(self.recipe(), known=["F-2026-0417"])
        contents = sorted(open(path, "rb").read()[len(PDF):].decode() for path in files)
        self.assertEqual(contents, ["2026-04-bis", "2026-05"])

    def test_a_given_invoices_page_is_opened_directly(self):
        portal(self.site.name)
        files = self.fetch(self.recipe(invoices_url=f"{self.base}/factures.html"))
        self.assertEqual(len(files), 3)

    def test_the_listing_downloads_nothing_and_says_what_it_would_do(self):
        portal(self.site.name)
        rows = list_website_invoices(
            self.recipe(), self.downloads.name, date(2026, 4, 1), date(2026, 5, 31), log=self.logged.append
        )
        self.assertEqual(
            [(row["date"], row["decision"]) for row in rows],
            [("01/05/2026", "à télécharger"), ("01/04/2026", "à télécharger"), ("01/03/2026", "hors période"),
             ("02/04/2026", "à télécharger"), ("01/02/2026", "hors période")],
        )
        self.assertEqual([name for name in os.listdir(self.downloads.name) if name.endswith(".pdf")], [])
        # A test is how a site is set up: the page it read is kept, found or not.
        kept = os.listdir(os.path.join(self.downloads.name, website.DEBUG_DIR))
        self.assertEqual(sorted(name.rsplit(".", 1)[1] for name in kept), ["html", "png"])

    def test_a_listing_finding_no_invoice_names_the_links_it_saw(self):
        """Signed in, on a page with no invoice it recognises: the links
        there are what "Liens à suivre" is set from."""
        portal(self.site.name)
        rows = list_website_invoices(
            self.recipe(invoices_url=f"{self.base}/home.html"), self.downloads.name,
            date(2026, 4, 1), date(2026, 5, 31), log=self.logged.append,
        )
        self.assertEqual(rows, [])
        said = "\n".join(self.logged)
        self.assertIn("Mes factures", said)
        self.assertIn("Mon compte", said)

    def test_a_menu_clickable_as_a_whole_is_not_clicked_for_its_entry(self):
        """The menu holding "Mes factures" comes first in the page and says
        it too; the entry itself is followed, found or named."""
        portal(self.site.name, home=MENU_HOME)
        self.assertEqual(len(self.fetch(self.recipe())), 3, self.logged)
        self.assertEqual(len(self.fetch(self.recipe(navigation=["Mes factures"]))), 3, self.logged)

    def test_a_page_drawn_in_place_clicks_its_own_links_not_the_old_ones(self):
        """The marks of an earlier read stayed on the hidden menu: its
        "Mon compte" was clicked for the first invoice, and nothing came."""
        portal(self.site.name, home=SPA_HOME)
        files = self.fetch(self.recipe())
        contents = sorted(open(path, "rb").read()[len(PDF):].decode() for path in files)
        self.assertEqual(contents, ["2026-04", "2026-05"], self.logged)

    def test_a_check_before_the_login_form_hands_over_to_a_person(self):
        """TotalEnergies showed a slider to drag instead of its form: the
        scraper looked for the form twenty seconds and said there was none."""
        portal(self.site.name, login_page=SLIDER)
        with self.assertRaises(NeedsAPerson) as raised:
            self.fetch(self.recipe())
        self.assertIn("Navigateur visible", str(raised.exception))

    def test_a_login_form_redrawn_while_it_is_filled_still_signs_in(self):
        """Free Mobile's redrew its button as the fields were filled: the
        button found a moment before was clicked, and was gone."""
        portal(self.site.name, login_page=REDRAWN_LOGIN.replace("NEXT", "home.html"))
        self.assertEqual(len(self.fetch(self.recipe())), 3, self.logged)

    def test_a_login_page_explaining_its_sms_code_is_signed_into(self):
        """Free Mobile's login page says a first sign-in asks for a code sent
        by SMS: read as a check, it stopped every run before its form was
        filled - "waiting for a verification" with nothing to verify."""
        portal(self.site.name, login_page=EXPLAINED_LOGIN.replace("NEXT", "home.html"))
        self.assertEqual(len(self.fetch(self.recipe())), 3, self.logged)
        self.assertFalse(any("vérification" in line for line in self.logged), self.logged)

    def test_that_page_refusing_a_wrong_password_says_so(self):
        """Still on the login page after sending, its words are the same:
        the refusal is what is said, not a check to hand over."""
        portal(self.site.name, login_page=EXPLAINED_LOGIN)
        with mock.patch.dict(os.environ, {"PORTAIL_PASSWORD": "faux"}):
            with self.assertRaises(WebsiteError) as raised:
                self.fetch(self.recipe())
        self.assertNotIsInstance(raised.exception, NeedsAPerson)
        self.assertIn("Identifiants incorrects", str(raised.exception))

    def test_an_invisible_captcha_is_not_a_check(self):
        portal(self.site.name, login_page=INVISIBLE_CAPTCHA_LOGIN.replace("NEXT", "home.html"))
        self.assertEqual(len(self.fetch(self.recipe())), 3, self.logged)

    def test_a_captcha_box_beside_the_form_hands_over_once_the_form_is_sent(self):
        portal(self.site.name, login_page=TICK_BOX_LOGIN)
        with self.assertRaises(NeedsAPerson) as raised:
            self.fetch(self.recipe())
        self.assertNotIn("avant même la connexion", str(raised.exception))

    def test_a_site_asking_for_a_code_hands_over_to_a_person(self):
        """Headless, it stops and says how to go on - never guesses."""
        portal(self.site.name, after_login="verify.html")
        with self.assertRaises(NeedsAPerson) as raised:
            self.fetch(self.recipe())
        self.assertIn("Navigateur visible", str(raised.exception))

    def test_a_page_asking_for_a_code_in_words_only_hands_over_too(self):
        """Once the login form is gone, words asking for a code are the
        check - no field marked as one needed."""
        portal(self.site.name, after_login="verify.html")
        with open(os.path.join(self.site.name, "verify.html"), "w", encoding="utf-8") as handle:
            handle.write(
                '<!doctype html><html><head><meta charset="utf-8"></head><body>'
                "<p>Saisissez le code reçu par SMS</p><input name=\"c\"></body></html>"
            )
        with self.assertRaises(NeedsAPerson):
            self.fetch(self.recipe())

    def test_a_site_turning_automated_browsers_away_says_so(self):
        portal(self.site.name, login_page=REJECTED)
        with self.assertRaises(RefusedByTheSite):
            self.fetch(self.recipe())

    def test_a_wrong_password_names_the_variables_to_check(self):
        portal(self.site.name)
        with mock.patch.dict(os.environ, {"PORTAIL_PASSWORD": "faux"}):
            with self.assertRaises(WebsiteError) as raised:
                self.fetch(self.recipe())
        self.assertIn("Identifiants incorrects", str(raised.exception))
        self.assertIn("PORTAIL_PASSWORD", str(raised.exception))
        # What the page looked like is kept, for whoever fixes the settings.
        self.assertTrue(os.listdir(os.path.join(self.downloads.name, website.DEBUG_DIR)))

    def contents(self, files):
        return sorted(open(path, "rb").read()[len(PDF):].decode() for path in files)

    def test_a_list_of_cards_downloads_by_its_icon_once_each(self):
        """Free Mobile's list: « Voir ma facture » and a button holding only
        a download icon - nothing said « Télécharger », and the icon's row
        stopped at the block holding the link, which printed no date. May and
        April, once each, not March."""
        portal(self.site.name)
        with open(os.path.join(self.site.name, "factures.html"), "w", encoding="utf-8") as handle:
            handle.write(CARDS)
        files = self.fetch(self.recipe(navigation=["Mes factures"]))
        self.assertEqual(self.contents(files), ["2026-04", "2026-05"], self.logged)

    def test_a_list_offering_only_voir_ma_facture_is_followed(self):
        portal(self.site.name)
        with open(os.path.join(self.site.name, "factures.html"), "w", encoding="utf-8") as handle:
            handle.write(VIEW_ONLY_CARDS)
        files = self.fetch(self.recipe(navigation=["Mes factures"]))
        self.assertEqual(self.contents(files), ["2026-04", "2026-05"], self.logged)

    def test_a_gather_finding_nothing_keeps_the_page_and_names_its_links(self):
        """Only « Tester » kept the page it read: a gather finding no
        invoice said so and left nothing to set the site up from."""
        portal(self.site.name, home=NOTHING_HOME)
        # A failure on its own line, not "0 found": it passed for a quiet month.
        with self.assertRaises(WebsiteError) as raised:
            self.fetch(self.recipe())
        self.assertIn("Mon compte", str(raised.exception))
        kept = os.listdir(os.path.join(self.downloads.name, website.DEBUG_DIR))
        self.assertEqual(sorted(name.rsplit(".", 1)[1] for name in kept), ["html", "png"])

    # What a second review of the sign-in found: pages served from here.

    def page(self, name, body):
        with open(os.path.join(self.site.name, name), "w", encoding="utf-8") as handle:
            handle.write('<!doctype html><html><head><meta charset="utf-8"></head><body>' + body + "</body></html>")

    def test_a_code_prompt_drawn_after_the_form_goes_is_handed_over(self):
        """The form replaced by « Connexion en cours… », the code asked a
        moment later: taken for signed in, the run ended with nothing."""
        portal(self.site.name, after_login="attente.html")
        self.page("attente.html", """<p>Connexion en cours…</p><script>setTimeout(function () {
          document.body.innerHTML = '<p>Entrez le code envoyé par SMS</p><input name="c">'; }, 1000);</script>""")
        with self.assertRaises(NeedsAPerson):
            self.fetch(self.recipe())

    def test_a_dashboard_with_a_search_box_and_the_recaptcha_notice_is_signed_into(self):
        portal(self.site.name, home=HOME.replace(
            "<p>Bienvenue</p>",
            '<form role="search"><input type="text" placeholder="Rechercher"></form><p>Bienvenue</p>'
            "<footer>Ce site est protégé par reCAPTCHA et les règles de confidentialité de Google.</footer>",
        ))
        self.assertEqual(len(self.fetch(self.recipe())), 3, self.logged)

    def test_a_check_page_with_an_answer_box_gets_no_login_typed_in(self):
        """No password field: the first text field took the login - the
        captcha's answer, or the site's search box."""
        portal(self.site.name, login_page="""<!doctype html><html><head><meta charset="utf-8"></head><body>
        <p>Prouvez que vous n'êtes pas un robot : recopiez les caractères de l'image.</p>
        <form action="typed.html"><input name="answer"><button type="submit">Valider</button></form></body></html>""")
        with self.assertRaises(NeedsAPerson):
            self.fetch(self.recipe())

    def test_a_code_between_identifier_and_password_is_handed_over(self):
        portal(self.site.name, login_page="""<!doctype html><html><head><meta charset="utf-8"></head><body>
        <form action="code.html"><label>Adresse e-mail <input type="email" name="u1"></label>
        <button type="submit">Suivant</button></form></body></html>""")
        self.page("code.html", '<p>Saisissez le code reçu par SMS</p><input name="c">')
        with self.assertRaises(NeedsAPerson) as raised:
            self.fetch(self.recipe())
        self.assertIn("avant même la connexion", str(raised.exception))

    def test_a_form_drawn_late_beside_its_words_is_filled(self):
        """The page's help text is there at once, its form a moment later:
        taken for a check, it was handed over."""
        late = EXPLAINED_LOGIN.replace("NEXT", "home.html").replace(
            '<form id="f">', '<form id="f" style="display:none">'
        ).replace("</script>", "setTimeout(function () { document.getElementById('f').style.display = ''; }, 1500);</script>")
        portal(self.site.name, login_page=late)
        self.assertEqual(len(self.fetch(self.recipe())), 3, self.logged)

    # How an invoice's file is obtained.

    def serve_list(self, html):
        portal(self.site.name)
        with open(os.path.join(self.site.name, "factures.html"), "w", encoding="utf-8") as handle:
            handle.write(html)

    def test_a_link_naming_its_file_is_fetched_without_a_click_to_wait_on(self):
        """Free Mobile's « Voir ma facture » opens a new tab, which a
        script's click could not: 45 seconds went by for every invoice."""
        self.serve_list(VIEW_ONLY_CARDS)
        started = time.monotonic()
        with mock.patch.object(website, "DOWNLOAD_TIMEOUT_SECONDS", 30):
            files = self.fetch(self.recipe(navigation=["Mes factures"]))
        self.assertEqual(self.contents(files), ["2026-04", "2026-05"], self.logged)
        self.assertLess(time.monotonic() - started, 20)

    def test_a_script_opening_the_invoice_after_a_moment_is_downloaded(self):
        self.serve_list(scripted_list([("de mai 2026", "2026-05", OPEN_LATER), ("d'avril 2026", "2026-04", OPEN_LATER)]))
        files = self.fetch(self.recipe(navigation=["Mes factures"]))
        self.assertEqual(self.contents(files), ["2026-04", "2026-05"], self.logged)

    def test_several_invoices_saved_from_memory_all_arrive(self):
        self.serve_list(scripted_list([
            ("de mai 2026", "2026-05", SAVE_FROM_MEMORY),
            ("d'avril 2026", "2026-04", SAVE_FROM_MEMORY),
            ("du 02/04/2026", "2026-04-bis", SAVE_FROM_MEMORY),
        ]))
        files = self.fetch(self.recipe(navigation=["Mes factures"]))
        self.assertEqual(self.contents(files), ["2026-04", "2026-04-bis", "2026-05"], self.logged)

    def test_a_list_growing_under_voir_plus_is_downloaded_once(self):
        """Free Mobile's list shows five, then eight: all eight were
        downloaded again - the five first twice."""
        self.serve_list(scripted_list(
            [("de mai 2026", "2026-05", SAVE_FROM_MEMORY), ("d'avril 2026", "2026-04", SAVE_FROM_MEMORY)],
            more_rows=[("du 02/04/2026", "2026-04-bis", SAVE_FROM_MEMORY)],
        ))
        files = self.fetch(self.recipe(navigation=["Mes factures"]))
        self.assertEqual(self.contents(files), ["2026-04", "2026-04-bis", "2026-05"], self.logged)

    def test_a_click_starting_nothing_is_given_up_early_and_the_source_fails(self):
        """Eau de Paris: nothing came for three invoices, 45 seconds each, and
        the gather said there was nothing to download."""
        self.serve_list(scripted_list([("de mai 2026", "2026-05", NOTHING_HAPPENS), ("d'avril 2026", "2026-04", NOTHING_HAPPENS)]))
        started = time.monotonic()
        with mock.patch.object(website, "DOWNLOAD_TIMEOUT_SECONDS", 60), \
                mock.patch.object(website, "DOWNLOAD_START_SECONDS", 2), self.assertRaises(WebsiteError) as raised:
            self.fetch(self.recipe(navigation=["Mes factures"]))
        self.assertLess(time.monotonic() - started, 40)
        self.assertIn("aucune des 2 factures", str(raised.exception))

    def test_an_invoice_downloaded_again_under_the_same_name_is_taken(self):
        """The file an earlier run left, of the same name: written over, it
        was never new."""
        self.serve_list(scripted_list([("de mai 2026", "2026-05", SAVE_FROM_MEMORY)]))
        with open(os.path.join(self.downloads.name, "2026-05.pdf"), "wb") as handle:
            handle.write(PDF + b"an earlier run")
        files = self.fetch(self.recipe(navigation=["Mes factures"]))
        self.assertEqual(self.contents(files), ["2026-05"], self.logged)
