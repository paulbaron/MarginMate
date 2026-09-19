"""Achats: where invoices come from, and who they are filed under - a tab
and a word for each.

The owner, on 19/09: "make a clear distinction between the sources of the
invoices (create a new source linked to a specific « Enseignes et
fournisseurs ») and the « Enseignes et fournisseurs »". The Sources tab
listed the invoice « types » and, under them, every supplier: « type » read
as a kind of document rather than where documents come from, and the two
lists were one page. A source (InvoiceType) is now « une source de
factures », always for one fournisseur, on the Sources tab; the suppliers
have a tab of their own at the address that used to send to the foot of the
Sources tab, with the sources fetching for each. A gather is « récupérer »
everywhere. The internal names do not change. Data invented.
"""

import re
from datetime import date
from unittest import mock

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from invoices import workspace
from invoices.models import InvoiceType, ScrapeJob, Supplier, SupplierChange
from invoices.parsers import LLM_PARSER_KEY
from tests.factories import (
    make_invoice,
    make_invoice_line,
    make_invoice_type,
    make_product,
    make_supplier,
)

SUPPLIERS = reverse("invoices:supplier_list")
SOURCES = reverse("invoices:invoice_type_list")
CREATE_SOURCE = reverse("invoices:invoice_type_create")


def messages_of(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def row_of(page, url):
    """The table row leading to `url` (data-row-href), as HTML."""
    html = page.content.decode()
    start = html.index(f'<tr data-row-href="{url}">')
    return html[start:html.index("</tr>", start)]


def element_with_id(page, element_id):
    html = page.content.decode()
    start = html.rindex("<", 0, html.index(f'id="{element_id}"'))
    tag = re.match(r"<(\w+)", html[start:]).group(1)
    return html[start:html.index(f"</{tag}>", start)]


def said(page):
    """The page's HTML, attributes included, spaces collapsed, lower-case."""
    return " ".join(page.content.decode().split()).lower()


class SuppliersTabTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(
            code="EPICERIE_X", name="Epicerie Exemple", parser_key="", ticket_header="EPICERIE EXEMPLE"
        )
        self.source = make_invoice_type(supplier=self.shop, name="Epicerie Exemple - Factures")
        self.alone = make_supplier(code="CAVE_X", name="Cave Exemple", parser_key="")

    def test_it_is_the_fourth_tab_at_the_suppliers_address(self):
        response = self.client.get(SUPPLIERS)
        self.assertEqual(response.status_code, 200)
        tabs = response.context["tabs"]
        self.assertEqual(
            [tab["label"] for tab in tabs], ["Documents", "À vérifier", "Sources", "Enseignes et fournisseurs"]
        )
        self.assertEqual([tab["active"] for tab in tabs], [False, False, False, True])
        self.assertEqual(tabs[3]["url"], SUPPLIERS)
        # Both tables together: every supplier but the AI pseudo-supplier.
        self.assertEqual(tabs[3]["count"], Supplier.objects.exclude(parser_key=LLM_PARSER_KEY).count())
        # The import card stays above it, as on every tab.
        self.assertContains(response, f'action="{reverse("invoices:receipt_upload")}"')

    def test_it_holds_both_tables_and_the_new_supplier_button(self):
        response = self.client.get(SUPPLIERS)
        self.assertContains(response, "<h2>Enseignes et fournisseurs</h2>", html=True)
        self.assertContains(response, 'data-table-label="fournisseurs"')
        # Metro and UBA, seeded in every database.
        self.assertContains(response, "<h3>Fournisseurs avec leur propre lecteur</h3>", html=True)
        self.assertContains(
            response,
            f'<a class="btn" href="{reverse("invoices:supplier_create")}">+ Nouveau fournisseur</a>',
            html=True,
        )
        self.assertContains(response, f'data-row-href="{reverse("invoices:supplier_detail", args=[self.shop.pk])}"')

    def test_the_sources_column_names_the_sources_fetching_for_each(self):
        inactive = make_invoice_type(supplier=self.shop, name="Epicerie Exemple - Ancienne boîte", is_active=False)
        response = self.client.get(SUPPLIERS)
        self.assertContains(response, "<th>Sources</th>", html=True)
        edit = reverse("invoices:invoice_type_update", args=[self.source.pk])
        shop_row = row_of(response, reverse("invoices:supplier_detail", args=[self.shop.pk]))
        self.assertIn(f'<a href="{edit}">Epicerie Exemple - Factures</a>', shop_row)
        self.assertIn(
            f'<a href="{reverse("invoices:invoice_type_update", args=[inactive.pk])}">'
            "Epicerie Exemple - Ancienne boîte</a> <span class=\"muted\">(inactive)</span>",
            shop_row,
        )
        alone_row = row_of(response, reverse("invoices:supplier_detail", args=[self.alone.pk]))
        self.assertNotIn("invoice_type", alone_row)
        self.assertNotIn(reverse("invoices:invoice_type_create"), alone_row)
        self.assertIn("—", alone_row)

    def test_each_tab_computes_only_its_own_list(self):
        """The suppliers' table reads the text of every document, and the
        tabs are on every page of Achats: the count must not build it."""
        with mock.patch("invoices.workspace._suppliers", wraps=workspace._suppliers) as suppliers, mock.patch(
            "invoices.workspace._sources", wraps=workspace._sources
        ) as sources:
            self.client.get(reverse("invoices:invoice_list"))
            self.client.get(reverse("invoices:receipt_queue"))
            suppliers.assert_not_called()
            sources.assert_not_called()
            self.client.get(SOURCES)
            sources.assert_called_once()
            suppliers.assert_not_called()
            self.client.get(SUPPLIERS)
            suppliers.assert_called_once()
            sources.assert_called_once()

    def test_a_change_to_see_lights_the_tab(self):
        """Amber, the number is what waits - as beside it on « À vérifier »:
        the suppliers with a change to see. Quiet, it is every supplier."""
        everyone = Supplier.objects.exclude(parser_key=LLM_PARSER_KEY).count()
        tab = self.client.get(reverse("invoices:invoice_list")).context["tabs"][3]
        self.assertEqual((tab["attention"], tab["count"]), (False, everyone))
        for summary in ("Appris : un numéro.", "Appris : un site."):
            SupplierChange.objects.create(
                supplier=self.shop, kind=SupplierChange.Kind.IDENTIFIERS, summary=summary, needs_review=True
            )
        # Seen, undone: nothing waits on those.
        SupplierChange.objects.create(
            supplier=self.alone, kind=SupplierChange.Kind.IDENTIFIERS, summary="Appris : un numéro.",
            needs_review=True, reviewed_at=timezone.now(),
        )
        page = self.client.get(reverse("invoices:invoice_list"))
        tab = page.context["tabs"][3]
        self.assertEqual((tab["attention"], tab["count"]), (True, 1))
        self.assertContains(
            page, '<span class="count-pill">1</span>', html=True
        )
        SupplierChange.objects.create(
            supplier=self.alone, kind=SupplierChange.Kind.IDENTIFIERS, summary="Appris : un site.", needs_review=True
        )
        tab = self.client.get(reverse("invoices:invoice_list")).context["tabs"][3]
        self.assertEqual((tab["attention"], tab["count"]), (True, 2))

    def test_the_suppliers_with_their_own_reader_name_their_sources_too(self):
        """UBA's invoices come by a mailbox source, Metro's by its own module
        (« Récupérer les nouvelles factures »): the second table said neither,
        so the tab showed fewer sources than the Sources tab."""
        uba, metro = Supplier.objects.get(code="UBA"), Supplier.objects.get(code="METRO")
        source = make_invoice_type(supplier=uba, name="UBA Exemple - Factures")
        response = self.client.get(SUPPLIERS)
        self.assertContains(
            response, "<tr><th>Fournisseur</th><th>Reconnu par</th><th>Sources</th></tr>", html=True
        )
        uba_row = row_of(response, reverse("invoices:supplier_detail", args=[uba.pk]))
        self.assertIn(
            f'<a href="{reverse("invoices:invoice_type_update", args=[source.pk])}">UBA Exemple - Factures</a>', uba_row
        )
        self.assertNotIn("son propre module", uba_row)
        metro_row = row_of(response, reverse("invoices:supplier_detail", args=[metro.pk]))
        self.assertIn('<span class="muted">son propre module</span>', metro_row)


class SourcesTabTests(TestCase):
    def test_it_lists_the_sources_only(self):
        shop = make_supplier(code="EPICERIE_X", name="Epicerie Exemple", parser_key="")
        make_invoice_type(supplier=shop, name="Epicerie Exemple - Factures")
        make_invoice_type(
            supplier=shop, name="Epicerie Exemple - Espace client", source_kind=InvoiceType.SourceKind.WEBSITE
        )
        response = self.client.get(SOURCES)
        self.assertContains(response, "<h2>Sources de factures</h2>", html=True)
        self.assertContains(
            response, f'<a class="btn" href="{CREATE_SOURCE}">+ Nouvelle source</a>', html=True
        )
        self.assertContains(response, "<th>Canal</th>", html=True)
        self.assertContains(response, '<td class="muted">E-mail</td>', html=True)
        self.assertContains(response, '<td class="muted">Espace client</td>', html=True)
        self.assertContains(response, "<th>Active</th>", html=True)
        # The suppliers are on their own tab now.
        self.assertNotIn("ticket_shops", response.context)
        self.assertNotContains(response, 'data-table-label="fournisseurs"')
        self.assertNotContains(response, "Fournisseurs avec leur propre lecteur")
        self.assertNotContains(response, reverse("invoices:supplier_create"))

    def test_an_old_bookmark_of_its_suppliers_lands_on_the_way_there(self):
        """/invoices/types/#fournisseurs: a fragment never reaches the
        server, so nothing can redirect it - the anchor still exists."""
        pointer = element_with_id(self.client.get(SOURCES), "fournisseurs")
        self.assertIn(f'href="{SUPPLIERS}"', pointer)
        self.assertIn("Les enseignes et fournisseurs ont leur onglet", pointer)


class SourceFormTests(TestCase):
    def test_it_says_first_that_a_source_is_for_one_supplier(self):
        page = self.client.get(CREATE_SOURCE)
        self.assertContains(page, "<h1>Nouvelle source de factures</h1>", html=True)
        html = page.content.decode()
        promise = "Une source récupère pour un seul fournisseur : tout ce qu'elle trouve est rangé chez lui."
        self.assertIn(promise, html)
        # Said at its supplier's choice, the first field of the form.
        self.assertLess(html.index('id="type-supplier"'), html.index(promise))
        self.assertLess(html.index(promise), html.index('name="name"'))

    def test_the_channel_is_e_mail_or_espace_client(self):
        page = self.client.get(CREATE_SOURCE)
        form = page.context["type_form"]
        self.assertEqual(form["source_kind"].label, "Canal")
        self.assertEqual(list(form.fields["source_kind"].choices), [("EMAIL", "E-mail"), ("WEBSITE", "Espace client")])
        self.assertContains(page, '<option value="WEBSITE">Espace client</option>', html=True)
        self.assertEqual(form["is_active"].help_text, "Inclure cette source dans « Récupérer les nouvelles factures ».")
        self.assertContains(page, "<h3>Réglages : boîte mail</h3>", html=True)
        self.assertContains(page, "<h3>Réglages : espace client</h3>", html=True)
        # Said by the form, not the model: no migration for a word.
        self.assertEqual(InvoiceType.SourceKind.WEBSITE.label, "Site web")

    def test_its_edit_page_names_the_source(self):
        source = make_invoice_type(name="Box Exemple - Factures")
        page = self.client.get(reverse("invoices:invoice_type_update", args=[source.pk]))
        self.assertContains(page, "<h1>Modifier la source « Box Exemple - Factures »</h1>", html=True)

    def test_saving_one_says_source(self):
        response = self.client.post(
            CREATE_SOURCE,
            {
                "name": "Traiteur Exemple - Factures", "supplier": "new", "new_name": "Traiteur Exemple",
                "source_kind": "EMAIL", "parser_key": "", "is_active": "on", "action": "save",
                "sender_pattern": r"factures@traiteur\.exemple", "subject_pattern": "", "body_pattern": "",
                "attachment_pattern": r"\.pdf$",
            },
        )
        said_now = messages_of(response)
        self.assertIn("Source enregistrée : Traiteur Exemple - Factures", said_now)
        self.assertIn(
            "Traiteur Exemple est créé : cette source range chez lui ce qu'elle récupère, et il apprend ce que ses "
            "documents impriment dès le premier.",
            said_now,
        )
        created = SupplierChange.objects.get(supplier__name="Traiteur Exemple", kind=SupplierChange.Kind.CREATED)
        self.assertEqual(created.cause, "source « Traiteur Exemple - Factures »")


class SupplierPagesTests(TestCase):
    def setUp(self):
        self.shop = make_supplier(code="EPICERIE_X", name="Epicerie Exemple", parser_key="")
        self.fiche = reverse("invoices:supplier_detail", args=[self.shop.pk])

    def test_they_lead_back_to_the_suppliers_tab(self):
        back = f'<a href="{SUPPLIERS}">← Achats · Enseignes et fournisseurs</a>'
        self.assertContains(self.client.get(self.fiche), back, html=True)
        self.assertContains(self.client.get(reverse("invoices:supplier_create")), back, html=True)
        ai = self.client.get(reverse("invoices:supplier_detail", args=[Supplier.objects.get(code="OTHER").pk]))
        self.assertRedirects(ai, SUPPLIERS)
        deleted = self.client.post(reverse("invoices:supplier_delete", args=[self.shop.pk]), {"confirme": "1"})
        self.assertRedirects(deleted, SUPPLIERS)

    def test_a_creation_asked_from_elsewhere_leads_back_there(self):
        page = self.client.get(reverse("invoices:supplier_create") + f"?retour={CREATE_SOURCE}")
        self.assertContains(page, f'<a href="{CREATE_SOURCE}">← Retour</a>', html=True)
        self.assertNotContains(page, "← Achats · Enseignes et fournisseurs")

    def test_a_supplier_names_its_sources(self):
        make_invoice_type(supplier=self.shop, name="Epicerie Exemple - Factures")
        make_invoice_type(
            supplier=self.shop, name="Epicerie Exemple - Portail",
            source_kind=InvoiceType.SourceKind.WEBSITE, is_active=False,
        )
        page = self.client.get(self.fiche)
        self.assertContains(page, "· 2 sources de factures")
        self.assertContains(page, '<h2 id="types">Sources de ses factures</h2>', html=True)
        self.assertContains(page, '<span class="muted">· E-mail</span>', html=True)
        self.assertContains(page, '<span class="muted">· Espace client · inactive</span>', html=True)
        self.assertContains(page, "+ Nouvelle source pour Epicerie Exemple")
        refused = self.client.get(reverse("invoices:supplier_delete", args=[self.shop.pk]))
        self.assertContains(refused, "ne peut pas être supprimé : 2 sources récupèrent pour lui.")
        self.assertIn("rattachez ses sources de factures à un autre fournisseur", said(refused))

    def test_one_fetched_by_its_own_module_says_so(self):
        """Metro has no source: « Récupérer les nouvelles factures » fetches it
        with a module of its own. Its page said its documents came in by the
        import, beside a gather card listing it first among the sources."""
        metro = Supplier.objects.get(code="METRO")
        make_invoice(supplier=metro)
        page = self.client.get(reverse("invoices:supplier_detail", args=[metro.pk]))
        own = "Récupérées par son propre module, avec « Récupérer les nouvelles factures » en haut d'Achats"
        self.assertContains(page, own)
        self.assertNotContains(page, "arrivent par l'import")
        self.assertNotContains(page, "Comment arrivera son premier document")
        # UBA is seeded is_scrapable too, but only its source fetches it.
        uba = Supplier.objects.get(code="UBA")
        page = self.client.get(reverse("invoices:supplier_detail", args=[uba.pk]))
        self.assertNotContains(page, own)
        [source] = page.context["invoice_types"]
        self.assertContains(page, f'<a href="{reverse("invoices:invoice_type_update", args=[source.pk])}">')

    def test_one_with_nothing_yet_is_told_how_its_first_document_comes(self):
        page = self.client.get(self.fiche)
        self.assertIn("par une source qui le récupère pour lui (e-mail, espace client)", said(page))
        self.assertContains(page, "sauf si une source le récupère pour lui.")
        created = self.client.post(
            reverse("invoices:supplier_create"),
            {"name": "Cave Exemple", "nature": "produits", "header": "", "arrivee": "import"},
        )
        self.assertIn("Rien ne le reconnaît encore : une source qui récupère pour lui", " ".join(messages_of(created)))


class GatherWordsTests(TestCase):
    """One verb: « Récupérer ». The button said « Rechercher », the card
    « Récupérer (Metro, e-mails) » while the portals are fetched too."""

    def test_the_card_says_recuperer(self):
        ScrapeJob.objects.create(
            kind=ScrapeJob.Kind.GATHER, status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now(),
            progress={"METRO": {"label": "Metro", "found": 2, "imported": 1}},
        )
        page = self.client.get(reverse("invoices:invoice_list"))
        self.assertContains(page, "🔄 Récupérer depuis les sources")
        self.assertContains(page, 'title="Récupération en cours"')
        self.assertContains(page, ">Récupérer les nouvelles factures</button>")
        self.assertContains(page, 'hx-confirm="Annuler cette récupération ?"')
        self.assertContains(page, "<th>Source</th>", html=True)
        # The import beside it: a document's shop or supplier, not only a shop.
        self.assertIn("son enseigne ou son fournisseur est reconnu sur le document", said(page))

    def post(self, sources):
        with mock.patch("invoices.views.threading.Thread"):
            return self.client.post(
                reverse("invoices:gather"),
                {"start_date": "2026-01-01", "end_date": "2026-09-18", "sources": sources},
                follow=True,
            )

    def test_its_messages_say_it_too(self):
        self.assertContains(self.post([]), "Aucune source cochée : rien à récupérer.")
        ScrapeJob.objects.create(
            kind=ScrapeJob.Kind.GATHER, status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now()
        )
        self.assertContains(self.post(["METRO"]), "Une récupération est déjà en cours")

    def test_a_failed_one_says_why_in_the_same_words(self):
        from invoices.tasks import gather_invoices_task

        job = ScrapeJob.objects.create()
        with mock.patch("invoices.tasks._GatherHeartbeat"), mock.patch(
            "invoices.tasks._raise_if_cancelled", side_effect=RuntimeError("base verrouillée")
        ):
            gather_invoices_task(job.id, date(2026, 1, 1), date(2026, 9, 18), set())
        job.refresh_from_db()
        self.assertEqual(job.status, ScrapeJob.Status.FAILED)
        self.assertEqual(job.last_log_line, "Échec de la récupération : base verrouillée")


class NoOldWordsTests(TestCase):
    """Nowhere a person reads Achats: the invoice « type », « Récupérées »,
    « Rechercher », and the stock item's old names."""

    OLD = (
        "type de facture",
        "types de facture",
        "nouveau type",
        "récupérées",
        "rechercher de nouvelles factures",
        "recherche en cours",
        "cette recherche",
        "type de stock",
        "article de stock",
        "à rattacher",
        "ouvrir la file",
        "alimentent le stock",
        "le stock qu'elles avaient fait entrer",
    )

    def test_no_page_of_achats_says_them(self):
        shop = make_supplier(code="EPICERIE_X", name="Epicerie Exemple", parser_key="")
        source = make_invoice_type(supplier=shop, name="Epicerie Exemple - Factures")
        waiting = make_supplier(code="CAVE_X", name="Cave Exemple", parser_key="")
        invoice = make_invoice(supplier=shop)
        make_invoice_line(
            invoice=invoice, product=make_product(supplier=shop, raw_name="FARINE EXEMPLE"), total_ht="3.00"
        )
        ScrapeJob.objects.create(
            kind=ScrapeJob.Kind.GATHER, status=ScrapeJob.Status.RUNNING, last_heartbeat=timezone.now(),
            progress={"METRO": {"label": "Metro", "found": 0}},
        )
        urls = [
            reverse("invoices:invoice_list"),
            reverse("invoices:receipt_queue"),
            SOURCES,
            SUPPLIERS,
            CREATE_SOURCE,
            reverse("invoices:invoice_type_update", args=[source.pk]),
            reverse("invoices:supplier_detail", args=[shop.pk]),
            reverse("invoices:supplier_detail", args=[waiting.pk]),
            reverse("invoices:supplier_create"),
            reverse("invoices:supplier_delete", args=[shop.pk]),
            reverse("invoices:supplier_delete", args=[waiting.pk]),
            reverse("invoices:invoice_detail", args=[invoice.pk]),
            reverse("invoices:invoice_preview", args=[invoice.pk]),
            reverse("invoices:invoice_create_manual"),
            reverse("invoices:invoice_delete", args=[invoice.pk]),
        ]
        for url in urls:
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200)
                text = said(page)
                for old in self.OLD:
                    self.assertNotIn(old, text)


class ArticleWordsTests(TestCase):
    """A product bought is classified in an article, on « Produits &
    charges » - what Achats' pages say of a line not classified yet."""

    def setUp(self):
        self.shop = make_supplier(code="EPICERIE_X", name="Epicerie Exemple", parser_key="")
        self.invoice = make_invoice(supplier=self.shop, status="NEEDS_REVIEW")
        make_invoice_line(invoice=self.invoice, product=make_product(supplier=self.shop, raw_name="FARINE EXEMPLE"))

    def test_a_line_not_classified_is_a_classer(self):
        page = self.client.get(reverse("invoices:invoice_detail", args=[self.invoice.pk]))
        self.assertContains(page, "<th>Article</th>", html=True)
        self.assertContains(page, '<span class="status-pill status-NEEDS_REVIEW">À classer</span>', html=True)
        self.assertContains(page, '<span class="stat-label">À classer</span>', html=True)
        preview = self.client.get(reverse("invoices:invoice_preview", args=[self.invoice.pk]))
        self.assertContains(preview, "<th>Article</th>", html=True)
        self.assertContains(preview, "à classer dans")
        self.assertContains(preview, "Produits &amp; charges</a>")
        waiting = self.client.get(reverse("invoices:receipt_queue"))
        self.assertContains(waiting, "les classer dans Produits &amp; charges</a>")

    def test_a_poste_of_charge_is_no_line_to_classify(self):
        """A charge's postes are never classified (Product.is_expense): marked
        « À classer », with a link to a panel they never appear in, every
        line of a rent or a phone bill contradicted the tab that says so."""
        rent = make_supplier(code="LOYER_X", name="Bailleur Exemple", parser_key="", expenses_only=True)
        invoice = make_invoice(supplier=rent)
        make_invoice_line(
            invoice=invoice, product=make_product(supplier=rent, raw_name="Bailleur Exemple", is_expense=True)
        )
        page = self.client.get(reverse("invoices:invoice_detail", args=[invoice.pk]))
        self.assertContains(page, '<span class="muted">poste de charge</span>', html=True)
        self.assertNotContains(page, "À classer")
        preview = self.client.get(reverse("invoices:invoice_preview", args=[invoice.pk]))
        self.assertContains(preview, '<span class="muted">poste de charge</span>', html=True)
        self.assertNotContains(preview, "à classer")
        self.assertNotContains(preview, "#a-classer")

    def test_what_goes_with_a_deleted_invoice_is_said_in_articles(self):
        page = self.client.get(reverse("invoices:invoice_delete", args=[self.invoice.pk]))
        self.assertIn("ses lignes et ce qu'elles avaient ajouté à leurs articles", said(page).replace("&#x27;", "'"))
        manual = self.client.get(reverse("invoices:invoice_create_manual"))
        self.assertIn("rejoignent leur article une fois classés", said(manual))
