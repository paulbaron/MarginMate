"""The invoice mailbox, open to every bar: what keeps it from hurting the
server and the other bars.

- In every espace a source's patterns pass the motif guard
  (returnables.patterns.check_invoice_mail_pattern, the audit of 04/10/2026)
  when saved and are matched with a timeout, case-sensitive as `re` matched
  them, a timeout making the search an incomplete one. Outside the platform
  owner's espace a refusal also names its field when saved, a pattern
  finding something in an empty text is refused, and one too slow on
  MAX_MAIL_TIMEOUTS mails stops its source; the owner's take « .* ».
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
from invoices.scrapers.generic_email import IncompleteSearch, find_matching_emails
from invoices.tests.test_email_search import fake_mailbox
from returnables import patterns
from returnables.patterns import PatternError
from returnables.tests.test_patterns import NeverCompile, SlowPattern
from tests.factories import make_invoice_type, make_supplier
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

    def test_a_6to4_address_is_judged_as_the_ipv4_it_wraps(self):
        """Python calls 2002::/16 global; a 6to4 relay takes 2002:c0a8:0101::1
        to 192.168.1.1."""
        for refused in ("2002:c0a8:0101::1", "2002:7f00:0001::1", "2002:0a00:0001::1", "2002:a9fe:a9fe::1"):
            with self.subTest(address=refused):
                self.assertFalse(egress.address_allowed(refused))
        # One wrapping a public IPv4 (8.8.8.8) is as public as it.
        self.assertTrue(egress.address_allowed("2002:0808:0808::1"))

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


class ByteBudgetTests(SimpleTestCase):
    """Outside the platform owner's espace the connection counts every byte
    the server sends - its literals (`read`) and its lines (`readline`) -
    against MAX_RESPONSE_BYTES per command's answer and MAX_SEARCH_BYTES per
    search: one FETCH of 150 messages of 49 MB each passed the cap on each
    literal and held 7 GB. Nothing is allocated: the stand-in's `read`
    returns a few bytes whatever it is asked."""

    class Fake:
        def __init__(self, host, ssl_context=None, timeout=None):
            self.sent = []

        def read(self, size):
            return b"x" * min(size, 4)

        def readline(self):
            return b"* 1 FETCH (UID 1)\r\n"

        def send(self, data):
            self.sent.append(data)

    MB = 1024 * 1024

    def connection(self, budgeted=True):
        with mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL", self.Fake):
            return generic_email._open_mailbox("imap.exemple.invalid", budgeted=budgeted)

    def test_one_answer_is_held_to_its_budget_and_each_command_starts_again(self):
        imap = self.connection()
        imap.read(40 * self.MB)
        imap.read(40 * self.MB)
        with self.assertRaisesMessage(generic_email.MessageTooBig, generic_email.RESPONSE_TOO_BIG):
            imap.read(30 * self.MB)
        imap = self.connection()
        imap.read(40 * self.MB)
        imap.read(40 * self.MB)
        imap.send(b"A2 FETCH 3 (BODY.PEEK[])\r\n")
        imap.read(40 * self.MB)
        self.assertEqual(imap.sent, [b"A2 FETCH 3 (BODY.PEEK[])\r\n"])

    def test_lines_count_too_however_short_each_is(self):
        imap = self.connection()
        with (
            mock.patch.object(generic_email, "MAX_RESPONSE_BYTES", 1_000),
            self.assertRaisesMessage(generic_email.MessageTooBig, generic_email.RESPONSE_TOO_BIG),
        ):
            for _line in range(1_000):
                imap.readline()

    def test_one_search_is_held_to_its_budget_across_commands(self):
        imap = self.connection()
        with self.assertRaisesMessage(generic_email.MessageTooBig, generic_email.SEARCH_TOO_BIG):
            for command in range(20):
                imap.send(b"A%d FETCH\r\n" % command)
                imap.read(45 * self.MB)

    def test_a_literal_past_the_cap_is_still_said_as_one(self):
        with self.assertRaisesMessage(generic_email.MessageTooBig, generic_email.MESSAGE_TOO_BIG):
            self.connection().read(generic_email.MAX_LITERAL_BYTES + 1)

    def test_the_owner_s_connection_counts_nothing(self):
        imap = self.connection(budgeted=False)
        for _command in range(20):
            imap.read(45 * self.MB)
        self.assertNotIsInstance(imap, generic_email._Budgeted)

    def test_sizes_are_read_before_or_after_the_header(self):
        answer = [
            (b"1 (RFC822.SIZE 1200 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {12}", b"From: a\r\n\r\n"),
            b")",
            (b"2 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {12}", b"From: b\r\n\r\n"),
            b" RFC822.SIZE 60000000)",
            (b"3 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {12}", b"From: c\r\n\r\n"),
            b")",
        ]
        self.assertEqual(generic_email._parse_sizes(answer), {b"1": 1200, b"2": 60_000_000})

    def test_phase_two_is_batched_by_size(self):
        ids = [b"1", b"2", b"3", b"4", b"5"]
        sizes = {b"1": 10 * self.MB, b"2": 25 * self.MB, b"3": 45 * self.MB, b"4": 1 * self.MB}
        # 4 is light, 5 says no size: fetched alone, as if at the cap.
        self.assertEqual(generic_email._batches_by_size(ids, sizes), [[b"1", b"2"], [b"3"], [b"4"], [b"5"]])
        light = [str(number).encode() for number in range(400)]
        batches = generic_email._batches_by_size(light, dict.fromkeys(light, 1_000))
        self.assertEqual([len(batch) for batch in batches], [150, 150, 100])


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
        patterns._invoice_mail_compiled.cache_clear()
        self.addCleanup(patterns._invoice_mail_compiled.cache_clear)
        with mock.patch.object(regex, "compile", new=never):
            with self.assertRaisesMessage(PatternError, "Motif de contenu : répétition trop grande"):
                patterns.invoice_mail_matcher("x{500}", field_label="Motif de contenu")
        self.assertEqual(never.calls, [])

    def test_the_shape_checked_is_the_tree_compiled_with_no_flag(self):
        """One implementation for every espace (GitHub's main's audit rule):
        the motif guard walks the tree `regex` builds with the flags it is
        compiled with - none, `re`'s meaning."""
        patterns._invoice_mail_compiled.cache_clear()
        self.addCleanup(patterns._invoice_mail_compiled.cache_clear)
        with mock.patch.object(patterns, "_check_shape", wraps=patterns._check_shape) as shape:
            compiled = patterns.check_invoice_mail_pattern("Facture forme essai")
        shape.assert_called_once_with("Facture forme essai", 0)
        self.assertEqual(compiled.flags & (regex.IGNORECASE | regex.MULTILINE), 0)

    def test_a_pattern_keeping_every_mail_is_refused_only_when_told_why(self):
        """The owner's « .* » is accepted, as `re` took it; another bar's
        caller passes its field's sentence, and it is refused with it."""
        self.assertIsNotNone(patterns.check_invoice_mail_pattern(".*"))
        self.assertIsNotNone(patterns.invoice_mail_matcher("a?").search(""))
        reason = EmailInvoiceSource.EMPTY_MATCH_REASONS["sender_pattern"]
        with self.assertRaisesMessage(PatternError, f"Motif d'expéditeur : {reason}."):
            patterns.invoice_mail_matcher(".*", field_label="Motif d'expéditeur", empty_reason=reason)
        self.assertTrue(patterns.invoice_mail_matcher("@", empty_reason=reason).search("f@exemple.invalid"))

    def test_a_timeout_is_counted_and_never_stops_the_owner_s_source(self):
        said = []
        matcher = patterns.InvoiceMailMatcher(SlowPattern(), log=said.append)
        with self.assertLogs("returnables.patterns", level="WARNING"):
            for text in ("un", "deux", "trois", "quatre"):
                self.assertIsNone(matcher.search(text))
        self.assertEqual(matcher.timed_out, 4)
        self.assertEqual(len(said), 4)

    def test_slow_on_three_mails_stops_another_bar_s_source(self):
        said = []
        slow = SlowPattern()
        matcher = patterns.InvoiceMailMatcher(
            slow, log=said.append, max_timeouts=patterns.MAX_MAIL_TIMEOUTS, field_label="Motif de contenu"
        )
        with self.assertLogs("returnables.patterns", level="WARNING"):
            self.assertIsNone(matcher.search("un"))
            self.assertIsNone(matcher.search("deux"))
            with self.assertRaisesMessage(PatternError, "Motif de contenu : motif trop lent sur ces mails"):
                matcher.search("trois")
        self.assertEqual((matcher.timed_out, len(said)), (3, 3))
        # GitHub's main's limits for an invoice source, in every espace.
        self.assertEqual(slow.calls[0][1], {"timeout": patterns.INVOICE_MAIL_TIMEOUT, "concurrent": True})


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

    def beta_s_sources(self):
        """A new hosted espace starts without the original bar's mailbox
        source and slip format (invoices.seeds): Beta sets up its own."""
        from returnables.tests.support import make_format

        with bound_tenant(self.bar_b):
            supplier = make_supplier(code="GROSSISTE_B", name="Grossiste Beta")
            make_invoice_type(supplier=supplier, name="Grossiste Beta - Factures")
            make_format(name="Grossiste Beta — bon du livreur", supplier=supplier)

    def search(self, tenant, message=None, **patterns_given):
        """A search in `tenant`'s mailbox: Beta's typed on its
        « Identifiants » (type_the_mailbox), the owner's from the settings,
        as his .env gives them."""
        with (
            bound_tenant(tenant),
            override_settings(INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_EMAIL_APP_PASSWORD="x"),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
            mock.patch.object(egress, "resolve", answers(PUBLIC)),
        ):
            fake_mailbox(client, message or a_mail())
            return find_matching_emails(START, END, log=lambda line: None, **patterns_given), client

    def search_slow(self, tenant, mails: int):
        """A search over `mails` mails whose body pattern times out on every
        one of them (simulated: SlowOn on their body « Votre document. »,
        never a real catastrophic match)."""
        from invoices.tests.test_email_search import FakeMailbox, SlowOn, dated_mail

        real = patterns.check_invoice_mail_pattern

        def check(text, **kwargs):
            compiled = real(text, **kwargs)
            return SlowOn(compiled, "document") if text == "Votre" else compiled

        boxes = {
            str(n).encode(): dated_mail(date(2026, 2, n), f"facture-{n}", sender="f@traiteur.invalid")
            for n in range(1, mails + 1)
        }
        with (
            bound_tenant(tenant),
            override_settings(INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_EMAIL_APP_PASSWORD="x"),
            mock.patch("invoices.scrapers.generic_email.BATCH_SIZE", 1),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
            mock.patch.object(egress, "resolve", answers(PUBLIC)),
            mock.patch("returnables.patterns.check_invoice_mail_pattern", check),
            self.assertLogs("returnables.patterns", level="WARNING"),
        ):
            FakeMailbox(boxes).install(client)
            return find_matching_emails(START, END, "traiteur", body_pattern="Votre", log=lambda line: None)

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

    def test_every_espace_s_patterns_are_guarded_and_case_sensitive(self):
        """GitHub's main's audit rule, the owner's espace included: checked
        by the motif guard before anything compiles them or signs in."""
        self.type_the_mailbox(self.bar_b)
        for tenant in (self.bar_b, self.bar_a):
            with self.subTest(tenant=tenant.name):
                self.assertEqual(self.search(tenant, sender_pattern="TRAITEUR")[0], [])
                body = "x" * 2_000 + " Référence MOTCLE"
                self.assertEqual(
                    len(self.search(tenant, a_mail(body=body), sender_pattern="traiteur", body_pattern="MOTCLE")[0]),
                    1,
                )
                never = NeverCompile()
                patterns._invoice_mail_compiled.cache_clear()
                self.addCleanup(patterns._invoice_mail_compiled.cache_clear)
                with (
                    mock.patch.object(regex, "compile", new=never),
                    self.assertRaisesMessage(PatternError, "Motif d'expéditeur : répétition trop grande"),
                ):
                    self.search(tenant, sender_pattern="x{500}")
                self.assertEqual(never.calls, [])

    def test_a_pattern_slow_on_mails_stops_another_bar_s_source_only(self):
        """A timeout is no « no match » in either espace (GitHub's main): the
        owner's search ends incomplete, every slow mail counted, to be
        searched again; another bar's source is stopped at the
        MAX_MAIL_TIMEOUTS-th, naming its pattern - the gather's thread is the
        one process every bar runs in."""
        self.type_the_mailbox(self.bar_b)
        with self.assertRaises(IncompleteSearch) as incomplete:
            self.search_slow(self.bar_a, 4)
        self.assertEqual((incomplete.exception.slow, incomplete.exception.matches), (4, []))
        with self.assertRaises(IncompleteSearch) as incomplete:
            self.search_slow(self.bar_b, patterns.MAX_MAIL_TIMEOUTS - 1)
        self.assertEqual(incomplete.exception.slow, patterns.MAX_MAIL_TIMEOUTS - 1)
        with self.assertRaisesMessage(PatternError, "Motif de contenu : motif trop lent sur ces mails"):
            self.search_slow(self.bar_b, patterns.MAX_MAIL_TIMEOUTS + 1)

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
        # Named as the source's form names it: which of four patterns failed,
        # and the source's to correct (GitHub's main's words), not the mailbox's.
        self.assertEqual(
            job.progress[f"type-{source_type.pk}"]["error"],
            "Motif de la source à corriger : Motif d'expéditeur : répétition trop grande : 100 fois au plus.",
        )

    def test_each_pattern_is_named_in_every_espace(self):
        self.type_the_mailbox(self.bar_b)
        for tenant in (self.bar_b, self.bar_a):
            for given, said in (
                ({"sender_pattern": "traiteur", "body_pattern": "x{500}"}, "Motif de contenu :"),
                ({"sender_pattern": "traiteur", "subject_pattern": "("}, "Motif d'objet :"),
                ({"sender_pattern": "traiteur", "attachment_pattern": "x{500}"}, "Motif de pièce jointe :"),
            ):
                with self.subTest(tenant=tenant.name, given=given), self.assertRaises(PatternError) as refused:
                    self.search(tenant, **given)
                self.assertTrue(str(refused.exception).startswith(said), str(refused.exception))
            # Stripped, as the source's form strips what it is typed
            # (check_invoice_mail_pattern, GitHub's main's rule).
            self.assertEqual(
                len(self.search(tenant, sender_pattern="traiteur", subject_pattern="Votre facture ")[0]), 1
            )

    def test_a_pattern_keeping_every_mail_is_told_how_to_say_so(self):
        with bound_tenant(self.bar_b):
            supplier = make_supplier(code="TRAITEUR", name="Traiteur", parser_key="")
            source_type = InvoiceType.objects.create(name="Traiteur - Factures", supplier=supplier)
            source = EmailInvoiceSource(invoice_type=source_type, sender_pattern=".*", subject_pattern="a?")
            with self.assertRaises(ValidationError) as refused:
                source.full_clean()
            errors = refused.exception.message_dict
            self.assertIn("écrivez @", errors["sender_pattern"][0])
            self.assertTrue(errors["sender_pattern"][0].startswith("Motif d'expéditeur : le motif retient"))
            self.assertIn("laissez le champ vide pour ne pas filtrer", errors["subject_pattern"][0])
            self.assertNotIn("ligne", errors["sender_pattern"][0])
            EmailInvoiceSource(invoice_type=source_type, sender_pattern="@").full_clean()
        # The owner's « .* » is `re`'s, as GitHub's main's rule takes it.
        with bound_tenant(self.bar_a):
            supplier = make_supplier(code="TRAITEUR", name="Traiteur", parser_key="")
            source_type = InvoiceType.objects.create(name="Traiteur - Factures", supplier=supplier)
            EmailInvoiceSource(invoice_type=source_type, sender_pattern=".*", subject_pattern="a?").full_clean()

    def test_a_search_keeping_every_mail_is_refused_before_signing_in_in_another_bar_only(self):
        self.type_the_mailbox(self.bar_b)
        with self.assertRaises(PatternError) as refused:
            self.search(self.bar_b, sender_pattern=".*")
        self.assertIn("pour tous les expéditeurs, écrivez @", str(refused.exception))
        self.assertEqual(len(self.search(self.bar_a, sender_pattern=".*")[0]), 1)

    def test_another_bar_s_search_asks_the_sizes_and_passes_over_a_message_too_big(self):
        self.type_the_mailbox(self.bar_b)
        message = a_mail()
        headers = message.split(b"\n\n", 1)[0] + b"\n\n"
        said = []
        with (
            bound_tenant(self.bar_b),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
            mock.patch.object(egress, "resolve", answers(PUBLIC)),
        ):
            client.return_value.search.return_value = ("OK", [b"1 2"])
            client.return_value.fetch.side_effect = [
                (
                    "OK",
                    [
                        (
                            b"1 (RFC822.SIZE 60000000 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {%d}" % len(headers),
                            headers,
                        ),
                        b")",
                        (b"2 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {%d}" % len(headers), headers),
                        b" RFC822.SIZE %d)" % len(message),
                    ],
                ),
                ("OK", [(b"2 (BODY[] {%d}" % len(message), message), b")"]),
            ]
            matches = find_matching_emails(START, END, "traiteur", log=said.append)
        self.assertEqual([match.message_id for match in matches], [b"2"])
        first, second = client.return_value.fetch.call_args_list
        self.assertEqual(first.args[1], generic_email.SIZED_HEADER_QUERY)
        self.assertEqual(second.args[0], b"2")
        self.assertIn(generic_email.TOO_BIG_SKIPPED.format(subject="Votre facture"), said)

    def test_the_owner_s_search_asks_what_it_always_asked(self):
        with (
            bound_tenant(self.bar_a),
            override_settings(INVOICE_EMAIL_ADDRESS="f@exemple.invalid", INVOICE_EMAIL_APP_PASSWORD="x"),
            mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
        ):
            fake_mailbox(client, a_mail())
            find_matching_emails(START, END, "traiteur", log=lambda line: None)
        self.assertEqual(client.return_value.fetch.call_args_list[0].args[1], generic_email.HEADER_QUERY)

    def test_a_library_s_words_never_reach_another_bar_s_card(self):
        import imaplib
        import ssl

        self.type_the_mailbox(self.bar_b)
        refused = imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'")
        with bound_tenant(self.bar_b):
            supplier = make_supplier(code="TRAITEUR_B", name="Traiteur Beta", parser_key="")
            source_type = InvoiceType.objects.create(
                name="Traiteur Beta - Factures", supplier=supplier, source_kind=InvoiceType.SourceKind.EMAIL
            )
            EmailInvoiceSource.objects.create(invoice_type=source_type, sender_pattern="traiteur")
            job = ScrapeJob.objects.create()
            from invoices.tasks import gather_invoices_task

            with (
                mock.patch("invoices.scrapers.generic_email.imaplib.IMAP4_SSL") as client,
                mock.patch.object(egress, "resolve", answers(PUBLIC)),
                mock.patch("invoices.tasks._GatherHeartbeat"),
            ):
                client.return_value.login.side_effect = refused
                gather_invoices_task(job.pk, START, END, {f"type-{source_type.pk}"})
            job.refresh_from_db()
        error = job.progress[f"type-{source_type.pk}"]["error"]
        self.assertEqual(error, f"Boîte mail : {generic_email.LOGIN_REFUSED}")
        self.assertNotIn("AUTHENTICATIONFAILED", job.log)
        # Each kind its sentence; the app's own refusals as they are.
        with bound_tenant(self.bar_b):
            for exc, sentence in (
                (imaplib.IMAP4.abort("socket error: EOF"), generic_email.CONNECTION_CUT),
                (ssl.SSLCertVerificationError("certificate verify failed"), generic_email.CERTIFICATE_REFUSED),
                (ssl.SSLError("wrong version number"), generic_email.TLS_FAILED),
                (TimeoutError("timed out"), generic_email.NO_ANSWER),
                (ConnectionRefusedError("[WinError 10061]"), generic_email.CONNECTION_REFUSED),
                (socket.gaierror("getaddrinfo failed"), generic_email.SERVER_NOT_FOUND),
                (OSError("Network is unreachable"), generic_email.NETWORK_FAILED),
                (RuntimeError(generic_email.MAILBOX_MISSING), generic_email.MAILBOX_MISSING),
                (RuntimeError("something internal"), "Erreur inattendue sur le serveur"),
                (ValueError("C:\\Serveur\\x.py"), "Erreur inattendue sur le serveur"),
                (egress.EgressRefused("Le serveur IMAP « x » …"), "Le serveur IMAP « x » …"),
                (generic_email.MessageTooBig(generic_email.SEARCH_TOO_BIG), generic_email.SEARCH_TOO_BIG),
                # GitHub's main's: a search the server refused or answered in
                # part is the app's own sentence, never « erreur inattendue ».
                (
                    generic_email.SearchRefused(generic_email.SEARCH_REFUSED.format(status="NO")),
                    "Recherche refusée par le serveur mail (NO).",
                ),
                (generic_email.IncompleteSearch(2, []), "Recherche incomplète : 2 e-mail(s) non lu(s)"),
            ):
                with self.subTest(exc=exc):
                    self.assertTrue(generic_email.failure_said(exc).startswith(sentence))
        # The owner reads the exception's own words, as always.
        with bound_tenant(self.bar_a):
            self.assertEqual(generic_email.failure_said(refused), str(refused))

    def test_saving_every_espace_s_patterns_goes_through_the_guard(self):
        """One check in both espaces (GitHub's main's audit rule), a syntax
        error said in French by the guard; another bar's refusal names its
        field, the owner's says « Expression régulière invalide » as GitHub's
        main wrote it."""
        for tenant, label in ((self.bar_b, "Motif d'expéditeur"), (self.bar_a, "Expression régulière invalide")):
            with bound_tenant(tenant):
                supplier = make_supplier(code="TRAITEUR", name="Traiteur", parser_key="")
                source_type = InvoiceType.objects.create(name="Traiteur - Factures", supplier=supplier)
                source = EmailInvoiceSource(invoice_type=source_type, sender_pattern="x{500}", body_pattern="(")
                with self.assertRaises(ValidationError) as refused:
                    source.full_clean()
            errors = refused.exception.message_dict
            self.assertEqual(errors["sender_pattern"], [f"{label} : répétition trop grande : 100 fois au plus."])
            body_label = "Motif de contenu" if tenant == self.bar_b else label
            self.assertEqual(errors["body_pattern"], [f"{body_label} : parenthèse non fermée (position 1)."])

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
        self.beta_s_sources()
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
        # Nothing to gather: no form answering « Aucune source cochée », and
        # no « Aucune source configurée » beside sources it has.
        self.assertNotContains(page, "Récupérer les nouvelles factures")
        self.assertNotContains(page, "Aucune source configurée")
        self.assertContains(page, integrations.PORTALS)
        self.type_the_mailbox(self.bar_b)
        page = self.client.get(card)
        self.assertNotContains(page, integrations.MAILBOX_TO_FILL)
        self.assertContains(page, "Récupérer les nouvelles factures")
        for code in mailbox:
            self.assertContains(page, f'value="{code}"')
        self.assertContains(page, 'value="bons-')
        # The owner's card is as it was, whatever his store holds.
        self.client.force_login(self.user_a)
        self.assertNotContains(self.client.get(card), integrations.MAILBOX_TO_FILL)

    def test_a_test_of_real_tenants_never_resolves_a_name_for_real(self):
        """TenancyTestCase refuses a lookup of a name as NoNetworkTestCase
        does: every hosted mailbox test patches egress.resolve, and one that
        forgot would have asked the network."""
        with self.assertRaises(AssertionError):
            socket.getaddrinfo("imap.exemple.invalid", 993)
        self.assertTrue(socket.getaddrinfo("127.0.0.1", 80))

    def test_the_achats_page_reads_the_store_once(self):
        """The gather card and the mailbox offered are asked of ONE reading of
        another bar's « Identifiants » (pages are measured)."""
        self.type_the_mailbox(self.bar_b)
        self.client.force_login(self.user_b)
        with mock.patch("accounts.vault.load", wraps=vault.load) as load:
            page = self.client.get(reverse("invoices:invoice_list") + "?ajouter=recuperer")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(load.call_count, 1)

    def test_consignes_shows_a_running_gather_whatever_the_mailbox(self):
        """A gather started before the mailbox was emptied on « Identifiants »
        is still shown, and holds the button."""
        with bound_tenant(self.bar_b):
            job = ScrapeJob.objects.create(
                status=ScrapeJob.Status.RUNNING, progress={"bons-1": {"label": "Bons", "found": 0}}
            )
        self.client.force_login(self.user_b)
        page = self.client.get("/consignes/")
        self.assertContains(page, integrations.MAILBOX_TO_FILL)
        self.assertContains(page, reverse("invoices:gather_status", args=[job.pk]))

    def test_another_bar_tests_one_source_at_a_time(self):
        self.type_the_mailbox(self.bar_b)
        with bound_tenant(self.bar_b):
            supplier = make_supplier(code="TRAITEUR_B", name="Traiteur Beta", parser_key="")
        create = reverse("invoices:invoice_type_create")
        data = {
            "name": "Traiteur Beta - Factures",
            "supplier": str(supplier.pk),
            "source_kind": "EMAIL",
            "parser_key": "",
            "is_active": "on",
            "sender_pattern": "traiteur",
            "attachment_pattern": r"\.pdf$",
            "action": "test",
        }
        self.client.force_login(self.user_b)
        with bound_tenant(self.bar_b):
            running = ScrapeJob.objects.create(kind=ScrapeJob.Kind.TEST, status=ScrapeJob.Status.RUNNING)
        with mock.patch("invoices.views.threading.Thread") as thread:
            response = self.client.post(create, data)
        thread.assert_not_called()
        self.assertContains(response, "Un test de source est déjà en cours")
        with bound_tenant(self.bar_b):
            self.assertEqual(ScrapeJob.objects.filter(kind=ScrapeJob.Kind.TEST).count(), 1)
            # One whose thread died says nothing any more and holds nothing.
            ScrapeJob.objects.filter(pk=running.pk).update(
                started_at=running.started_at - ScrapeJob.STALE_AFTER - ScrapeJob.STALE_AFTER
            )
        with mock.patch("invoices.views.threading.Thread") as thread:
            self.client.post(create, data)
        thread.assert_called_once()
        # The owner's tests run as they always did, one beside the other.
        with bound_tenant(self.bar_a):
            owner_supplier = make_supplier(code="TRAITEUR_A", name="Traiteur Alpha", parser_key="")
            ScrapeJob.objects.create(kind=ScrapeJob.Kind.TEST, status=ScrapeJob.Status.RUNNING)
        self.client.force_login(self.user_a)
        with mock.patch("invoices.views.threading.Thread") as thread:
            self.client.post(create, {**data, "supplier": str(owner_supplier.pk)})
        thread.assert_called_once()

    def test_an_archive_s_source_goes_through_the_guard_in_every_espace(self):
        from transfer.archive import ArchiveReader
        from transfer.sections.base import Strategy
        from transfer.tests.support import forge, import_archive

        def archive(sender):
            payload = {
                "supplier_names": {"TRAITEUR_B": "Traiteur Beta"},
                "sources": [
                    {
                        "supplier": "TRAITEUR_B",
                        "name": "Traiteur Beta - Factures",
                        "parser_key": "",
                        "source_kind": "EMAIL",
                        "is_active": True,
                        "email": {
                            "sender_pattern": sender,
                            "subject_pattern": "",
                            "body_pattern": "",
                            "attachment_pattern": r"\.pdf$",
                        },
                        "website": None,
                    }
                ],
            }
            reader = ArchiveReader(forge({"sources": payload}))
            self.addCleanup(reader.close)
            return reader

        for tenant, said in ((self.bar_b, "Motif d'expéditeur"), (self.bar_a, "Expression régulière invalide")):
            with self.subTest(tenant=tenant.name), bound_tenant(tenant):
                make_supplier(code="TRAITEUR_B", name="Traiteur Beta", parser_key="")
                report = import_archive(archive("x{500}"), {"sources": Strategy.MERGE}).section("sources")
                imported = InvoiceType.objects.filter(name="Traiteur Beta - Factures").exists()
                # Refused in both since GitHub's main's audit, each in its words.
                self.assertFalse(imported)
                self.assertEqual(len(report.skipped), 1, report.skipped)
                self.assertIn(f"{said} : répétition trop grande", report.skipped[0])

    def test_consignes_offers_another_bar_s_slips_once_its_mailbox_is_filled_in(self):
        self.beta_s_sources()
        self.client.force_login(self.user_b)
        page = self.client.get("/consignes/")
        self.assertContains(page, integrations.MAILBOX_TO_FILL)
        self.assertNotContains(page, f'action="{reverse("invoices:gather")}"')
        self.type_the_mailbox(self.bar_b)
        page = self.client.get("/consignes/")
        self.assertNotContains(page, integrations.MAILBOX_TO_FILL)
        self.assertContains(page, f'action="{reverse("invoices:gather")}"')
