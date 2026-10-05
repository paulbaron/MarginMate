"""The top navigation: three workspaces instead of eight pages.

Stock and its review queue are one page ("Produits & charges"); invoices, tickets and
their sources and suppliers another ("Factures"); recipes, till products and sales a third
("Recettes & ventes"). Each link lights up on every page of its workspace -
and only it - and carries the count of what is waiting there.

Under 860 px the links fold into « Menu » (30/09, the owner: « the top menu
is too big, maybe do something that can be expanded »; templates/base.html,
static/js/topbar.js): the bar is then the brand, the page's section - the
words of the link it lights (config/navigation.py SECTION_LABELS) - and the
button. What the markup promises is checked here; what Chrome draws, in
accounts/tests/test_topbar_browser.py.
"""

import re
from types import SimpleNamespace

from django.test import RequestFactory, TestCase
from django.urls import resolve, reverse
from django.utils.html import escape

from accounts.access import Access
from config import navigation
from inventory.models import GapExclusion, GapFillEntry, ShoppingExclusion, ShoppingList, StockType
from recipes.models import PosProduct
from staff.models import Employee
from tests.factories import (
    make_invoice,
    make_product,
    make_recipe,
    make_sale_document,
    make_stock_take,
    make_stock_type,
    make_supplier,
)
from tests.runner import employee_of_the_test_tenant
from tests.test_views_smoke import make_gaps_to_fill, make_shopping_history, make_shopping_lists

LABELS = [
    "Produits &amp; charges",
    "Factures",
    "Banque",
    "Recettes &amp; ventes",
    # The margins read both sides - what came in, what went out - so
    # the link sits between them rather than at the end.
    "Marges",
    # The employees' timesheets: nothing to do with the margins, but
    # placed there the link moves none of those already in use.
    "Personnel",
    "Inventaires",
    # The empties handed back to the delivery driver: opened on the phone
    # at every delivery. After « Inventaires » (counting happens there too),
    # before « Données » and « Admin », which stay at the end: no link
    # already in use moves.
    "Consignes",
    "Données",
    # « Admin » follows for a superuser only (accounts/tests/test_admin.py).
]
#: The words of links no owner's bar draws: « Courses », an employee's given
#: « Liste de courses » without « Produits & charges », in that link's place
#: (config/navigation.py SHOPPING_SECTION).
EMPLOYEE_LABELS = ["Courses"]
#: The routes of « Listes de courses », « Produits & charges »' pages.
SHOPPING_LIST_ROUTES = (
    "shopping_lists",
    "shopping_list_page",
    "shopping_list_add",
    "shopping_list_add_all",
    "shopping_list_item_edit",
    "shopping_list_item_delete",
    "shopping_list_item_tick",
    "shopping_list_finish",
)


def shopping_list_landings(client, made, lists) -> list:
    """Every page the shopping lists' forms land on, followed - each form
    posted as its page posts it, refused or not - with the pages themselves:
    the lists, a store's list to prepare, to tick, an item's card, a list
    finished. `made`, `lists`: tests/test_views_smoke.py's invented data."""
    store = made.wholesaler.pk
    page = reverse("inventory:shopping_list_page")
    add = reverse("inventory:shopping_list_add")
    edit = reverse("inventory:shopping_list_item_edit")
    landed = [
        client.get(reverse("inventory:shopping_lists")),
        client.get(page, {"fournisseur": store}),
        client.get(page, {"fournisseur": store, "mode": "courses"}),
        client.get(page, {"fournisseur": store, "ligne": lists.bread.pk}),
        client.get(page, {"liste": lists.finished.pk}),
        client.get(page, {"liste": lists.open.pk}, follow=True),
        client.get(page, {"fournisseur": "abc"}, follow=True),
        client.post(add, {"fournisseur": store, "nom": "Serviettes exemple"}, follow=True),
        client.post(add, {"fournisseur": store, "nom": ""}, follow=True),
        client.post(add, {"fournisseur": "abc", "nom": "Serviettes exemple"}, follow=True),
        client.post(
            add,
            {"fournisseur": store, "article": made.olives.pk, "quantite": "1", "retour": "peut-etre", "dans": "30"},
            follow=True,
        ),
        client.post(reverse("inventory:shopping_list_add_all"), {"fournisseur": store}, follow=True),
        client.post(edit, {"fournisseur": store, "ligne": lists.bread.pk, "quantite": "3", "note": ""}, follow=True),
        client.post(edit, {"fournisseur": store, "ligne": lists.bread.pk, "quantite": "abc"}, follow=True),
        client.post(
            reverse("inventory:shopping_list_item_tick"),
            {"fournisseur": store, "ligne": lists.beer.pk, "pris": "1"},
            follow=True,
        ),
        client.post(
            reverse("inventory:shopping_list_item_delete"), {"fournisseur": store, "ligne": lists.bread.pk}, follow=True
        ),
        client.post(reverse("inventory:shopping_list_finish"), {"liste": lists.open.pk, "garder": "1"}, follow=True),
        client.post(reverse("inventory:shopping_list_finish"), {"liste": "abc"}, follow=True),
        client.get(add, follow=True),
    ]
    assert ShoppingList.objects.filter(pk=lists.open.pk, finished_at__isnull=False).exists()
    return landed


def nav_links(response):
    html = response.content.decode()
    nav = html[html.index("<nav") : html.index("</nav>")]
    return re.findall(r"<a [^>]*>.*?</a>", nav, flags=re.DOTALL)


def label_of(link):
    inner = re.sub(r"^<a [^>]*>|</a>$", "", link)
    return re.sub(r"<[^>]+>", "", re.sub(r"<span class=\"badge\">.*?</span>", "", inner, flags=re.DOTALL)).strip()


def active_labels(response):
    return [label_of(link) for link in nav_links(response) if 'class="active"' in link]


def topbar_of(response) -> str:
    """The page's <header class="topbar">, as HTML."""
    html = response.content.decode()
    start = html.index('<header class="topbar">')
    return html[start : html.index("</header>", start)]


def section_shown(response):
    """What the folded bar says the page is (its .topbar-section), as HTML -
    None when it draws none."""
    found = re.findall(r'<span class="topbar-section">(.*?)</span>', topbar_of(response), flags=re.DOTALL)
    return found[0].strip() if len(found) == 1 else None


def toggle_of(response) -> str:
    """The opening tag of « Menu », the one element carrying data-topbar-toggle."""
    header = topbar_of(response)
    tags = re.findall(r"<[a-z]+\b[^>]*\bdata-topbar-toggle\b[^>]*>", header)
    assert len(tags) == 1, tags
    return tags[0]


class NavigationTests(TestCase):
    def test_the_workspaces_in_order(self):
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual([label_of(link) for link in nav_links(response)], LABELS)

    def test_each_page_lights_up_its_workspace_only(self):
        supplier = make_supplier(code="METRO", name="Metro")
        invoice = make_invoice(supplier=supplier)
        recipe = make_recipe(name="Mule")
        sale_document = make_sale_document(reference="FV-NAV-1", stated_total_ttc="10.00")
        stock_type = make_stock_type(name="Vodka")
        # An invented employee: the repository is public.
        person = Employee.objects.create(last_name="Dupont", first_name="Jeanne", tuesday_hours=7)
        # An invented pickup and slip (returnables/tests/support.py).
        from returnables.tests.support import make_pickup, make_slip, seeded_format

        pickup = make_pickup()
        slip = make_slip()
        # A count, so « Combler les écarts » draws its form rather than its
        # empty state - and an amount typed, so it draws its list too.
        take = make_gaps_to_fill()
        gap_filler = reverse("inventory:stock_gap_filler")
        added = self.client.post(reverse("inventory:stock_gap_filler_add"), {"depuis": take.pk, "montant": "70"})
        self.assertEqual(added.status_code, 302)
        self.assertTrue(GapFillEntry.objects.filter(stock_take=take).exists())
        self.assertContains(self.client.get(gap_filler, {"depuis": take.pk}), 'id="a-encaisser"')
        # And one count with no list: the latest, so the page with none.
        make_stock_take()
        # « Prévoir les courses », reached from the products page's header:
        # a store's list, another's, and the rhythm of every article.
        shopping = make_shopping_history()
        shopping_list = reverse("inventory:shopping_list")
        shopping_rhythm = reverse("inventory:shopping_rhythm")
        # « Listes de courses »: the lists, a store's to prepare and to tick,
        # an item's card, a list finished.
        lists = make_shopping_lists(shopping)
        list_page = reverse("inventory:shopping_list_page")
        pages = {
            "Produits &amp; charges": [
                reverse("inventory:stock_list"),
                reverse("inventory:stock_type_update", args=[stock_type.pk]),
                reverse("inventory:stock_type_create"),
                shopping_list,
                f"{shopping_list}?fournisseur={shopping.grocer.pk}&dans=30",
                f"{shopping_list}?fournisseur=abc&dans=abc",
                shopping_rhythm,
                f"{shopping_rhythm}?fournisseur={shopping.wholesaler.pk}",
                reverse("inventory:shopping_lists"),
                f"{list_page}?fournisseur={shopping.wholesaler.pk}",
                f"{list_page}?fournisseur={shopping.wholesaler.pk}&mode=courses",
                f"{list_page}?fournisseur={shopping.wholesaler.pk}&ligne={lists.bread.pk}",
                f"{list_page}?liste={lists.finished.pk}",
            ],
            "Factures": [
                reverse("invoices:invoice_list"),
                reverse("invoices:receipt_queue"),
                reverse("invoices:invoice_type_list"),
                reverse("invoices:supplier_list"),
                reverse("invoices:supplier_detail", args=[supplier.pk]),
                reverse("invoices:invoice_type_create"),
                reverse("invoices:invoice_detail", args=[invoice.pk]),
                reverse("invoices:invoice_edit_lines", args=[invoice.pk]),
                reverse("invoices:invoice_create_manual"),
                # « Récupération automatique », reached from Factures' card.
                reverse("invoices:auto_gathers"),
                # « Ajouter des factures »: the import's form on a page of its own.
                reverse("invoices:invoice_add"),
            ],
            "Recettes &amp; ventes": [
                reverse("recipes:recipe_list"),
                reverse("recipes:recipe_detail", args=[recipe.pk]),
                reverse("recipes:recipe_create"),
                reverse("recipes:pos_product_list"),
                reverse("recipes:sales_list"),
                reverse("recipes:sale_document_create"),
                reverse("recipes:sale_document_update", args=[sale_document.pk]),
                # « Import automatique des ventes », reached from the Ventes tab.
                reverse("recipes:auto_sales"),
            ],
            "Inventaires": [
                reverse("inventory:stock_take_list"),
                reverse("inventory:stock_take_create"),
                gap_filler,
                # The page with a list.
                f"{gap_filler}?depuis={take.pk}",
                # An address from before the list, its amount now read as none.
                f"{gap_filler}?montant=50",
                f"{gap_filler}?depuis={take.pk}&montant=50",
            ],
            "Banque": [reverse("bank:bank_home"), reverse("bank:treasury")],
            "Marges": [reverse("margins:margins_home")],
            "Personnel": [
                reverse("staff:home"),
                reverse("staff:employee", args=[person.pk]),
                reverse("staff:month", args=[person.pk, "2026-06"]),
                # « Accès des employés » is about the employees, though its
                # route is the accounts app's (« Données »'s):
                # navigation.SECTION_BY_VIEW.
                reverse("accounts:members"),
            ],
            "Consignes": [
                reverse("returnables:home"),
                reverse("returnables:pickup_detail", args=[pickup.pk]),
                reverse("returnables:slip_detail", args=[slip.pk]),
                reverse("returnables:format_list"),
                reverse("returnables:format_create"),
                reverse("returnables:format_edit", args=[seeded_format().pk]),
                reverse("returnables:type_list"),
            ],
            "Données": [
                reverse("transfer:data_home"),
                reverse("transfer:data_import"),
                reverse("transfer:data_clear"),
                # « Notifications », reached from Données' header like
                # « Identifiants »: no link of its own.
                reverse("notifications:home"),
                reverse("notifications:reminders"),
                reverse("notifications:events"),
            ],
        }
        for label, urls in pages.items():
            for url in urls:
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(active_labels(response), [label])
                    # Folded under 860 px, the bar says it in the lit link's
                    # own words: the one place left saying where the page is
                    # once its title has scrolled away.
                    self.assertEqual(section_shown(response), label)

    def test_the_gap_filler_with_exclusions_lights_inventaires(self):
        """« Combler les écarts » with articles and a category left out, on
        every page its exclusion forms land on: still « Inventaires », the
        folded bar saying so. Both routes are of that section too."""
        take = make_gaps_to_fill()
        StockType.objects.filter(name="Ambrée exemple").update(category="Bières exemple")
        red = StockType.objects.get(name="Rouge exemple")
        exclude = reverse("inventory:stock_gap_filler_exclude")
        include = reverse("inventory:stock_gap_filler_include")
        landed = [
            self.client.post(exclude, {"depuis": take.pk, "article": red.pk}, follow=True),
            self.client.post(exclude, {"depuis": take.pk, "categorie": "Bières exemple"}, follow=True),
            # Refused, the page it lands on all the same.
            self.client.post(exclude, {"depuis": take.pk, "article": "abc"}, follow=True),
        ]
        self.assertEqual(GapExclusion.objects.count(), 2)
        landed.append(self.client.get(reverse("inventory:stock_gap_filler"), {"depuis": take.pk}))
        landed.append(self.client.get(reverse("inventory:stock_gap_filler")))
        landed.append(
            self.client.post(
                include,
                {"depuis": take.pk, "exclusion": GapExclusion.objects.get(category="Bières exemple").pk},
                follow=True,
            )
        )
        for number, response in enumerate(landed):
            with self.subTest(page=number):
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="exclusions"')
                self.assertEqual(active_labels(response), ["Inventaires"])
                self.assertEqual(section_shown(response), "Inventaires")
        for url in (exclude, include):
            with self.subTest(url=url):
                self.assertEqual(navigation.section_of(resolve(url)), "stock_takes")

    def test_the_gap_filler_s_recent_sales_light_inventaires(self):
        """« Recettes vendues il y a moins de … », chosen, refused and taken
        back: every page it lands on is « Inventaires », and so is its route."""
        take = make_gaps_to_fill()
        recent = reverse("inventory:stock_gap_filler_recent")
        landed = [
            self.client.post(recent, {"depuis": take.pk, "duree": "3", "unite": "mois"}, follow=True),
            self.client.post(recent, {"depuis": take.pk, "duree": "0", "unite": "mois"}, follow=True),
            self.client.post(recent, {"depuis": take.pk, "depuis_inventaire": "1"}, follow=True),
            self.client.get(recent, follow=True),
        ]
        for number, response in enumerate(landed):
            with self.subTest(page=number):
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="recettes-vendues"')
                self.assertEqual(active_labels(response), ["Inventaires"])
                self.assertEqual(section_shown(response), "Inventaires")
        self.assertEqual(navigation.section_of(resolve(recent)), "stock_takes")

    def test_the_shopping_forms_land_on_produits_et_charges(self):
        """« Prévoir les courses »' forms - « Réglages », « Pas ici », « Ne
        plus proposer », « Ne jamais proposer la catégorie », « Réinclure »,
        refused or not - « Rythme d'achat »'s own and the shopping lists':
        every page they land on is « Produits & charges », the folded bar
        saying so, and so is each of their routes. None is an
        « Inventaires » page."""
        made = make_shopping_history()
        lists = make_shopping_lists(made)
        for number, response in enumerate(shopping_list_landings(self.client, made, lists)):
            with self.subTest(list_page=number):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(active_labels(response), ["Produits &amp; charges"])
                self.assertEqual(section_shown(response), "Produits &amp; charges")
        for name in SHOPPING_LIST_ROUTES:
            with self.subTest(route=name):
                self.assertNotIn(name, navigation.STOCK_TAKE_VIEWS)
                self.assertEqual(navigation.section_of(resolve(reverse(f"inventory:{name}"))), "products")
        store = made.wholesaler.pk
        settings = reverse("inventory:shopping_settings")
        exclude = reverse("inventory:shopping_exclude")
        include = reverse("inventory:shopping_include")
        landed = [
            self.client.post(settings, {"fournisseur": store, "seuil": "30", "memoire": "6"}, follow=True),
            self.client.post(settings, {"fournisseur": store, "seuil": "abc"}, follow=True),
            self.client.post(settings, {"fournisseur": store, "defaut": "1"}, follow=True),
            self.client.post(
                exclude, {"fournisseur": store, "article": made.syrup.pk, "chez": store, "retour": "liste"}, follow=True
            ),
            self.client.post(exclude, {"fournisseur": store, "categorie": "Consignes exemple"}, follow=True),
            self.client.post(exclude, {"fournisseur": store, "article": "abc"}, follow=True),
            self.client.post(exclude, {"fournisseur": store, "article": made.rum.pk, "retour": "rythme"}, follow=True),
            self.client.post(
                include,
                {"fournisseur": store, "exclusion": ShoppingExclusion.objects.get(category="Consignes exemple").pk},
                follow=True,
            ),
            self.client.post(include, {"fournisseur": store, "exclusion": "999999"}, follow=True),
            self.client.get(exclude, follow=True),
        ]
        for number, response in enumerate(landed):
            with self.subTest(page=number):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(active_labels(response), ["Produits &amp; charges"])
                self.assertEqual(section_shown(response), "Produits &amp; charges")
        for name in ("shopping_list", "shopping_rhythm", "shopping_settings", "shopping_exclude", "shopping_include"):
            with self.subTest(route=name):
                self.assertNotIn(name, navigation.STOCK_TAKE_VIEWS)
                self.assertEqual(navigation.section_of(resolve(reverse(f"inventory:{name}"))), "products")

    def test_every_section_has_its_words(self):
        """A section navigation can light with no words in SECTION_LABELS
        draws a folded bar saying nothing (base.html's `{% if %}`): a new
        app added to SECTION_BY_APP needs its words too. And the words are
        a link's own, one link per section, nothing else - « Courses » an
        employee's, lit where « Produits & charges » would be
        (`navigation()`, not `section_of`)."""
        pages = [("inventory", url_name) for url_name in ("stock_list", *navigation.STOCK_TAKE_VIEWS)]
        pages += [(app, "any_view") for app in navigation.SECTION_BY_APP]
        can_light = {navigation.section_of(SimpleNamespace(app_name=app, url_name=view)) for app, view in pages}
        self.assertNotIn(navigation.SHOPPING_SECTION, can_light)
        self.assertEqual(set(navigation.SECTION_LABELS), can_light | {navigation.SHOPPING_SECTION})
        self.assertEqual(navigation.SECTION_LABELS[navigation.SHOPPING_SECTION], "Courses")
        self.assertEqual(
            sorted(escape(words) for words in navigation.SECTION_LABELS.values()), sorted(LABELS + EMPLOYEE_LABELS)
        )

    def test_each_workspace_counts_what_waits_there(self):
        supplier = make_supplier(code="SABBH", name="Sabbh")
        make_product(supplier=supplier, raw_name="RHUM INCONNU")
        make_invoice(supplier=supplier, parse_checks=[{"label": "x", "passed": False, "detail": ""}])
        PosProduct.objects.create(name="Pinte", total_quantity=3)
        PosProduct.objects.create(name="Café", ignored=True)
        links = {label_of(link): link for link in nav_links(self.client.get(reverse("inventory:stock_list")))}
        self.assertIn('<span class="badge">1</span>', links["Produits &amp; charges"])
        self.assertIn('<span class="badge">1</span>', links["Factures"])
        self.assertIn('<span class="badge">1</span>', links["Recettes &amp; ventes"])
        self.assertNotIn("badge", links["Inventaires"])

    def test_nothing_waiting_means_no_badge(self):
        links = nav_links(self.client.get(reverse("inventory:stock_list")))
        self.assertFalse(any("badge" in link for link in links))


class EmployeeNavigationTests(TestCase):
    """An employee's bar (accounts/access.py): the links of the pages his
    employer opened to him, each lit on its pages as the owner's are - and
    nothing lit on a page the gate refuses him, nor on « Aucune page
    ouverte »: the page shown is none of his links' (accounts.access.refused
    lights none, navigation.SECTION_BY_VIEW). Lit there, his « Factures »
    would say he is on a page of his while the page says he may not open
    it. Names and addresses invented."""

    def log_in_an_employee(self, *pages):
        employee = employee_of_the_test_tenant("employe-nav@example.invalid", pages, name="Léa Exemple")
        self.client.force_login(employee)
        return employee

    def test_his_pages_light_their_link(self):
        """« Factures » leads one who may only add to « Ajouter des
        factures », and lights there."""
        self.log_in_an_employee("invoices_add", "stock_takes", "returnables")
        for url, label in (
            (reverse("invoices:invoice_add"), "Factures"),
            (reverse("inventory:stock_take_list"), "Inventaires"),
            (reverse("inventory:stock_take_create"), "Inventaires"),
            (reverse("returnables:home"), "Consignes"),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    [label_of(link) for link in nav_links(response)], ["Factures", "Inventaires", "Consignes"]
                )
                self.assertEqual(active_labels(response), [label])
                self.assertEqual(section_shown(response), label)
        links = {label_of(link): link for link in nav_links(response)}
        self.assertIn(f'href="{reverse("invoices:invoice_add")}"', links["Factures"])

    def test_a_refused_page_lights_nothing(self):
        """Pages of his links' sections he may not open: « Factures »'s list
        (he may only add) and « Accès des employés » (lit « Personnel » for
        the owner, the owner's alone) - and a page of no link of his."""
        self.log_in_an_employee("invoices_add", "staff")
        for url in (reverse("invoices:invoice_list"), reverse("accounts:members"), reverse("bank:bank_home")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)
                self.assertContains(response, "Page non accessible", status_code=403)
                self.assertEqual([label_of(link) for link in nav_links(response)], ["Factures", "Personnel"])
                self.assertEqual(active_labels(response), [])
                self.assertIsNone(section_shown(response))

    def test_the_lists_alone_light_courses(self):
        """Given « Liste de courses » without « Produits & charges », his
        one link is « Courses », lit - and said by the folded bar - on the
        lists, a store's list to prepare and to tick, « Prévoir les
        courses », « Rythme d'achat » and every page a list's form lands
        on."""
        self.log_in_an_employee("shopping")
        made = make_shopping_history()
        lists = make_shopping_lists(made)
        landed = [
            self.client.get(reverse("inventory:shopping_list")),
            self.client.get(reverse("inventory:shopping_list"), {"fournisseur": made.grocer.pk, "dans": "30"}),
            self.client.get(reverse("inventory:shopping_rhythm")),
            self.client.get(reverse("inventory:shopping_rhythm"), {"fournisseur": made.wholesaler.pk}),
            *shopping_list_landings(self.client, made, lists),
        ]
        for number, response in enumerate(landed):
            with self.subTest(page=number):
                self.assertEqual(response.status_code, 200)
                self.assertEqual([label_of(link) for link in nav_links(response)], ["Courses"])
                self.assertEqual(active_labels(response), ["Courses"])
                self.assertEqual(section_shown(response), "Courses")
        links = {label_of(link): link for link in nav_links(landed[0])}
        self.assertIn(f'href="{reverse("inventory:shopping_lists")}"', links["Courses"])

    def test_a_refused_page_of_the_products_lights_nothing_for_the_lists_alone(self):
        """« Produits & charges »' other pages, and the forecast's settings
        and exclusions, are not his: refused, nothing lit - « Courses » is
        not the page asked for."""
        self.log_in_an_employee("shopping")
        for method, url in (
            ("get", reverse("inventory:stock_type_create")),
            ("get", reverse("inventory:review_queue")),
            ("post", reverse("inventory:shopping_settings")),
            ("post", reverse("inventory:shopping_exclude")),
        ):
            with self.subTest(url=url):
                response = getattr(self.client, method)(url)
                self.assertContains(response, "Page non accessible", status_code=403)
                self.assertEqual([label_of(link) for link in nav_links(response)], ["Courses"])
                self.assertEqual(active_labels(response), [])
                self.assertIsNone(section_shown(response))

    def test_a_page_of_the_products_lights_courses_only_for_one_given_the_lists(self):
        """`navigation()` alone, on a « Produits & charges » route: « Courses »
        for one given « Liste de courses » without the products; the products
        for one given them, and for the owner; nothing for one given
        neither - no link of his names the page."""
        url = reverse("inventory:shopping_lists")
        request = RequestFactory().get(url)
        request.resolver_match = resolve(url)
        for given, section in (
            (Access(owner=True), "products"),
            (Access(owner=False, areas=["shopping"]), "shopping"),
            (Access(owner=False, areas=["products"]), "products"),
            (Access(owner=False, areas=["shopping", "products"]), "products"),
            (Access(owner=False, areas=["invoices"]), ""),
        ):
            with self.subTest(access=repr(given)):
                request.access = given
                drawn = navigation.navigation(request)
                self.assertEqual(drawn["nav_section"], section)
                self.assertEqual(drawn["nav_section_label"], navigation.SECTION_LABELS.get(section, ""))

    def test_with_the_products_his_shopping_pages_are_produits_et_charges(self):
        self.log_in_an_employee("shopping", "products")
        response = self.client.get(reverse("inventory:shopping_lists"))
        self.assertEqual([label_of(link) for link in nav_links(response)], ["Produits &amp; charges"])
        self.assertEqual(active_labels(response), ["Produits &amp; charges"])
        self.assertEqual(section_shown(response), "Produits &amp; charges")

    def test_no_access_lights_nothing(self):
        self.log_in_an_employee()
        url = reverse("accounts:no_access")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Aucune page ouverte")
        self.assertEqual(nav_links(response), [])
        self.assertIsNone(section_shown(response))
        self.assertEqual(navigation.section_of(resolve(url)), "")

    def test_a_route_lighting_another_section_names_one_with_words(self):
        """A route of SECTION_BY_VIEW lights a section the folded bar can
        say, or none."""
        for view, section in navigation.SECTION_BY_VIEW.items():
            with self.subTest(view=view):
                self.assertTrue(section == "" or section in navigation.SECTION_LABELS, section)
                self.assertEqual(navigation.section_of(resolve(reverse(view))), section)


class MenuButtonTests(TestCase):
    """« Menu » and what it opens, as base.html draws them (30/09). The
    stylesheet opens #topbar-menu with `.topbar-toggle[aria-expanded="true"]
    ~ .topbar-menu`, so the button comes BEFORE the menu; it stays out of the
    <nav>, whose links nav_links() reads one by one - inside, it would be a
    tenth workspace to every test reading them."""

    def page(self):
        return self.client.get(reverse("inventory:stock_list"))

    def test_the_button_says_what_it_opens(self):
        response = self.page()
        header = topbar_of(response)
        toggle = toggle_of(response)
        self.assertTrue(toggle.startswith("<button "), toggle)
        for attribute in ('type="button"', 'aria-expanded="false"', 'aria-controls="topbar-menu"'):
            self.assertIn(attribute, toggle)
        # Drawn shut, as every page arrives (aria-expanded="false" above): the
        # state is that attribute, which the stylesheet reads.
        self.assertEqual(header.count('id="topbar-menu"'), 1)
        button = header.index(toggle)
        menu = header.index('id="topbar-menu"')
        self.assertLess(button, menu)
        self.assertLess(menu, header.index("<nav"))
        self.assertLess(header.index("</button>", button), menu)
        # What the button says, and the dot's words for a screen reader.
        inside = header[button : header.index("</button>", button)]
        self.assertIn("<span>Menu</span>", inside)
        self.assertIn('<span class="topbar-waiting-text visually-hidden">, du travail en attente</span>', inside)
        # Nothing of it in the navigation: its links are the workspaces.
        self.assertNotIn("data-topbar-toggle", header[header.index("<nav") : header.index("</nav>")])
        self.assertEqual([label_of(link) for link in nav_links(response)], LABELS)

    def test_the_script_runs_in_the_head_before_the_bar(self):
        """static/js/topbar.js puts the class that folds the links on <html>:
        deferred like the others, a phone painted three rows of links, then
        one. So it is in the head, after the stylesheet it switches, with no
        defer nor async - and once, htmx never swapping the head in again."""
        html = self.page().content.decode()
        head = html[: html.index("</head>")]
        scripts = re.findall(r"<script\b[^>]*\bsrc=\"[^\"]*js/topbar\.js[^\"]*\"[^>]*>", html)
        self.assertEqual(len(scripts), 1, scripts)
        (script,) = scripts
        self.assertIn(script, head)
        self.assertNotRegex(script, r"\s(defer|async)\b")
        self.assertRegex(script, r'src="/static/js/topbar\.js\?v=\d+"')
        stylesheet = re.search(r'<link rel="stylesheet" href="[^"]*css/marginmate\.css[^"]*">', head)
        self.assertIsNotNone(stylesheet)
        self.assertLess(stylesheet.start(), head.index(script))
