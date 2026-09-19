"""The top navigation: three workspaces instead of eight pages.

Stock and its review queue are one page ("Produits & charges"); invoices, tickets and
their sources and suppliers another ("Achats"); recipes, till products and sales a third
("Recettes & ventes"). Each link lights up on every page of its workspace -
and only it - and carries the count of what is waiting there.
"""

import re

from django.test import TestCase
from django.urls import reverse

from recipes.models import PosProduct
from tests.factories import make_invoice, make_product, make_recipe, make_stock_type, make_supplier

LABELS = ["Produits &amp; charges", "Achats", "Banque", "Recettes &amp; ventes", "Inventaires", "Données", "Admin"]


def nav_links(response):
    html = response.content.decode()
    nav = html[html.index("<nav"):html.index("</nav>")]
    return re.findall(r"<a [^>]*>.*?</a>", nav, flags=re.S)


def label_of(link):
    inner = re.sub(r"^<a [^>]*>|</a>$", "", link)
    return re.sub(r"<[^>]+>", "", re.sub(r"<span class=\"badge\">.*?</span>", "", inner, flags=re.S)).strip()


def active_labels(response):
    return [label_of(link) for link in nav_links(response) if 'class="active"' in link]


class NavigationTests(TestCase):
    def test_the_workspaces_in_order(self):
        response = self.client.get(reverse("inventory:stock_list"))
        self.assertEqual([label_of(link) for link in nav_links(response)], LABELS)

    def test_each_page_lights_up_its_workspace_only(self):
        supplier = make_supplier(code="METRO", name="Metro")
        invoice = make_invoice(supplier=supplier)
        recipe = make_recipe(name="Mule")
        stock_type = make_stock_type(name="Vodka")
        pages = {
            "Produits &amp; charges": [
                reverse("inventory:stock_list"),
                reverse("inventory:stock_type_update", args=[stock_type.pk]),
                reverse("inventory:stock_type_create"),
            ],
            "Achats": [
                reverse("invoices:invoice_list"),
                reverse("invoices:receipt_queue"),
                reverse("invoices:invoice_type_list"),
                reverse("invoices:supplier_list"),
                reverse("invoices:supplier_detail", args=[supplier.pk]),
                reverse("invoices:invoice_type_create"),
                reverse("invoices:invoice_detail", args=[invoice.pk]),
                reverse("invoices:invoice_edit_lines", args=[invoice.pk]),
                reverse("invoices:invoice_create_manual"),
            ],
            "Recettes &amp; ventes": [
                reverse("recipes:recipe_list"),
                reverse("recipes:recipe_detail", args=[recipe.pk]),
                reverse("recipes:recipe_create"),
                reverse("recipes:pos_product_list"),
                reverse("recipes:sales_list"),
                reverse("recipes:sale_document_create"),
            ],
            "Inventaires": [reverse("inventory:stock_take_list"), reverse("inventory:stock_take_create")],
            "Banque": [reverse("bank:bank_home")],
            "Données": [reverse("transfer:data_home"), reverse("transfer:data_import"), reverse("transfer:data_clear")],
        }
        for label, urls in pages.items():
            for url in urls:
                with self.subTest(url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(active_labels(response), [label])

    def test_each_workspace_counts_what_waits_there(self):
        supplier = make_supplier(code="SABBH", name="Sabbh")
        make_product(supplier=supplier, raw_name="RHUM INCONNU")
        make_invoice(supplier=supplier, parse_checks=[{"label": "x", "passed": False, "detail": ""}])
        PosProduct.objects.create(name="Pinte", total_quantity=3)
        PosProduct.objects.create(name="Café", ignored=True)
        links = {label_of(link): link for link in nav_links(self.client.get(reverse("inventory:stock_list")))}
        self.assertIn('<span class="badge">1</span>', links["Produits &amp; charges"])
        self.assertIn('<span class="badge">1</span>', links["Achats"])
        self.assertIn('<span class="badge">1</span>', links["Recettes &amp; ventes"])
        self.assertNotIn("badge", links["Inventaires"])

    def test_nothing_waiting_means_no_badge(self):
        links = nav_links(self.client.get(reverse("inventory:stock_list")))
        self.assertFalse(any("badge" in link for link in links))
