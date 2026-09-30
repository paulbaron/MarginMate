"""« Personnel » with several tenants (multi mode, accounts/tenancy.py): each
bar signs with an authority of its own, in a private folder of its own, and
an employee's public link binds the tenant that issued it - never another.

Two real tenants in temporary files (accounts.tests.support.TwoTenantsTestCase),
each with its establishment, one employee and a June sent for signature.
Both databases number their rows from 1: A's employee and B's are both
pk 1, their requests too - exactly what a shared folder (keys/employees/1)
or a page reading the wrong database would mix up without a word. Keys,
files and timestamps offline (signing_support); every name INVENTED."""

import datetime as dt
import io
import re
import shutil
from datetime import date
from decimal import Decimal
from html import unescape
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.x509.oid import NameOID
from django.contrib.auth.models import AnonymousUser
from django.core import mail
from django.core.management import CommandError, call_command
from django.db import connections
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse

from accounts import paths, provisioning
from accounts.models import SigningLink
from accounts.tenancy import NoTenantBound, bound_tenant, current_tenant
from accounts.tests.support import TwoTenantsTestCase
from invoices.integrations import TO_CONFIGURE
from staff import (
    checks,
    private_files,
    public_views,
    signature_deletion,
    signature_mail,
    signature_views,
    signing,
)
from staff import (
    signature_requests as requests_,
)
from staff.models import Establishment, SignatureEvent, SignatureRequest
from staff.tests.page_forms import as_post, form_posting_to
from staff.tests.signing_support import OfflineTimestamps, data_url, drawn_signature, employer_signature
from staff.tests.support import employee
from staff.timesheet import PostedDay, save_month
from tests.support import _Forbidden

JUNE = date(2026, 6, 1)
MAY_2021 = date(2021, 5, 1)
IP = "203.0.113.7"  # TEST-NET-3: an address that belongs to nobody
PHONE = "Mozilla/5.0 (Linux; Android 14) Essai/1.0"
LINK = "https://bar.example.invalid/personnel/signer/jeton-d-essai/"
MAIL = override_settings(EMAIL_HOST="smtp.example.invalid", DEFAULT_FROM_EMAIL="plateforme@example.invalid")

Status = SignatureRequest.Status
Kind = SignatureEvent.Kind
HANDED_OVER = SignatureRequest.Identification.CODE_HANDED_OVER

#: What each tenant holds - and what must never show in the other's.
ALPHA = {
    "establishment": "BAR ALPHA",
    "last_name": "Dupont",
    "first_name": "Jeanne",
    "shown": "DUPONT Jeanne",
    "note": "inventaire alpha",
    "email": "jeanne.dupont@example.invalid",
}
BETA = {
    "establishment": "BAR BETA",
    "last_name": "Martin",
    "first_name": "Paul",
    "shown": "MARTIN Paul",
    "note": "livraison beta",
    "email": "paul.martin@example.invalid",
}


def _text(response) -> str:
    """What a page reads as: the tags out, the entities read, the spaces single."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", response.content.decode())).split())


class TenantsCase(TwoTenantsTestCase):
    """Bar Alpha and Bar Beta, each with its employee's June sent for
    signature (`self.alpha` / `self.beta`: the person, the request, the
    link's token). No network: the timestamps are offline, and anything
    reaching for a mail server or a timestamp server fails loudly
    (tests.support.NoNetworkTestCase's guards - that class is a TestCase,
    and binding a tenant inside a TestCase's transaction is refused)."""

    def setUp(self):
        super().setUp()
        for target, label in (
            ("smtplib.SMTP", "SMTP"),
            ("smtplib.SMTP_SSL", "SMTP"),
            (
                "pyhanko.sign.timestamps.requests_client.RequestsHTTPTimeStamper.async_request_tsa_response",
                "timestamp server (RFC 3161)",
            ),
        ):
            patcher = mock.patch(target, new=_Forbidden(label))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.enterContext(override_settings(MARGINMATE_SIGNING_PASSPHRASE=""))
        self.timestamps = OfflineTimestamps().start()
        self.addCleanup(self.timestamps.stop)
        self.alpha = self.fill(self.bar_a, ALPHA)
        self.beta = self.fill(self.bar_b, BETA)

    def fill(self, tenant, facts, month=JUNE, now=None):
        with bound_tenant(tenant):
            if not Establishment.objects.exists():
                Establishment.objects.create(pk=Establishment.SINGLETON_PK, name=facts["establishment"])
            person = employee(last_name=facts["last_name"], first_name=facts["first_name"])
            person.email = facts["email"]
            person.save()
            save_month(
                person, month, [PostedDay(date(month.year, month.month, 2), hours=Decimal("9"), note=facts["note"])]
            )
            request, token = requests_.create_request(person, month, now=now)
        return {"person": person, "request": request, "token": token}

    def signed_by_the_employee(self, tenant, request):
        with bound_tenant(tenant):
            session = {}
            requests_.check_code(request, requests_.issue_code(request, HANDED_OVER), session)
            return requests_.sign_for_employee(request, drawn_signature(), session=session, statement_accepted=True)

    def private(self, tenant) -> Path:
        return paths.tenant_dir(tenant) / paths.PRIVATE

    def indexed(self, token):
        """The tenant the accounts database files this token's link under, or None."""
        link = SigningLink.objects.filter(token_hash=requests_.hash_token(token)).first()
        return link.tenant if link else None


# -- The private folder ---------------------------------------------------------------------------------------


class PrivateFolderPerTenantTests(TenantsCase):
    def test_both_databases_number_from_one(self):
        """What every test below relies on: the same pks on both sides."""
        self.assertEqual(self.alpha["person"].pk, self.beta["person"].pk)
        self.assertEqual(self.alpha["request"].pk, self.beta["request"].pk)

    def test_each_tenant_keeps_its_signature_files_in_its_own_folder(self):
        for tenant, mine, other in ((self.bar_a, self.alpha, self.beta), (self.bar_b, self.beta, self.alpha)):
            with self.subTest(tenant=tenant.name), bound_tenant(tenant):
                self.assertEqual(private_files.private_dir(), self.private(tenant).resolve())
                folder = self.private(tenant) / "signatures"
                self.assertTrue((folder / str(mine["request"].uuid) / "document.pdf").is_file())
                self.assertFalse((folder / str(other["request"].uuid)).exists())

    def test_unbound_there_is_no_private_folder_at_all(self):
        """A folder every bar shared is how one would read another's keys."""
        for call in (private_files.private_dir, private_files.keys_dir, private_files.keys_folder):
            with self.subTest(call=call.__name__), self.assertRaises(NoTenantBound):
                call()

    def test_each_tenant_keeps_its_own_deletions_log(self):
        with bound_tenant(self.bar_a):
            signature_deletion.delete_signature_request(self.alpha["request"], how=signature_deletion.PAGE, ip=IP)
            (record,) = private_files.read_deletion_records()
            self.assertEqual(record["employee"], ALPHA["shown"])
        self.assertTrue((self.private(self.bar_a) / private_files.DELETIONS_LOG).is_file())
        self.assertFalse((self.private(self.bar_b) / private_files.DELETIONS_LOG).exists())
        with bound_tenant(self.bar_b):
            self.assertEqual(private_files.read_deletion_records(), [])


# -- Each tenant its own signing authority --------------------------------------------------------------------


class AuthorityPerTenantTests(TenantsCase):
    def setUp(self):
        super().setUp()
        self.signed_by_the_employee(self.bar_a, self.alpha["request"])
        self.signed_by_the_employee(self.bar_b, self.beta["request"])

    def certificate(self, tenant, stem) -> x509.Certificate:
        path = self.private(tenant) / "keys" / f"{stem}.cert.pem"
        return x509.load_pem_x509_certificate(path.read_bytes())

    @staticmethod
    def names(certificate) -> tuple[str, str]:
        subject = certificate.subject
        return (
            subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value,
            subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value,
        )

    def test_each_tenant_signs_under_its_own_authority(self):
        self.assertEqual(
            self.names(self.certificate(self.bar_a, "authority")), ("Autorité interne de BAR ALPHA", "BAR ALPHA")
        )
        self.assertEqual(
            self.names(self.certificate(self.bar_b, "authority")), ("Autorité interne de BAR BETA", "BAR BETA")
        )
        with bound_tenant(self.bar_a):
            alpha = signing.authority_fingerprint()
            recorded = self.alpha["request"].events.get(kind=Kind.EMPLOYEE_SIGNED).detail["authority_sha256"]
        with bound_tenant(self.bar_b):
            beta = signing.authority_fingerprint()
        self.assertTrue(alpha and beta)
        self.assertNotEqual(alpha, beta)
        # Each signature recorded the authority of its own tenant.
        self.assertEqual(recorded, alpha)

    def test_employee_one_of_each_tenant_has_his_own_certificate_and_nothing_is_archived(self):
        """Shared, employees/1 of A would have been archived and reissued the
        moment B's employee 1 signed, under B's name."""
        self.assertEqual(self.names(self.certificate(self.bar_a, "employees/1")), (ALPHA["shown"], "BAR ALPHA"))
        self.assertEqual(self.names(self.certificate(self.bar_b, "employees/1")), (BETA["shown"], "BAR BETA"))
        for tenant in (self.bar_a, self.bar_b):
            with self.subTest(tenant=tenant.name):
                self.assertFalse((self.private(tenant) / "keys" / "archive").exists())

    def test_verify_trusts_only_the_tenant_s_own_authority(self):
        with bound_tenant(self.bar_b):
            data = private_files.read(self.beta["request"].uuid, private_files.EMPLOYEE_SIGNED)
            at_home = signing.verify(data)
        self.assertTrue(at_home.ok, at_home.verdict)
        with bound_tenant(self.bar_a):
            elsewhere = signing.verify(data)
        (check,) = elsewhere.signatures
        self.assertTrue(check.intact)
        self.assertFalse(check.trusted)
        self.assertFalse(elsewhere.ok)
        self.assertIn("ne se rattache à aucune autorité connue", elsewhere.verdict)

    def test_the_employer_countersigns_under_his_own_tenant(self):
        with bound_tenant(self.bar_b):
            done = requests_.countersign_request(self.beta["request"], employer_signature())
            final = private_files.read(done.uuid, private_files.FINAL)
            self.assertTrue(signing.verify(final).ok)
            self.assertEqual(signing.employer_identity(Establishment.objects.get()).name, "BAR BETA")
        self.assertFalse((self.private(self.bar_a) / "keys" / f"{signing.EMPLOYER_STEM}.cert.pem").exists())


# -- The link's index -----------------------------------------------------------------------------------------


class LinkIndexTests(TenantsCase):
    def test_issuing_a_link_files_it_under_its_tenant(self):
        self.assertEqual(self.indexed(self.alpha["token"]), self.bar_a)
        self.assertEqual(self.indexed(self.beta["token"]), self.bar_b)
        self.assertEqual(SigningLink.objects.count(), 2)

    def test_a_new_link_takes_the_old_one_s_place(self):
        old = self.alpha["token"]
        with bound_tenant(self.bar_a):
            new = requests_.renew_link(self.alpha["request"])
        self.assertIsNone(self.indexed(old))
        self.assertEqual(self.indexed(new), self.bar_a)
        self.assertEqual(self.indexed(self.beta["token"]), self.bar_b)

    def test_a_cancelled_request_keeps_its_link_so_the_page_says_it_was_cancelled(self):
        """The page answers « annulée par l'employeur » (410) - forgotten,
        the link would read « copié en entier ? » (404)."""
        with bound_tenant(self.bar_a):
            requests_.cancel_request(self.alpha["request"], "envoyé par erreur")
        self.assertEqual(self.indexed(self.alpha["token"]), self.bar_a)
        response = Client().get(reverse("staff:sign", args=[self.alpha["token"]]))
        self.assertEqual(response.status_code, 410)
        self.assertIn(requests_.CANCELLED_LINK, _text(response))

    def test_a_superseded_request_keeps_its_link_so_the_page_says_so(self):
        with bound_tenant(self.bar_a):
            self.signed_by_the_employee(self.bar_a, self.alpha["request"])
            requests_.countersign_request(self.alpha["request"], employer_signature())
            requests_.reopen_month(self.alpha["request"].timesheet)
        self.assertEqual(self.indexed(self.alpha["token"]), self.bar_a)
        response = Client().get(reverse("staff:sign", args=[self.alpha["token"]]))
        self.assertEqual(response.status_code, 410)
        self.assertIn(requests_.SUPERSEDED_LINK, _text(response))

    def test_deleting_a_version_forgets_its_link(self):
        with bound_tenant(self.bar_a):
            signature_deletion.delete_signature_request(self.alpha["request"], how=signature_deletion.PAGE)
        self.assertIsNone(self.indexed(self.alpha["token"]))
        self.assertEqual(self.indexed(self.beta["token"]), self.bar_b)
        response = Client().get(reverse("staff:sign", args=[self.alpha["token"]]))
        self.assertEqual(response.status_code, 404)
        self.assertIn(requests_.UNKNOWN_LINK, _text(response))

    def test_a_refused_deletion_keeps_the_link(self):
        with bound_tenant(self.bar_a), self.assertRaises(signature_deletion.DeletionRefused):
            signature_deletion.delete_signature_request(
                self.alpha["request"], how=signature_deletion.PAGE, expected={"status": Status.COMPLETE}
            )
        self.assertEqual(self.indexed(self.alpha["token"]), self.bar_a)

    def test_the_index_is_rebuilt_from_the_tenant_s_own_requests(self):
        """After a database copy was put back (or a tenant adopted): the
        requests it holds are indexed, the hashes it no longer holds go - and
        nothing of the other tenant is touched."""
        SigningLink.objects.filter(tenant=self.bar_a).delete()
        SigningLink.objects.create(token_hash=requests_.hash_token("jeton-perime-essai"), tenant=self.bar_a)
        with bound_tenant(self.bar_a):
            self.assertEqual(requests_.index_links(), (1, 1))
            self.assertEqual(requests_.index_links(), (0, 0))
        self.assertEqual(self.indexed(self.alpha["token"]), self.bar_a)
        self.assertIsNone(self.indexed("jeton-perime-essai"))
        self.assertEqual(self.indexed(self.beta["token"]), self.bar_b)

    def test_a_database_put_back_from_a_copy_needs_its_links_indexed_again(self):
        """The operator's procedure (CLAUDE.md, « Données »): the copy's
        requests are not the ones the index knows. A version deleted since
        the copy is back in the database, its link forgotten: « lien
        inconnu » (404) until `tenant <folder> staff_index_links` - which
        reads the requests in the copy's schema, so after `migrate_tenants`."""
        database = paths.tenant_database(self.bar_a)
        copy = paths.tenant_dir(self.bar_a) / paths.BACKUPS / "copie-essai.sqlite3"
        provisioning.copy_database(database, copy)
        with bound_tenant(self.bar_a):
            signature_deletion.delete_signature_request(self.alpha["request"], how=signature_deletion.PAGE)
        link = reverse("staff:sign", args=[self.alpha["token"]])
        self.assertEqual(Client().get(link).status_code, 404)

        connections.close_all()
        for side in (Path(f"{database}-wal"), Path(f"{database}-shm")):
            side.unlink(missing_ok=True)
        shutil.copyfile(copy, database)
        call_command("migrate_tenants", "--tenant", self.bar_a.dir_name, stdout=io.StringIO())
        with bound_tenant(self.bar_a):
            self.assertTrue(SignatureRequest.objects.filter(pk=self.alpha["request"].pk).exists())
        self.assertEqual(Client().get(link).status_code, 404)

        call_command("tenant", self.bar_a.dir_name, "staff_index_links", stdout=io.StringIO())
        self.assertEqual(Client().get(link).status_code, 200)
        self.assertEqual(self.indexed(self.beta["token"]), self.bar_b)

    def test_the_index_command_runs_for_one_tenant_or_every_one(self):
        SigningLink.objects.all().delete()
        output = io.StringIO()
        call_command("tenant", self.bar_a.dir_name, "staff_index_links", stdout=output)
        self.assertIn("1 lien ajouté", output.getvalue())
        self.assertEqual(self.indexed(self.alpha["token"]), self.bar_a)
        self.assertIsNone(self.indexed(self.beta["token"]))
        output = io.StringIO()
        call_command("staff_index_links", stdout=output)
        self.assertIn("Bar Beta", output.getvalue())
        self.assertEqual(self.indexed(self.beta["token"]), self.bar_b)


# -- The employee's public pages ------------------------------------------------------------------------------


class PublicPagesTests(TenantsCase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True, REMOTE_ADDR=IP, HTTP_USER_AGENT=PHONE)

    def url(self, facts, name="staff:sign"):
        return reverse(name, args=[facts["token"]])

    def post_form(self, page, facts, name, values, follow=True):
        form = form_posting_to(page.content.decode(), self.url(facts, name))
        return self.client.post(form.action, as_post(form.submission(values=values)), follow=follow)

    def test_each_link_opens_its_own_tenant_s_month(self):
        for facts, mine, other in ((self.alpha, ALPHA, BETA), (self.beta, BETA, ALPHA)):
            with self.subTest(employee=mine["shown"]):
                response = self.client.get(self.url(facts))
                self.assertEqual(response.status_code, 200)
                text = _text(response)
                for word in (mine["shown"], mine["establishment"], mine["note"]):
                    self.assertIn(word, text)
                for word in (other["shown"], other["establishment"], other["note"]):
                    self.assertNotIn(word, text)

    def test_opening_a_link_is_logged_in_its_own_tenant_only(self):
        self.client.get(self.url(self.alpha))
        with bound_tenant(self.bar_a):
            self.assertEqual(SignatureEvent.objects.filter(kind=Kind.LINK_OPENED).count(), 1)
        with bound_tenant(self.bar_b):
            self.assertEqual(SignatureEvent.objects.filter(kind=Kind.LINK_OPENED).count(), 0)

    def test_an_unknown_link_is_the_plain_404(self):
        for url in (
            "/personnel/signer/inconnu/",
            f"/personnel/signer/{'x' * 500}/",
            reverse("staff:sign_document", args=["inconnu"]),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                self.assertIn(requests_.UNKNOWN_LINK, _text(response))

    def test_the_frozen_pdf_is_the_tenant_s_own(self):
        response = self.client.get(self.url(self.beta, "staff:sign_document"))
        self.assertEqual(response.status_code, 200)
        with bound_tenant(self.bar_b):
            self.assertEqual(response.content, private_files.read(self.beta["request"].uuid, private_files.DOCUMENT))

    def test_the_employee_signs_from_his_phone_in_his_own_tenant(self):
        with bound_tenant(self.bar_b):
            code = requests_.issue_code(self.beta["request"], HANDED_OVER)
        answer = self.post_form(
            self.client.get(self.url(self.beta)), self.beta, "staff:sign_check_code", {"code": code}
        )
        self.assertIn(public_views.CODE_VERIFIED, _text(answer))
        signed = self.post_form(
            answer,
            self.beta,
            "staff:sign_submit",
            {"signature": data_url(drawn_signature()), "certification": True},
        )
        self.assertEqual(signed.status_code, 200)
        with bound_tenant(self.bar_b):
            request = SignatureRequest.objects.get(pk=self.beta["request"].pk)
            self.assertEqual(request.status, Status.EMPLOYEE_SIGNED)
            self.assertTrue(signing.verify(private_files.read(request.uuid, private_files.EMPLOYEE_SIGNED)).ok)
        with bound_tenant(self.bar_a):
            self.assertEqual(SignatureRequest.objects.get(pk=self.alpha["request"].pk).status, Status.PENDING)
        uuid = str(self.beta["request"].uuid)
        self.assertTrue((self.private(self.bar_b) / "signatures" / uuid / "signed_employee.pdf").is_file())
        self.assertFalse((self.private(self.bar_a) / "signatures" / uuid).exists())

    def test_a_code_typed_on_one_tenant_s_link_identifies_nobody_in_the_other(self):
        with bound_tenant(self.bar_a):
            code = requests_.issue_code(self.alpha["request"], HANDED_OVER)
        answer = self.post_form(
            self.client.get(self.url(self.alpha)), self.alpha, "staff:sign_check_code", {"code": code}
        )
        self.assertIn(public_views.CODE_VERIFIED, _text(answer))
        # B's request 1 shares A's pk, not its uuid: B's page still asks for a code.
        self.assertNotIn(public_views.CODE_VERIFIED, _text(self.client.get(self.url(self.beta))))

    def test_logged_in_in_the_same_tenant_the_link_opens(self):
        self.client.force_login(self.user_a)
        response = self.client.get(self.url(self.alpha))
        self.assertEqual(response.status_code, 200)
        self.assertIn(ALPHA["shown"], _text(response))

    def test_logged_in_in_another_tenant_the_link_opens_and_signs_in_its_own(self):
        """TenantMiddleware binds nothing of the visitor's on a public view:
        the page binds the LINK's tenant, whoever is logged in on this
        browser. Beta's manager lends his phone to Alpha's employee: Alpha's
        month opens, is signed and logged in Alpha, nothing of Beta is shown
        or touched - and Beta's login is still there afterwards."""
        self.client.force_login(self.user_b)
        page = self.client.get(self.url(self.alpha))
        self.assertEqual(page.status_code, 200)
        text = _text(page)
        for word in (ALPHA["shown"], ALPHA["establishment"], ALPHA["note"]):
            self.assertIn(word, text)
        for word in (BETA["shown"], BETA["establishment"], BETA["note"], "Bar Beta"):
            self.assertNotIn(word, text)

        with bound_tenant(self.bar_a):
            code = requests_.issue_code(self.alpha["request"], HANDED_OVER)
        answer = self.post_form(page, self.alpha, "staff:sign_check_code", {"code": code})
        self.assertIn(public_views.CODE_VERIFIED, _text(answer))
        signed = self.post_form(
            answer,
            self.alpha,
            "staff:sign_submit",
            {"signature": data_url(drawn_signature()), "certification": True},
        )
        self.assertEqual(signed.status_code, 200)
        with bound_tenant(self.bar_a):
            request = SignatureRequest.objects.get(pk=self.alpha["request"].pk)
            self.assertEqual(request.status, Status.EMPLOYEE_SIGNED)
            self.assertTrue(signing.verify(private_files.read(request.uuid, private_files.EMPLOYEE_SIGNED)).ok)
            self.assertTrue(SignatureEvent.objects.filter(kind=Kind.LINK_OPENED).exists())
        with bound_tenant(self.bar_b):
            self.assertEqual(SignatureRequest.objects.get(pk=self.beta["request"].pk).status, Status.PENDING)
            self.assertFalse(SignatureEvent.objects.filter(kind=Kind.LINK_OPENED).exists())
        self.assertFalse((self.private(self.bar_b) / "signatures" / str(request.uuid)).exists())

        # Still Beta's manager, on Beta's pages. (Not « DUPONT Jeanne »: the
        # employee form's own help prints that name as its example.)
        home = self.client.get(reverse("staff:home"))
        self.assertEqual(home.status_code, 200)
        for word in (BETA["shown"], BETA["establishment"], "Bar Beta"):
            self.assertIn(word, _text(home))
        for word in (ALPHA["establishment"], ALPHA["email"], "Bar Alpha"):
            self.assertNotIn(word, _text(home))

    def test_the_request_ends_unbound(self):
        self.client.get(self.url(self.alpha))
        self.assertIsNone(current_tenant())

    def test_the_view_binds_the_link_s_tenant_and_releases_it(self):
        seen = []
        view = public_views._for_the_link(lambda request, token: seen.append(current_tenant()) or "vu")
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        self.assertEqual(view(request, self.beta["token"]), "vu")
        self.assertEqual(seen, [self.bar_b])
        self.assertIsNone(current_tenant())


class OwnerPagesTests(TenantsCase):
    """The owner's « Signature » section, through the real middleware: his
    request is bound to his tenant, and what it issues is filed there."""

    JULY = date(2026, 7, 1)
    LINK = re.compile(r'id="signature-link"[^>]*value="https?://[^"]+/personnel/signer/([A-Za-z0-9_-]+)/"')

    def send_july(self, tenant, facts, user):
        with bound_tenant(tenant):
            save_month(facts["person"], self.JULY, [])
        self.client.force_login(user)
        page = self.client.get(reverse("staff:month", args=[facts["person"].pk, self.JULY]))
        self.assertEqual(page.status_code, 200)
        form = form_posting_to(
            page.content.decode(), reverse("staff:signature_send", args=[facts["person"].pk, self.JULY])
        )
        answer = self.client.post(form.action, as_post(form.submission(values={signature_views.HAND_OVER: True})))
        self.assertEqual(answer.status_code, 200)
        return self.LINK.search(answer.content.decode()).group(1)

    def test_the_link_the_owner_s_page_issues_opens_his_tenant(self):
        token = self.send_july(self.bar_b, self.beta, self.user_b)
        self.assertEqual(self.indexed(token), self.bar_b)
        response = Client().get(reverse("staff:sign", args=[token]))
        self.assertEqual(response.status_code, 200)
        text = _text(response)
        self.assertIn(BETA["shown"], text)
        self.assertIn("juillet 2026", text.lower())
        self.assertNotIn(ALPHA["shown"], text)

    def test_a_new_link_from_the_owner_s_page_is_filed_and_the_old_one_forgotten(self):
        old = self.send_july(self.bar_a, self.alpha, self.user_a)
        page = self.client.get(reverse("staff:month", args=[self.alpha["person"].pk, self.JULY]))
        form = form_posting_to(
            page.content.decode(), reverse("staff:signature_link", args=[self.alpha["person"].pk, self.JULY, 1])
        )
        answer = self.client.post(form.action, as_post(form.submission()))
        new = self.LINK.search(answer.content.decode()).group(1)
        self.assertIsNone(self.indexed(old))
        self.assertEqual(self.indexed(new), self.bar_a)


class PublicViewsArePublicTests(SimpleTestCase):
    def test_the_employee_s_pages_are_public(self):
        """Deny by default (LoginRequiredMiddleware, multi mode) - but for
        these seven, reached from a phone with no account."""
        for view in (
            public_views.sign,
            public_views.send_code,
            public_views.check_code,
            public_views.submit,
            public_views.document,
            public_views.copy,
            public_views.unknown,
        ):
            with self.subTest(view=view.__name__):
                self.assertIs(getattr(view, "login_required", True), False)


# -- The purge, in every tenant -------------------------------------------------------------------------------


class PurgeInEveryTenantTests(TenantsCase):
    def setUp(self):
        super().setUp()
        moment = dt.datetime(2021, 5, 20, 10, tzinfo=dt.UTC)
        self.old_alpha = self.fill(self.bar_a, {**ALPHA, "last_name": "Durand", "first_name": "Luc"}, MAY_2021, moment)
        self.old_beta = self.fill(self.bar_b, {**BETA, "last_name": "Petit", "first_name": "Anne"}, MAY_2021, moment)

    def purge(self, *arguments, command=("staff_purge_signatures",), today=date(2026, 6, 15)):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("staff.management.commands.staff_purge_signatures.timezone.localdate", return_value=today):
            call_command(*command, *arguments, stdout=output, stderr=errors)
        return output.getvalue(), errors.getvalue()

    def remaining(self, tenant):
        with bound_tenant(tenant):
            return set(SignatureRequest.objects.values_list("uuid", flat=True))

    def test_unbound_it_purges_every_tenant_each_in_its_own_folder(self):
        text, _errors = self.purge()
        self.assertIn("Bar Alpha", text)
        self.assertIn("Bar Beta", text)
        self.assertEqual(self.remaining(self.bar_a), {self.alpha["request"].uuid})
        self.assertEqual(self.remaining(self.bar_b), {self.beta["request"].uuid})
        for tenant, old, name in (
            (self.bar_a, self.old_alpha, "DURAND Luc"),
            (self.bar_b, self.old_beta, "PETIT Anne"),
        ):
            with self.subTest(tenant=tenant.name):
                with bound_tenant(tenant):
                    (record,) = private_files.read_deletion_records()
                    self.assertEqual((record["employee"], record["how"]), (name, "purge"))
                    self.assertFalse(private_files.request_dir(old["request"].uuid).exists())
                self.assertIsNone(self.indexed(old["token"]))
        self.assertEqual(self.indexed(self.alpha["token"]), self.bar_a)

    def test_through_the_tenant_command_it_purges_that_tenant_only(self):
        self.purge(command=("tenant", self.bar_a.dir_name, "staff_purge_signatures"))
        self.assertEqual(self.remaining(self.bar_a), {self.alpha["request"].uuid})
        self.assertEqual(self.remaining(self.bar_b), {self.beta["request"].uuid, self.old_beta["request"].uuid})

    def test_a_dry_run_names_every_tenant_and_deletes_nothing(self):
        text, _errors = self.purge("--dry-run")
        self.assertIn("DURAND Luc", text)
        self.assertIn("PETIT Anne", text)
        self.assertIn("Rien n'a été supprimé", text)
        self.assertEqual(len(self.remaining(self.bar_a)), 2)
        self.assertEqual(len(self.remaining(self.bar_b)), 2)

    def test_a_tenant_whose_database_is_gone_is_said_and_the_others_are_purged(self):
        database = paths.tenant_database(self.bar_a)
        database.rename(database.with_suffix(".absent"))
        with self.assertRaises(CommandError) as caught:
            self.purge()
        self.assertIn("Bar Alpha", str(caught.exception))
        self.assertEqual(self.remaining(self.bar_b), {self.beta["request"].uuid})


# -- The mails name the right bar -----------------------------------------------------------------------------


@MAIL
class MailNamesTheRightBarTests(TenantsCase):
    def test_every_mail_names_the_tenant_s_own_establishment(self):
        for tenant, facts, mine, other in (
            (self.bar_a, self.alpha, "BAR ALPHA", "BAR BETA"),
            (self.bar_b, self.beta, "BAR BETA", "BAR ALPHA"),
        ):
            with self.subTest(tenant=tenant.name), bound_tenant(tenant):
                mail.outbox = []
                self.assertTrue(signature_mail.send_link(facts["request"], LINK).sent)
                self.assertTrue(signature_mail.send_code(facts["request"]).sent)
                self.assertEqual(len(mail.outbox), 2)
                for message in mail.outbox:
                    self.assertIn(mine, message.subject)
                    self.assertNotIn(other, message.subject + message.body)
                    self.assertEqual(message.to, [facts["person"].email])
                self.assertIn(mine, mail.outbox[0].body)


# -- What a tenant that is not the owner's is told ------------------------------------------------------------


class NotTheOwnersTenantTests(TenantsCase):
    owner_a = True

    def test_the_passphrase_warning_is_for_the_owner_s_tenant_only(self):
        """The passphrase is the platform's: another bar can do nothing about
        it and is never shown a server setting's name (the system check
        staff.W001 warns the operator)."""
        with bound_tenant(self.bar_a):
            self.assertEqual(signing.key_warning(), signing.KEY_WARNING)
        with bound_tenant(self.bar_b):
            self.assertEqual(signing.key_warning(), "")

    def test_the_retention_note_names_the_purge_command_in_the_owner_s_tenant_only(self):
        """A hosted bar runs no command on the server: its « Signature »
        section says the deletion after the retention years is « à
        configurer », as every other page says what it cannot do yet."""
        self.client.force_login(self.user_a)
        text = _text(self.client.get(reverse("staff:month", args=[self.alpha["person"].pk, JUNE])))
        self.assertIn("manage.py staff_purge_signatures", text)
        self.client.force_login(self.user_b)
        text = _text(self.client.get(reverse("staff:month", args=[self.beta["person"].pk, JUNE])))
        self.assertNotIn("staff_purge_signatures", text)
        self.assertNotIn("manage.py", text)
        self.assertIn(f"leur effacement ensuite est {TO_CONFIGURE}", text)

    def test_no_tenant_is_told_the_app_must_not_go_online_before_a_login_exists(self):
        """The old single mode's sentence (no login there): every owner's
        page is behind the login it said did not exist yet - the owner's
        tenant and a hosted bar alike."""
        for user, facts in ((self.user_a, self.alpha), (self.user_b, self.beta)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("staff:month", args=[facts["person"].pk, JUNE]))
                self.assertEqual(response.status_code, 200)
                text = _text(response)
                self.assertIn("Signature", text)
                self.assertNotIn("ne doit pas être mise en ligne", text)
                self.assertNotIn("étape de connexion", text)

    def test_no_mail_server_is_said_without_its_setting_s_name_elsewhere(self):
        with bound_tenant(self.bar_a):
            self.assertIn("EMAIL_HOST", signature_mail.send_link(self.alpha["request"], LINK).message)
        with bound_tenant(self.bar_b):
            message = signature_mail.send_link(self.beta["request"], LINK).message
        self.assertNotIn("EMAIL_HOST", message)
        self.assertIn("Aucun serveur d'e-mail", message)


class PassphraseCheckTests(SimpleTestCase):
    def test_without_a_passphrase_the_operator_is_warned(self):
        with override_settings(MARGINMATE_SIGNING_PASSPHRASE=""):
            (warning,) = checks.signing_passphrase()
        self.assertEqual(warning.id, "staff.W001")

    def test_nothing_to_say_otherwise(self):
        with override_settings(MARGINMATE_SIGNING_PASSPHRASE="phrase-d-essai"):
            self.assertEqual(checks.signing_passphrase(), [])

    def test_a_leftover_single_mode_setting_silences_nothing(self):
        """Single mode's check said nothing (its pages said it); single mode
        is gone, and a stale TENANCY_MODE is read by nobody."""
        with override_settings(TENANCY_MODE="single", MARGINMATE_SIGNING_PASSPHRASE=""):
            (warning,) = checks.signing_passphrase()
        self.assertEqual(warning.id, "staff.W001")
