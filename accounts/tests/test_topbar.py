"""The topbar of an espace's pages (templates/base.html): the bar's name and
« Se déconnecter », OUTSIDE the first <nav> (tests/test_navigation.py reads
its links). How much room it takes is measured in a browser
(accounts/tests/test_topbar_browser.py)."""

from django.urls import reverse

from accounts.tests.support import TwoTenantsTestCase
from tests.test_navigation import LABELS, label_of, nav_links

LOGOUT = reverse("accounts:logout")


def after_the_nav(response) -> str:
    html = response.content.decode()
    return html[html.index("</nav>"):html.index("</header>")]


class EspaceTopbarTests(TwoTenantsTestCase):
    def test_the_bar_s_name_and_the_logout_follow_the_links(self):
        for user, mine, other in ((self.user_a, "Bar Alpha", "Bar Beta"), (self.user_b, "Bar Beta", "Bar Alpha")):
            with self.subTest(bar=mine):
                self.client.force_login(user)
                response = self.client.get(reverse("invoices:supplier_list"))
                bar = after_the_nav(response)
                self.assertIn(f'<span class="topbar-espace" title="{mine}">{mine}</span>', bar)
                self.assertIn(f'<form method="post" action="{LOGOUT}" class="topbar-logout">', bar)
                self.assertIn('name="csrfmiddlewaretoken"', bar)
                self.assertIn("Se déconnecter", bar)
                self.assertNotContains(response, other)
                # The links themselves are the ones every page has.
                labels = [label_of(link) for link in nav_links(response)]
                self.assertEqual(labels, LABELS)

    def test_the_name_is_text_whatever_it_holds(self):
        type(self.bar_a).objects.filter(pk=self.bar_a.pk).update(name='Le <b>Zinc</b> "essai"')
        self.client.force_login(self.user_a)
        bar = after_the_nav(self.client.get(reverse("invoices:supplier_list")))
        self.assertIn("Le &lt;b&gt;Zinc&lt;/b&gt; &quot;essai&quot;", bar)
        self.assertNotIn("<b>Zinc</b>", bar)

    def test_the_topbar_s_logout_logs_out(self):
        self.client.force_login(self.user_a)
        self.client.get(reverse("invoices:supplier_list"))
        response = self.client.post(LOGOUT, follow=True)
        self.assertContains(response, "Vous êtes déconnecté.")
        self.assertNotIn("_auth_user_id", self.client.session)
