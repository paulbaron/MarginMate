"""Delete the signatures of months that ended more than
`settings.STAFF_SIGNATURE_RETENTION_YEARS` (5) years ago.

    python manage.py staff_purge_signatures --dry-run
    python manage.py staff_purge_signatures

A signed timesheet and its whole evidence - the request, its events, the
frozen and signed PDFs, the drawn signature, the proof file - are kept five
years after the month (payslips are kept five years; a wage claim reaches
back three), then deleted TOGETHER: evidence without its document, or the
other way round, is worth nothing (research report, « Retention and
GDPR »). The month's own timesheet is not touched: that is the hours record,
kept under its own rule. Never run automatically - deleting is the owner's
act, and --dry-run says what would go first.

Each request goes through the ONE function the owner's « Supprimer… » uses
(`staff.signature_deletion.delete_signature_request`, `how="purge"`): its
own transaction, its files removed once committed, and its line in
STAFF_PRIVATE_DIR/deletions.log.
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from staff import signature_deletion
from staff.models import SignatureRequest
from staff.timesheet import month_label, next_month


def _expired_before(month, years: int, today) -> bool:
    """Whether `month` (its 1st) ended more than `years` years before
    `today`: its last day plus `years` years is before today - that is,
    the 1st of the following month plus `years` years is today or earlier.
    (The 1st of a month has the same day in every year: no 29 February.)"""
    following = next_month(month)
    return following.replace(year=following.year + years) <= today


class Command(BaseCommand):
    help = (
        "Supprime les signatures (demandes, journaux et fichiers) des mois terminés depuis plus de "
        "STAFF_SIGNATURE_RETENTION_YEARS ans."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Dire ce qui serait supprimé, sans rien supprimer.")

    def handle(self, *args, dry_run=False, **options):
        years = int(getattr(settings, "STAFF_SIGNATURE_RETENTION_YEARS", 5))
        today = timezone.localdate()
        doomed = [
            request
            for request in SignatureRequest.objects.select_related("timesheet__employee").order_by(
                "timesheet__month", "timesheet__employee__last_name", "version"
            )
            if _expired_before(request.timesheet.month, years, today)
        ]
        if not doomed:
            self.stdout.write(
                f"Aucune demande de signature d'un mois terminé depuis plus de {years} ans : rien à supprimer."
            )
            return
        if dry_run:
            count = len(doomed)
            plural = "s" if count > 1 else ""
            self.stdout.write(f"{count} demande{plural} de signature serai{'ent' if count > 1 else 't'} supprimée{plural}, "
                              f"avec leur journal et leurs fichiers :")
            self.stdout.write("\n".join(self._line(request) for request in doomed))
            self.stdout.write("Rien n'a été supprimé (--dry-run).")
            return
        deleted, outcomes = [], []
        for request in doomed:
            try:
                outcomes.append(
                    signature_deletion.delete_signature_request(request, how=signature_deletion.PURGE)
                )
            except signature_deletion.DeletionRefused as error:
                self.stderr.write(f"{self._line(request).strip()} : {error}")
                continue
            deleted.append(request)
        if not deleted:
            self.stdout.write("Aucune demande de signature n'a été supprimée.")
            return
        count = len(deleted)
        plural = "s" if count > 1 else ""
        files = sum(len(outcome.files) for outcome in outcomes)
        self.stdout.write(f"{count} demande{plural} de signature supprimée{plural}, avec leur journal et {files} "
                          f"fichier{'s' if files > 1 else ''} :")
        self.stdout.write("\n".join(self._line(request) for request in deleted))
        self.stdout.write("Une trace de chaque suppression est gardée dans deletions.log, dans le dossier privé.")
        for outcome in outcomes:
            if outcome.files_error:
                self.stderr.write(f"Version {outcome.version} de {month_label(outcome.month)} : {outcome.files_error}")

    @staticmethod
    def _line(request) -> str:
        return (
            f"  {request.timesheet.employee.display_name} — {month_label(request.timesheet.month)} — version "
            f"{request.version} ({request.get_status_display().lower()}, document n° {request.uuid})"
        )
