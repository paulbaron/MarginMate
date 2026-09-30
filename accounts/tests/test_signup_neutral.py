"""A signup never says whether an address has an account (security audit
ANON-5).

Holding one valid invitation code, anyone could ask the signup page about
any address: « Un compte existe déjà avec cette adresse » answered yes, and
the code stayed unused for the next guess. Now an address that has a login
is refused with the SAME sentence, on the same field, as a code that opens
nothing - and the invitation is voided (`Invitation.used_at` set, nobody
`used_by`) once `signup.TAKEN_ADDRESSES_BEFORE_VOID` (3) addresses with a
login were tried with it, so a code can no longer be spent asking. Counted
on the invitation's row (`refused_addresses`, migration accounts/0002): a
count in the cache would be forgotten at a restart, or pushed out of it.

Names and addresses invented."""

import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from accounts import invitations, provisioning, signup
from accounts.models import Invitation, Tenant
from accounts.tests.support import TenancyTestCase

SIGNUP = reverse("accounts:signup")
PASSWORD = "Tabouret-Zinc-47"
BAD_CODE = "ABCD-EFGH-JKLM-NPQR-STUV-WXYZ"


def comparable(html: str) -> str:
    """The page without what differs by nature from one post to another: the
    CSRF token and the values typed back into the fields."""
    html = re.sub(r'name="csrfmiddlewaretoken" value="[^"]*"', "", html)
    return re.sub(r'value="[^"]*"', 'value=""', html)


class NeutralSignupTests(TenancyTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.invitation, self.code = invitations.create_invitation(note="Bar de la gare (essai)")
        for n in range(4):
            get_user_model().objects.create_user(
                username=f"inscrit{n}@example.invalid", email=f"inscrit{n}@example.invalid", password=PASSWORD
            )

    def post(self, code=None, email="inscrit0@example.invalid", ip="192.0.2.20"):
        return Client().post(
            SIGNUP,
            {"code": code or self.code, "bar_name": "Le Zinc d'Essai", "email": email,
             "password1": PASSWORD, "password2": PASSWORD},
            REMOTE_ADDR=ip,
        )

    def refresh(self) -> Invitation:
        self.invitation.refresh_from_db()
        return self.invitation

    def test_a_taken_address_reads_exactly_like_a_code_that_opens_nothing(self):
        taken = self.post()
        unknown = self.post(code=BAD_CODE, ip="192.0.2.21")
        self.assertEqual((taken.status_code, unknown.status_code), (200, 200))
        self.assertNotContains(taken, "existe déjà")
        self.assertContains(taken, "Ce code d&#x27;invitation n&#x27;ouvre pas d&#x27;inscription")
        self.assertEqual(comparable(taken.content.decode()), comparable(unknown.content.decode()))
        self.assertEqual(signup.EMAIL_TAKEN, signup.CODE_REFUSED)
        self.assertFalse(Tenant.objects.exists())

    def test_two_taken_addresses_leave_the_code_to_its_invitee(self):
        self.post(email="inscrit0@example.invalid")
        self.post(email="inscrit1@example.invalid")
        self.assertEqual((self.refresh().refused_addresses, self.invitation.used_at), (2, None))
        response = self.post(email="nouvelle@example.invalid")
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(Tenant.objects.count(), 1)

    def test_the_third_taken_address_voids_the_invitation(self):
        self.assertEqual(signup.TAKEN_ADDRESSES_BEFORE_VOID, 3)
        with self.assertLogs("accounts.signup", "WARNING") as logged:
            for n in range(3):
                self.post(email=f"inscrit{n}@example.invalid")
        invitation = self.refresh()
        self.assertEqual(invitation.refused_addresses, 3)
        self.assertIsNotNone(invitation.used_at)
        self.assertIsNone(invitation.used_by)
        self.assertIn(f"Invitation {invitation.pk}", "\n".join(logged.output))
        # Spent: a free address is refused like any used code, and nothing is made.
        response = self.post(email="nouvelle@example.invalid")
        self.assertContains(response, "n&#x27;ouvre pas d&#x27;inscription")
        self.assertFalse(Tenant.objects.exists())
        self.assertFalse(get_user_model().objects.filter(username="nouvelle@example.invalid").exists())

    def test_an_address_taken_while_the_espace_was_made_is_said_and_counted_the_same(self):
        real_copy = provisioning.copy_database

        def address_taken(*args, **kwargs):
            real_copy(*args, **kwargs)
            get_user_model().objects.create_user(username="lente@example.invalid", email="lente@example.invalid",
                                                 password="x")

        with mock.patch.object(provisioning, "copy_database", address_taken):
            with self.assertRaises(signup.SignupRefused) as refused:
                signup.sign_up(code=self.code, bar_name="Bar Lent", email="lente@example.invalid", password=PASSWORD)
        self.assertEqual((refused.exception.field, refused.exception.message), ("code", signup.CODE_REFUSED))
        self.assertEqual(self.refresh().refused_addresses, 1)
        self.assertFalse(Tenant.objects.exists())

    def test_a_wrong_code_counts_nothing_on_any_invitation(self):
        self.post(code=BAD_CODE)
        self.assertEqual(self.refresh().refused_addresses, 0)
