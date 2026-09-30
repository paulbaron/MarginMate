"""« Back to the page that asked »: one rule, `common.safe_next`, for every
`next` a form or an address carries (security audit LB-5).

`next=abc` passed Django's url_has_allowed_host_and_scheme as a relative
address; redirect() then took it for the NAME of a route, found none and
raised NoReverseMatch - a 500 for anybody who edited the address, on Achats'
deletions, the bank's actions and the margins' panel. Now anything that is
not a path of this site goes to the page's own default."""

from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from common import local_path, local_return, safe_next

#: What an edited address or a crafted form can carry, and none of it is a
#: path of this site.
NOT_A_PATH = [
    "abc",
    "invoices:invoice_delete",
    "invoices:invoice_list",
    "https://ailleurs.example/",
    "//ailleurs.example/",
    "/\\ailleurs.example/",
    "\\\\ailleurs.example",
    "javascript:alert(1)",
    "/a\nSet-Cookie: x=1",
    "/a\rb",
    "/a\x00b",
    " /banque/",
]


class SafeNextTests(SimpleTestCase):
    def request(self, method="get", **data):
        return getattr(RequestFactory(), method)("/quelque-part/", data)

    def test_a_path_of_this_site_is_followed(self):
        for target in ("/banque/", "/banque/?vue=depenses&mois=2026-07", "/marges/#panneau"):
            with self.subTest(target=target):
                self.assertEqual(safe_next(self.request(next=target), "/defaut/"), target)
                self.assertEqual(safe_next(self.request("post", next=target), "/defaut/"), target)

    def test_anything_else_is_the_default(self):
        for target in NOT_A_PATH:
            with self.subTest(target=target):
                self.assertEqual(safe_next(self.request(next=target), "/defaut/"), "/defaut/")
                self.assertEqual(local_path(self.request(), target), "")

    def test_nothing_sent_is_the_default(self):
        self.assertEqual(safe_next(self.request(), "/defaut/"), "/defaut/")

    def test_the_return_address_keeps_its_rule(self):
        self.assertEqual(local_return(self.request(retour="/consignes/")), "/consignes/")
        for target in NOT_A_PATH:
            with self.subTest(target=target):
                self.assertEqual(local_return(self.request(retour=target)), "")


class NextOnThePagesTests(TestCase):
    """Each view that reads `next`, asked with what is no path: a redirect to
    its own default, never a 500 (each of these raised NoReverseMatch)."""

    CASES = [
        # (url, method, default)
        ("invoices:invoice_bulk_delete", "get", "invoices:invoice_list"),
        ("bank:bank_reconcile", "get", "bank:bank_home"),
        ("bank:bank_reconcile", "post", "bank:bank_home"),
        ("bank:link_proposals", "get", "bank:proposals"),
        ("margins:count_articles", "get", "margins:margins_home"),
        ("margins:count_articles", "post", "margins:margins_home"),
    ]

    def test_a_next_that_is_no_path_goes_to_the_default(self):
        for name, method, default in self.CASES:
            for target in ("abc", "invoices:invoice_delete", "//ailleurs.example/"):
                with self.subTest(view=name, method=method, next=target):
                    url = reverse(name)
                    if method == "get":
                        response = self.client.get(url, {"next": target})
                    else:
                        response = self.client.post(url, {"next": target})
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith(reverse(default)), response["Location"])

    def test_a_path_of_this_site_is_still_where_it_goes(self):
        for name, method, _default in self.CASES:
            with self.subTest(view=name, method=method):
                url = reverse(name)
                target = "/banque/?vue=depenses"
                response = (self.client.get if method == "get" else self.client.post)(url, {"next": target})
                self.assertEqual((response.status_code, response["Location"]), (302, target))

    def test_the_deletion_page_offers_only_a_safe_way_back(self):
        from tests.factories import make_invoice

        invoice = make_invoice()
        page = self.client.get(reverse("invoices:invoice_delete", args=[invoice.pk]), {"next": "abc"})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["next"], reverse("invoices:invoice_list"))
