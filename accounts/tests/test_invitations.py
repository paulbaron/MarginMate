"""Invitation codes (accounts/invitations.py) and `manage.py
create_invitation`: shown once, kept hashed, used once, optionally
expiring."""

import re
from datetime import timedelta
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from accounts import invitations
from accounts.models import Invitation, Tenant, hash_secret
from accounts.tests.support import TenancyTestCase

CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){5}$")


class CodeTests(SimpleTestCase):
    def test_a_code_is_24_characters_nothing_to_misread_in_groups_of_four(self):
        codes = {invitations.new_code() for _ in range(200)}
        self.assertEqual(len(codes), 200)
        for code in codes:
            self.assertRegex(code, CODE_RE)
            self.assertEqual(len(invitations.normalize(code)), 24)

    def test_what_is_typed_back_is_read_the_same(self):
        code = "K7QM-2XWD-9PLA-4RTV-8NBC-6EFG"
        for typed in (code, code.lower(), code.replace("-", ""), f" {code.replace('-', ' ')} ", f"« {code} »."):
            with self.subTest(typed=typed):
                self.assertEqual(invitations.code_hash(typed), invitations.code_hash(code))
        self.assertEqual(invitations.code_hash(code), hash_secret("K7QM2XWD9PLA4RTV8NBC6EFG"))
        self.assertEqual(invitations.normalize(None), "")


class InvitationTests(TenancyTestCase):
    def test_only_the_hash_is_kept(self):
        invitation, code = invitations.create_invitation(note="Essai")
        self.assertEqual(invitation.code_hash, invitations.code_hash(code))
        self.assertNotIn(invitations.normalize(code), invitation.code_hash)
        self.assertEqual(invitation.note, "Essai")

    def test_an_invitation_expires_after_thirty_days_unless_told_otherwise(self):
        """A code sent in a message and never used would otherwise open a
        signup for ever, to whoever reads that message one day."""
        now = timezone.now()
        self.assertEqual(invitations.DEFAULT_DAYS, 30)
        invitation, _code = invitations.create_invitation(now=now)
        self.assertEqual(invitation.expires_at, now + timedelta(days=30))
        invitation, _code = invitations.create_invitation(days=14, now=now)
        self.assertEqual(invitation.expires_at, now + timedelta(days=14))
        for never in (0, None):
            with self.subTest(days=never):
                invitation, _code = invitations.create_invitation(days=never, now=now)
                self.assertIsNone(invitation.expires_at)

    def test_usable_means_unused_and_unexpired(self):
        now = timezone.now()
        invitation, code = invitations.create_invitation(days=2, now=now)
        self.assertEqual(invitations.usable_invitation(code, now=now), invitation)
        self.assertEqual(invitations.usable_invitation(code.lower(), now=now), invitation)
        self.assertIsNone(invitations.usable_invitation(code, now=now + timedelta(days=2)))
        self.assertIsNone(invitations.usable_invitation("ABCD-EFGH-JKLM-NPQR-STUV-WXYZ", now=now))
        self.assertIsNone(invitations.usable_invitation("", now=now))
        Invitation.objects.filter(pk=invitation.pk).update(used_at=now)
        self.assertIsNone(invitations.usable_invitation(code, now=now))

    def test_days_out_of_range_are_refused(self):
        for days in (-1, invitations.MAX_DAYS + 1):
            with self.subTest(days=days), self.assertRaises(ValueError):
                invitations.create_invitation(days=days)


class CreateInvitationCommandTests(TenancyTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("create_invitation", *args, stdout=out)
        return out.getvalue()

    def printed_code(self, output):
        return re.search(r"^\s+([A-Z0-9-]{29})$", output, re.MULTILINE).group(1)

    def test_the_code_is_printed_once_and_opens_a_signup(self):
        output = self.run_command("--note", "Bar de la gare (essai)", "--days", "14")
        code = self.printed_code(output)
        self.assertRegex(code, CODE_RE)
        invitation = Invitation.objects.get()
        self.assertEqual(invitation.code_hash, invitations.code_hash(code))
        self.assertEqual(invitation.note, "Bar de la gare (essai)")
        self.assertAlmostEqual(invitation.expires_at, timezone.now() + timedelta(days=14), delta=timedelta(minutes=1))
        self.assertIn("affiché cette fois seulement", output)
        self.assertIn(f"jusqu'au {timezone.localtime(invitation.expires_at):%d/%m/%Y à %H:%M} (14 jours)", output)
        # Nothing else in the database says it.
        self.assertFalse(Invitation.objects.filter(note__contains=code).exists())

        cache.clear()
        self.addCleanup(cache.clear)
        password = "Tabouret-Zinc-47"
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "code": code,
                "bar_name": "Bar de la Gare",
                "email": "gare@example.invalid",
                "password1": password,
                "password2": password,
            },
        )
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertTrue(Tenant.objects.filter(name="Bar de la Gare").exists())

    def test_without_days_it_expires_in_thirty_days_and_says_when(self):
        output = self.run_command()
        expires = Invitation.objects.get().expires_at
        self.assertAlmostEqual(expires, timezone.now() + timedelta(days=30), delta=timedelta(minutes=1))
        self.assertIn(f"jusqu'au {timezone.localtime(expires):%d/%m/%Y à %H:%M} (30 jours", output)
        self.assertIn("--days 0", output)
        self.assertIn("Sans note.", output)

    def test_days_0_never_expires_and_says_so(self):
        output = self.run_command("--days", "0")
        self.assertIsNone(Invitation.objects.get().expires_at)
        self.assertIn("Sans date d'expiration (--days 0)", output)

    def test_each_run_is_a_new_code(self):
        first, second = self.printed_code(self.run_command()), self.printed_code(self.run_command())
        self.assertNotEqual(first, second)
        self.assertEqual(Invitation.objects.count(), 2)

    def test_refusals(self):
        for args in (("--days", "-1"), ("--days", str(invitations.MAX_DAYS + 1)), ("--note", "x" * 201)):
            with self.subTest(args=args), self.assertRaises(CommandError):
                self.run_command(*args)
        self.assertFalse(Invitation.objects.exists())
