"""The one-time code's five tries hold when guesses arrive together
(security audit SIGN-1).

`check_code` read `code_attempts`, compared the code, then wrote the count
read + 1 - outside any lock. Guesses sent at the same moment (a threaded
server) each read the same count: 40 wrong codes at once were 40
comparisons against the stored hash, and `code_attempts` ended at 1. Now a
try is RESERVED by one conditional UPDATE before anything is compared, and
the code is used up by an UPDATE filtered on that same code, so two right
guesses cannot both identify.

A real tenant in temporary files (TenancyTestCase): every thread has its own
connection to the tenant's SQLite file, as a threaded server's requests do.
`code_hash` is slowed down to open the window the old code left between
reading the count and writing it; nothing else is patched. Names invented."""

import threading
import time
import traceback
from datetime import date
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.test import override_settings

from accounts.tenancy import bound, bound_tenant
from accounts.tests.support import TenancyTestCase
from staff import signature_requests as requests_
from staff.models import Establishment, SignatureEvent, SignatureRequest
from staff.tests.signing_support import OfflineTimestamps
from staff.tests.support import employee
from staff.timesheet import PostedDay, save_month
from tests.support import _Forbidden

JUNE = date(2026, 6, 1)
IP = "203.0.113.7"  # TEST-NET-3: an address that belongs to nobody
GUESSES = 16
Kind = SignatureEvent.Kind
HANDED_OVER = SignatureRequest.Identification.CODE_HANDED_OVER


class ConcurrentCodeTests(TenancyTestCase):
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
        # Production's SQLite options on the tenant's connections (WAL,
        # IMMEDIATE transactions, a 60 s wait): the test settings' plain
        # `default` has none, and a deferred transaction racing another
        # writer fails at once with « database is locked » - a failure of
        # the test's database, not of the code under test.
        self.enterContext(
            mock.patch.dict(connections.settings[DEFAULT_DB_ALIAS], {"OPTIONS": dict(settings.SQLITE_OPTIONS)})
        )
        self.timestamps = OfflineTimestamps().start()
        self.addCleanup(self.timestamps.stop)
        self.tenant = self.make_tenant("Bar Essai")
        with bound_tenant(self.tenant):
            Establishment.objects.create(pk=Establishment.SINGLETON_PK, name="BAR ESSAI")
            person = employee(last_name="Durand", first_name="Jeanne")
            save_month(person, JUNE, [PostedDay(date(2026, 6, 2), hours=Decimal("8"))])
            self.sign_request, _token = requests_.create_request(person, JUNE)
            self.code = requests_.issue_code(self.sign_request, HANDED_OVER)

    def guess_together(self, codes):
        """Each code typed by its own thread, all released at once. Returns
        how many codes were compared with the stored one, and the sessions
        that came out identified."""
        compared, identified, errors = [], [], []
        barrier = threading.Barrier(len(codes), timeout=60)
        lock = threading.Lock()
        real_hash = requests_.code_hash

        def slow_hash(request, code):
            # What a busy server does between reading the count and writing
            # it, made certain.
            with lock:
                compared.append(code)
            time.sleep(0.05)
            return real_hash(request, code)

        def guess(code):
            session = {}
            try:
                request = SignatureRequest.objects.get(pk=self.sign_request.pk)
                barrier.wait()
                requests_.check_code(request, code, session, ip=IP, user_agent="Essai")
            except requests_.CodeError:
                pass
            except Exception:  # noqa: BLE001 - reported below, with where it came from
                errors.append(traceback.format_exc())
            else:
                with lock:
                    identified.append(session)

        with bound_tenant(self.tenant):
            workers = [threading.Thread(target=bound(guess), args=(code,)) for code in codes]
        with mock.patch.object(requests_, "code_hash", slow_hash):
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(90)
        self.assertEqual(errors, [])
        return compared, identified

    def failed_reasons(self):
        with bound_tenant(self.tenant):
            return [
                event.detail.get("reason", "")
                for event in SignatureEvent.objects.filter(request_id=self.sign_request.pk, kind=Kind.CODE_FAILED)
            ]

    def test_wrong_codes_sent_together_are_compared_five_times_at_most(self):
        wrong = "000000" if self.code != "000000" else "111111"
        compared, identified = self.guess_together([wrong] * GUESSES)
        self.assertEqual(identified, [])
        self.assertLessEqual(len(compared), requests_.CODE_MAX_ATTEMPTS)
        reasons = self.failed_reasons()
        # Every guess compared leaves its event, and only five were; the
        # others, refused before any comparison, are said once for this
        # device (signature_requests._log_unless_repeated).
        self.assertEqual(sum(reason.startswith("code erroné") for reason in reasons), len(compared))
        refused = [reason for reason in reasons if not reason.startswith("code erroné")]
        self.assertTrue(refused)
        self.assertEqual(len(refused), len(set(refused)))
        with bound_tenant(self.tenant):
            request = SignatureRequest.objects.get(pk=self.sign_request.pk)
            self.assertEqual(request.code_attempts, requests_.CODE_MAX_ATTEMPTS)
            # The fifth wrong try used the code up: the right one is refused now.
            self.assertEqual(request.code_hash, "")
            with self.assertRaises(requests_.CodeError):
                requests_.check_code(request, self.code, {}, ip=IP)

    def test_the_right_code_sent_twice_at_once_identifies_once(self):
        _compared, identified = self.guess_together([self.code] * 4)
        self.assertEqual(len(identified), 1)
        with bound_tenant(self.tenant):
            verified = SignatureEvent.objects.filter(request_id=self.sign_request.pk, kind=Kind.CODE_VERIFIED)
            self.assertEqual(verified.count(), 1)
            request = SignatureRequest.objects.get(pk=self.sign_request.pk)
            self.assertEqual(request.code_hash, "")
            self.assertIsNotNone(request.code_verified_at)
            self.assertTrue(requests_.is_identified(identified[0], request))

    def test_one_at_a_time_the_five_tries_are_unchanged(self):
        """The reservation changes nothing for a person typing: four wrong,
        then the right one, still identifies."""
        wrong = "000000" if self.code != "000000" else "111111"
        session = {}
        with bound_tenant(self.tenant):
            request = SignatureRequest.objects.get(pk=self.sign_request.pk)
            for left in (4, 3, 2, 1):
                with self.assertRaises(requests_.CodeError) as caught:
                    requests_.check_code(request, wrong, session, ip=IP)
                self.assertIn(f"Encore {left} essai", str(caught.exception))
            requests_.check_code(request, self.code, session, ip=IP)
            self.assertTrue(requests_.is_identified(session, SignatureRequest.objects.get(pk=request.pk)))
