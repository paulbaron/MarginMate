"""The invoice mailbox, open to every bar: what keeps it from hurting the
server and the other bars.

- Outside the platform owner's espace, a source's patterns pass the pattern
  guard (returnables.patterns) when saved and are matched with a timeout,
  case-sensitive as `re` matched them; the owner's are matched by `re` as
  always.
- Outside the owner's espace, the IMAP server must be a public address
  (invoices/scrapers/egress.py), checked before anything connects; a local
  name is refused when typed on « Identifiants ».
- Everywhere, a message announced past MAX_LITERAL_BYTES is never read into
  memory.
- The gather card and Consignes offer another bar's mailbox once it is
  filled in on its « Identifiants ».

Nothing resolves a name or opens a socket: the resolver and the IMAP client
are replaced. Every address, name and mail invented (the public addresses
are documentation's or well-known resolvers').
"""

from __future__ import annotations

import socket
from datetime import date
from html import unescape
from unittest import mock

import regex
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from accounts import vault
from accounts.tenancy import bound_tenant
from accounts.tests.support import TwoTenantsTestCase
from invoices import integrations
from invoices.models import EmailInvoiceSource, InvoiceType, ScrapeJob
from invoices.scrapers import egress, generic_email
from invoices.scrapers.generic_email import find_matching_emails
from invoices.tests.test_email_search import fake_mailbox
from returnables import patterns
from returnables.patterns import PatternError
from returnables.tests.test_patterns import NeverCompile, SlowPattern
from tests.factories import make_supplier
from tests.support import NoNetworkTestCase

START, END = date(2026, 2, 1), date(2026, 2, 28)
#: A public address (one of a well-known public resolver's).
PUBLIC = "8.8.8.8"


def answers(*addresses):
    """A resolver answering `addresses` for any name, as getaddrinfo shapes them."""

    def resolve(host, port):
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return resolve


def a_mail(sender="factures@traiteur.invalid", subject="Votre facture", body="Bonjour") -> bytes:
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = sender
    message["To"] = "beta@exemple.invalid"
    message["Subject"] = subject
    message["Date"] = "Tue, 10 Feb 2026 08:15:02 +0100"
    message.set_content(body)
    message.add_attachment(b"%PDF-1.4 essai", maintype="application", subtype="pdf", filename="facture.pdf")
    return message.as_bytes()


class EgressTests(NoNetworkTestCase):
    def test_only_public_unicast_addresses_pass(self):
        for refused in (
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
            "::1",
            "::",
            "fe80::1",
            "fe80::1%12",
            "fc00::1",
            "ff02::1",
            "::ffff:127.0.0.1",
            "::ffff:192.168.1.1",
            "pas-une-adresse",
        ):
            with self.subTest(address=refused):
                self.assertFalse(egress.address_allowed(refused))
        for allowed in (PUBLIC, "93.184.216.34", "2001:4860:4860::8888", "::ffff:8.8.8.8"):
            with self.subTest(address=allowed):
                self.assertTrue(egress.address_allowed(allowed))

    def test_local_names(self):
        for name in (
            "localhost",
            "LOCALHOST.",
            "imap",
            "x.localhost",
            "nas.local",
            "box.lan",
            "a.home.arpa",
            "a.internal",
        ):
            with self.subTest(name=name):
                self.assertTrue(egress.local_name(name))
        self.assertFalse(egress.local_name("imap.gmail.com"))

    def test_a_public_name_leading_to_the_local_network_is_refused(self):
        with mock.patch.object(egress, "resolve", answers(PUBLIC)):
            egress.check_mail_host("imap.exemple.invalid")
        for addresses in (("192.168.1.20",), (PUBLIC, "127.0.0.1"), ("::ffff:10.0.0.1",), ()):
            with self.subTest(addresses=addresses), mock.patch.object(egress, "resolve", answers(*addresses)):
                with self.assertRaisesMessage(egress.EgressRefused, "mène au réseau local du serveur"):
                    egress.check_mail_host("imap.exemple.invalid")

    def test_a_local_name_is_refused_without_a_lookup(self):
        with mock.patch.object(egress, "resolve", side_effect=AssertionError("looked up")):
            with self.assertRaisesMessage(egress.EgressRefused, "désigne un réseau local"):
                egress.check_mail_host("nas.local")

    def test_a_name_that_does_not_resolve_is_said(self):
        with mock.patch.object(egress, "resolve", side_effect=socket.gaierror("introuvable")):
            with self.assertRaisesMessage(egress.EgressRefused, "est introuvable"):
                egress.check_mail_host("imap.exemple.invalid")

    def test_the_tests_never_resolve_a_name_for_real(self):
        with self.assertRaises(AssertionError):
            socket.getaddrinfo("imap.exemple.invalid", 993)
        self.assertTrue(socket.getaddrinfo("localhost", 80))


class LiteralCapTests(SimpleTestCase):
    """imaplib reads a literal the server announces with `read(size)`: one
    past MAX_LITERAL_BYTES is refused, in every espace."""

    class Fake:
        def __init__(self, host, ssl_context=None, timeout=None):
            self.host = host

        def read(self, size):
            return b"x" * size

    def test_a_message_announced_too_big_is_never_read(self):
        with mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL", self.Fake):
            connection = generic_email._open_mailbox("imap.exemple.invalid")
        self.assertIsInstance(connection, self.Fake)
        self.assertEqual(connection.read(3), b"xxx")
        with self.assertRaises(generic_email.MessageTooBig):
            connection.read(generic_email.MAX_LITERAL_BYTES + 1)

    def test_the_cap_is_generous(self):
        self.assertEqual(generic_email.MAX_LITERAL_BYTES, 50 * 1024 * 1024)


class InvoiceMailMatcherTests(SimpleTestCase):
    def test_case_sensitive_as_re_and_the_whole_body_read(self):
        matcher = patterns.invoice_mail_matcher("Traiteur")
        self.assertIsNone(matcher.search("factures@traiteur.invalid"))
        self.assertTrue(matcher.search("Le Traiteur <f@t.invalid>"))
        self.assertTrue(matcher.search("x" * 2_000 + "Traiteur"))
        self.assertIsNone(matcher.search("x" * patterns.INVOICE_MAIL_TEXT_LIMIT + "Traiteur"))
        # Single-line, as `re` read it: « ^ » is the text's start only.
        self.assertIsNone(patterns.invoice_mail_matcher("^Total").search("Facture\nTotal 12,00"))

    def test_a_refused_pattern_is_refused_before_compiling(self):
        never = NeverCompile()
        patterns._checked.cache_clear()
        self.addCleanup(patterns._checked.cache_clear)
        with mock.patch.object(regex, "compile", new=never):
            with self.assertRaisesMessage(PatternError, "Motif de la source : répétition trop grande"):
                patterns.invoice_mail_matcher("x{500}")
        self.assertEqual(never.calls, [])

    def test_slow_on_three_mails_stops_the_source(self):
        said = []
        matcher = patterns.MailMatcher(
            SlowPattern(), log=said.append, max_timeouts=patterns.MAX_MAIL_TIMEOUTS, field_label="Motif de la source"
        )
        with self.assertLogs("returnables.patterns", level="WARNING"):
            self.assertIsNone(matcher.search("un"))
            self.assertIsNone(matcher.search("deux"))
            with self.assertRaisesMessage(PatternError, "Motif de la source : motif trop lent sur ces mails"):
                matcher.search("trois")
        self.assertEqual(len(said), 3)


class HostedMailboxTests(TwoTenantsTestCase):
    """Bar Alpha is the platform owner's espace, Bar Beta another bar with its
    own mailbox on its « Identifiants »."""

    owner_a = True

    def type_the_mailbox(self, tenant, host="imap.beta.invalid"):
        with bound_tenant(tenant):
            vault.save(
                {"INVOICE_EMAIL_ADDRESS": "beta@exemple.invalid", "INVOICE_EMAIL_APP_PASSWORD": "secret-beta"},
                bindings={"INVOICE_EMAIL_APP_PASSWORD": host},
            )

    def search(self, tenant, message=None, **patterns_given):
        with (
            bound_tenant(tenant),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
            mock.patch.object(egress, "resolve", answers(PUBLIC)),
        ):
            fake_mailbox(client, message or a_mail())
            return find_matching_emails(START, END, log=lambda line: None, **patterns_given), client

    def test_another_bar_s_server_leading_to_the_local_network_is_never_connected(self):
        self.type_the_mailbox(self.bar_b)
        with (
            bound_tenant(self.bar_b),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
            mock.patch.object(egress, "resolve", answers("192.168.1.20")),
            self.assertRaisesMessage(egress.EgressRefused, "imap.beta.invalid"),
        ):
            find_matching_emails(START, END, "traiteur")
        client.assert_not_called()
        matches, client = self.search(self.bar_b, sender_pattern="traiteur")
        self.assertEqual(client.call_args.args[0], "imap.beta.invalid")
        self.assertEqual(len(matches), 1)

    def test_the_owner_s_server_is_not_looked_up(self):
        with (
            bound_tenant(self.bar_a),
            override_settings(INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_EMAIL_APP_PASSWORD="x"),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
            mock.patch.object(egress, "resolve", side_effect=AssertionError("looked up")),
        ):
            fake_mailbox(client, a_mail())
            self.assertEqual(len(find_matching_emails(START, END, "traiteur", log=lambda line: None)), 1)

    def test_another_bar_s_patterns_are_guarded_and_case_sensitive(self):
        self.type_the_mailbox(self.bar_b)
        self.assertEqual(self.search(self.bar_b, sender_pattern="TRAITEUR")[0], [])
        body = "x" * 2_000 + " Référence MOTCLE"
        self.assertEqual(
            len(self.search(self.bar_b, a_mail(body=body), sender_pattern="traiteur", body_pattern="MOTCLE")[0]), 1
        )
        never = NeverCompile()
        patterns._checked.cache_clear()
        self.addCleanup(patterns._checked.cache_clear)
        with mock.patch.object(regex, "compile", new=never), self.assertRaises(PatternError):
            self.search(self.bar_b, sender_pattern="x{500}")
        self.assertEqual(never.calls, [])

    def test_a_refused_pattern_fails_its_source_on_its_own_line(self):
        self.type_the_mailbox(self.bar_b)
        with bound_tenant(self.bar_b):
            supplier = make_supplier(code="TRAITEUR_B", name="Traiteur Beta", parser_key="")
            source_type = InvoiceType.objects.create(
                name="Traiteur Beta - Factures", supplier=supplier, source_kind=InvoiceType.SourceKind.EMAIL
            )
            # Saved before the guard knew it (an old archive, the admin).
            EmailInvoiceSource.objects.create(invoice_type=source_type, sender_pattern="x{500}")
            job = ScrapeJob.objects.create()
            from invoices.tasks import gather_invoices_task

            with (
                mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
                mock.patch.object(egress, "resolve", answers(PUBLIC)),
                mock.patch("invoices.tasks._GatherHeartbeat"),
            ):
                gather_invoices_task(job.pk, START, END, {f"type-{source_type.pk}"})
            job.refresh_from_db()
        client.assert_not_called()
        self.assertEqual(job.status, ScrapeJob.Status.SUCCESS)
        self.assertTrue(job.progress[f"type-{source_type.pk}"]["error"].startswith("Motif de la source :"))

    def test_saving_another_bar_s_patterns_goes_through_the_guard(self):
        for tenant, guarded in ((self.bar_b, True), (self.bar_a, False)):
            with bound_tenant(tenant):
                supplier = make_supplier(code="TRAITEUR", name="Traiteur", parser_key="")
                source_type = InvoiceType.objects.create(name="Traiteur - Factures", supplier=supplier)
                source = EmailInvoiceSource(invoice_type=source_type, sender_pattern="x{500}", body_pattern="(")
                with self.assertRaises(ValidationError) as refused:
                    source.full_clean()
            errors = refused.exception.message_dict
            # A syntax error is said as it always was, in both.
            self.assertTrue(errors["body_pattern"][0].startswith("Expression régulière invalide :"))
            if guarded:
                self.assertEqual(
                    errors["sender_pattern"], ["Motif d'expéditeur : répétition trop grande : 100 fois au plus."]
                )
            else:
                self.assertNotIn("sender_pattern", errors)

    def test_a_local_imap_server_is_refused_on_another_bar_s_page(self):
        from tests.runner import confirm_password

        url = reverse("accounts:credentials")
        confirm_password(self.client, self.user_b)
        page = self.client.get(url).content.decode()
        drawn = dict(__import__("re").findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page))
        response = self.client.post(
            url,
            {**drawn, "boite_adresse": "beta@exemple.invalid", "boite_mot_de_passe": "x", "boite_serveur": "nas.local"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("désigne un réseau local", unescape(response.content.decode()))
        with bound_tenant(self.bar_b):
            self.assertEqual(vault.load().values, {})

    def test_the_gather_card_offers_another_bar_s_mailbox_once_filled_in(self):
        with bound_tenant(self.bar_b):
            mailbox = [f"type-{pk}" for pk in InvoiceType.objects.values_list("pk", flat=True)]
            InvoiceType.objects.update(is_active=True)
        self.assertTrue(mailbox)
        card = reverse("invoices:invoice_list") + "?ajouter=recuperer"
        self.client.force_login(self.user_b)
        page = self.client.get(card)
        self.assertContains(page, integrations.MAILBOX_TO_FILL)
        for code in mailbox:
            self.assertNotContains(page, f'value="{code}"')
        self.assertNotContains(page, 'value="bons-')
        self.type_the_mailbox(self.bar_b)
        page = self.client.get(card)
        self.assertNotContains(page, integrations.MAILBOX_TO_FILL)
        for code in mailbox:
            self.assertContains(page, f'value="{code}"')
        self.assertContains(page, 'value="bons-')
        # The owner's card is as it was, whatever his store holds.
        self.client.force_login(self.user_a)
        self.assertNotContains(self.client.get(card), integrations.MAILBOX_TO_FILL)

    def test_consignes_offers_another_bar_s_slips_once_its_mailbox_is_filled_in(self):
        self.client.force_login(self.user_b)
        page = self.client.get("/consignes/")
        self.assertContains(page, integrations.MAILBOX_TO_FILL)
        self.assertNotContains(page, f'action="{reverse("invoices:gather")}"')
        self.type_the_mailbox(self.bar_b)
        page = self.client.get("/consignes/")
        self.assertNotContains(page, integrations.MAILBOX_TO_FILL)
        self.assertContains(page, f'action="{reverse("invoices:gather")}"')
