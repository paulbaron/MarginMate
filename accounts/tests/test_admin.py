"""The admin (accounts/admin_site.py, accounts/admin.py): superusers only, on
their own tenant."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.checks import run_checks
from django.test import TestCase
from django.urls import reverse

from accounts.admin_site import MarginMateAdminSite
from accounts.models import Invitation, Membership, Tenant
from accounts.tenancy import bound_tenant
from accounts.tests.support import TenancyTestCase, TwoTenantsTestCase
from tests.factories import make_supplier


class AdminSiteTests(TestCase):
    def test_the_project_s_site_is_the_one_every_app_registers_on(self):
        self.assertIsInstance(admin.site, MarginMateAdminSite)
        for model in (Tenant, Membership, Invitation, get_user_model()):
            self.assertTrue(admin.site.is_registered(model), model)


class MultiModeAdminTests(TwoTenantsTestCase):
    def setUp(self):
        super().setUp()
        with bound_tenant(self.bar_a):
            make_supplier(code="T-ALPHA", name="Grossiste Alpha")
        with bound_tenant(self.bar_b):
            make_supplier(code="T-BETA", name="Grossiste Beta")

    def test_staff_alone_is_not_enough(self):
        get_user_model().objects.filter(pk=self.user_a.pk).update(is_staff=True)
        self.client.force_login(self.user_a)
        for url in (reverse("admin:index"), reverse("admin:auth_user_changelist")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response["Location"].startswith(reverse("admin:login")), response["Location"])

    def test_a_superuser_works_on_his_own_tenant(self):
        get_user_model().objects.filter(pk=self.user_a.pk).update(is_staff=True, is_superuser=True)
        self.client.force_login(self.user_a)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        suppliers = self.client.get(reverse("admin:invoices_supplier_changelist"))
        self.assertContains(suppliers, "Grossiste Alpha")
        self.assertNotContains(suppliers, "Grossiste Beta")
        for name in ("tenant", "membership", "invitation"):
            with self.subTest(model=name):
                self.assertEqual(self.client.get(reverse(f"admin:accounts_{name}_changelist")).status_code, 200)

    def test_a_tenant_is_never_added_here_nor_its_owner_s_accounts_handed_out(self):
        get_user_model().objects.filter(pk=self.user_a.pk).update(is_staff=True, is_superuser=True)
        self.client.force_login(self.user_a)
        self.assertEqual(self.client.get(reverse("admin:accounts_tenant_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:accounts_invitation_add")).status_code, 403)
        page = self.client.get(reverse("admin:accounts_tenant_change", args=[self.bar_b.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="uses_server_integrations"')
        self.assertContains(page, 'name="is_active"')

    def test_the_admin_link_is_a_superuser_s_only(self):
        from tests.test_navigation import LABELS, label_of, nav_links

        self.client.force_login(self.user_a)
        labels = [label_of(link) for link in nav_links(self.client.get(reverse("inventory:stock_list")))]
        self.assertEqual(labels, LABELS)
        get_user_model().objects.filter(pk=self.user_a.pk).update(is_staff=True, is_superuser=True)
        labels = [label_of(link) for link in nav_links(self.client.get(reverse("inventory:stock_list")))]
        self.assertEqual(labels, [*LABELS, "Admin"])

    def test_a_superuser_cannot_close_his_own_tenant_here(self):
        """The admin needs an open tenant like every page: closing his own
        one click away shut it on him (« Aucun espace »), and the admin is
        where a tenant is reopened. Another bar's « actif » stays his."""
        get_user_model().objects.filter(pk=self.user_a.pk).update(is_staff=True, is_superuser=True)
        self.client.force_login(self.user_a)
        change = reverse("admin:accounts_tenant_change", args=[self.bar_a.pk])
        self.assertNotContains(self.client.get(change), 'name="is_active"')
        # « actif » left out of the POST is « actif » unticked, were it editable.
        response = self.client.post(change, {"name": "Bar Alpha", "_save": "Enregistrer"})
        self.assertEqual(response.status_code, 302)
        self.bar_a.refresh_from_db()
        self.assertTrue(self.bar_a.is_active)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        # Bar Beta's can be closed from here.
        other = reverse("admin:accounts_tenant_change", args=[self.bar_b.pk])
        self.assertContains(self.client.get(other), 'name="is_active"')
        self.client.post(other, {"name": "Bar Beta", "_save": "Enregistrer"})
        self.bar_b.refresh_from_db()
        self.assertFalse(self.bar_b.is_active)


class SuperuserWithNoTenantTests(TenancyTestCase):
    """The admin needs an open tenant like every page: a superuser with none
    - made by `createsuperuser --database accounts` before `adopt_database`,
    or whose tenant was closed - reads « Aucun espace » there too, the
    central rows' pages included (then: `manage.py shell`)."""

    def test_the_admin_says_no_tenant(self):
        operator = get_user_model().objects.create_superuser(
            username="exploitant@example.invalid", email="exploitant@example.invalid", password="x"
        )
        self.client.force_login(operator)
        for url in (reverse("admin:index"), reverse("admin:accounts_tenant_changelist")):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)
                self.assertContains(response, "Aucun espace", status_code=403)


class OneOwnerTenantInTheAdminTests(TenancyTestCase):
    """accounts.E005 (at most one OPEN tenant using the server's accounts)
    held in the admin too: « utilise les accès du serveur » is read-only
    there, but « actif » reopened an owner's tenant that adopt_database
    --leave-current had closed, next to the one open - two Metro pauses on
    one account until the next restart's check said so."""

    def setUp(self):
        super().setUp()
        self.mine = self.make_tenant("Bar de l'Exploitant")
        operator = self.make_member(self.mine, "exploitant@example.invalid")
        get_user_model().objects.filter(pk=operator.pk).update(is_staff=True, is_superuser=True)
        self.client.force_login(operator)
        self.old = self.make_tenant("Bar Proprio Ancien", owner=True)
        Tenant.objects.filter(pk=self.old.pk).update(is_active=False)

    def reopen(self, tenant):
        return self.client.post(
            reverse("admin:accounts_tenant_change", args=[tenant.pk]),
            {"name": tenant.name, "is_active": "on", "_save": "Enregistrer"},
        )

    def test_a_second_open_one_is_refused_and_named(self):
        current = self.make_tenant("Bar Proprio", owner=True)
        response = self.reopen(self.old)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "utilise déjà les accès du serveur")
        self.assertContains(response, f"« Bar Proprio » (dossier {current.dir_name})")
        self.old.refresh_from_db()
        self.assertFalse(self.old.is_active)
        self.assertNotIn("accounts.E005", [issue.id for issue in run_checks()])

    def test_with_none_open_it_reopens(self):
        self.assertRedirects(
            self.reopen(self.old), reverse("admin:accounts_tenant_changelist"), fetch_redirect_response=False
        )
        self.old.refresh_from_db()
        self.assertTrue(self.old.is_active)

    def test_a_tenant_without_the_server_s_accounts_reopens_whatever_is_open(self):
        self.make_tenant("Bar Proprio", owner=True)
        closed = self.make_tenant("Bar Voisin Fermé")
        Tenant.objects.filter(pk=closed.pk).update(is_active=False)
        self.assertEqual(self.reopen(closed).status_code, 302)
        closed.refresh_from_db()
        self.assertTrue(closed.is_active)
