"""A membership moved to another espace, or given to another login, loses
its push devices (notifications/signals.py): a device serves one login of
ONE espace - the old bar's shared browser must not receive the new bar's
alerts, nor a leaver's phone those of the login now holding the row. Through
the admin's form and through a plain save (the shell). Real espaces in real
files; endpoints and keys invented."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.urls import reverse

from accounts.models import Membership, PushDevice
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from notifications import sending
from notifications.tests.support import QuietLogs, make_device
from tests.runner import confirm_password


class MembershipMovedTests(QuietLogs, TwoTenantsTestCase):
    def setUp(self):
        super().setUp()
        self.newcomer = get_user_model().objects.create_user(
            username="serveur@example.invalid", email="serveur@example.invalid", password="mot-de-passe-essai"
        )
        self.membership = Membership.objects.create(user=self.newcomer, tenant=self.bar_a, role=Membership.Role.MEMBER)
        self.device = make_device(self.membership)
        with bound_tenant(self.bar_a):
            self.assertEqual(sending.devices_for(), [self.device])

    def assert_gone_everywhere(self):
        self.assertFalse(PushDevice.objects.filter(pk=self.device.pk).exists())
        for tenant in (self.bar_a, self.bar_b):
            with bound_tenant(tenant):
                self.assertEqual(sending.devices_for(), [])

    def test_moved_in_the_admin_its_devices_go(self):
        get_user_model().objects.filter(pk=self.user_a.pk).update(is_staff=True, is_superuser=True)
        confirm_password(self.client, self.user_a)
        response = self.client.post(
            reverse("admin:accounts_membership_change", args=[self.membership.pk]),
            {
                "user": self.newcomer.pk,
                "tenant": self.bar_b.pk,
                "role": Membership.Role.MEMBER,
                "created_at_0": "2026-10-01",
                "created_at_1": "10:00:00",
                "_save": "Enregistrer",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.tenant, self.bar_b)
        self.assert_gone_everywhere()

    def test_moved_by_a_plain_save_its_devices_go(self):
        self.membership.tenant = self.bar_b
        self.membership.save()
        self.assert_gone_everywhere()

    def make_successor(self):
        return get_user_model().objects.create_user(
            username="releve@example.invalid", email="releve@example.invalid", password="mot-de-passe-essai"
        )

    def assert_gone_for(self, successor):
        self.assertFalse(PushDevice.objects.filter(pk=self.device.pk).exists())
        with bound_tenant(self.bar_a):
            self.assertEqual(sending.devices_for([successor.pk]), [])
            self.assertEqual(sending.devices_for(), [])

    def test_given_to_another_login_in_the_admin_its_devices_go(self):
        successor = self.make_successor()
        get_user_model().objects.filter(pk=self.user_a.pk).update(is_staff=True, is_superuser=True)
        confirm_password(self.client, self.user_a)
        response = self.client.post(
            reverse("admin:accounts_membership_change", args=[self.membership.pk]),
            {
                "user": successor.pk,
                "tenant": self.bar_a.pk,
                "role": Membership.Role.MEMBER,
                "created_at_0": "2026-10-01",
                "created_at_1": "10:00:00",
                "_save": "Enregistrer",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.membership.refresh_from_db()
        self.assertEqual((self.membership.user, self.membership.tenant), (successor, self.bar_a))
        self.assert_gone_for(successor)

    def test_given_to_another_login_by_a_plain_save_its_devices_go(self):
        successor = self.make_successor()
        self.membership.user = successor
        self.membership.save()
        self.assert_gone_for(successor)

    def test_any_other_change_keeps_them(self):
        self.membership.role = Membership.Role.OWNER
        self.membership.save()
        Membership.objects.get(pk=self.membership.pk).save()
        self.assertTrue(PushDevice.objects.filter(pk=self.device.pk).exists())
        with bound_tenant(self.bar_a):
            self.assertEqual(sending.devices_for(), [self.device])

    def test_another_membership_s_devices_are_untouched(self):
        theirs = make_device(Membership.objects.get(user=self.user_a))
        self.membership.tenant = self.bar_b
        self.membership.save()
        self.assertTrue(PushDevice.objects.filter(pk=theirs.pk).exists())
