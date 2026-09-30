"""« Entrées d'argent » in multi mode: what the till could not read is said
in every espace, and the commands that re-read its exports only in the
owner's.

Three places name one of those commands: the day with sales and no means of
payment read, the day with no price read, and « Pas de solde » when no card
payment is read at all. Another bar can neither run a server command nor has
any export to re-read (its till is « à configurer »), so each says that
instead. Data invented; two real espaces in temporary files
(accounts/tests/support.py).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.urls import reverse

from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from bank.models import BankTransaction
from recipes.models import PosProduct, PosProductDailyQuantity

JUNE = {"du": "2026-06-01", "au": "2026-06-30"}


class TillRemediesTests(TwoTenantsTestCase):
    owner_a = True

    def unread_day(self, tenant) -> None:
        """A till day with sales, no price read and no payment read - and a
        statement to set it beside (the page draws nothing without one)."""
        with bound_tenant(tenant):
            product = PosProduct.objects.create(name="Pinte Exemple")
            PosProductDailyQuantity.objects.create(
                product=product, sold_on=date(2026, 6, 6), quantity=2, revenue_read=False
            )
            BankTransaction.objects.create(
                operation_date=date(2026, 6, 8),
                bank_type="VIREMENT",
                label="VIR SEPA RECU /FRM CLIENT EXEMPLE REF0001",
                counterparty="CLIENT EXEMPLE",
                amount=Decimal("40.00"),
                kind=BankTransaction.Kind.TRANSFER,
                fingerprint="credit-essai-1",
            )

    def page(self, user) -> str:
        self.client.force_login(user)
        return self.client.get(reverse("bank:income_home"), JUNE).content.decode()

    def test_another_bar_is_not_handed_a_server_command(self):
        self.unread_day(self.bar_b)
        page = self.page(self.user_b)
        self.assertIn("aucun moyen de paiement lu", page)
        self.assertIn("pas encore de prix", page)
        self.assertIn("Pas de solde", page)
        self.assertNotIn("laddition_backfill", page)
        self.assertNotIn("manage.py", page)
        # Said once per warning, and in the balance's reason.
        self.assertEqual(page.count("à configurer — disponible prochainement dans les réglages de votre espace"), 3)

    def test_the_owner_s_espace_is_given_the_commands(self):
        self.unread_day(self.bar_a)
        page = self.page(self.user_a)
        self.assertIn("manage.py laddition_backfill_payments", page)
        self.assertIn("manage.py laddition_backfill_revenue", page)
        self.assertNotIn("à configurer", page)
