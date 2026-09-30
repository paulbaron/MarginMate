"""`manage.py staff_purge_signatures [--dry-run]`: a signed month and its
evidence are kept `STAFF_SIGNATURE_RETENTION_YEARS` (5) after the month
ends, then deleted together - rows, events and files - by the ONE function
the owner's « Supprimer… » uses (staff/signature_deletion.py), which leaves
a line in deletions.log for each. Never run automatically; --dry-run writes
nothing. The files go once the deletion is committed: a TestCase runs that
only inside `captureOnCommitCallbacks(execute=True)`."""

import datetime as dt
import io
from datetime import date
from unittest import mock

from django.core.management import call_command

from staff import private_files, signature_deletion
from staff import signature_requests as requests_
from staff.models import Establishment, SignatureEvent, SignatureRequest, Timesheet
from staff.tests.signing_support import SigningTestMixin
from staff.tests.support import employee
from staff.timesheet import save_month
from tests.support import NoNetworkTestCase

MAY_2021 = date(2021, 5, 1)
JUNE_2021 = date(2021, 6, 1)
JUNE_2026 = date(2026, 6, 1)


class PurgeTests(SigningTestMixin, NoNetworkTestCase):
    def setUp(self):
        super().setUp()
        Establishment.objects.create(pk=Establishment.SINGLETON_PK, name="BAR EXEMPLE")
        self.person = employee()
        self.requests = {}
        for month in (MAY_2021, JUNE_2021, JUNE_2026):
            save_month(self.person, month, [])
            moment = dt.datetime(month.year, month.month, 20, 10, tzinfo=dt.UTC)
            self.requests[month], _token = requests_.create_request(self.person, month, now=moment)

    def purge(self, today, *arguments):
        output = io.StringIO()
        with (
            mock.patch("staff.management.commands.staff_purge_signatures.timezone.localdate", return_value=today),
            self.captureOnCommitCallbacks(execute=True),
        ):
            call_command("staff_purge_signatures", *arguments, stdout=output)
        return output.getvalue()

    def test_the_purge_deletes_through_the_one_shared_function(self):
        shared = signature_deletion.delete_signature_request
        with mock.patch("staff.signature_deletion.delete_signature_request", wraps=shared) as spy:
            self.purge(date(2026, 7, 1))
        self.assertEqual(
            sorted(call.args[0].pk for call in spy.call_args_list),
            sorted(self.requests[month].pk for month in (MAY_2021, JUNE_2021)),
        )
        for call in spy.call_args_list:
            self.assertEqual(call.kwargs["how"], signature_deletion.PURGE)
            self.assertFalse(call.kwargs.get("with_hours", False))

    def test_each_purged_request_leaves_its_line_in_deletions_log(self):
        old = self.requests[MAY_2021]
        text = self.purge(date(2026, 6, 15))
        self.assertIn("deletions.log", text)
        (record,) = private_files.read_deletion_records()
        expected = {
            "how": "purge",
            "employee": "DUPONT Jeanne",
            "month": "2021-05",
            "version": 1,
            "uuid": str(old.uuid),
            "status": SignatureRequest.Status.PENDING,
            "document_sha256": old.document_sha256,
            "final_pdf_sha256": "",
            "ip": None,
            "hours_deleted": False,
        }
        self.assertEqual({key: record[key] for key in expected}, expected)

    def test_a_dry_run_leaves_no_line(self):
        self.purge(date(2026, 7, 1), "--dry-run")
        self.assertEqual(private_files.read_deletion_records(), [])

    def test_a_trace_that_cannot_be_written_deletes_nothing_and_says_so(self):
        old = self.requests[MAY_2021]
        errors = io.StringIO()
        with (
            mock.patch("staff.private_files.append_deletion_record", side_effect=OSError("disque plein (test)")),
            mock.patch(
                "staff.management.commands.staff_purge_signatures.timezone.localdate", return_value=date(2026, 6, 15)
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            output = io.StringIO()
            call_command("staff_purge_signatures", stdout=output, stderr=errors)
        self.assertIn("Aucune demande de signature n'a été supprimée.", output.getvalue())
        self.assertIn(signature_deletion.TRACE_FAILED, errors.getvalue())
        self.assertTrue(SignatureRequest.objects.filter(pk=old.pk).exists())
        self.assertTrue(private_files.exists(old.uuid, private_files.DOCUMENT))

    def test_dry_run_says_what_would_go_and_writes_nothing(self):
        before = (SignatureRequest.objects.count(), SignatureEvent.objects.count())
        text = self.purge(date(2026, 6, 15), "--dry-run")
        self.assertIn("DUPONT Jeanne", text)
        self.assertIn("mai 2021", text)
        self.assertNotIn("juin 2021", text)
        self.assertIn("Rien n'a été supprimé", text)
        self.assertEqual((SignatureRequest.objects.count(), SignatureEvent.objects.count()), before)
        self.assertTrue(private_files.exists(self.requests[MAY_2021].uuid, private_files.DOCUMENT))

    def test_the_month_ended_more_than_five_years_ago_goes_whole(self):
        old = self.requests[MAY_2021]
        text = self.purge(date(2026, 6, 15))
        self.assertIn("1 demande de signature supprimée", text)
        self.assertIn("mai 2021", text)
        self.assertFalse(SignatureRequest.objects.filter(pk=old.pk).exists())
        self.assertFalse(SignatureEvent.objects.filter(request_id=old.pk).exists())
        self.assertFalse(private_files.request_dir(old.uuid).exists())
        # The others stay, and so does the month itself.
        for month in (JUNE_2021, JUNE_2026):
            kept = self.requests[month]
            self.assertTrue(SignatureRequest.objects.filter(pk=kept.pk).exists())
            self.assertTrue(private_files.exists(kept.uuid, private_files.DOCUMENT))
        self.assertTrue(Timesheet.objects.filter(employee=self.person, month=MAY_2021).exists())

    def test_the_boundary_is_the_day_after_the_fifth_anniversary_of_the_months_end(self):
        """June 2021 ends on 30/06/2021: kept on 30/06/2026, gone on 01/07/2026."""
        self.purge(date(2026, 6, 30))
        self.assertTrue(SignatureRequest.objects.filter(pk=self.requests[JUNE_2021].pk).exists())
        self.purge(date(2026, 7, 1))
        self.assertFalse(SignatureRequest.objects.filter(pk=self.requests[JUNE_2021].pk).exists())

    def test_nothing_to_delete_is_said(self):
        text = self.purge(date(2026, 1, 1))
        self.assertIn("Aucune demande de signature", text)
        self.assertEqual(SignatureRequest.objects.count(), 3)
