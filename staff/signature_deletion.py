"""Deleting one version of a month sent for signature - the request, its
journal and its files - and, when it is the month's last version and the
owner asks, the month's saved hours too. ONE function does it,
`delete_signature_request`: the owner's « Supprimer… »
(signature_views.signature_delete / signature_delete_confirm) and
`manage.py staff_purge_signatures` both call it.

**Why it is guarded so.** A signed timesheet and its proof file are the
employer's evidence of the hours an employee worked. The working-time
records are to be kept: one year for the labour inspection (Code du
travail, D3171-16), three for a wage claim (L3245-1), five as this app
recommends (`STAFF_SIGNATURE_RETENTION_YEARS`). And a deletion is final:
nothing in the app undoes it. So the page asks twice (the owner, 28/09:
« with double verifications as this can be dangerous »):

1. **Step 1** (a GET, then a POST) says what goes - the version, its state
   and dates, every file by name, the number of events, the proof - and
   why it is dangerous; it passes only with « Je comprends que la
   suppression est définitive » ticked AND the phrase typed
   (`deletion_phrase`, « supprimer juin 2026 », compared by `phrase_matches`
   after trimming and case-folding). It deletes nothing: it signs a token.
2. **Step 2** (« Dernière vérification ») receives that token
   (`confirmation_token`: django.core.signing, `TOKEN_SALT`, ten minutes),
   which binds the request's uuid, its state and its document / final
   hashes as step 1 saw them, and the hours option. Its POST reads the token
   back (`read_confirmation`) and deletes - `expected` hands the state the
   token saw to `delete_signature_request`, which compares it again under
   the write lock.

Every refusal is a `DeletionRefused` whose words end in « Rien n'a été
supprimé. », meant to be shown as they are.

**What `delete_signature_request` does**, in one transaction (SQLite's
IMMEDIATE mode takes the write lock when it starts: two deletions cannot
both read the row):

* reads the row again - gone, or no longer what the caller saw
  (`expected`), is refused;
* refuses the hours while another version of the month remains: they are
  what every version of the month was drawn from, and `Timesheet` is
  PROTECTed by its requests;
* deletes the request (its events cascade), and the Timesheet with its days
  when asked - the month is then the typical week again, as if never saved;
* appends the **tombstone** to the private folder's `deletions.log`
  (`private_files.append_deletion_record`) - LAST, still inside the
  transaction: a line that cannot be written rolls the deletion back, so
  nothing is deleted without its trace. The line says when, how (« page »
  or « purge »), whose, which month and version, the uuid, the state, the
  SHA-256 of the frozen, signed and countersigned documents, the files and
  the number of events, whether the hours went, and the client's address;
* once committed, removes the request's folder of files
  (`transaction.on_commit`): a deletion rolled back keeps its files with its
  rows. A folder that cannot be removed then (a file held open on Windows)
  is said on the outcome (`files_error`), never raised: the rows are gone.

The request's public link then reaches nothing, and answers as a link that
never existed (`signature_requests.resolve_link`: 404) - its hash also
leaves the accounts database's index once the deletion is
committed (`accounts.links.forget`; left there by a failure, it would still
answer 404). The tombstone goes to the BOUND tenant's private folder: each
bar keeps its own deletions.log. A month the deleted version held is
editable again.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from functools import partial

from django.core import signing as django_signing
from django.db import transaction
from django.utils import timezone

from accounts import links

from . import private_files
from .models import SignatureRequest, Timesheet
from .signature_requests import _clean_ip
from .timesheet import month_label, month_slug

logger = logging.getLogger(__name__)

#: How a deletion was made, in its tombstone.
PAGE = "page"
PURGE = "purge"
HOW = (PAGE, PURGE)

#: Step 1's fields, and the token step 2 carries.
UNDERSTOOD_FIELD = "definitive"
PHRASE_FIELD = "phrase"
HOURS_FIELD = "supprimer_heures"
TOKEN_FIELD = "jeton"

#: The token of step 2: signed for this purpose only, valid ten minutes.
TOKEN_SALT = "staff.signature_deletion.confirmation"
TOKEN_MAX_AGE = 10 * 60
#: A token is some 250 characters; anything much longer is none.
TOKEN_MAX_LENGTH = 1000
#: What the token binds: the request's state at step 1.
BOUND_FIELDS = ("status", "document_sha256", "final_pdf_sha256")

NOTHING_DELETED = "Rien n'a été supprimé."
UNDERSTOOD_MISSING = "Cochez « Je comprends que la suppression est définitive » pour continuer."
PHRASE_WRONG = "Tapez exactement « {phrase} » pour continuer."
#: Step 1 says it beside « Rien n'a été supprimé. »; `hours_refused` adds it.
HOURS_NOT_LAST = (
    "Les heures du mois ne se suppriment qu'avec sa dernière version : d'autres versions de {month} en dépendent."
)
TOKEN_INVALID = (
    f"Cette confirmation n'est pas valable : recommencez la suppression depuis la première étape. {NOTHING_DELETED}"
)
TOKEN_EXPIRED = (
    "Cette confirmation a expiré (elle vaut 10 minutes) : recommencez la suppression depuis la première étape. "
    f"{NOTHING_DELETED}"
)
TOKEN_OTHER = (
    "Cette confirmation a été donnée pour une autre version : recommencez depuis la première étape de celle-ci. "
    f"{NOTHING_DELETED}"
)
CHANGED = (
    "Cette version a changé depuis la première étape (son état ou ses documents) : relisez ce qui serait "
    f"supprimé, puis recommencez. {NOTHING_DELETED}"
)
GONE = "Cette version n'existe pas ou plus : elle a peut-être déjà été supprimée."
TRACE_FAILED = (
    "La trace de la suppression n'a pas pu être écrite dans deletions.log (dossier privé) : sans elle, rien "
    f"ne se supprime. {NOTHING_DELETED}"
)


class DeletionRefused(Exception):
    """A deletion refused, with what to say (French)."""


def hours_refused(month: date) -> DeletionRefused:
    return DeletionRefused(f"{HOURS_NOT_LAST.format(month=month_label(month))} {NOTHING_DELETED}")


# -- Step 1 -----------------------------------------------------------------------------------------------------


def deletion_phrase(month: date) -> str:
    """What the owner types at step 1: « supprimer juin 2026 »."""
    return f"supprimer {month_label(month)}"


def _folded(text) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split()).casefold()


def phrase_matches(typed, month: date) -> bool:
    """Whether `typed` is the phrase: the same words, trimmed, runs of
    spaces made one, case folded - « SUPPRIMER Juin 2026 » is it; « supprimer
    juin » or « supprimer aout 2026 » for août is not."""
    return bool(typed) and _folded(typed) == _folded(deletion_phrase(month))


def is_last_version(request: SignatureRequest) -> bool:
    """Whether no other version of the month remains - the only case where
    the month's hours may go with it."""
    return not SignatureRequest.objects.filter(timesheet_id=request.timesheet_id).exclude(pk=request.pk).exists()


@dataclass(frozen=True)
class Preview:
    """What step 1 lists as going."""

    files: tuple[str, ...]  # the names in the request's folder
    events: int
    last_version: bool  # the hours may be offered
    days: int  # the month's saved days


def preview(request: SignatureRequest) -> Preview:
    return Preview(
        files=tuple(private_files.request_files(request.uuid)),
        events=request.events.count(),
        last_version=is_last_version(request),
        days=request.timesheet.days.count(),
    )


# -- The token between the steps --------------------------------------------------------------------------------


def _state(request: SignatureRequest) -> dict:
    return {name: getattr(request, name) or "" for name in BOUND_FIELDS}


@dataclass(frozen=True)
class Confirmation:
    """What step 1 signed, read back at step 2."""

    with_hours: bool
    expected: dict  # BOUND_FIELDS as step 1 saw them


def confirmation_token(request: SignatureRequest, *, with_hours: bool) -> str:
    """Step 1's answer: this request, its state now, and the hours option -
    signed and timestamped (`TOKEN_SALT`)."""
    payload = {"uuid": str(request.uuid), "hours": bool(with_hours), **_state(request)}
    return django_signing.dumps(payload, salt=TOKEN_SALT)


def read_confirmation(token, request: SignatureRequest) -> Confirmation:
    """The token step 2 carries, checked: signed by step 1 (not tampered,
    not for another purpose), under ten minutes old, for THIS request, whose
    state has not changed since. `DeletionRefused` otherwise."""
    token = token.strip() if isinstance(token, str) else ""
    if not token or len(token) > TOKEN_MAX_LENGTH:
        raise DeletionRefused(TOKEN_INVALID)
    try:
        payload = django_signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except django_signing.SignatureExpired:
        raise DeletionRefused(TOKEN_EXPIRED) from None
    except (django_signing.BadSignature, ValueError, TypeError, UnicodeError):
        raise DeletionRefused(TOKEN_INVALID) from None
    if not isinstance(payload, dict):
        raise DeletionRefused(TOKEN_INVALID)
    if payload.get("uuid") != str(request.uuid):
        raise DeletionRefused(TOKEN_OTHER)
    expected = {name: payload.get(name) for name in BOUND_FIELDS}
    if expected != _state(request):
        raise DeletionRefused(CHANGED)
    return Confirmation(with_hours=payload.get("hours") is True, expected=expected)


# -- The deletion -----------------------------------------------------------------------------------------------


@dataclass
class Deleted:
    """What went. `files_error` is filled once committed when the folder
    could not be removed."""

    uuid: str
    version: int
    month: date
    employee: str
    status: str
    held_month: bool
    files: list = field(default_factory=list)
    events: int = 0
    hours_deleted: bool = False
    days_deleted: int = 0
    files_error: str = ""


def _remove_files(outcome: Deleted) -> None:
    try:
        private_files.delete_request_files(outcome.uuid)
    except OSError as error:
        logger.error("Version supprimée %s : son dossier de fichiers n'a pas pu être retiré (%s)", outcome.uuid, error)
        outcome.files_error = (
            f"Ses fichiers n'ont pas pu être retirés du dossier privé (signatures/{outcome.uuid}) : effacez ce "
            "dossier vous-même."
        )


def delete_signature_request(
    request: SignatureRequest, *, how: str, with_hours: bool = False, expected: dict | None = None, ip=None, now=None
) -> Deleted:
    """Delete `request` - its events cascade - and, with `with_hours`, its
    month's Timesheet and days (refused while another version remains);
    leave the tombstone line; remove its files once committed. `expected`
    maps some of `BOUND_FIELDS` to what the caller saw: a row that differs
    is refused. Call it outside any transaction of your own: its files go
    when IT commits."""
    if how not in HOW:
        raise ValueError(f"Suppression inconnue : {how!r}")
    now = now or timezone.now()
    with transaction.atomic():
        row = SignatureRequest.objects.select_related("timesheet__employee").filter(pk=request.pk).first()
        if row is None:
            raise DeletionRefused(GONE)
        if expected is not None and any((getattr(row, name) or "") != value for name, value in expected.items()):
            raise DeletionRefused(CHANGED)
        timesheet = row.timesheet
        if with_hours and not is_last_version(row):
            raise hours_refused(timesheet.month)
        outcome = Deleted(
            uuid=str(row.uuid),
            version=row.version,
            month=timesheet.month,
            employee=timesheet.employee.display_name,
            status=row.status,
            held_month=row.locks_month,
            files=private_files.request_files(row.uuid),
            events=row.events.count(),
        )
        SignatureRequest.objects.filter(pk=row.pk).delete()
        if with_hours:
            outcome.days_deleted = timesheet.days.count()
            Timesheet.objects.filter(pk=timesheet.pk).delete()
            outcome.hours_deleted = True
        record = {
            "when": timezone.localtime(now).isoformat(timespec="seconds"),
            "how": how,
            "employee": outcome.employee,
            "month": month_slug(timesheet.month),
            "version": row.version,
            "uuid": outcome.uuid,
            "status": row.status,
            "document_sha256": row.document_sha256,
            "employee_pdf_sha256": row.employee_pdf_sha256,
            "final_pdf_sha256": row.final_pdf_sha256,
            "files": outcome.files,
            "events": outcome.events,
            "hours_deleted": outcome.hours_deleted,
            "ip": _clean_ip(ip),
        }
        try:
            private_files.append_deletion_record(record)
        except OSError as error:
            logger.error("Version %s : la trace de sa suppression n'a pas pu être écrite (%s)", outcome.uuid, error)
            raise DeletionRefused(TRACE_FAILED) from None
        transaction.on_commit(partial(_remove_files, outcome))
        # The link's hash leaves the index. Robust: a hash left behind
        # reaches no request - still 404.
        transaction.on_commit(partial(links.forget, row.token_hash), robust=True)
    return outcome
