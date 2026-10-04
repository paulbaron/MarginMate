"""« Accès des employés » (accounts/members.py): the owner invites an
employee, chooses the pages he opens, renews his link or takes his access
away - and « Votre accès » (/invitation/<token>/), where the employee chooses
his password.

What is pinned here is what the feature's promises rest on: the page is the
owner's alone and asks his password again; the link is shown once and kept
nowhere but as its hash; an address with a login elsewhere is refused and
counted, so the form cannot be used to ask which addresses have an account;
a link chooses one password, once, for a login this espace alone holds; and
an employee removed is logged out everywhere. Every name and address is
invented."""

import re
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connections
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from accounts import invitations, limiter, members, signup, sudo
from accounts.access import AREAS, DEFAULT_AREAS
from accounts.forms import EMAIL_INVALID, PASSWORDS_DIFFER, REQUIRED
from accounts.models import MemberInvitation, Membership, Tenant, hash_secret
from accounts.router import ACCOUNTS_ALIAS
from accounts.tests.support import TenancyTestCase
from accounts.users import free_the_address
from tests.runner import (
    TEST_TENANT_NAME,
    TEST_TENANT_PK,
    confirm_password,
    employee_of_the_test_tenant,
    forget_the_confirmation,
    test_user,
)
from tests.test_navigation import active_labels
from tests.test_views_smoke import assertNoUnrenderedTemplateSyntax

MEMBERS = reverse("accounts:members")
INVOICE_ADD = reverse("invoices:invoice_add")
PASSWORD = "Comptoir-Ardoise-58"
OTHER_PASSWORD = "Tabouret-Zinc-47"
#: The link the owner's page shows once, in its read-only field.
LINK_RE = re.compile(r'id="member-link" type="text" readonly value="([^"]+)"')
#: A token of the right shape that no invitation has.
UNKNOWN_TOKEN = "Zq3vK8mPw2xR7tLc9bN4hJ6yF1dS5gA0eU-_oiQWERT"
DEAD = "Lien expiré ou déjà utilisé"
OTHER_BROWSER = "connecté avec un autre compte"


def invitation_url(token):
    return reverse("accounts:member_invitation", args=[token])


def link_of(response):
    """(link, token) of the link a response of the owner's page shows."""
    found = LINK_RE.search(response.content.decode())
    if found is None:
        raise AssertionError("No invitation link on the page.")
    link = found.group(1)
    token = re.fullmatch(r"https?://[^/]+/invitation/([^/]+)/", link)
    if token is None:
        raise AssertionError(f"Not an invitation link: {link}")
    return link, token.group(1)


def the_test_tenant():
    return Tenant.objects.get(pk=TEST_TENANT_PK)


def neighbour_tenant():
    """Another espace's row in the accounts database (no files: nothing here
    opens it)."""
    return Tenant.objects.create(name="Bar Voisin", dir_name="voisin000001")


def warnings_of(response) -> list[str]:
    return [str(message) for message in response.context["messages"] if message.level_tag == "warning"]


class MembersTestCase(TestCase):
    """`self.client` is the test espace's owner, his password confirmed."""

    def setUp(self):
        super().setUp()
        # The taken addresses' daily count lives in the cache.
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner = test_user()

    def invite(self, name="Léa Martin", email="lea@example.invalid", pages=("invoices_add",), client=None):
        data = {"action": "invite", "name": name, "email": email, "pages": list(pages)}
        return (client or self.client).post(MEMBERS, data)

    def act(self, action, member, client=None, **data):
        """A POST about one employee: `member` a membership or what is posted
        as its pk."""
        posted = member.pk if isinstance(member, Membership) else member
        return (client or self.client).post(MEMBERS, {"action": action, "member": posted, **data})

    @staticmethod
    def logged_in_as(user) -> Client:
        client = Client()
        client.force_login(user)
        return client

    def taken_count(self):
        return cache.get(members._taken_key(SimpleNamespace(pk=TEST_TENANT_PK)))


class TheOwnersPageTests(MembersTestCase):
    def test_the_owner_sees_his_employees_and_the_page_lights_personnel(self):
        employee_of_the_test_tenant("lea@example.invalid", ["returnables", "invoices_add"], name="Léa Martin")
        stranger = get_user_model().objects.create_user(
            username="voisin@example.invalid", email="voisin@example.invalid"
        )
        Membership.objects.create(user=stranger, tenant=neighbour_tenant(), role=Membership.Role.MEMBER)

        response = self.client.get(MEMBERS)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<h1>Accès des employés</h1>", html=True)
        self.assertContains(response, "Léa Martin")
        self.assertContains(response, "lea@example.invalid")
        # What she opens, in the page's order.
        self.assertContains(response, "Ajouter des factures, Consignes")
        # Another espace's employee is not his.
        self.assertNotContains(response, "voisin@example.invalid")
        # Under « Personnel », where the owner's button to it is.
        self.assertEqual(active_labels(response), ["Personnel"])
        assertNoUnrenderedTemplateSyntax(self, response, MEMBERS)

    def test_with_no_employee_the_page_says_so(self):
        response = self.client.get(MEMBERS)
        # Words of the template itself, its apostrophes as typed.
        self.assertContains(response, "Aucun employé n'a encore d'accès")

    def test_without_his_password_confirmed_the_page_asks_for_it_first(self):
        """A login made here is a door into the bar: a PC left logged in
        for two weeks must not open one. Asked on GET too - a confirmation
        asked at the POST would throw away what was typed."""
        self.assertEqual(self.client.get(MEMBERS).status_code, 200)
        forget_the_confirmation(self.client)
        self.assertRedirects(self.client.get(MEMBERS), sudo.confirm_url(MEMBERS), fetch_redirect_response=False)

        response = self.invite()
        self.assertRedirects(response, sudo.confirm_url(MEMBERS), fetch_redirect_response=False)
        self.assertFalse(get_user_model().objects.filter(username="lea@example.invalid").exists())
        self.assertFalse(MemberInvitation.objects.exists())
        # And says, once confirmed, that nothing was saved.
        confirm_password(self.client)
        self.assertContains(self.client.get(MEMBERS), escape(sudo.EXPIRED_POST))

    def test_a_login_never_confirmed_is_asked_too(self):
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get(MEMBERS), sudo.confirm_url(MEMBERS), fetch_redirect_response=False)

    def test_an_employee_meets_the_gate_whatever_is_ticked_for_him(self):
        """« Accès des employés » is in no area: an employee given every box
        still cannot give himself, or a colleague, more."""
        lea = employee_of_the_test_tenant(
            "lea@example.invalid", [area.key for area in AREAS], name="Léa Martin", password=PASSWORD
        )
        phone = self.logged_in_as(lea)
        confirm_password(phone, lea)

        response = phone.get(MEMBERS)
        self.assertContains(response, "Page non accessible", status_code=403)

        response = self.invite(email="complice@example.invalid", client=phone)
        self.assertContains(response, escape("Rien n'a été enregistré."), status_code=403)
        self.assertFalse(get_user_model().objects.filter(username="complice@example.invalid").exists())

        response = self.act("pages", lea.memberships.get(), client=phone, pages=["invoices_add"])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(lea.memberships.get().pages, [area.key for area in AREAS])


class InviteTests(MembersTestCase):
    def test_an_invitation_makes_his_login_his_membership_and_a_link_shown_once(self):
        before = timezone.now()
        response = self.invite(
            name="  Léa   Martin ", email=" Lea@Example.INVALID ", pages=["returnables", "invoices_add"]
        )
        # The answer itself, not a redirect: the link is in it and nowhere else.
        self.assertEqual(response.status_code, 200)

        user = get_user_model().objects.get(username="lea@example.invalid")
        self.assertEqual(user.email, "lea@example.invalid")
        self.assertEqual(user.first_name, "Léa Martin")
        # Nobody can log in with it before he chooses one - the owner never
        # knows his password.
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.tenant_id, TEST_TENANT_PK)
        self.assertEqual(membership.role, Membership.Role.MEMBER)
        self.assertEqual(membership.pages, ["invoices_add", "returnables"])

        link, token = link_of(response)
        self.assertEqual(link, "http://testserver" + invitation_url(token))
        invitation = MemberInvitation.objects.get(membership=membership)
        self.assertEqual(invitation.token_hash, hash_secret(token))
        self.assertEqual(members.INVITATION_DAYS, 7)
        self.assertEqual(invitation.expires_at - invitation.created_at, timedelta(days=7))
        self.assertGreaterEqual(invitation.created_at, before)
        self.assertContains(response, "Lien d'invitation de Léa Martin")
        self.assertContains(response, "Accès créé pour Léa Martin")
        # Kept by no browser either.
        self.assertIn("no-store", response["Cache-Control"])

    def test_the_link_is_kept_nowhere_but_as_its_hash(self):
        response = self.invite()
        _link, token = link_of(response)

        session = self.client.session
        for key, value in session.items():
            self.assertNotIn(token, repr(value), key)
        self.assertNotIn(token, str(self.client.cookies))
        for alias in ("default", ACCOUNTS_ALIAS):
            connection = connections[alias]
            with connection.cursor() as cursor:
                for table in connection.introspection.table_names(cursor):
                    cursor.execute(f'SELECT * FROM "{table}"')
                    for row in cursor.fetchall():
                        self.assertNotIn(token, repr(row), f"{alias}.{table}")

        # Drawn again, the page does not have it any more.
        again = self.client.get(MEMBERS)
        self.assertNotContains(again, token)
        self.assertContains(again, "Invitation en attente")

    def test_he_is_listed_as_waiting_for_his_password(self):
        self.invite(name="Léa Martin")
        page = self.client.get(MEMBERS)
        self.assertContains(page, "Léa Martin")
        self.assertContains(page, "Invitation en attente")
        self.assertContains(page, "Nouveau lien d'invitation")

    def test_a_new_employee_s_boxes_are_the_defaults_the_lists_included(self):
        """The invitation's boxes are every area's, in the page's order; the
        ones ticked DEFAULT_AREAS - « Liste de courses » among them since
        04/10/2026 -, each with what it shows."""
        page = self.client.get(MEMBERS)
        boxes = re.findall(
            r'<input type="checkbox" name="pages" value="([^"]+)" id="pages-nouveau-[^"]+"([^>]*)>',
            page.content.decode(),
        )
        self.assertEqual([key for key, _rest in boxes], [area.key for area in AREAS])
        self.assertEqual([key for key, rest in boxes if "checked" in rest], list(DEFAULT_AREAS))
        self.assertIn("shopping", DEFAULT_AREAS)
        (lists,) = [area for area in AREAS if area.key == "shopping"]
        self.assertContains(page, f'<span class="area-choice-label">{lists.label}</span>', html=True)
        self.assertContains(page, escape(lists.help))

    def test_an_invitation_with_the_defaults_stores_them_in_the_page_s_order(self):
        self.invite(pages=list(reversed(DEFAULT_AREAS)))
        membership = Membership.objects.get(user__username="lea@example.invalid")
        self.assertEqual(membership.pages, ["invoices_add", "stock_takes", "returnables", "shopping"])
        self.assertContains(
            self.client.get(MEMBERS), "Ajouter des factures, Faire un inventaire, Consignes, Liste de courses"
        )


class LinkAddressTests(MembersTestCase):
    """The link is sent to a phone: it starts with the site's public address
    (MARGINMATE_SITE_URL) when there is one, and the page says when it
    starts with an address no phone outside this PC's network can open."""

    def test_the_link_starts_with_the_site_s_public_address(self):
        with self.settings(SITE_URL="https://gestion.example.invalid/"):
            response = self.invite()
        link, token = link_of(response)
        self.assertEqual(link, f"https://gestion.example.invalid/invitation/{token}/")
        self.assertNotContains(response, escape(members.LOCAL_LINK.format(name="Léa Martin")))

    def test_a_link_only_this_pc_can_open_is_said(self):
        for n, host in enumerate(("127.0.0.1", "localhost")):
            with self.subTest(host=host):
                response = self.client.post(
                    MEMBERS,
                    {"action": "invite", "name": "Léa Martin", "email": f"lea{n}@example.invalid"},
                    HTTP_HOST=host,
                )
                link, _token = link_of(response)
                self.assertTrue(link.startswith(f"http://{host}/invitation/"), link)
                self.assertContains(response, escape(members.LOCAL_LINK.format(name="Léa Martin")))

    def test_which_addresses_are_local(self):
        local = (
            "http://localhost:8000/x",
            "http://app.localhost/x",
            "http://127.0.0.1/x",
            "http://[::1]:8000/x",
            "http://192.168.1.20:8000/x",
            "http://10.0.0.5/x",
            "http://169.254.1.1/x",
            "/invitation/x/",
        )
        for link in local:
            with self.subTest(link=link):
                self.assertIs(members._is_local(link), True)
        # (An IP of the documentation ranges, 203.0.113.0/24, counts as
        # private to Python: a public one would be somebody's.)
        for link in ("https://gestion.example.invalid/x", "https://localhost.example.invalid/x"):
            with self.subTest(link=link):
                self.assertIs(members._is_local(link), False)


class RefusedAddressTests(MembersTestCase):
    def setUp(self):
        super().setUp()
        self.users_before = get_user_model().objects.count()

    def elsewhere(self, email):
        """A login that exists somewhere - another espace, or none."""
        return get_user_model().objects.create_user(username=email, email=email, password=PASSWORD)

    def assertNothingMade(self):
        self.assertEqual(get_user_model().objects.count(), self.users_before)
        self.assertFalse(Membership.objects.filter(role=Membership.Role.MEMBER).exists())
        self.assertFalse(MemberInvitation.objects.exists())

    def test_an_address_with_a_login_elsewhere_is_refused_counted_and_logged(self):
        self.elsewhere("ailleurs@example.invalid")
        self.users_before += 1
        with self.assertLogs("accounts.members", "WARNING") as logged:
            response = self.invite(email="Ailleurs@Example.invalid")
        self.assertContains(response, escape(members.EMAIL_TAKEN), status_code=400)
        self.assertNothingMade()
        self.assertEqual(self.taken_count(), 1)
        self.assertEqual(len(logged.output), 1)
        self.assertIn("(1 aujourd'hui)", logged.output[0])

    def test_past_the_day_s_count_no_address_is_checked_at_all(self):
        """Refused, an address tells the owner it has an account somewhere -
        which the public signup never says. Past the count the form answers
        the same for every address, free or not, until the next day."""
        for n in range(members.TAKEN_ADDRESSES_PER_DAY):
            self.elsewhere(f"pris-{n}@example.invalid")
            self.users_before += 1
            with self.assertLogs("accounts.members", "WARNING"):
                self.assertEqual(self.invite(email=f"pris-{n}@example.invalid").status_code, 400)

        for email in ("libre@example.invalid", "pris-0@example.invalid"):
            with self.subTest(email=email):
                with self.assertNoLogs("accounts.members", "WARNING"):
                    response = self.invite(email=email)
                self.assertContains(response, escape(members.TOO_MANY_TAKEN), status_code=429)
                self.assertNotContains(response, escape(members.EMAIL_TAKEN), status_code=429)
                self.assertNothingMade()

        # The count is the day's: tomorrow the form checks again.
        tomorrow = timezone.localdate() + timedelta(days=1)
        with mock.patch.object(members.timezone, "localdate", return_value=tomorrow):
            self.assertEqual(self.invite(email="libre@example.invalid").status_code, 200)

    def test_the_count_is_the_espace_s_own(self):
        cache.set(members._taken_key(SimpleNamespace(pk=TEST_TENANT_PK + 1)), 50)
        self.assertEqual(self.invite(email="libre@example.invalid").status_code, 200)

    def test_one_of_this_espace_s_own_logins_is_said_and_counts_for_nothing(self):
        employee_of_the_test_tenant("lea@example.invalid", name="Léa Martin")
        self.users_before += 1
        for _ in range(members.TAKEN_ADDRESSES_PER_DAY + 1):
            with self.assertNoLogs("accounts.members", "WARNING"):
                response = self.invite(email="LEA@example.invalid")
            self.assertContains(response, escape(members.ALREADY_HERE.format(name="Léa Martin")), status_code=400)
        # The owner's own address is one of them too.
        response = self.invite(email=self.owner.email)
        self.assertContains(response, escape(members.ALREADY_HERE.format(name=self.owner.email)), status_code=400)
        self.assertIsNone(self.taken_count())
        self.assertEqual(MemberInvitation.objects.count(), 0)
        # Nothing was counted: a free address still goes through.
        self.assertEqual(self.invite(email="noe@example.invalid").status_code, 200)

    def test_his_own_expired_invitation_is_renewed_not_made_again(self):
        """An employee of this espace whose link expired unused is his
        card's « Nouveau lien d'invitation »: inviting the address again is
        said as one of the espace's own."""
        membership, _token = members.invite(the_test_tenant(), name="Léa Martin", email="lea@example.invalid", pages=[])
        MemberInvitation.objects.filter(membership=membership).update(expires_at=timezone.now() - timedelta(minutes=1))
        response = self.invite(email="lea@example.invalid")
        self.assertContains(response, escape(members.ALREADY_HERE.format(name="Léa Martin")), status_code=400)
        self.assertTrue(Membership.objects.filter(pk=membership.pk).exists())
        self.assertContains(self.client.get(MEMBERS), "Invitation expirée")

    def test_an_address_held_by_another_espace_s_invitation_that_expired_unused_is_freed(self):
        """Its login was never used and never can be (no password): it held
        the address for nothing."""
        old, _token = members.invite(neighbour_tenant(), name="Noé", email="noe@example.invalid", pages=[])
        MemberInvitation.objects.filter(membership=old).update(expires_at=timezone.now() - timedelta(minutes=1))

        response = self.invite(name="Noé Petit", email="noe@example.invalid")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(pk=old.user_id).exists())
        self.assertFalse(Membership.objects.filter(pk=old.pk).exists())
        user = get_user_model().objects.get(username="noe@example.invalid")
        self.assertEqual(user.first_name, "Noé Petit")
        self.assertEqual(Membership.objects.get(user=user).tenant_id, TEST_TENANT_PK)
        self.assertIsNone(self.taken_count())

    def test_another_espace_s_invitation_still_running_holds_the_address(self):
        old, _token = members.invite(neighbour_tenant(), name="Noé", email="noe@example.invalid", pages=[])
        self.users_before += 1
        with self.assertLogs("accounts.members", "WARNING"):
            response = self.invite(email="noe@example.invalid")
        self.assertContains(response, escape(members.EMAIL_TAKEN), status_code=400)
        self.assertTrue(Membership.objects.filter(pk=old.pk).exists())

    def test_the_form_s_own_refusals(self):
        cases = {
            EMAIL_INVALID: {"email": "pas-une-adresse"},
            REQUIRED: {"name": "   "},
        }
        for message, changes in cases.items():
            with self.subTest(message=message):
                response = self.invite(**changes)
                self.assertContains(response, escape(message), status_code=400)
                self.assertNothingMade()
        self.assertIsNone(self.taken_count())

    def test_an_unknown_page_is_refused(self):
        response = self.invite(pages=["invoices_add", "donnees"])
        self.assertEqual(response.status_code, 400)
        self.assertNothingMade()


class PagesTests(MembersTestCase):
    def setUp(self):
        super().setUp()
        self.lea = employee_of_the_test_tenant("lea@example.invalid", ["invoices_add"], name="Léa Martin")
        self.membership = self.lea.memberships.get()

    def test_the_boxes_ticked_are_saved_in_the_page_s_order(self):
        response = self.act("pages", self.membership, pages=["staff", "returnables", "invoices_add"])
        self.assertRedirects(response, f"{MEMBERS}#employe-{self.membership.pk}", fetch_redirect_response=False)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.pages, ["invoices_add", "returnables", "staff"])
        self.assertContains(self.client.get(MEMBERS), "Accès de Léa Martin enregistrés : 3 pages ouvertes.")

    def test_no_box_ticked_opens_no_page(self):
        """An unticked box sends nothing: a POST with none is « every box
        unticked », and the employee lands on « Aucune page ouverte »."""
        self.act("pages", self.membership)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.pages, [])
        self.assertContains(self.client.get(MEMBERS), "Accès de Léa Martin enregistrés : aucune page ouverte.")

        phone = self.logged_in_as(self.lea)
        self.assertRedirects(phone.get("/"), reverse("accounts:no_access"), fetch_redirect_response=False)
        self.assertContains(phone.get(reverse("accounts:no_access")), "Aucune page ouverte")
        self.assertEqual(phone.get(INVOICE_ADD).status_code, 403)

    def test_his_next_request_obeys_the_new_boxes(self):
        phone = self.logged_in_as(self.lea)
        self.assertEqual(phone.get(INVOICE_ADD).status_code, 200)
        self.assertEqual(phone.get(reverse("returnables:home")).status_code, 403)
        self.act("pages", self.membership, pages=["returnables"])
        self.assertEqual(phone.get(INVOICE_ADD).status_code, 403)
        self.assertEqual(phone.get(reverse("returnables:home")).status_code, 200)

    def test_an_unknown_page_is_refused_and_nothing_saved(self):
        response = self.act("pages", self.membership, pages=["returnables", "donnees"])
        self.assertEqual(response.status_code, 400)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.pages, ["invoices_add"])

    def test_a_page_refused_is_said_in_french(self):
        """What the page displays is French (CLAUDE.md, « English code,
        French screens »): a box the page does not offer, posted by a page
        drawn before an area went or by hand, is refused under the boxes in
        French - on both forms that carry them."""
        for name, response in (
            ("pages", self.act("pages", self.membership, pages=["donnees"])),
            ("invite", self.invite(email="noe@example.invalid", pages=["donnees"])),
        ):
            with self.subTest(form=name):
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, 'class="field-error"', status_code=400)
                self.assertNotContains(response, "Select a valid choice", status_code=400)

    def test_only_an_employee_of_this_espace_is_ever_changed(self):
        """`member` comes from the page: a pk of the owner's own membership,
        of another espace's employee, or no pk at all changes nothing, and
        the page says the employee is gone."""
        owner_membership = Membership.objects.get(user=self.owner, tenant_id=TEST_TENANT_PK)
        owner_pages = owner_membership.pages
        stranger = get_user_model().objects.create_user(
            username="voisin@example.invalid", email="voisin@example.invalid", password=PASSWORD
        )
        elsewhere = Membership.objects.create(
            user=stranger, tenant=neighbour_tenant(), role=Membership.Role.MEMBER, pages=["staff"]
        )
        for action in ("pages", "renew", "remove"):
            for member in (owner_membership.pk, elsewhere.pk, "abc", "", "²", "-1", "1e3", "9" * 30):
                with self.subTest(action=action, member=member):
                    response = self.act(action, member, pages=["bank"])
                    self.assertRedirects(response, MEMBERS, fetch_redirect_response=False)
                    self.assertEqual(warnings_of(self.client.get(MEMBERS)), [members.NO_SUCH_EMPLOYEE])
        owner_membership.refresh_from_db()
        self.assertEqual((owner_membership.role, owner_membership.pages), (Membership.Role.OWNER, owner_pages))
        elsewhere.refresh_from_db()
        self.assertEqual(elsewhere.pages, ["staff"])
        self.assertTrue(get_user_model().objects.filter(pk=stranger.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())
        self.assertFalse(MemberInvitation.objects.exists())

    def test_an_unknown_action_changes_nothing(self):
        response = self.act("promote", self.membership, pages=["bank"])
        self.assertRedirects(response, MEMBERS, fetch_redirect_response=False)
        self.membership.refresh_from_db()
        self.assertEqual((self.membership.role, self.membership.pages), (Membership.Role.MEMBER, ["invoices_add"]))


class RenewTests(MembersTestCase):
    def test_a_waiting_invitation_gets_a_new_link_and_the_old_one_dies(self):
        _link, old_token = link_of(self.invite())
        membership = Membership.objects.get(user__username="lea@example.invalid")
        # Even once expired: « Nouveau lien » is the way back in.
        MemberInvitation.objects.filter(membership=membership).update(expires_at=timezone.now() - timedelta(days=1))

        response = self.act("renew", membership)
        self.assertEqual(response.status_code, 200)
        _link, new_token = link_of(response)
        self.assertNotEqual(new_token, old_token)
        self.assertContains(
            response, escape("Nouveau lien d'invitation pour Léa Martin : l'ancien ne fonctionne plus.")
        )
        invitation = MemberInvitation.objects.get(membership=membership)
        self.assertEqual(invitation.token_hash, hash_secret(new_token))
        self.assertTrue(invitation.is_usable())

        anonymous = Client()
        self.assertContains(anonymous.get(invitation_url(old_token)), DEAD, status_code=404)
        self.assertEqual(anonymous.get(invitation_url(new_token)).status_code, 200)
        membership.user.refresh_from_db()
        self.assertFalse(membership.user.has_usable_password())
        self.assertContains(self.client.get(MEMBERS), "Invitation en attente")

    def test_an_active_employee_gets_a_link_for_a_new_password_and_keeps_his_own_meanwhile(self):
        lea = employee_of_the_test_tenant("lea@example.invalid", ["invoices_add"], name="Léa Martin", password=PASSWORD)
        page = self.client.get(MEMBERS)
        self.assertContains(page, "Actif")
        self.assertContains(page, "Nouveau mot de passe…")

        response = self.act("renew", lea.memberships.get())
        self.assertEqual(response.status_code, 200)
        _link, token = link_of(response)
        self.assertContains(response, "Lien pour le nouveau mot de passe de Léa Martin")
        lea.refresh_from_db()
        self.assertTrue(lea.check_password(PASSWORD))
        self.assertTrue(Client().login(username="lea@example.invalid", password=PASSWORD))

        anonymous = Client()
        page = anonymous.get(invitation_url(token))
        self.assertContains(page, "nouveau mot de passe")
        self.assertNotContains(page, "Créer mon compte")
        response = anonymous.post(invitation_url(token), {"password1": OTHER_PASSWORD, "password2": OTHER_PASSWORD})
        self.assertRedirects(response, INVOICE_ADD, fetch_redirect_response=False)
        lea.refresh_from_db()
        self.assertFalse(lea.check_password(PASSWORD))
        self.assertTrue(lea.check_password(OTHER_PASSWORD))

    def test_a_login_this_espace_does_not_hold_alone_is_never_given_a_link(self):
        """The owner of one bar must not choose the password of somebody's
        login in another, nor a staff's or a superuser's."""
        neighbour = neighbour_tenant()
        cases = {
            "staff": {"is_staff": True},
            "superuser": {"is_staff": True, "is_superuser": True},
            "ailleurs": {},
        }
        for name, flags in cases.items():
            with self.subTest(name=name):
                user = employee_of_the_test_tenant(f"{name}@example.invalid", name=name.title(), password=PASSWORD)
                get_user_model().objects.filter(pk=user.pk).update(**flags)
                if name == "ailleurs":
                    Membership.objects.create(user=user, tenant=neighbour, role=Membership.Role.MEMBER)
                membership = Membership.objects.get(user=user, tenant_id=TEST_TENANT_PK)

                response = self.act("renew", membership)
                self.assertRedirects(response, f"{MEMBERS}#employe-{membership.pk}", fetch_redirect_response=False)
                page = self.client.get(MEMBERS)
                self.assertContains(page, escape(members.NO_RESET))
                self.assertFalse(MemberInvitation.objects.filter(membership=membership).exists())
                user.refresh_from_db()
                self.assertTrue(user.check_password(PASSWORD))

    def test_his_card_offers_no_link_then(self):
        user = employee_of_the_test_tenant("ailleurs@example.invalid", name="Noé", password=PASSWORD)
        Membership.objects.create(user=user, tenant=neighbour_tenant(), role=Membership.Role.MEMBER)
        employee_of_the_test_tenant("lea@example.invalid", name="Léa Martin", password=PASSWORD)
        page = self.client.get(MEMBERS)
        # Léa's card offers one; Noé's none.
        self.assertContains(page, '<input type="hidden" name="action" value="renew">', count=1, html=True)
        self.assertContains(page, '<input type="hidden" name="action" value="remove">', count=2, html=True)

    def test_a_link_revoked_in_the_admin_is_made_again_here(self):
        """Deleting an employee's invitation in the admin revokes its link
        (accounts/admin.py); his card's « Nouveau lien » makes another."""
        _link, old_token = link_of(self.invite())
        membership = Membership.objects.get(user__username="lea@example.invalid")
        MemberInvitation.objects.filter(membership=membership).delete()
        self.assertContains(Client().get(invitation_url(old_token)), DEAD, status_code=404)

        self.assertContains(self.client.get(MEMBERS), "Nouveau lien d'invitation")
        _link, token = link_of(self.act("renew", membership))
        self.assertEqual(Client().get(invitation_url(token)).status_code, 200)

    def test_a_link_revoked_in_the_admin_is_not_said_running(self):
        """With no invitation left, the card must not say until when his
        link opens: it drew « le lien vaut jusqu'au . », a date that does not
        exist, under « Invitation en attente »."""
        self.invite()
        MemberInvitation.objects.all().delete()
        page = self.client.get(MEMBERS).content.decode()
        self.assertNotRegex(page, r"jusqu(?:'|&#x27;)au\s*\.")

    def test_a_waiting_login_that_joined_another_espace_meanwhile_opens_nothing(self):
        """`_may_reset` is asked again when the link is opened."""
        _link, token = link_of(self.invite())
        user = get_user_model().objects.get(username="lea@example.invalid")
        Membership.objects.create(user=user, tenant=neighbour_tenant(), role=Membership.Role.MEMBER)
        anonymous = Client()
        self.assertContains(anonymous.get(invitation_url(token)), DEAD, status_code=404)
        response = anonymous.post(invitation_url(token), {"password1": PASSWORD, "password2": PASSWORD})
        self.assertContains(response, DEAD, status_code=404)
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())


class RemoveTests(MembersTestCase):
    def test_removing_an_employee_deletes_his_login_and_every_session_of_his_goes_to_the_login(self):
        lea = employee_of_the_test_tenant("lea@example.invalid", ["invoices_add"], name="Léa Martin", password=PASSWORD)
        phone = self.logged_in_as(lea)
        self.assertEqual(phone.get(INVOICE_ADD).status_code, 200)

        response = self.act("remove", lea.memberships.get())
        self.assertRedirects(response, MEMBERS, fetch_redirect_response=False)
        self.assertFalse(get_user_model().objects.filter(pk=lea.pk).exists())
        self.assertFalse(Membership.objects.filter(user_id=lea.pk).exists())
        self.assertContains(self.client.get(MEMBERS), escape("Léa Martin n'a plus accès à l'espace."))

        response = phone.get(INVOICE_ADD)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])
        self.assertFalse(Client().login(username="lea@example.invalid", password=PASSWORD))

    def test_a_waiting_invitation_dies_with_him(self):
        _link, token = link_of(self.invite())
        self.act("remove", Membership.objects.get(user__username="lea@example.invalid"))
        self.assertFalse(MemberInvitation.objects.exists())
        self.assertContains(Client().get(invitation_url(token)), DEAD, status_code=404)

    def test_a_staff_a_superuser_or_a_login_of_another_espace_loses_this_membership_only(self):
        neighbour = neighbour_tenant()
        cases = {
            "staff": {"is_staff": True},
            "superuser": {"is_staff": True, "is_superuser": True},
            "ailleurs": {},
        }
        for name, flags in cases.items():
            with self.subTest(name=name):
                user = employee_of_the_test_tenant(f"{name}@example.invalid", name=name.title(), password=PASSWORD)
                get_user_model().objects.filter(pk=user.pk).update(**flags)
                other = None
                if name == "ailleurs":
                    other = Membership.objects.create(user=user, tenant=neighbour, role=Membership.Role.MEMBER)
                membership = Membership.objects.get(user=user, tenant_id=TEST_TENANT_PK)

                self.assertRedirects(self.act("remove", membership), MEMBERS, fetch_redirect_response=False)
                self.assertFalse(Membership.objects.filter(pk=membership.pk).exists())
                self.assertTrue(get_user_model().objects.filter(pk=user.pk).exists())
                if other is not None:
                    self.assertTrue(Membership.objects.filter(pk=other.pk).exists())


class InvitationPageTests(MembersTestCase):
    """« Votre accès », public: the link's token is the key."""

    def setUp(self):
        super().setUp()
        self.membership, self.token = members.invite(
            the_test_tenant(), name="Léa Martin", email="lea@example.invalid", pages=["invoices_add", "returnables"]
        )
        self.lea = self.membership.user
        self.url = invitation_url(self.token)
        self.anonymous = Client()

    def choose(self, client=None, first=PASSWORD, second=None):
        return (client or self.anonymous).post(self.url, {"password1": first, "password2": second or first})

    def assertUnchanged(self):
        self.lea.refresh_from_db()
        self.assertFalse(self.lea.has_usable_password())
        self.assertTrue(MemberInvitation.objects.filter(membership=self.membership).exists())

    def test_the_link_opens_a_page_kept_by_no_browser_and_no_search_engine(self):
        response = self.anonymous.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Votre accès à « {TEST_TENANT_NAME} »")
        self.assertContains(response, "Léa Martin")
        self.assertContains(response, 'value="lea@example.invalid"')
        self.assertContains(response, "Créer mon compte")
        self.assertNotContains(response, "nouveau mot de passe")
        self.assertNotContains(response, OTHER_BROWSER)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        for word in ("no-cache", "no-store"):
            self.assertIn(word, response["Cache-Control"])
        assertNoUnrenderedTemplateSyntax(self, response, self.url)

    def test_a_link_that_opens_nothing_says_so_and_offers_the_login(self):
        expired, expired_token = members.invite(the_test_tenant(), name="", email="expire@example.invalid", pages=[])
        MemberInvitation.objects.filter(membership=expired).update(expires_at=timezone.now() - timedelta(seconds=1))
        used, used_token = members.invite(the_test_tenant(), name="", email="utilise@example.invalid", pages=[])
        MemberInvitation.objects.filter(membership=used).delete()
        links = {
            "inconnu": UNKNOWN_TOKEN,
            "expiré": expired_token,
            "utilisé": used_token,
            "trop long": "a" * 101,
        }
        for name, token in links.items():
            with self.subTest(name=name):
                response = self.anonymous.get(invitation_url(token))
                self.assertContains(response, DEAD, status_code=404)
                self.assertContains(response, f'href="{reverse("accounts:login")}"', status_code=404)
                self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
                self.assertIn("no-store", response["Cache-Control"])
                response = self.anonymous.post(invitation_url(token), {"password1": PASSWORD, "password2": PASSWORD})
                self.assertContains(response, DEAD, status_code=404)
                self.assertNotIn("_auth_user_id", self.anonymous.session)
        for membership in (expired, used):
            membership.user.refresh_from_db()
            self.assertFalse(membership.user.has_usable_password())

    def test_a_closed_espace_opens_no_link(self):
        Tenant.objects.filter(pk=TEST_TENANT_PK).update(is_active=False)
        self.assertContains(self.anonymous.get(self.url), DEAD, status_code=404)
        self.assertContains(self.choose(), DEAD, status_code=404)
        self.assertUnchanged()

    def test_a_password_the_validators_refuse_changes_nothing(self):
        cases = {
            PASSWORDS_DIFFER: (PASSWORD, PASSWORD + "x"),
            "Ce mot de passe est trop court": ("Zinc-47", "Zinc-47"),
            "Ce mot de passe est trop courant": ("password", "password"),
            "Ce mot de passe est entièrement numérique": ("8412953760", "8412953760"),
            # His own attributes are handed to the validators.
            "trop semblable": ("lea.example.invalid", "lea.example.invalid"),
            REQUIRED: ("", ""),
        }
        for message, (first, second) in cases.items():
            with self.subTest(message=message):
                response = self.choose(first=first, second=second) if first else self.anonymous.post(self.url, {})
                self.assertContains(response, escape(message), status_code=400)
                self.assertUnchanged()
                self.assertNotIn("_auth_user_id", self.anonymous.session)

    def test_a_password_chosen_logs_him_in_on_his_first_page_and_the_link_dies(self):
        response = self.choose()
        self.assertRedirects(response, INVOICE_ADD, fetch_redirect_response=False)
        self.assertIn(limiter.DEVICE_COOKIE, response.cookies)
        self.lea.refresh_from_db()
        self.assertTrue(self.lea.check_password(PASSWORD))
        self.assertFalse(MemberInvitation.objects.filter(membership=self.membership).exists())
        self.assertEqual(int(self.anonymous.session["_auth_user_id"]), self.lea.pk)

        home = self.anonymous.get(INVOICE_ADD)
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, escape(f"Bienvenue dans l'espace « {TEST_TENANT_NAME} »"))
        self.assertContains(home, "Pour revenir : http://testserver,")

        # The same link a second time, from anywhere: dead, his password kept.
        for client in (Client(), self.anonymous):
            with self.subTest(logged_in=client is self.anonymous):
                response = self.choose(client=client, first=OTHER_PASSWORD)
                self.assertContains(response, DEAD, status_code=404)
        self.lea.refresh_from_db()
        self.assertTrue(self.lea.check_password(PASSWORD))
        # Logged in, the dead page offers his pages rather than the login.
        self.assertContains(self.anonymous.get(self.url), "Revenir à mes pages", status_code=404)

    def test_his_first_page_is_his_first_area_s(self):
        cases = (
            (["returnables", "staff"], reverse("returnables:home")),
            (["products", "invoices_add"], "/"),
            ([], reverse("accounts:no_access")),
        )
        for n, (pages, home) in enumerate(cases):
            with self.subTest(pages=pages):
                _membership, token = members.invite(
                    the_test_tenant(), name="", email=f"e{n}@example.invalid", pages=pages
                )
                response = Client().post(invitation_url(token), {"password1": PASSWORD, "password2": PASSWORD})
                self.assertRedirects(response, home, fetch_redirect_response=False)

    def test_a_browser_logged_in_as_somebody_else_is_told_then_logged_in_as_him(self):
        self.assertEqual(self.client.get(MEMBERS).status_code, 200)
        page = self.client.get(self.url)
        self.assertContains(page, OTHER_BROWSER)

        response = self.choose(client=self.client)
        self.assertRedirects(response, INVOICE_ADD, fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.lea.pk)
        # This browser is the employee's now: the owner's page is not his.
        self.assertEqual(self.client.get(MEMBERS).status_code, 403)

    def test_the_csrf_token_is_required(self):
        response = self.choose(client=Client(enforce_csrf_checks=True))
        self.assertEqual(response.status_code, 403)
        self.assertUnchanged()

    def test_a_link_used_meanwhile_by_another_request_is_said_dead(self):
        """Two tabs sending the same link: the second request read a usable
        invitation, then the first one used it up."""
        real_accept = members.accept

        def the_other_tab_first(invitation, password):
            MemberInvitation.objects.filter(pk=invitation.pk).delete()
            return real_accept(invitation, password)

        with mock.patch.object(members, "accept", side_effect=the_other_tab_first):
            response = self.choose()
        self.assertContains(response, DEAD, status_code=404)
        self.lea.refresh_from_db()
        self.assertFalse(self.lea.has_usable_password())
        self.assertNotIn("_auth_user_id", self.anonymous.session)


class AcceptTests(MembersTestCase):
    """`accept`: a DELETE filtered on the hash it was read with - only the
    first request passes."""

    def setUp(self):
        super().setUp()
        self.membership, self.token = members.invite(
            the_test_tenant(), name="Léa Martin", email="lea@example.invalid", pages=["invoices_add"]
        )
        self.lea = self.membership.user
        self.read = members.usable_invitation(self.token)
        self.assertIsNotNone(self.read)

    def assertNoPassword(self):
        self.lea.refresh_from_db()
        self.assertFalse(self.lea.has_usable_password())

    def test_an_invitation_used_meanwhile_sets_no_password(self):
        MemberInvitation.objects.filter(pk=self.read.pk).delete()
        self.assertIs(members.accept(self.read, PASSWORD), False)
        self.assertNoPassword()

    def test_a_link_replaced_meanwhile_sets_no_password_and_the_new_one_still_works(self):
        new_token = members.renew(self.membership)
        self.assertIs(members.accept(self.read, PASSWORD), False)
        self.assertNoPassword()
        self.assertIsNotNone(members.usable_invitation(new_token))

    def test_a_login_that_joined_another_espace_meanwhile_sets_no_password(self):
        Membership.objects.create(user=self.lea, tenant=neighbour_tenant(), role=Membership.Role.MEMBER)
        self.assertIs(members.accept(self.read, PASSWORD), False)
        self.assertNoPassword()

    def test_the_first_request_sets_it_and_uses_the_link_up(self):
        self.assertIs(members.accept(self.read, PASSWORD), True)
        self.lea.refresh_from_db()
        self.assertTrue(self.lea.check_password(PASSWORD))
        self.assertIsNone(members.usable_invitation(self.token))
        self.assertIs(members.accept(self.read, OTHER_PASSWORD), False)
        self.lea.refresh_from_db()
        self.assertTrue(self.lea.check_password(PASSWORD))


class FreeTheAddressTests(MembersTestCase):
    """`users.free_the_address`: a login an employee's invitation made and
    nobody ever used holds its address for nothing - and only such a login
    is deleted."""

    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def invited(self, email, *, days_ago=8):
        """A waiting employee of the test espace, invited `days_ago` days
        ago (a link lasts 7)."""
        membership, _token = members.invite(
            the_test_tenant(), name="", email=email, pages=[], now=self.now - timedelta(days=days_ago)
        )
        return membership.user

    def test_a_login_whose_invitation_expired_unused_is_freed(self):
        user = self.invited("fige@example.invalid")
        self.assertEqual(free_the_address("Fige@Example.INVALID", self.now), 1)
        self.assertFalse(get_user_model().objects.filter(pk=user.pk).exists())
        self.assertFalse(Membership.objects.filter(user_id=user.pk).exists())
        self.assertFalse(MemberInvitation.objects.exists())

    def test_freed_from_the_moment_its_link_stops_opening(self):
        user = self.invited("fige@example.invalid", days_ago=7)
        invitation = MemberInvitation.objects.get(membership__user=user)
        self.assertFalse(invitation.is_usable(invitation.expires_at))
        self.assertEqual(free_the_address("fige@example.invalid", invitation.expires_at - timedelta(seconds=1)), 0)
        self.assertEqual(free_the_address("fige@example.invalid", invitation.expires_at), 1)

    def test_every_other_login_is_kept(self):
        neighbour = neighbour_tenant()

        def running(user):
            MemberInvitation.objects.filter(membership__user=user).update(expires_at=self.now + timedelta(days=1))

        def active(user):
            # An active employee's link for a new password, expired.
            user.set_password(PASSWORD)
            user.save()

        def staff(user):
            get_user_model().objects.filter(pk=user.pk).update(is_staff=True)

        def superuser(user):
            get_user_model().objects.filter(pk=user.pk).update(is_staff=True, is_superuser=True)

        def elsewhere(user):
            Membership.objects.create(user=user, tenant=neighbour, role=Membership.Role.MEMBER)

        for change in (running, active, staff, superuser, elsewhere):
            with self.subTest(change=change.__name__):
                email = f"{change.__name__}@example.invalid"
                user = self.invited(email)
                change(user)
                self.assertEqual(free_the_address(email, self.now), 0)
                self.assertTrue(get_user_model().objects.filter(pk=user.pk).exists())
                self.assertTrue(Membership.objects.filter(user=user, tenant_id=TEST_TENANT_PK).exists())

    def test_a_login_with_no_invitation_is_never_touched(self):
        get_user_model().objects.create_user(username="client@example.invalid", email="client@example.invalid")
        for email in ("client@example.invalid", self.owner.email, "", "personne@example.invalid"):
            with self.subTest(email=email):
                self.assertEqual(free_the_address(email, self.now), 0)
        self.assertTrue(get_user_model().objects.filter(username="client@example.invalid").exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())


class SignupFreesTheAddressTests(TenancyTestCase):
    """The public signup (accounts/signup.py) frees such an address before
    it asks whether the address has a login: an employee's link that
    expired unused must not keep its address from creating a bar."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.bar = self.make_tenant("Bar Alpha")
        self.employee, _token = members.invite(
            self.bar, name="Noé", email="noe@example.invalid", pages=["invoices_add"]
        )
        self.invitation, self.code = invitations.create_invitation(note="Essai")

    def sign_up(self):
        return signup.sign_up(code=self.code, bar_name="Bar Neuf", email="Noe@Example.invalid", password=PASSWORD)

    def test_an_address_held_by_an_invitation_that_expired_unused_signs_up(self):
        MemberInvitation.objects.filter(membership=self.employee).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        user, tenant = self.sign_up()
        self.assertNotEqual(user.pk, self.employee.user_id)
        self.assertEqual(user.username, "noe@example.invalid")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(
            [(membership.tenant, membership.role) for membership in user.memberships.all()],
            [(tenant, Membership.Role.OWNER)],
        )
        self.assertFalse(Membership.objects.filter(pk=self.employee.pk).exists())
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.used_by, user)

    def test_an_invitation_still_running_holds_the_address(self):
        with self.assertRaises(signup.SignupRefused) as refused:
            self.sign_up()
        self.assertEqual((refused.exception.field, refused.exception.message), ("code", signup.CODE_REFUSED))
        self.assertTrue(Membership.objects.filter(pk=self.employee.pk).exists())
        self.assertEqual(list(Tenant.objects.all()), [self.bar])
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.used_at)
        self.assertEqual(self.invitation.refused_addresses, 1)


class AdminTests(MembersTestCase):
    """The admin (accounts/admin.py): a membership's pages as the boxes of
    « Accès des employés », and an employee's invitation read only."""

    def setUp(self):
        super().setUp()
        get_user_model().objects.filter(pk=self.owner.pk).update(is_staff=True, is_superuser=True)
        confirm_password(self.client)
        self.lea = employee_of_the_test_tenant("lea@example.invalid", ["returnables", "invoices_add"], name="Léa")
        self.membership = self.lea.memberships.get()
        self.change = reverse("admin:accounts_membership_change", args=[self.membership.pk])

    def save(self, pages=()):
        return self.client.post(
            self.change,
            {
                "user": self.lea.pk,
                "tenant": TEST_TENANT_PK,
                "role": Membership.Role.MEMBER,
                "pages": list(pages),
                "created_at_0": "2026-10-01",
                "created_at_1": "10:00:00",
                "_save": "Enregistrer",
            },
        )

    def test_a_membership_s_pages_are_boxes(self):
        page = self.client.get(self.change)
        self.assertEqual(page.status_code, 200)
        boxes = re.findall(r'<input type="checkbox" name="pages" value="([^"]+)"([^>]*)>', page.content.decode())
        self.assertEqual([key for key, _rest in boxes], [area.key for area in AREAS])
        self.assertEqual({key for key, rest in boxes if "checked" in rest}, {"invoices_add", "returnables"})
        self.assertNotContains(page, '<textarea name="pages"')

    def test_saved_with_no_box_ticked_it_opens_no_page(self):
        """Emptied, the JSON text was a null the column refuses."""
        response = self.save()
        self.assertRedirects(response, reverse("admin:accounts_membership_changelist"), fetch_redirect_response=False)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.pages, [])

    def test_the_boxes_are_saved_in_the_page_s_order(self):
        self.save(["staff", "invoices_add"])
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.pages, ["invoices_add", "staff"])

    def test_the_list_says_what_each_one_opens(self):
        page = self.client.get(reverse("admin:accounts_membership_changelist"))
        self.assertContains(page, "Ajouter des factures, Consignes")
        self.assertContains(page, "toutes")

    def test_an_employee_s_invitation_is_never_added_nor_changed_here(self):
        membership, token = members.invite(the_test_tenant(), name="Noé", email="noe@example.invalid", pages=[])
        invitation = membership.invitation
        self.assertEqual(self.client.get(reverse("admin:accounts_memberinvitation_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:accounts_memberinvitation_changelist")).status_code, 200)

        change = reverse("admin:accounts_memberinvitation_change", args=[invitation.pk])
        page = self.client.get(change)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="_save"')
        self.assertNotContains(page, 'name="token_hash"')
        response = self.client.post(
            change,
            {"token_hash": hash_secret("autre"), "expires_at_0": "2099-01-01", "expires_at_1": "00:00:00"},
        )
        self.assertEqual(response.status_code, 403)
        invitation.refresh_from_db()
        self.assertEqual(invitation.token_hash, hash_secret(token))

    def test_deleting_one_here_revokes_its_link(self):
        membership, token = members.invite(the_test_tenant(), name="Noé", email="noe@example.invalid", pages=[])
        delete = reverse("admin:accounts_memberinvitation_delete", args=[membership.invitation.pk])
        self.client.post(delete, {"post": "yes"})
        self.assertFalse(MemberInvitation.objects.exists())
        self.assertContains(Client().get(invitation_url(token)), DEAD, status_code=404)
