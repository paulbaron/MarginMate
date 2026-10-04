"""« Identifiants » (accounts/credentials.py), the store behind it
(accounts/vault.py) and the password asked again before it
(accounts/sudo.py): typed on the page, encrypted under a key Windows seals,
never shown back, bound to the site it was typed for, read by every
connector before the .env."""

import re
import ssl
import time
from datetime import date
from pathlib import Path
from unittest import mock

from cryptography.fernet import InvalidToken
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import limiter, paths, sudo, vault
from accounts.credentials import HOST_NEEDS_PASSWORD
from accounts.models import Membership
from accounts.tenancy import current_tenant
from config.security import DEVELOPMENT_SECRET_KEY
from invoices.models import InvoiceType, WebsiteInvoiceSource
from tests.factories import make_supplier
from tests.runner import test_user
from tests.support import NoNetworkTestCase

URL = reverse("accounts:credentials")
CONFIRM = reverse("accounts:confirm_password")
NO_ENV_FILE = Path("does-not-exist") / ".env"
SECRET = "Tres-Secret-123"


class VaultCase(TestCase):
    """Every test starts and ends with no store: the test espace's folder is
    shared by the whole run, and a password left there would reach the
    Metro and mailbox tests that expect none."""

    def setUp(self):
        super().setUp()
        self.forget()
        self.addCleanup(self.forget)
        patcher = mock.patch("accounts.credentials._env_file", return_value=NO_ENV_FILE)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def forget():
        folder = paths.private_dir()
        for name in vault.FILE_NAMES:
            (folder / name).unlink(missing_ok=True)
        for leftover in folder.glob(vault.TEMPORARY_PREFIX + "*"):
            leftover.unlink()

    @staticmethod
    def files_bytes() -> bytes:
        folder = paths.private_dir()
        return b"".join((folder / name).read_bytes() for name in vault.FILE_NAMES)


class VaultTests(VaultCase):
    def test_nothing_stored_reads_empty(self):
        state = vault.load()
        self.assertEqual(state.values, {})
        self.assertFalse(state.unreadable)

    def test_a_value_saved_is_read_back_and_nothing_readable_on_disk(self):
        vault.save({"METRO_EMAIL": "acheteur@exemple.invalid", "METRO_PASSWORD": SECRET})
        self.assertEqual(vault.value("METRO_PASSWORD"), SECRET)
        on_disk = self.files_bytes()
        for clear in (SECRET.encode(), b"acheteur", b"METRO"):
            self.assertNotIn(clear, on_disk)

    def test_the_random_key_is_sealed_by_windows(self):
        vault.save({"METRO_PASSWORD": SECRET})
        sealed = (paths.private_dir() / vault.KEY_FILE_NAME).read_bytes()
        expected = vault._SEALED if vault.os_protection_available() else vault._PLAIN
        self.assertTrue(sealed.startswith(expected))

    def test_a_key_windows_will_not_open_leaves_the_store_unreadable(self):
        """Another PC, another Windows account: DPAPI refuses."""
        vault.save({"METRO_PASSWORD": SECRET})
        with mock.patch("accounts.vault._unseal", side_effect=OSError("refused")):
            state = vault.load()
        self.assertTrue(state.unreadable)
        self.assertEqual(state.values, {})

    def test_the_store_without_its_key_file_is_unreadable(self):
        vault.save({"METRO_PASSWORD": SECRET})
        (paths.private_dir() / vault.KEY_FILE_NAME).unlink()
        self.assertTrue(vault.load().unreadable)

    def test_the_key_depends_on_the_espace_and_the_secret_key(self):
        tenant = current_tenant()
        other = mock.Mock(dir_name="un-autre-espace")
        random_key = b"k" * vault.RANDOM_KEY_BYTES
        token = vault._fernet(random_key, tenant).encrypt(b"x")
        with self.assertRaises(InvalidToken):
            vault._fernet(random_key, other).decrypt(token)
        with override_settings(SECRET_KEY="une-autre-cle-" + "x" * 50), self.assertRaises(InvalidToken):
            vault._fernet(random_key, tenant).decrypt(token)

    def test_a_merge_keeps_the_others_and_none_removes(self):
        vault.save({"METRO_EMAIL": "a@exemple.invalid", "METRO_PASSWORD": "un"})
        vault.save({"METRO_PASSWORD": None, "LADDITION_EMAIL": "b@exemple.invalid"})
        self.assertEqual(
            vault.load().values, {"METRO_EMAIL": "a@exemple.invalid", "LADDITION_EMAIL": "b@exemple.invalid"}
        )

    def test_removing_the_last_value_removes_both_files(self):
        vault.save({"METRO_PASSWORD": "un"})
        vault.save({"METRO_PASSWORD": ""})
        for name in vault.FILE_NAMES:
            self.assertFalse((paths.private_dir() / name).exists())

    def test_the_page_value_wins_over_the_setting_and_the_setting_is_the_fallback(self):
        with override_settings(METRO_EMAIL="du-fichier@exemple.invalid"):
            self.assertEqual(vault.setting("METRO_EMAIL"), "du-fichier@exemple.invalid")
            vault.save({"METRO_EMAIL": "de-la-page@exemple.invalid"})
            self.assertEqual(vault.setting("METRO_EMAIL"), "de-la-page@exemple.invalid")

    def test_another_secret_key_leaves_the_file_unreadable_never_an_exception(self):
        vault.save({"METRO_PASSWORD": "un"})
        with override_settings(SECRET_KEY="une-autre-cle-" + "x" * 50):
            state = vault.load()
            self.assertTrue(state.unreadable)
            self.assertEqual(state.values, {})
            self.assertEqual(vault.setting("METRO_PASSWORD"), "")

    def test_an_unreadable_store_is_never_written_over_unless_started_again(self):
        vault.save({"METRO_PASSWORD": "un", "LADDITION_PASSWORD": "deux"})
        before = (paths.private_dir() / vault.FILE_NAME).read_bytes()
        with override_settings(SECRET_KEY="une-autre-cle-" + "x" * 50):
            with self.assertRaisesMessage(vault.VaultError, "ne peuvent pas être lus"):
                vault.save({"METRO_PASSWORD": "trois"})
            self.assertEqual((paths.private_dir() / vault.FILE_NAME).read_bytes(), before)
            vault.save({"METRO_PASSWORD": "trois"}, start_over=True)
            self.assertEqual(vault.load().values, {"METRO_PASSWORD": "trois"})

    def test_a_file_being_replaced_is_busy_not_unreadable_and_nothing_is_written(self):
        vault.save({"METRO_PASSWORD": "un"})
        with mock.patch("accounts.vault._BUSY_PAUSE_SECONDS", 0):
            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("in use")):
                self.assertEqual(vault.load().problem, vault.BUSY)
                with self.assertRaisesMessage(vault.VaultError, "momentanément"):
                    vault.save({"LADDITION_PASSWORD": "deux"})
        self.assertEqual(vault.load().values, {"METRO_PASSWORD": "un"})

    def test_a_damaged_file_is_unreadable(self):
        vault.save({"METRO_PASSWORD": "un"})
        (paths.private_dir() / vault.FILE_NAME).write_bytes(b"pas du fernet")
        self.assertTrue(vault.load().unreadable)

    def test_a_public_secret_key_refuses_every_save(self):
        with override_settings(SECRET_KEY=DEVELOPMENT_SECRET_KEY):
            with self.assertRaisesMessage(vault.VaultError, "DJANGO_SECRET_KEY"):
                vault.save({"METRO_PASSWORD": SECRET})
        self.assertFalse((paths.private_dir() / vault.FILE_NAME).exists())

    def test_a_state_never_prints_its_values(self):
        vault.save({"METRO_PASSWORD": SECRET})
        self.assertNotIn(SECRET, repr(vault.load()))

    def test_a_binding_is_kept_with_its_value_and_goes_with_it(self):
        vault.save({"BOX_PASSWORD": SECRET}, bindings={"BOX_PASSWORD": "box.exemple.fr"})
        self.assertEqual(vault.bound_host("BOX_PASSWORD"), "box.exemple.fr")
        vault.save({"BOX_PASSWORD": None})
        self.assertEqual(vault.bound_host("BOX_PASSWORD"), "")

    def test_a_name_that_is_no_variable_is_refused(self):
        for name in ("metro_password", "../x", "", "A" * 65):
            with self.subTest(name=name), self.assertRaises(vault.VaultError):
                vault.save({name: "x"})

    def test_a_value_too_long_is_refused(self):
        with self.assertRaises(vault.VaultError):
            vault.save({"METRO_PASSWORD": "x" * (vault.MAX_VALUE_LENGTH + 1)})


def portal(
    name="Box Exemple - Factures", login="BOX_LOGIN", password="BOX_PASSWORD", url="https://box.exemple.fr/login"
):
    invoice_type = InvoiceType.objects.create(
        supplier=make_supplier(name=f"Fournisseur {name}"), name=name, source_kind=InvoiceType.SourceKind.WEBSITE
    )
    # objects.create, not the form: these tests set up states the form refuses.
    return WebsiteInvoiceSource.objects.create(
        invoice_type=invoice_type, login_url=url, username_env=login, password_env=password
    )


HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)"')


class ConfirmedCase(VaultCase, NoNetworkTestCase):
    """The page's tests run with the password confirmed, as the owner's."""

    def setUp(self):
        super().setUp()
        self.user = test_user()
        self.client.force_login(self.user)
        self.confirm()

    def confirm(self, until=None):
        session = self.client.session
        session[sudo.SESSION_KEY] = {"user": self.user.pk, "until": until or time.time() + 600}
        session.save()

    def page(self) -> str:
        return self.client.get(URL).content.decode()

    def drawn(self) -> dict:
        """The page's hidden fields, as the browser posts them back."""
        return dict(HIDDEN.findall(self.page()))

    def post(self, **fields):
        return self.client.post(URL, {**self.drawn(), **fields})

    def env_file(self, text: str):
        """A .env file holding `text`, read by the page in place of the real one."""
        env = paths.private_dir() / "essai.env"
        env.write_text(text, encoding="utf-8")
        self.addCleanup(env.unlink, missing_ok=True)
        patcher = mock.patch("accounts.credentials._env_file", return_value=env)
        patcher.start()
        self.addCleanup(patcher.stop)


class ConfirmationTests(ConfirmedCase):
    def setUp(self):
        super().setUp()
        session = self.client.session
        session.pop(sudo.SESSION_KEY, None)
        session.save()
        self.user.set_password("le-bon-mot-de-passe")
        self.user.save()
        # Changing the password logs the test client out: log it back in.
        self.client.force_login(self.user)

    def test_the_page_asks_for_the_password_first(self):
        response = self.client.get(URL)
        self.assertRedirects(response, f"{CONFIRM}?next=%2Fidentifiants%2F", fetch_redirect_response=False)

    def test_a_post_without_confirmation_writes_nothing(self):
        response = self.client.post(URL, {"METRO_PASSWORD": SECRET})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(vault.load().values, {})

    def test_htmx_is_answered_with_a_redirect_header(self):
        response = self.client.get(URL, headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response["HX-Redirect"].startswith(CONFIRM))

    def test_the_right_password_opens_the_page_and_changes_the_session_key(self):
        before = self.client.session.session_key
        response = self.client.post(CONFIRM, {"password": "le-bon-mot-de-passe", "next": URL})
        self.assertRedirects(response, URL, fetch_redirect_response=False)
        self.assertNotEqual(self.client.session.session_key, before)
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_a_wrong_password_is_refused_and_counted_by_the_login_limiter(self):
        response = self.client.post(CONFIRM, {"password": "faux", "next": URL})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, sudo.WRONG_PASSWORD, status_code=400)
        self.assertEqual(self.client.get(URL).status_code, 302)
        with mock.patch.object(limiter, "LIMIT", 1), mock.patch.object(limiter, "IP_LIMIT", 1):
            response = self.client.post(CONFIRM, {"password": "le-bon-mot-de-passe", "next": URL})
        self.assertEqual(response.status_code, 429)

    def test_the_confirmation_ends(self):
        self.client.post(CONFIRM, {"password": "le-bon-mot-de-passe", "next": URL})
        with mock.patch("accounts.sudo._now", return_value=time.time() + sudo.WINDOW_SECONDS + 1):
            self.assertEqual(self.client.get(URL).status_code, 302)

    def test_another_logins_confirmation_does_not_count(self):
        session = self.client.session
        session[sudo.SESSION_KEY] = {"user": self.user.pk + 1000, "until": time.time() + 600}
        session.save()
        self.assertEqual(self.client.get(URL).status_code, 302)

    def test_next_is_a_path_of_this_site_only(self):
        response = self.client.post(CONFIRM, {"password": "le-bon-mot-de-passe", "next": "https://ailleurs.example/"})
        self.assertRedirects(response, URL, fetch_redirect_response=False)

    @override_settings(
        PASSWORD_HASHERS=[
            "django.contrib.auth.hashers.PBKDF2PasswordHasher",
            "django.contrib.auth.hashers.MD5PasswordHasher",
        ]
    )
    def test_a_hash_upgraded_by_the_check_keeps_the_session(self):
        """check_password rewrites an old hash; the session, carrying the
        old one, would have been logged out right after confirming."""
        from django.contrib.auth.hashers import make_password

        self.user.password = make_password("le-bon-mot-de-passe", hasher="md5")
        self.user.save()
        self.client.force_login(self.user)
        before = self.client.session.session_key
        self.client.post(CONFIRM, {"password": "le-bon-mot-de-passe", "next": URL})
        self.assertNotEqual(self.client.session.session_key, before)
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_a_login_on_the_login_page_confirms_for_its_window(self):
        self.client.logout()
        self.client.post(
            reverse("accounts:login"), {"username": self.user.get_username(), "password": "le-bon-mot-de-passe"}
        )
        self.assertEqual(self.client.get(URL).status_code, 200)

    def test_a_login_on_the_admins_page_confirms_for_its_window(self):
        self.user.is_staff = self.user.is_superuser = True
        self.user.save()
        self.client.logout()
        self.client.post(
            reverse("admin:login"), {"username": self.user.get_username(), "password": "le-bon-mot-de-passe"}
        )
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_the_password_posted_is_never_in_an_error_report(self):
        response = self.client.post(CONFIRM, {"password": "faux", "next": URL})
        self.assertEqual(tuple(response.wsgi_request.sensitive_post_parameters), ("password",))


class PageTests(ConfirmedCase):
    def test_the_page_lists_every_account_and_every_portal(self):
        portal()
        page = self.page()
        for title in ("Boîte mail des factures", "Metro", "L&#x27;Addition (caisse)", "Box Exemple - Factures"):
            self.assertIn(title, page)
        self.assertIn('name="BOX_PASSWORD"', page)
        self.assertIn("no-store", self.client.get(URL)["Cache-Control"])

    def test_password_fields_are_kept_from_password_managers(self):
        page = self.page()
        self.assertIn('autocomplete="new-password"', page)
        self.assertIn('data-1p-ignore="true"', page)
        self.assertIn('data-lpignore="true"', page)

    def test_every_posted_value_is_hidden_from_error_reports(self):
        response = self.post(METRO_PASSWORD=SECRET)
        self.assertEqual(response.wsgi_request.sensitive_post_parameters, "__ALL__")

    def test_a_saved_password_is_never_shown_back(self):
        response = self.post(METRO_EMAIL="acheteur@exemple.invalid", METRO_PASSWORD=SECRET)
        self.assertRedirects(response, URL)
        self.assertEqual(vault.value("METRO_PASSWORD"), SECRET)
        page = self.page()
        self.assertNotIn(SECRET, page)
        self.assertIn("acheteur@exemple.invalid", page)  # the login is shown
        self.assertIn("enregistré - laisser vide pour le garder", page)

    def test_a_password_left_blank_is_kept_and_effacer_removes_it(self):
        vault.save({"METRO_EMAIL": "a@exemple.invalid", "METRO_PASSWORD": SECRET})
        self.post(METRO_EMAIL="a@exemple.invalid", METRO_PASSWORD="")
        self.assertEqual(vault.value("METRO_PASSWORD"), SECRET)
        self.post(METRO_EMAIL="a@exemple.invalid", METRO_PASSWORD__clear="on")
        self.assertEqual(vault.value("METRO_PASSWORD"), "")
        self.assertEqual(vault.value("METRO_EMAIL"), "a@exemple.invalid")

    def test_a_login_emptied_is_removed_and_one_not_posted_is_kept(self):
        vault.save({"LADDITION_EMAIL": "a@exemple.invalid", "METRO_EMAIL": "b@exemple.invalid"})
        self.post(LADDITION_EMAIL="")
        self.assertEqual(vault.value("LADDITION_EMAIL"), "")
        self.assertEqual(vault.value("METRO_EMAIL"), "b@exemple.invalid")

    def test_a_password_keeps_its_spaces(self):
        self.post(METRO_PASSWORD="  deux espaces ")
        self.assertEqual(vault.value("METRO_PASSWORD"), "  deux espaces ")

    def test_a_name_no_source_names_is_ignored(self):
        self.post(FREEBOX_PASSWORD=SECRET, DJANGO_SECRET_KEY="x")
        self.assertEqual(vault.load().values, {})

    def test_a_portals_values_are_bound_to_its_site(self):
        portal()
        self.post(BOX_LOGIN="jean", BOX_PASSWORD=SECRET)
        state = vault.load()
        self.assertEqual(state.values, {"BOX_LOGIN": "jean", "BOX_PASSWORD": SECRET})
        self.assertEqual(state.bindings, {"BOX_LOGIN": "box.exemple.fr", "BOX_PASSWORD": "box.exemple.fr"})

    def test_a_portal_moved_to_another_site_asks_for_its_password_again(self):
        source = portal()
        self.post(BOX_LOGIN="jean", BOX_PASSWORD=SECRET)
        source.login_url = "https://ailleurs.exemple.fr/login"
        source.save()
        self.assertIn("À ressaisir : le site a changé", self.page())
        self.post(BOX_LOGIN="jean", BOX_PASSWORD="nouveau")
        self.assertEqual(vault.bound_host("BOX_PASSWORD"), "ailleurs.exemple.fr")
        self.assertEqual(vault.bound_host("BOX_LOGIN"), "ailleurs.exemple.fr")

    def test_a_login_typed_alone_does_not_move_the_password_to_its_site(self):
        source = portal()
        self.post(BOX_LOGIN="jean", BOX_PASSWORD=SECRET)
        source.login_url = "https://ailleurs.exemple.fr/login"
        source.save()
        self.post(BOX_LOGIN="paul")
        self.assertEqual(vault.bound_host("BOX_PASSWORD"), "box.exemple.fr")

    def test_a_source_naming_another_ones_password_as_its_login_never_prints_it(self):
        """The audit's probe: a source whose LOGIN variable is the bank's
        password printed that password in clear in a text field."""
        portal(name="Banque", login="BANK_LOGIN", password="BANK_PASSWORD", url="https://banque.exemple.fr/")
        vault.save({"BANK_LOGIN": "client42", "BANK_PASSWORD": SECRET}, bindings={"BANK_PASSWORD": "banque.exemple.fr"})
        portal(name="0 Essai", login="BANK_PASSWORD", password="ESSAI_PASSWORD", url="https://essai.exemple.fr/")
        portal(name="Même nom", login="SAME", password="SAME", url="https://meme.exemple.fr/")
        page = self.page()
        self.assertNotIn(SECRET, page)
        self.assertIn("un même nom y sert d&#x27;identifiant et de mot de passe", page)

    def test_a_site_changed_while_the_page_was_open_saves_nothing(self):
        """The audit's probe: the page drawn for the bank's site, the source
        repointed before the owner submits - the password was typed for the
        site the page showed, and is not bound to the new one."""
        source = portal()
        drawn = self.drawn()
        source.login_url = "https://ailleurs.exemple.fr/login"
        source.save()
        response = self.client.post(URL, {**drawn, "BOX_LOGIN": "jean", "BOX_PASSWORD": SECRET})
        self.assertEqual(response.status_code, 400)
        self.assertIn("a changé depuis l&#x27;ouverture de cette page", response.content.decode())
        self.assertEqual(vault.load().values, {})

    def test_a_post_without_the_drawn_host_saves_nothing_for_that_portal(self):
        portal()
        response = self.client.post(URL, {"BOX_LOGIN": "jean", "BOX_PASSWORD": SECRET})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(vault.load().values, {})

    def test_env_values_go_to_a_portal_only_once_its_site_is_confirmed(self):
        source = portal()
        self.env_file("BOX_LOGIN=jean\nBOX_PASSWORD=du-env\n")
        page = self.page()
        self.assertIn("Fichier .env : site à confirmer", page)
        self.assertNotIn("du-env", page)
        self.post(**{f"site_env__portal-{source.pk}": "on", "BOX_LOGIN": ""})
        state = vault.load()
        self.assertEqual(state.env_bindings, {"BOX_LOGIN": "box.exemple.fr", "BOX_PASSWORD": "box.exemple.fr"})
        self.assertEqual(state.values, {})
        self.assertNotIn("site à confirmer", self.page())
        # Unticked: forgotten.
        self.post(BOX_LOGIN="")
        self.assertEqual(vault.load().env_bindings, {})

    def test_a_password_typed_for_a_portal_whose_login_is_the_envs_takes_its_login_along(self):
        """Typed alone, the password left the portal refused for its login."""
        portal()
        self.env_file("BOX_LOGIN=jean\n")
        self.post(BOX_LOGIN="", BOX_PASSWORD=SECRET)
        state = vault.load()
        self.assertEqual(state.bindings, {"BOX_PASSWORD": "box.exemple.fr"})
        self.assertEqual(state.env_bindings, {"BOX_LOGIN": "box.exemple.fr"})

    def test_two_sites_sharing_only_a_password_name_are_not_offered(self):
        portal(name="Banque", login="BANQUE_LOGIN", password="SHARED_PASSWORD", url="https://banque.exemple.fr/")
        portal(name="Box", login="BOX_LOGIN", password="SHARED_PASSWORD", url="https://box.exemple.fr/")
        page = self.page()
        self.assertNotIn('name="SHARED_PASSWORD"', page)
        self.assertEqual(page.count("aussi ceux d&#x27;une source d&#x27;un autre site"), 2)

    def test_a_page_drawn_before_another_save_saves_nothing(self):
        """Two tabs: the second would have put the first one's login back."""
        vault.save({"METRO_EMAIL": "a@exemple.invalid"})
        drawn = self.drawn()
        vault.save({"METRO_EMAIL": "b@exemple.invalid"})
        response = self.client.post(URL, {**drawn, "METRO_EMAIL": "a@exemple.invalid", "METRO_PASSWORD": SECRET})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(vault.load().values, {"METRO_EMAIL": "b@exemple.invalid"})

    def test_a_page_drawn_while_the_store_did_not_open_removes_no_login(self):
        """The audit's probe: drawn unreadable, every login read blank; saved
        once the store opened again, they were all removed."""
        vault.save({"METRO_EMAIL": "a@exemple.invalid", "LADDITION_EMAIL": "b@exemple.invalid"})
        with mock.patch("accounts.vault._unseal", side_effect=OSError("refused")):
            drawn = self.drawn()
        response = self.client.post(
            URL, {**drawn, "LADDITION_PASSWORD": SECRET, "METRO_EMAIL": "", "LADDITION_EMAIL": ""}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            vault.load().values, {"METRO_EMAIL": "a@exemple.invalid", "LADDITION_EMAIL": "b@exemple.invalid"}
        )

    def test_starting_over_alone_clears_the_unreadable_store(self):
        vault.save({"METRO_PASSWORD": "un"}, env_bindings={"BOX_LOGIN": "box.exemple.fr"})
        (paths.private_dir() / vault.FILE_NAME).write_bytes(b"pas du fernet")
        response = self.post(tout_ressaisir="on")
        self.assertRedirects(response, URL, fetch_redirect_response=False)
        for name in vault.FILE_NAMES:
            self.assertFalse((paths.private_dir() / name).exists())
        self.assertIn("Identifiants illisibles effacés", self.page())

    def test_login_fields_are_not_capitalised_by_a_phone(self):
        self.assertIn('name="METRO_EMAIL" autocomplete="off" spellcheck="false" autocapitalize="none"', self.page())

    def test_a_value_typed_as_a_password_is_never_drawn_as_a_login(self):
        """Stored as BOX_PASSWORD's password, then a source renamed so that
        this name is its LOGIN: still a password field, and that source is
        not offered."""
        source = portal()
        self.post(BOX_LOGIN="jean", BOX_PASSWORD=SECRET)
        self.assertEqual(vault.load().secret_names, frozenset({"BOX_PASSWORD"}))
        source.username_env, source.password_env = "BOX_PASSWORD", "BOX_PASSWORD_2"
        source.save()
        page = self.page()
        self.assertNotIn(SECRET, page)
        self.assertIn("son identifiant porte le nom d&#x27;un mot de passe enregistré", page)

    def test_a_store_windows_holds_shows_no_form(self):
        """Drawn from an empty state, every login would read blank and the
        next save remove them."""
        vault.save({"METRO_EMAIL": "a@exemple.invalid"})
        with mock.patch("accounts.vault._BUSY_PAUSE_SECONDS", 0):
            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("in use")):
                response = self.client.get(URL)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('name="METRO_EMAIL"', response.content.decode())

    def test_an_inactive_source_is_said_on_its_card(self):
        source = portal()
        source.invoice_type.is_active = False
        source.invoice_type.save()
        self.assertIn("(inactive)", self.page())

    def test_two_sources_of_one_pair_on_one_site_are_one_account(self):
        portal(name="Box - Fixe")
        portal(name="Box - Mobile")
        page = self.page()
        self.assertEqual(page.count('name="BOX_PASSWORD"'), 1)
        self.assertIn("Box - Fixe, Box - Mobile", page)

    def test_two_sites_sharing_names_are_not_offered(self):
        portal(name="Box - Fixe")
        portal(name="Autre site", url="https://autre.exemple.fr/login")
        page = self.page()
        self.assertNotIn('name="BOX_PASSWORD"', page)
        self.assertIn("aussi ceux d&#x27;une source d&#x27;un autre site", page)

    def test_a_portal_whose_login_page_is_not_https_is_not_offered(self):
        portal(url="http://box.exemple.fr/login")
        page = self.page()
        self.assertNotIn('name="BOX_PASSWORD"', page)
        self.assertIn("pas une adresse https", page)

    def test_a_portal_naming_an_application_variable_is_not_offered(self):
        portal(login="METRO_EMAIL", password="METRO_PASSWORD")
        page = self.page()
        self.assertIn("ses noms sont ceux de l&#x27;application", page)
        self.assertEqual(page.count('name="METRO_PASSWORD"'), 1)  # Metro's own field

    def test_values_no_account_uses_are_listed_by_name_and_removed(self):
        vault.save({"OLD_PORTAL_PASSWORD": SECRET})
        page = self.page()
        self.assertIn("Identifiants qui ne servent plus", page)
        self.assertIn("OLD_PORTAL_PASSWORD", page)
        self.assertNotIn(SECRET, page)
        self.post(OLD_PORTAL_PASSWORD__clear="on")
        self.assertEqual(vault.load().values, {})

    def test_a_key_typed_for_the_removed_ai_reading_is_offered_for_deletion(self):
        """The AI reading and its account went on 04/10/2026: a key typed
        before stays in the store until « Effacer », and no card asks for
        one any more."""
        vault.save({"ANTHROPIC_API_KEY": SECRET})
        page = self.page()
        self.assertNotIn("Analyse IA", page)
        self.assertIn("Identifiants qui ne servent plus", page)
        # An account's key, not a source's: the sentence says both.
        self.assertIn("Enregistrés pour un compte ou une source qui n'existe plus", page)
        self.assertIn('name="ANTHROPIC_API_KEY__clear"', page)
        self.assertNotIn(SECRET, page)
        self.post(ANTHROPIC_API_KEY__clear="on")
        self.assertEqual(vault.load().values, {})

    def test_the_status_says_where_a_value_comes_from_never_the_value(self):
        self.env_file("LADDITION_EMAIL=du-fichier@exemple.invalid\n")
        page = self.page()
        self.assertIn("Fichier .env", page)
        self.assertNotIn("du-fichier@exemple.invalid", page)

    def test_the_imap_default_is_no_env_line(self):
        page = self.page()
        self.assertNotIn("Fichier .env", page)

    def test_an_old_mailbox_line_of_the_env_is_named_by_its_own_name(self):
        self.env_file("UBA_EMAIL_APP_PASSWORD=ancien\n")
        vault.save({"INVOICE_EMAIL_APP_PASSWORD": SECRET}, bindings={"INVOICE_EMAIL_APP_PASSWORD": "imap.gmail.com"})
        page = self.page()
        self.assertIn("Encore en clair dans le fichier .env", page)
        self.assertIn("UBA_EMAIL_APP_PASSWORD", page)
        self.assertNotIn("ancien", page.split("Encore en clair")[1][:400])

    def test_a_value_stored_and_still_in_the_env_file_is_pointed_out(self):
        self.env_file(f"METRO_PASSWORD={SECRET}\n")
        vault.save({"METRO_PASSWORD": "autre"})
        page = self.page()
        self.assertIn("Encore en clair dans le fichier .env", page)
        self.assertIn("METRO_PASSWORD", page)
        self.assertNotIn(SECRET, page)

    def test_an_unreadable_store_is_said_and_only_written_over_when_asked(self):
        vault.save({"METRO_PASSWORD": "un"})
        (paths.private_dir() / vault.FILE_NAME).write_bytes(b"pas du fernet")
        page = self.page()
        self.assertIn("ne peuvent pas être lus sur ce serveur", page)
        response = self.post(LADDITION_PASSWORD=SECRET)
        self.assertEqual(response.status_code, 400)
        self.assertEqual((paths.private_dir() / vault.FILE_NAME).read_bytes(), b"pas du fernet")
        self.post(LADDITION_PASSWORD=SECRET, tout_ressaisir="on")
        self.assertEqual(vault.load().values, {"LADDITION_PASSWORD": SECRET})

    def test_a_post_whose_confirmation_ran_out_says_nothing_was_saved(self):
        drawn = self.drawn()
        self.confirm(until=time.time() - 1)
        response = self.client.post(URL, {**drawn, "METRO_PASSWORD": SECRET}, follow=True)
        self.assertContains(response, "rien n&#x27;a été enregistré")
        self.assertEqual(vault.load().values, {})

    def test_a_public_server_key_is_said_not_a_500(self):
        # The key itself is not swapped: the session would no longer open.
        with mock.patch("config.security.secret_key_problem", return_value="clé publique"):
            response = self.post(METRO_PASSWORD=SECRET)
        self.assertEqual(response.status_code, 400)
        self.assertIn("DJANGO_SECRET_KEY", response.content.decode())

    @override_settings(DEBUG=True)
    def test_a_development_copy_says_not_to_type_real_passwords(self):
        self.assertIn("Copie de développement", self.page())

    def test_another_bar_s_page_offers_its_connectors_accounts_under_their_own_names(self):
        """The page opens in every espace (04/10/2026; it was refused outside
        the owner's): there, no Metro, no portal, and fields named after
        their account (accounts/tests/test_credentials_hosted.py has the
        real two espaces)."""
        portal()
        with mock.patch("accounts.credentials.server_accounts_allowed", return_value=False):
            page = self.page()
            self.assertNotIn("METRO", page)
            self.assertNotIn("BOX_", page)
            self.assertIn('name="caisse_mot_de_passe"', page)
            response = self.client.post(URL, {**self.drawn(), "caisse_mot_de_passe": SECRET})
        self.assertRedirects(response, URL)
        self.assertEqual(vault.load().values, {"LADDITION_PASSWORD": SECRET})

    def test_refused_to_a_member_who_is_not_the_owner_even_confirmed(self):
        Membership.objects.filter(tenant=current_tenant()).update(role=Membership.Role.MEMBER)
        response = self.post(METRO_PASSWORD=SECRET)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(vault.load().values, {})

    def test_reached_from_donnees_and_the_sources_tab(self):
        for name in ("transfer:data_home", "invoices:invoice_type_list"):
            with self.subTest(page=name):
                self.assertIn(f'href="{URL}"', self.client.get(reverse(name)).content.decode())


class MailboxTests(ConfirmedCase):
    """The mailbox's app password goes to the server it was typed for, over
    a TLS connection that checks the server - never elsewhere."""

    def login_attempt(self):
        from invoices.scrapers import generic_email

        imap = mock.MagicMock()
        imap.search.return_value = ("OK", [b""])
        with mock.patch("imaplib.IMAP4_SSL", return_value=imap) as opened:
            generic_email.find_matching_emails(date(2026, 5, 1), date(2026, 5, 31), sender_pattern="x", log=print)
        return opened, imap

    def test_the_connection_checks_the_certificate_and_the_host_name(self):
        self.post(INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_EMAIL_APP_PASSWORD=SECRET)
        opened, imap = self.login_attempt()
        context = opened.call_args.kwargs["ssl_context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(opened.call_args.args[0], "imap.gmail.com")
        imap.login.assert_called_once_with("f@exemple.invalid", SECRET)

    def test_changing_the_server_alone_is_refused(self):
        self.post(INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_EMAIL_APP_PASSWORD=SECRET)
        response = self.post(INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_IMAP_HOST="imap.pirate.example")
        self.assertEqual(response.status_code, 400)
        self.assertIn(HOST_NEEDS_PASSWORD.replace("'", "&#x27;"), response.content.decode())
        self.assertEqual(vault.value("INVOICE_IMAP_HOST"), "")
        self.assertEqual(self.login_attempt()[0].call_args.args[0], "imap.gmail.com")

    def test_changing_the_address_alone_is_refused(self):
        self.post(INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_EMAIL_APP_PASSWORD=SECRET)
        response = self.post(INVOICE_EMAIL_ADDRESS="autre@exemple.invalid")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(vault.value("INVOICE_EMAIL_ADDRESS"), "f@exemple.invalid")

    def test_a_new_server_with_its_password_is_where_it_goes(self):
        self.post(INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_EMAIL_APP_PASSWORD=SECRET)
        self.post(
            INVOICE_EMAIL_ADDRESS="f@exemple.invalid",
            INVOICE_IMAP_HOST="imap.exemple.fr",
            INVOICE_EMAIL_APP_PASSWORD="nouveau",
        )
        self.assertEqual(vault.bound_host("INVOICE_EMAIL_APP_PASSWORD"), "imap.exemple.fr")
        self.assertEqual(self.login_attempt()[0].call_args.args[0], "imap.exemple.fr")

    def test_a_server_name_must_be_a_plain_host(self):
        for host in ("192.168.1.10", "imap.exemple.fr:993", "localhost", "https://imap.exemple.fr", "imap"):
            with self.subTest(host=host):
                response = self.post(
                    INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_IMAP_HOST=host, INVOICE_EMAIL_APP_PASSWORD=SECRET
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(vault.load().values, {})

    def test_a_host_typed_on_the_page_never_takes_the_env_password(self):
        """The .env's app password goes to the .env's server."""
        vault.save({"INVOICE_IMAP_HOST": "imap.pirate.example"})
        with override_settings(INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_EMAIL_APP_PASSWORD="du-env"):
            opened, _ = self.login_attempt()
        self.assertEqual(opened.call_args.args[0], "imap.gmail.com")

    def test_a_stored_password_with_no_server_recorded_is_not_sent(self):
        from invoices.scrapers import generic_email

        vault.save({"INVOICE_EMAIL_ADDRESS": "f@exemple.invalid", "INVOICE_EMAIL_APP_PASSWORD": SECRET})
        with self.assertRaisesMessage(RuntimeError, "rattaché à aucun serveur"):
            self.login_attempt()
        self.assertNotIn(SECRET, generic_email.MAILBOX_UNBOUND)


class ConnectorTests(VaultCase, NoNetworkTestCase):
    """The connectors read the page's values before the .env's - checked up
    to the moment they would reach out, never past it."""

    def test_metro_reads_the_page_once(self):
        from invoices.scrapers import metro

        vault.save({"METRO_EMAIL": "a@exemple.invalid", "METRO_PASSWORD": SECRET})
        self.assertEqual(metro.metro_credentials(), ("a@exemple.invalid", SECRET))
        with mock.patch.object(metro, "metro_pause", side_effect=RuntimeError("stopped here")):
            with self.assertRaisesMessage(RuntimeError, "stopped here"):
                metro.scrape_metro_invoices("unused", date(2026, 5, 1), date(2026, 5, 31), log=lambda message: None)

    def test_metro_without_credentials_points_at_the_page(self):
        from invoices.scrapers import metro

        with self.assertRaises(metro.MetroError) as raised:
            metro.scrape_metro_invoices("unused", date(2026, 5, 1), date(2026, 5, 31), log=lambda message: None)
        self.assertIn("page Identifiants", str(raised.exception))

    def test_laddition_reads_the_page(self):
        from recipes.pos import laddition_session

        vault.save({"LADDITION_EMAIL": "c@exemple.invalid", "LADDITION_PASSWORD": SECRET})
        with mock.patch.object(laddition_session, "navigate", side_effect=RuntimeError("stopped here")):
            with self.assertRaisesMessage(RuntimeError, "stopped here"):
                laddition_session.log_in(mock.Mock(), log=lambda message: None)
