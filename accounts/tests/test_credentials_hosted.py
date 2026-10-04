"""« Identifiants » in another bar's espace (accounts/credentials.py).

Since 04/10/2026 every espace's owner types there what its own connectors
sign in with - the mailbox and L'Addition -, his MarginMate password
confirmed as in the owner's. Metro and the portals are the platform
owner's alone, so another bar's page has neither; and nothing of the
server's reaches it: no « Fichier .env », no server variable's name - not
even as a field's name -, no server setting in a refusal. Two real espaces
in temporary files (accounts/tests/support.py); Bar Alpha is the platform
owner's. Every value invented.
"""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path
from unittest import mock

from django.urls import reverse

from accounts import credentials, paths, vault
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from invoices import integrations
from invoices.models import InvoiceType, WebsiteInvoiceSource
from tests.factories import make_supplier
from tests.runner import confirm_password

URL = reverse("accounts:credentials")
HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)"')
SECRET = "Tres-Secret-456"
#: What a server's .env holds: Metro, the mailbox, the till, a portal.
SERVER_ENV_FILE = (
    "METRO_EMAIL=acheteur@exemple.invalid\n"
    "METRO_PASSWORD=secret-metro\n"
    "INVOICE_EMAIL_ADDRESS=factures@exemple.invalid\n"
    "INVOICE_EMAIL_APP_PASSWORD=secret-boite\n"
    "LADDITION_EMAIL=caisse@exemple.invalid\n"
    "BOX_LOGIN=gerant@exemple.invalid\n"
)
#: What a hosted bar's page must never hold.
SERVER_WORDS = (".env", "METRO_", "INVOICE_", "LADDITION_", "BOX_", "DJANGO_", "manage.py")


class HostedCredentialsTests(TwoTenantsTestCase):
    owner_a = True

    def setUp(self):
        super().setUp()
        env = Path(self.tenants_root).parent / "serveur.env"
        env.write_text(SERVER_ENV_FILE, encoding="utf-8")
        patcher = mock.patch("accounts.credentials._env_file", return_value=env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # A portal in both espaces (an archive could bring one to Beta).
        for bar in (self.bar_a, self.bar_b):
            with bound_tenant(bar):
                invoice_type = InvoiceType.objects.create(
                    supplier=make_supplier(name="Box Exemple"),
                    name="Box Exemple - Factures",
                    source_kind=InvoiceType.SourceKind.WEBSITE,
                )
                WebsiteInvoiceSource.objects.create(
                    invoice_type=invoice_type,
                    login_url="https://box.exemple.invalid/login",
                    username_env="BOX_LOGIN",
                    password_env="BOX_PASSWORD",
                )

    def page(self, user) -> str:
        confirm_password(self.client, user)
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def post(self, user, **fields):
        drawn = dict(HIDDEN.findall(self.page(user)))
        return self.client.post(URL, {**drawn, **fields})

    def test_another_bar_s_page_holds_its_connectors_accounts_and_nothing_of_the_server(self):
        page = self.page(self.user_b)
        for title in ("Boîte mail des factures", "L&#x27;Addition (caisse)"):
            self.assertIn(title, page)
        self.assertNotIn("docs.metro.fr", page)
        self.assertNotIn("Box Exemple", page)
        self.assertNotIn("Fichier .env", page)
        self.assertIn(integrations.PORTALS, unescape(page))
        for word in SERVER_WORDS:
            with self.subTest(word=word):
                self.assertNotIn(word, page)
        # The till's card says where the sales come from, and the other way.
        self.assertIn("Recettes &amp; ventes › Ventes", page)
        self.assertIn("importez son export sur la même page", page)

    def test_the_owner_s_page_is_as_it_was(self):
        page = self.page(self.user_a)
        self.assertIn('name="METRO_PASSWORD"', page)
        self.assertIn('name="BOX_PASSWORD"', page)
        self.assertIn("Fichier .env", page)
        self.assertNotIn(integrations.PORTALS, unescape(page))

    def test_what_another_bar_types_is_saved_in_its_own_store_only(self):
        response = self.post(
            self.user_b,
            boite_adresse="beta@exemple.invalid",
            boite_mot_de_passe=SECRET,
            caisse_identifiant="caisse-beta@exemple.invalid",
            caisse_mot_de_passe=SECRET,
        )
        self.assertRedirects(response, URL)
        with bound_tenant(self.bar_b):
            state = vault.load()
        self.assertEqual(
            state.values,
            {
                "INVOICE_EMAIL_ADDRESS": "beta@exemple.invalid",
                "INVOICE_EMAIL_APP_PASSWORD": SECRET,
                "LADDITION_EMAIL": "caisse-beta@exemple.invalid",
                "LADDITION_PASSWORD": SECRET,
            },
        )
        # Bound to the default server: the .env's is the owner's alone.
        self.assertEqual(state.bindings["INVOICE_EMAIL_APP_PASSWORD"], credentials.DEFAULT_IMAP_HOST)
        with bound_tenant(self.bar_a):
            self.assertEqual(vault.load().values, {})
        page = self.page(self.user_b)
        self.assertNotIn(SECRET, page)
        self.assertIn("beta@exemple.invalid", page)

    def test_starting_over_is_said_without_the_server_s_file(self):
        with bound_tenant(self.bar_b):
            vault.save({"LADDITION_PASSWORD": SECRET})
        with mock.patch("accounts.vault._unseal", side_effect=OSError("refusé")):
            page = self.page(self.user_b)
            self.assertIn("ne peuvent pas être lus sur ce serveur", page)
            drawn = dict(HIDDEN.findall(page))
            response = self.client.post(URL, {**drawn, "tout_ressaisir": "on"}, follow=True)
        self.assertContains(response, credentials.STARTED_OVER_HOSTED)
        for word in SERVER_WORDS:
            self.assertNotContains(response, word)

    def test_a_weak_server_key_is_said_without_its_setting_s_name(self):
        with mock.patch("config.security.secret_key_problem", return_value="clé publique"):
            response = self.post(self.user_b, caisse_mot_de_passe=SECRET)
        self.assertEqual(response.status_code, 400)
        self.assertIn(vault.WEAK_KEY_HOSTED, unescape(response.content.decode()))
        self.assertNotIn("DJANGO_SECRET_KEY", response.content.decode())

    def test_a_value_no_account_uses_is_marked_by_its_name_on_the_owner_s_page_only(self):
        """The owner's « Effacer » rows keep `data-credential="<NAME>"` as
        before; another bar's name no server variable."""
        for bar in (self.bar_a, self.bar_b):
            with bound_tenant(bar):
                vault.save({"OLD_EXEMPLE_PASSWORD": "ancien-secret"})
        owner = self.page(self.user_a)
        self.assertIn('data-credential="OLD_EXEMPLE_PASSWORD"', owner)
        self.assertIn('name="OLD_EXEMPLE_PASSWORD__clear"', owner)
        hosted = self.page(self.user_b)
        self.assertNotIn("OLD_EXEMPLE", hosted)
        self.assertIn("Effacer une valeur enregistrée", unescape(hosted))

    def test_another_bar_s_mailbox_server_is_its_own_and_never_the_env_s(self):
        with bound_tenant(self.bar_b):
            self.assertEqual(credentials.mailbox_host({}), credentials.DEFAULT_IMAP_HOST)

    def test_a_member_of_another_bar_is_refused(self):
        from accounts.models import Membership

        Membership.objects.filter(user=self.user_b).update(role=Membership.Role.MEMBER)
        self.client.force_login(self.user_b)
        confirm_password(self.client, self.user_b)
        self.assertEqual(self.client.get(URL).status_code, 403)

    def test_paths_of_the_page_are_the_espace_s(self):
        """The store is written in Beta's own private folder."""
        self.post(self.user_b, caisse_mot_de_passe=SECRET)
        self.assertTrue((paths.tenant_dir(self.bar_b) / "private" / vault.FILE_NAME).is_file())
        self.assertFalse((paths.tenant_dir(self.bar_a) / "private" / vault.FILE_NAME).exists())
