"""The topbar of a tenant's pages (templates/base.html): the bar's name and
« Se déconnecter », OUTSIDE the first <nav> (tests/test_navigation.py reads
its links) and inside #topbar-menu, what « Menu » opens under 860 px (30/09).
How much room it takes is measured in a browser
(accounts/tests/test_topbar_browser.py)."""

import re

from django.urls import reverse

from accounts.tests.support import TwoTenantsTestCase
from tests.test_navigation import LABELS, label_of, nav_links

LOGOUT = reverse("accounts:logout")


def after_the_nav(response) -> str:
    html = response.content.decode()
    return html[html.index("</nav>") : html.index("</header>")]


def element_by_id(html: str, element_id: str) -> str:
    """The element whose id is `element_id`, as HTML, up to ITS closing tag:
    the tags of its name opened and closed inside it are counted."""
    opening = re.search(r"<([a-z]+)\b[^>]*\bid=\"" + re.escape(element_id) + r"\"[^>]*>", html)
    assert opening is not None, element_id
    name = opening.group(1)
    depth = 0
    for tag in re.finditer(r"<(/?)" + name + r"\b[^>]*>", html[opening.start() :]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return html[opening.start() : opening.start() + tag.end()]
    raise AssertionError(f"#{element_id} is never closed")


class TenantTopbarTests(TwoTenantsTestCase):
    def test_the_bar_s_name_and_the_logout_follow_the_links(self):
        for user, mine, other in ((self.user_a, "Bar Alpha", "Bar Beta"), (self.user_b, "Bar Beta", "Bar Alpha")):
            with self.subTest(bar=mine):
                self.client.force_login(user)
                response = self.client.get(reverse("invoices:supplier_list"))
                bar = after_the_nav(response)
                self.assertIn(f'<span class="topbar-tenant" title="{mine}">{mine}</span>', bar)
                self.assertIn(f'<form method="post" action="{LOGOUT}" class="topbar-logout">', bar)
                self.assertIn('name="csrfmiddlewaretoken"', bar)
                self.assertIn("Se déconnecter", bar)
                self.assertNotContains(response, other)
                # The links themselves are the ones every page has.
                labels = [label_of(link) for link in nav_links(response)]
                self.assertEqual(labels, LABELS)

    def test_the_bar_s_name_and_the_logout_are_in_the_menu(self):
        """Folded under 860 px, « Menu » opens the links, the bar's name and
        « Se déconnecter » together: left beside the brand, the name and the
        logout were the row the fold exists to save (UX review, 30/09) - and
        a logout alone on the bar is one tap from throwing a count away."""
        self.client.force_login(self.user_a)
        response = self.client.get(reverse("invoices:supplier_list"))
        header = response.content.decode()
        header = header[header.index('<header class="topbar">') : header.index("</header>")]
        menu = element_by_id(header, "topbar-menu")
        self.assertIn('<span class="topbar-tenant" title="Bar Alpha">Bar Alpha</span>', menu)
        self.assertIn(f'<form method="post" action="{LOGOUT}" class="topbar-logout">', menu)
        self.assertIn("Se déconnecter", menu)
        # The links are in it too, first; the brand and the button are not.
        self.assertLess(menu.index("<nav"), menu.index('class="topbar-account"'))
        self.assertNotIn('class="brand"', menu)
        self.assertNotIn("data-topbar-toggle", menu)
        # Nothing of the account is left outside it.
        outside = header.replace(menu, "")
        self.assertNotIn("topbar-tenant", outside)
        self.assertNotIn("topbar-logout", outside)

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
