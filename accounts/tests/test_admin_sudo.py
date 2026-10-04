"""The admin behind « Confirmez votre mot de passe » (accounts/sudo.py,
accounts/admin_site.py; security review of 01/10/2026).

The admin edits every login's password, every membership's role and every
raw row of the espace - a source's channel and « actif » included: a
superuser's session alone, left open on the bar's PC for two weeks or
copied, reached all of it. Every admin page but its login and its logout
now asks for the MarginMate password first, as « Identifiants » does, and a
page in use keeps the confirmation alive.

A source fetching from a customer portal decides where a stored password
is typed: in the admin its channel and « actif » are read-only - it is
created and switched on from Achats → Sources, by the espace's owner, his
password confirmed (invoices/tests/test_sources_protection.py).
"""

import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils.html import escape

from accounts import sudo
from invoices.admin import PORTAL_IN_SOURCES
from invoices.models import InvoiceType, WebsiteInvoiceSource
from tests.factories import make_supplier
from tests.runner import confirm_password, forget_the_confirmation, test_user

OLD_PASSWORD = "Ancien-Mot-De-Passe-2026"
NEW_PASSWORD = "Nouveau-Mot-De-Passe-2026"
BOX_URL = "https://espace-client.box-exemple.invalid/connexion"


def as_superuser(user):
    get_user_model().objects.filter(pk=user.pk).update(is_staff=True, is_superuser=True)
    user.refresh_from_db()
    return user


class AdminCase(TestCase):
    """The test espace's owner made a superuser and logged in by the test
    itself: no confirmation in his session (tests.runner: only the client's
    implicit first login confirms)."""

    def setUp(self):
        super().setUp()
        self.owner = as_superuser(test_user())
        self.client.force_login(self.owner)
        self.supplier = make_supplier(code="T-ADMIN", name="Grossiste Admin")
        # No mailbox settings: the change page posts an empty inline.
        self.mailbox = InvoiceType.objects.create(supplier=self.supplier, name="Grossiste Admin - Factures")
        self.waiter = get_user_model().objects.create_user(
            username="serveur@example.invalid", email="serveur@example.invalid", password=OLD_PASSWORD
        )

    def confirm(self, seconds=600):
        confirm_password(self.client, self.owner, seconds)

    def type_change(self, invoice_type=None):
        return reverse("admin:invoices_invoicetype_change", args=[(invoice_type or self.mailbox).pk])

    def password_change(self):
        return reverse("admin:auth_user_password_change", args=[self.waiter.pk])

    def post_type(self, invoice_type=None, **fields):
        invoice_type = invoice_type or self.mailbox
        data = {
            "name": invoice_type.name,
            "supplier": invoice_type.supplier_id,
            "parser_key": invoice_type.parser_key,
            "source_kind": invoice_type.source_kind,
            "is_active": "on",
            "email_source-TOTAL_FORMS": "0",
            "email_source-INITIAL_FORMS": "0",
            "email_source-MIN_NUM_FORMS": "0",
            "email_source-MAX_NUM_FORMS": "1",
            "_save": "Enregistrer",
        }
        data.update(fields)
        data = {name: value for name, value in data.items() if value is not None}
        return self.client.post(self.type_change(invoice_type), data)

    def post_password(self):
        return self.client.post(
            self.password_change(),
            {"usable_password": "true", "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        )

    def assertAsked(self, response, path):
        self.assertRedirects(response, sudo.confirm_url(path), fetch_redirect_response=False)
        self.assertTrue(response["Location"].startswith("/identifiants/confirmer/?next="), response["Location"])


class WithoutTheConfirmationTests(AdminCase):
    def test_every_admin_page_asks_for_the_password_first(self):
        for path in (
            reverse("admin:index"),
            reverse("admin:invoices_invoicetype_changelist"),
            self.type_change(),
            self.password_change(),
            reverse("admin:password_change"),
            reverse("admin:accounts_membership_changelist"),
        ):
            with self.subTest(path=path):
                self.assertAsked(self.client.get(path), path)

    def test_a_post_changes_nothing_and_comes_back_to_its_page_once_confirmed(self):
        response = self.post_type(name="Renommée sans confirmation", is_active=None)
        self.assertAsked(response, self.type_change())
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.name, "Grossiste Admin - Factures")
        self.assertTrue(self.mailbox.is_active)

        response = self.post_password()
        self.assertAsked(response, self.password_change())
        self.waiter.refresh_from_db()
        self.assertTrue(self.waiter.check_password(OLD_PASSWORD))
        self.assertFalse(self.waiter.check_password(NEW_PASSWORD))

    def test_a_confirmation_that_ended_is_asked_again(self):
        self.confirm(seconds=-1)
        self.assertAsked(self.client.get(reverse("admin:index")), reverse("admin:index"))

    def test_another_logins_confirmation_does_not_count(self):
        session = self.client.session
        session[sudo.SESSION_KEY] = {"user": self.waiter.pk, "until": time.time() + 600}
        session.save()
        self.assertAsked(self.client.get(reverse("admin:index")), reverse("admin:index"))

    def test_the_logout_is_not_held_back(self):
        response = self.client.post(reverse("admin:logout"))
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_login_without_the_permission_still_goes_to_the_admin_login(self):
        """Staff alone is not a superuser: the admin's own refusal, as before -
        nothing to confirm for a page that will not open anyway."""
        get_user_model().objects.filter(pk=self.owner.pk).update(is_superuser=False)
        response = self.client.get(reverse("admin:index"))
        self.assertTrue(response["Location"].startswith(reverse("admin:login")), response["Location"])

    def test_the_admin_login_page_still_opens_and_logs_in(self):
        """Django's own login view is not one admin_view wraps: it opens
        without a login, and a login made there - the password just checked -
        confirms it for its window (sudo.stamp): the index opens at once."""
        # The login limiter counts in the process's cache (test_login.py).
        cache.clear()
        self.addCleanup(cache.clear)
        self.client.logout()
        self.owner.set_password(OLD_PASSWORD)
        self.owner.save()
        self.assertEqual(self.client.get(reverse("admin:login")).status_code, 200)
        response = self.client.post(
            reverse("admin:login"), {"username": self.owner.username, "password": OLD_PASSWORD, "next": "/admin/"}
        )
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_a_logged_in_session_posting_to_the_admin_login_is_not_confirmed(self):
        """Django's admin login re-renders its form on a refused POST with
        request.user still the session's login: a wrong password, an empty
        form or one the limiter refused confirmed nothing, yet the session
        was stamped - « Identifiants », « Accès des employés » and the whole
        admin opened to a session left open or copied, no password asked."""
        from accounts import limiter

        cache.clear()
        self.addCleanup(cache.clear)
        protected = (reverse("admin:index"), reverse("accounts:credentials"), reverse("accounts:members"))
        refused = {
            "wrong password": {"username": self.owner.username, "password": "Pas-Le-Bon-2026"},
            "unknown address": {"username": "personne@example.invalid", "password": "Pas-Le-Bon-2026"},
            "empty form": {},
        }
        for case, data in refused.items():
            with self.subTest(case=case):
                self.assertEqual(self.client.post(reverse("admin:login"), data).status_code, 200)
                self.assertNotIn(sudo.SESSION_KEY, self.client.session)
                for path in protected:
                    self.assertAsked(self.client.get(path), path)
        with self.subTest(case="limiter refused"), mock.patch.object(limiter, "reserve", return_value=False):
            self.owner.set_password(OLD_PASSWORD)
            self.owner.save()
            self.client.force_login(self.owner)
            data = {"username": self.owner.username, "password": OLD_PASSWORD}
            self.assertEqual(self.client.post(reverse("admin:login"), data).status_code, 200)
            self.assertNotIn(sudo.SESSION_KEY, self.client.session)
            for path in protected:
                self.assertAsked(self.client.get(path), path)

    def test_an_owner_who_is_not_superuser_is_not_confirmed_by_the_admin_login(self):
        cache.clear()
        self.addCleanup(cache.clear)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_staff=False, is_superuser=False)
        data = {"username": self.owner.username, "password": "Pas-Le-Bon-2026"}
        self.assertEqual(self.client.post(reverse("admin:login"), data).status_code, 200)
        self.assertNotIn(sudo.SESSION_KEY, self.client.session)
        for path in (reverse("accounts:credentials"), reverse("accounts:members")):
            self.assertAsked(self.client.get(path), path)

    def test_a_right_password_posted_from_a_logged_in_session_confirms(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner.set_password(OLD_PASSWORD)
        self.owner.save()
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("admin:login"), {"username": self.owner.username, "password": OLD_PASSWORD, "next": "/admin/"}
        )
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/admin/").status_code, 200)


class WithTheConfirmationTests(AdminCase):
    def setUp(self):
        super().setUp()
        self.confirm()

    def test_the_pages_open(self):
        for path in (reverse("admin:index"), self.type_change(), self.password_change()):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_a_post_changes_what_it_says(self):
        response = self.post_type(name="Renommée confirmée", is_active=None)
        self.assertRedirects(response, reverse("admin:invoices_invoicetype_changelist"), fetch_redirect_response=False)
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.name, "Renommée confirmée")
        self.assertFalse(self.mailbox.is_active)

        response = self.post_password()
        self.assertEqual(response.status_code, 302)
        self.waiter.refresh_from_db()
        self.assertTrue(self.waiter.check_password(NEW_PASSWORD))

    def test_a_page_in_use_keeps_the_confirmation_alive(self):
        self.confirm(seconds=60)
        before = self.client.session[sudo.SESSION_KEY]["until"]
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        self.assertGreater(self.client.session[sudo.SESSION_KEY]["until"], before + 300)

    def test_forgotten_it_is_asked_again(self):
        forget_the_confirmation(self.client)
        self.assertAsked(self.client.get(reverse("admin:index")), reverse("admin:index"))

    def test_it_ends_with_its_window(self):
        later = time.time() + sudo.WINDOW_SECONDS + 1
        with mock.patch("accounts.sudo._now", return_value=later):
            self.assertAsked(self.client.get(reverse("admin:index")), reverse("admin:index"))


class PortalSourceInTheAdminTests(AdminCase):
    """A source fetching from a customer portal: its channel and « actif »
    are not the admin's to change."""

    def setUp(self):
        super().setUp()
        self.confirm()
        self.portal = InvoiceType.objects.create(
            supplier=self.supplier, name="Box Exemple - Factures", source_kind=InvoiceType.SourceKind.WEBSITE
        )
        WebsiteInvoiceSource.objects.create(
            invoice_type=self.portal, login_url=BOX_URL, username_env="BOX_LOGIN", password_env="BOX_PASSWORD"
        )
        InvoiceType.objects.filter(pk=self.portal.pk).update(is_active=False)
        self.portal.refresh_from_db()

    def test_its_channel_and_actif_are_read_only_and_the_page_says_where_they_change(self):
        page = self.client.get(self.type_change(self.portal))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="source_kind"')
        self.assertNotContains(page, 'name="is_active"')
        self.assertContains(page, escape(PORTAL_IN_SOURCES))
        self.assertContains(page, reverse("invoices:invoice_type_update", args=[self.portal.pk]))
        # The name stays the admin's.
        self.assertContains(page, 'name="name"')

    def test_a_post_cannot_switch_it_on_nor_change_its_channel(self):
        response = self.post_type(self.portal, source_kind=InvoiceType.SourceKind.EMAIL, is_active="on")
        self.assertEqual(response.status_code, 302)
        self.portal.refresh_from_db()
        self.assertEqual(self.portal.source_kind, InvoiceType.SourceKind.WEBSITE)
        self.assertFalse(self.portal.is_active)
        self.assertTrue(WebsiteInvoiceSource.objects.filter(invoice_type=self.portal).exists())

    def test_a_mailbox_source_keeps_both_fields(self):
        page = self.client.get(self.type_change())
        self.assertContains(page, 'name="source_kind"')
        self.assertContains(page, 'name="is_active"')
        self.assertNotContains(page, escape(PORTAL_IN_SOURCES))
        add = self.client.get(reverse("admin:invoices_invoicetype_add"))
        self.assertContains(add, 'name="source_kind"')
        self.assertContains(add, 'name="is_active"')
