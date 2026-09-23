"""A change to see says what it is, why it asks to be seen, and how to
answer it - where it lights up.

The owner, 19/09: "I have 1 notification in « Enseignes et fournisseurs »
and I don't understand why, it is not obvious". It was a supplier's first
document (FIRST_DOCUMENT, recorded to be seen): the tab's amber « 1 »
counted the suppliers with a change to see, with no word nor title, and the
only trace was an « À voir » on one row among 29 - whose document the owner
had validated eight minutes after it was recorded. The tab now lists each
change at its top with why and a « Vu », the supplier's page likewise, « Vu »
says what it did, and validating a first document answers it.

Data invented.
"""

import re
import tempfile
from html import unescape

from django.conf import settings
from django.contrib.messages import get_messages
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, tag
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from inventory.models import Product
from invoices import supplier_changes
from invoices.models import Supplier, SupplierChange
from invoices.parsers import LLM_PARSER_KEY
from invoices.scrapers import website
from invoices.tests.page_posts import page_post
from recipes.models import PosProduct
from tests.factories import make_invoice, make_invoice_line, make_product, make_supplier
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

SUPPLIERS = reverse("invoices:supplier_list")
FIRST = SupplierChange.Kind.FIRST_DOCUMENT
SUMMARY = (
    "Premier document : Traiteur Exemple n° T-1 du 09/05/2026 (Reçue par e-mail (« Traiteur - Factures »)). "
    "Il lui a appris : n° SIREN 900 000 019, site traiteur-exemple.fr."
)
TEXT = "TRAITEUR EXEMPLE SARL\nPLATEAU APERITIF        40,00\nTOTAL TTC               40,00"


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def notice_of(page) -> str:
    """The tab's list of changes to see (id="a-voir"), as HTML."""
    html = page.content.decode()
    start = html.rindex("<div", 0, html.index('id="a-voir"'))
    return html[start:html.index('<div class="table-wrap">', start)]


def fiche_notice_of(page) -> str:
    """The supplier page's « à voir » notice, as HTML."""
    html = page.content.decode()
    start = html.index('class="message message-warning supplier-notice"')
    return html[start:html.index('<section class="card"', start)]


def text_of(html: str) -> str:
    """What a reader reads of `html`: no tags, entities decoded, one space
    between words."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def day_of(change) -> str:
    return timezone.localtime(change.created_at).strftime("%d/%m/%Y")


class Traiteur(TestCase):
    def setUp(self):
        self.caterer = make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")
        self.first = self.receipt("T-1")
        self.change = SupplierChange.objects.create(
            supplier=self.caterer, kind=FIRST, summary=SUMMARY, invoice=self.first, needs_review=True,
            cause="Reçue par e-mail (« Traiteur - Factures »)",
            data={"learned": ["siren:900000019", "web:traiteur-exemple.fr"], "printed": []},
        )
        self.fiche = reverse("invoices:supplier_detail", args=[self.caterer.pk])
        self.seen = reverse("invoices:supplier_change_seen", args=[self.caterer.pk, self.change.pk])

    def receipt(self, number):
        invoice = make_invoice(
            supplier=self.caterer, invoice_number=number, ocr_text=TEXT,
            parse_checks=[{"label": "Somme des lignes = total imprimé", "passed": True, "detail": ""}],
        )
        product = Product.objects.filter(supplier=self.caterer, raw_name="PLATEAU APERITIF").first()
        make_invoice_line(
            invoice=invoice, product=product or make_product(supplier=self.caterer, raw_name="PLATEAU APERITIF"),
            raw_name="PLATEAU APERITIF", quantity=1, total_ht="36.36", vat_rate="0.10", printed_ttc="40.00",
        )
        return invoice


class TabNoticeTests(Traiteur):
    def test_the_tab_lists_it_at_its_top_saying_why_with_a_vu(self):
        page = self.client.get(SUPPLIERS)
        html = page.content.decode()
        self.assertLess(html.index('id="a-voir"'), html.index('data-table-label="fournisseurs"'))
        notice = notice_of(page)
        self.assertIn("1 changement à voir", notice)
        self.assertIn(f'<a href="{self.fiche}">Traiteur Exemple</a>', notice)
        self.assertIn(escape(SUMMARY), notice)
        self.assertIn(escape(supplier_changes.why_to_see(self.change)), notice)
        self.assertIn(reverse("invoices:invoice_edit_lines", args=[self.first.pk]), notice)
        self.assertIn(f"{self.fiche}#historique", notice)
        self.assertIn(f'action="{self.seen}"', notice)
        self.assertIn(f'<input type="hidden" name="retour" value="{SUPPLIERS}">', notice)
        self.assertIn(">Vu</button>", notice)

    def test_vu_from_the_tab_comes_back_to_it_saying_what_it_did(self):
        response = self.client.post(self.seen, {"retour": SUPPLIERS})
        self.assertRedirects(response, SUPPLIERS, fetch_redirect_response=False)
        self.assertEqual(messages_of(response), ["Vu : premier document de Traiteur Exemple."])
        self.change.refresh_from_db()
        self.assertIsNotNone(self.change.reviewed_at)
        page = self.client.get(SUPPLIERS)
        self.assertNotIn("à voir", page.content.decode().lower())
        self.assertNotContains(page, 'id="a-voir"')
        self.assertEqual(page.context["tabs"][3]["to_see"], 0)

    def test_vu_twice_says_nothing_the_second_time(self):
        self.client.post(self.seen, {"retour": SUPPLIERS})
        self.client.get(SUPPLIERS)  # where the first one's message is shown
        response = self.client.post(self.seen, {"retour": SUPPLIERS})
        self.assertEqual(messages_of(response), [])

    def test_several_are_listed_oldest_first(self):
        other = make_supplier(code="CAVE_X", name="Cave Exemple", parser_key="")
        later = SupplierChange.objects.create(
            supplier=other, kind=SupplierChange.Kind.IDENTIFIERS, needs_review=True,
            summary="Ne reconnaît plus Cave Exemple : site cave-exemple.fr.",
        )
        page = self.client.get(SUPPLIERS)
        self.assertEqual([change.pk for change in page.context["changes_to_see"]], [self.change.pk, later.pk])
        notice = notice_of(page)
        self.assertIn("2 changements à voir", notice)
        # No document behind it: no link to one.
        self.assertEqual(notice.count("Voir le document"), 1)
        self.assertEqual(page.context["tabs"][3]["to_see"], 2)

    def test_an_undone_change_is_not_listed_nor_the_ai_pseudo_suppliers(self):
        SupplierChange.objects.filter(pk=self.change.pk).update(undone_at=timezone.now())
        SupplierChange.objects.create(
            supplier=Supplier.objects.get(parser_key=LLM_PARSER_KEY), kind=SupplierChange.Kind.IDENTIFIERS,
            summary="Appris : un site.", needs_review=True,
        )
        page = self.client.get(SUPPLIERS)
        self.assertEqual(page.context["changes_to_see"], [])
        self.assertNotContains(page, 'id="a-voir"')
        self.assertEqual(page.context["tabs"][3]["to_see"], 0)

    def test_both_tables_say_what_their_pill_means(self):
        """The second table's « À voir » had no title at all."""
        uba = Supplier.objects.get(code="UBA")
        SupplierChange.objects.create(
            supplier=uba, kind=SupplierChange.Kind.IDENTIFIERS, summary="Ne reconnaît plus UBA : un site.",
            needs_review=True,
        )
        page = self.client.get(SUPPLIERS)
        pill = '<span class="status-pill status-NEEDS_REVIEW" title="1 changement à voir sur sa fiche">À voir</span>'
        for supplier in (self.caterer, uba):
            html = page.content.decode()
            start = html.index(f'<tr data-row-href="{reverse("invoices:supplier_detail", args=[supplier.pk])}">')
            self.assertIn(pill, html[start:html.index("</tr>", start)])

    def test_the_tab_with_a_change_to_see_renders_whole(self):
        assertNoUnrenderedTemplateSyntax(self, self.client.get(SUPPLIERS), "suppliers tab, a change to see")
        assertNoUnrenderedTemplateSyntax(self, self.client.get(self.fiche), "supplier page, a change to see")


class FicheNoticeTests(Traiteur):
    def test_the_fiche_lists_it_with_why_and_a_vu_coming_back_to_it(self):
        page = self.client.get(self.fiche)
        (notice,) = [notice for notice in page.context["notices"] if notice["kind"] == "review"]
        self.assertEqual([change.pk for change in notice["changes"]], [self.change.pk])
        html = page.content.decode()
        start = html.index('class="message message-warning supplier-notice"')
        block = html[start:html.index('<section class="card"', start)]
        self.assertIn(escape(SUMMARY), block)
        self.assertIn(escape(supplier_changes.why_to_see(self.change)), block)
        self.assertIn('href="#historique"', block)
        self.assertIn(f'action="{self.seen}"', block)
        self.assertIn(f'<input type="hidden" name="retour" value="{self.fiche}">', block)
        # Still in the history, as before.
        self.assertContains(page, "À voir")
        response = self.client.post(self.seen, {"retour": self.fiche})
        self.assertRedirects(response, self.fiche, fetch_redirect_response=False)
        self.assertNotContains(self.client.get(self.fiche), "À voir")


class EachChangeSaysWhatItIsOnceTests(Traiteur):
    """« Premier document du 19/09/2026 : Premier document : Leroy Merlin
    n° … » (UX review, 19/09), on the tab and on the fiche: the kind was
    printed before a summary that already says what it is - every summary
    does, and the fiche's history prints the summary alone. A change's line
    is its supplier (on the tab), its day and its summary."""

    def test_the_tab_says_the_supplier_the_day_and_the_summary(self):
        notice = text_of(notice_of(self.client.get(SUPPLIERS)))
        self.assertIn(f"Traiteur Exemple — {day_of(self.change)} : {SUMMARY}", notice)
        self.assertEqual(notice.count("Premier document"), 1)

    def test_the_fiche_says_the_day_and_the_summary(self):
        notice = text_of(fiche_notice_of(self.client.get(self.fiche)))
        self.assertIn(f"Le {day_of(self.change)} : {SUMMARY}", notice)
        self.assertEqual(notice.count("Premier document"), 1)

    def test_a_first_document_as_receipts_records_it_says_so_once(self):
        """The summary receipts writes, not one typed here."""
        from invoices.receipts import _record_first_document

        self.change.delete()
        _record_first_document(
            self.caterer, self.first, "Reçue par e-mail (« Traiteur - Factures »)", ["siren:900000019"], TEXT
        )
        change = SupplierChange.objects.get(supplier=self.caterer, kind=FIRST)
        for where, notice in (
            ("tab", notice_of(self.client.get(SUPPLIERS))),
            ("fiche", fiche_notice_of(self.client.get(self.fiche))),
        ):
            with self.subTest(where):
                self.assertIn(escape(change.summary), notice)
                self.assertEqual(text_of(notice).count("Premier document"), 1)

    def test_no_kind_label_before_any_summary(self):
        """A lost identifier's summary says it too (« Ne reconnaît plus … »):
        no « Identifiants du … » before it."""
        other = make_supplier(code="CAVE_X", name="Cave Exemple", parser_key="")
        lost = SupplierChange.objects.create(
            supplier=other, kind=SupplierChange.Kind.IDENTIFIERS, needs_review=True,
            summary="Ne reconnaît plus Cave Exemple : site cave-exemple.fr.",
        )
        notice = text_of(notice_of(self.client.get(SUPPLIERS)))
        self.assertIn(f"Cave Exemple — {day_of(lost)} : Ne reconnaît plus Cave Exemple : site cave-exemple.fr.", notice)
        self.assertNotIn("Identifiants", notice)
        fiche = text_of(fiche_notice_of(self.client.get(reverse("invoices:supplier_detail", args=[other.pk]))))
        self.assertIn(f"Le {day_of(lost)} : Ne reconnaît plus Cave Exemple", fiche)
        self.assertNotIn("Identifiants", fiche)


def css_block(css: str, opening: str) -> str:
    """The body of the first block `opening` starts, braces matched."""
    start = css.index(opening) + len(opening)
    depth = 1
    for position in range(start, len(css)):
        depth += {"{": 1, "}": -1}.get(css[position], 0)
        if depth == 0:
            return css[start:position]
    raise AssertionError(f"{opening!r} is never closed")


class TopbarRoomTests(Traiteur):
    """The tab's link ends in #a-voir, but the topbar is sticky: arriving by
    the tab, the list's own heading and the supplier's name landed under it
    (UX review, 19/09 - at 1280 px the list's top at 0 under a 57 px bar;
    in a 450 px pane, the first line read was the middle of what the first
    document taught, « SIREN … , site … »). The
    page as a whole leaves the topbar's height above what it scrolls to:
    #a-voir, the fiche's #historique, the stock list's #a-classer, and the
    tab row htmx scrolls to the top when a tab is clicked lower down, which
    went under the bar too. Where it lands is measured in Chrome
    (TopbarRoomInBrowserTests); here, that the rule is there, and taller
    where the topbar wraps."""

    def test_the_page_leaves_the_topbars_height_above_what_it_scrolls_to(self):
        css = (settings.BASE_DIR / "static/css/marginmate.css").read_text(encoding="utf-8")
        self.assertIn("scroll-padding-top: var(--topbar-room)", css_block(css, "html {"))
        self.assertIn("--topbar-room:", css_block(css, ":root {"))
        # Under 860 px the topbar wraps (.topbar-inner): a taller room.
        narrow = css_block(css, "@media (max-width: 860px) {")
        self.assertIn(".topbar-inner { flex-wrap: wrap;", narrow)
        self.assertIn("--topbar-room:", narrow)

    def test_the_tabs_fragment_is_the_list(self):
        url = self.client.get(reverse("invoices:invoice_list")).context["tabs"][3]["url"]
        path, fragment = url.split("#")
        self.assertEqual(path, SUPPLIERS)
        self.assertIn(f'id="{fragment}"', notice_of(self.client.get(path)))


class WhyToSeeTests(TestCase):
    def setUp(self):
        self.supplier = make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")

    def change(self, kind, **data):
        return SupplierChange(supplier=self.supplier, kind=kind, summary="…", data=data)

    def test_a_first_document_that_taught_it_something(self):
        self.assertEqual(
            supplier_changes.why_to_see(self.change(FIRST, learned=["siren:900000019"])),
            "C'est son premier document : rien d'autre ne garantit cette lecture. Ce qu'il lui a appris rangera "
            "désormais chez Traiteur Exemple tout document qui l'imprime. S'il est bien de Traiteur Exemple, cliquez "
            "« Vu » ; sinon, « Retirer » l'identifiant sur sa fiche ou changez le document de fournisseur.",
        )

    def test_a_first_document_that_taught_it_nothing(self):
        """Recognised by its header, or printing nothing to learn: there is
        nothing to take back, only the filing to confirm."""
        for data in ({"learned": []}, {}):
            with self.subTest(data=data):
                self.assertEqual(
                    supplier_changes.why_to_see(self.change(FIRST, **data)),
                    "C'est son premier document : rien d'autre ne garantit cette lecture. Il ne lui a rien appris. "
                    "S'il est bien de Traiteur Exemple, cliquez « Vu » ; sinon, changez le document de fournisseur.",
                )

    def test_an_identifier_lost_without_anyone_asking(self):
        self.assertEqual(
            supplier_changes.why_to_see(self.change(SupplierChange.Kind.IDENTIFIERS, lost=["web:traiteur-exemple.fr"])),
            "Un identifiant lui a été retiré sans que personne ne l'ait demandé. S'il le reconnaît encore, "
            "« Annuler » sur sa fiche le lui rend ; sinon, cliquez « Vu ».",
        )

    def test_anything_else(self):
        self.assertEqual(
            supplier_changes.why_to_see(self.change(SupplierChange.Kind.HEADER)),
            "Fait automatiquement : vérifiez-le sur sa fiche, puis cliquez « Vu ».",
        )


class ValidatingAnswersTheFirstDocumentTests(Traiteur):
    """Validating a document says it is this supplier's - which is all its
    « premier document » asks. The owner validated Leroy Merlin's at 00:18,
    eight minutes after it was recorded, and it went on lighting the tab."""

    def validate(self, invoice):
        url = reverse("invoices:receipt_review", args=[invoice.pk])
        response = self.client.post(url, page_post(self.client.get(url)))
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.assertIsNotNone(invoice.reviewed_at)
        return response

    def test_validating_the_first_document_marks_it_seen(self):
        response = self.validate(self.first)
        self.change.refresh_from_db()
        self.assertIsNotNone(self.change.reviewed_at)
        self.assertIn("Premier document de Traiteur Exemple : vu.", messages_of(response))
        self.assertEqual(self.client.get(SUPPLIERS).context["tabs"][3]["to_see"], 0)

    def test_validating_another_document_of_it_does_not(self):
        response = self.validate(self.receipt("T-2"))
        self.change.refresh_from_db()
        self.assertIsNone(self.change.reviewed_at)
        self.assertFalse([message for message in messages_of(response) if message.startswith("Premier document")])

    def test_an_undone_or_already_seen_one_is_left_as_it_is(self):
        seen_at = timezone.now().replace(year=2026, month=9, day=1)
        SupplierChange.objects.filter(pk=self.change.pk).update(reviewed_at=seen_at)
        response = self.validate(self.first)
        self.change.refresh_from_db()
        self.assertEqual(self.change.reviewed_at, seen_at)
        self.assertFalse([message for message in messages_of(response) if message.startswith("Premier document")])

    def test_another_suppliers_first_document_is_not_answered_by_it(self):
        """The document moved to another supplier and validated there: the
        first supplier learned from a document that is not its own, which
        is exactly what its change asks to look at."""
        other = make_supplier(code="CAVE_X", name="Cave Exemple", parser_key="")
        type(self.first).objects.filter(pk=self.first.pk).update(supplier=other)
        self.first.refresh_from_db()
        self.validate(self.first)
        self.change.refresh_from_db()
        self.assertIsNone(self.change.reviewed_at)


@tag("browser")
class TopbarRoomInBrowserTests(StaticLiveServerTestCase):
    """Where #a-voir, #historique and a clicked tab's row land, in a real
    (headless) Chrome, at the widths the topbar changes height: 57 px on one
    line, two lines of links from 861 px to about 960, three and four rows
    on a phone (measured 19/09: 57, 82, 83, 112 and 141 px).

    Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped
    where Chrome or its driver is missing. Data invented."""

    WIDTHS = (1280, 900, 860, 768, 600, 450, 375, 320)
    HEIGHT = 700
    # Its flush then fires no post_migrate: recreated content types broke
    # every later class restoring its snapshot (tests/test_transaction_cases.py).
    # Every class does it, or the first one that does not breaks the others.
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.driver = website.build_chrome(tempfile.mkdtemp(), True)
        except Exception as exc:  # noqa: BLE001
            cls.tearDownClass()
            raise cls.skipTest(cls, f"Chrome indisponible : {exc}")

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        super().tearDownClass()

    def setUp(self):
        caterer = make_supplier(code="TRAITEUR_X", name="Traiteur Exemple", parser_key="")
        # The badges belong in the fixture, because they are what makes the
        # bar wrap. Without them this class measured a topbar one row shorter
        # than the owner's: adding « Marges » to the navigation took it to
        # four rows between 441 and 465 px, five pixels over the room - and
        # every width here passed all the same (20/09).
        make_product(supplier=caterer, raw_name="PRODUIT À CLASSER")
        PosProduct.objects.create(name="Produit caisse à lier", total_quantity=3)
        make_invoice(
            supplier=caterer,
            invoice_number="T-0",
            parse_checks=[{"label": "Total du ticket", "passed": False, "detail": ""}],
        )
        first = make_invoice(supplier=caterer, invoice_number="T-1", ocr_text=TEXT)
        SupplierChange.objects.create(
            supplier=caterer, kind=FIRST, summary=SUMMARY, invoice=first, needs_review=True,
            data={"learned": ["siren:900000019"], "printed": []},
        )
        # Enough below each target for the page to bring it to the top:
        # thirty suppliers under the list, thirty documents in « Documents »,
        # thirty changes in the history.
        for n in range(30):
            other = make_supplier(code=f"EXEMPLE_{n:02d}", name=f"Fournisseur Exemple {n:02d}", parser_key="")
            make_invoice(supplier=other, invoice_number=f"E-{n:02d}")
            SupplierChange.objects.create(
                supplier=caterer, kind=SupplierChange.Kind.HEADER, summary=f"En-tête « TRAITEUR {n:02d} ».",
                by_person=True,
            )
        self.fiche = reverse("invoices:supplier_detail", args=[caterer.pk])

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, 10).until(lambda driver: condition())

    def viewport(self, width):
        self.driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": self.HEIGHT, "deviceScaleFactor": 1, "mobile": False},
        )

    def settled(self):
        """Until the page stops scrolling: htmx scrolls once its swap has
        settled, a fragment once the page has laid out."""
        seen = []

        def still():
            seen.append(self.script("return window.scrollY"))
            return len(seen) > 1 and seen[-1] == seen[-2]

        self.wait_for(still)

    def assertClearOfTopbar(self, selector, width):
        top, bar, scrolled = self.script(
            "return [document.querySelector(arguments[0]).getBoundingClientRect().top,"
            " document.querySelector('.topbar').getBoundingClientRect().bottom, window.scrollY];",
            selector,
        )
        # Scrolled to it - or this proves nothing - and not under the bar.
        self.assertGreater(scrolled, 0, f"{selector} at {width} px: the page did not scroll")
        self.assertGreaterEqual(top, bar, f"{selector} at {width} px: at {top:.0f}, under the topbar ({bar:.0f})")
        # Still at the top of the screen, where the owner looks on arriving.
        self.assertLess(top, self.HEIGHT / 2, f"{selector} at {width} px: at {top:.0f}")

    def test_the_tab_brings_its_list_below_the_topbar(self):
        for width in self.WIDTHS:
            with self.subTest(width=width):
                self.viewport(width)
                self.driver.get(self.live_server_url + reverse("invoices:invoice_list"))
                # Clicked, as the owner does: the tabs are hx-boosted, and
                # htmx scrolls to the link's fragment itself.
                self.script(f"document.querySelector('nav.tabs a[href^=\"{SUPPLIERS}\"]').click()")
                self.wait_for(lambda: self.script("return !!document.getElementById('a-voir')"))
                self.settled()
                self.assertClearOfTopbar("#a-voir strong", width)

    def test_voir_la_fiche_brings_the_history_below_the_topbar(self):
        for width in self.WIDTHS:
            with self.subTest(width=width):
                self.viewport(width)
                self.driver.get(self.live_server_url + self.fiche + "#historique")
                self.settled()
                self.assertClearOfTopbar("#historique h2", width)

    def test_a_tab_clicked_lower_down_keeps_its_row_below_the_topbar(self):
        """htmx's boost brings the new #workspace to the top of the page: its
        tab row went under the topbar the same way."""
        documents = reverse("invoices:invoice_list")
        for width in self.WIDTHS:
            with self.subTest(width=width):
                self.viewport(width)
                self.driver.get(self.live_server_url + SUPPLIERS)
                self.script("window.scrollTo(0, document.documentElement.scrollHeight)")
                self.script(f"document.querySelector('nav.tabs a[href=\"{documents}\"]').click()")
                self.wait_for(lambda: self.script("return location.pathname") == documents)
                self.settled()
                self.assertClearOfTopbar("nav.tabs", width)
