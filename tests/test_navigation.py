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

from django.test import TestCase
from django.urls import resolve, reverse
from django.utils.html import escape

from config import navigation
from inventory.models import GapExclusion, GapFillEntry, StockType
from recipes.models import PosProduct
from staff.models import Employee
from tests.factories import (
    make_invoice,
    make_product,
    make_recipe,
    make_stock_take,
    make_stock_type,
    make_supplier,
)
from tests.runner import employee_of_the_test_tenant
from tests.test_views_smoke import make_gaps_to_fill

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
        pages = {
            "Produits &amp; charges": [
                reverse("inventory:stock_list"),
                reverse("inventory:stock_type_update", args=[stock_type.pk]),
                reverse("inventory:stock_type_create"),
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
            "Banque": [reverse("bank:bank_home")],
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
            "Données": [reverse("transfer:data_home"), reverse("transfer:data_import"), reverse("transfer:data_clear")],
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

    def test_every_section_has_its_words(self):
        """A section navigation can light with no words in SECTION_LABELS
        draws a folded bar saying nothing (base.html's `{% if %}`): a new
        app added to SECTION_BY_APP needs its words too. And the words are
        a link's own, one link per section, nothing else."""
        pages = [("inventory", url_name) for url_name in ("stock_list", *navigation.STOCK_TAKE_VIEWS)]
        pages += [(app, "any_view") for app in navigation.SECTION_BY_APP]
        can_light = {navigation.section_of(SimpleNamespace(app_name=app, url_name=view)) for app, view in pages}
        self.assertEqual(set(navigation.SECTION_LABELS), can_light)
        self.assertEqual(sorted(escape(words) for words in navigation.SECTION_LABELS.values()), sorted(LABELS))

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
