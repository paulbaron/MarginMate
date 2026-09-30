"""« Marges » in multi mode: the remedy for days whose money was never read
is a command on the server, and only the owner's tenant is handed it.

The unread days are counted and said in every tenant - the margins below
them are too low wherever they happen. What changes is the way out: the
command re-reads the till's exports on the server, which another bar can
neither run nor has any export for (its till is « à configurer »). Data
invented; two real tenants in temporary files (accounts/tests/support.py).
"""

from __future__ import annotations

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from recipes.models import PosProduct, PosProductDailyQuantity

PAGE = "margins:margins_home"


class UnreadDaysRemedyTests(TwoTenantsTestCase):
    owner_a = True

    def unread_day(self, tenant) -> None:
        with bound_tenant(tenant):
            product = PosProduct.objects.create(name="Limonade Exemple")
            PosProductDailyQuantity.objects.create(
                product=product, sold_on=timezone.localdate() - timedelta(days=3), quantity=4, revenue_read=False
            )

    def page(self, user) -> str:
        self.client.force_login(user)
        return self.client.get(reverse(PAGE)).content.decode()

    def test_another_bar_is_not_handed_a_server_command(self):
        self.unread_day(self.bar_b)
        page = self.page(self.user_b)
        self.assertIn("pas encore de prix", page)
        self.assertNotIn("laddition_backfill", page)
        self.assertNotIn("manage.py", page)
        self.assertIn("à configurer — disponible prochainement dans les réglages de votre espace", page)

    def test_the_owner_s_tenant_is_given_the_command(self):
        self.unread_day(self.bar_a)
        page = self.page(self.user_a)
        self.assertIn("pas encore de prix", page)
        self.assertIn("manage.py laddition_backfill_revenue", page)
        self.assertNotIn("à configurer", page)
