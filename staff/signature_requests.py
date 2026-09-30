"""The monthly signature of a timesheet, step by step - the service the
pages call (staff/views.py), pure of request objects: a view passes the
session (a mapping), the IP address and the user agent, and gets back a row
or a French refusal (`RequestError` and its kinds).

1. **The owner sends a saved month** (`create_request`): the month is
   frozen (`signing.freeze`: the PDF plus both empty fields, kept as
   `document.pdf`), a snapshot of it is kept on the row (`month_snapshot`:
   the employee's page is drawn from it, so later edits cannot change what
   he is shown), and a secret link is made - `token_urlsafe(32)`, of which
   only the SHA-256 is stored (`token_hash`): the caller shows or mails it
   once, and « Nouveau lien » (`renew_link`) replaces it. The link lasts
   `LINK_VALIDITY`. A month nobody saved is never sent (a planning is not a
   record), and a month has one request holding it at a time.
2. **While a request is waiting, signed or finished, the month is read-only**
   (`month_is_locked`). « Corriger ce mois » (`reopen_month`) cancels a
   waiting or half-signed request (« annulée ») or supersedes a finished one
   (« remplacée ») - files kept - and the next request is a new version.
3. **The employee opens the link** (`resolve_link`: unknown is 404; expired,
   cancelled or superseded is 410, each a plain French sentence) **and
   identifies himself with a one-time code** (`issue_code`, `check_code`):
   six digits, 15 minutes, 5 attempts, 3 codes an hour of each kind, kept
   as an HMAC (SECRET_KEY and the request's uuid) and never logged. Sent by
   e-mail (`code_email`, staff/signature_mail.py) or shown ONCE to the owner
   who passes it on by another channel than the link (`code_remis`) - which
   the proof file says honestly. A code by e-mail never voids the one the
   employer handed over while it can still be typed. The method recorded
   (`identification`) is the one of the code he TYPED, set when he types it
   - never by a code merely issued. A verified code is remembered in HIS
   session, for THIS request only (`is_identified`).
4. **He signs** (`sign_for_employee`): the certification ticked, his drawing
   checked (`signing.clean_signature_png`), the frozen file checked against
   its hash, then `signing.sign_as_employee` with a timestamp. No timestamp
   server answering is a refusal with NOTHING stored but the event saying
   so. The row moves only if it was still waiting (a conditional UPDATE: two
   posts cannot both sign).
5. **The owner countersigns** (`countersign_request`) with his own drawn
   signature - required, checked as the employee's is, kept as
   `employer_signature.png`, its SHA-256 in the COUNTERSIGNED event
   (`EMPLOYER_DRAWING`) and sealed in the countersignature: no column, so no
   migration (`employer_drawing_sha256`). The link gets a new fortnight so
   the employee can fetch his final copy. A request countersigned before
   28/09 has no drawing, and reads, verifies and proves as it did.

Every step is an event (`log_event`) in the request's **append-only hash
chain**: each hash covers the previous one and the event's canonical content
(`event_hash`), and the request row keeps the newest (`last_event_hash`), so
an event edited, removed or cut off the end no longer adds up
(`verify_event_chain`) - unless whoever did it could rewrite the database and
worked every hash out again. What resists that is outside the database: each
signature carries the chain's head at the moment it was made, in its /Reason,
under its timestamp, so the events before the last signature are SEALED. The
proof file (staff/proof.py) is written again after each step
(`store_proof`).

**Expiry is set lazily**: a waiting request past its `expires_at` becomes
« expirée » (with its event) the first time anything looks at it -
`settle_expiry`, called by `resolve_link`, `open_request` and every action.
A request already signed never « expires »: only its link does.

**Several tenants.** The link carries no tenant and the employee has no
account, so the accounts database indexes every link's hash under the
tenant that issued it (`accounts.links`): the public page resolves the
tenant from the hash, binds it, then reads the request in that tenant's
database. The index holds exactly the hashes this tenant's requests hold -
written when a link is issued (`create_request`) or renewed (`renew_link`,
the old hash forgotten), forgotten when its request is deleted or purged
(`signature_deletion`). A cancelled, superseded or expired request KEEPS
its link: its page says « annulée », « corrigé depuis » or « expiré » (410)
- forgotten, it would say « vérifiez qu'il a été copié en entier » (404),
which is not what happened. `index_links` rebuilds the tenant's index from
its requests (an adopted database, a copy put back). Each tenant's keys,
signed files and deletions.log are in its own private folder
(`private_files`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from typing import cast

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.ipv6 import clean_ipv6_address

from accounts import links
from accounts.models import SigningLink
from accounts.tenancy import require_tenant

from . import pdf, private_files, signing
from .models import Establishment, SignatureEvent, SignatureRequest, Timesheet
from .timesheet import first_of_month, month_label, month_sheet

logger = logging.getLogger(__name__)

Status = SignatureRequest.Status
Kind = SignatureEvent.Kind
Identification = SignatureRequest.Identification

LINK_VALIDITY = timedelta(days=14)
CODE_VALIDITY = timedelta(minutes=15)
CODE_MAX_ATTEMPTS = 5
CODES_PER_HOUR = 3
CODE_DIGITS = 6
#: A link token is 43 characters; anything much longer is no token.
TOKEN_MAX_LENGTH = 128
GENESIS = "0" * 64
USER_AGENT_LENGTH = SignatureEvent._meta.get_field("user_agent").max_length

#: The certification texts the employee accepts, versioned: the row records
#: which one (`statement_version`), so changing the words later never
#: changes what an earlier signature said. Add a version; never edit one.
STATEMENTS = {
    "2026-09": "Je certifie que ce relevé correspond aux heures que j'ai effectuées en {month}.",
}
CURRENT_STATEMENT = "2026-09"

UNKNOWN_LINK = (
    "Ce lien n'est pas valide. Vérifiez qu'il a été copié en entier, ou demandez un nouveau lien à votre employeur."
)
EXPIRED_LINK = "Ce lien a expiré. Demandez-en un nouveau à votre employeur."
CANCELLED_LINK = "Cette demande de signature a été annulée par l'employeur : il n'y a plus rien à signer avec ce lien."
SUPERSEDED_LINK = "Ce relevé a été corrigé depuis : c'est la nouvelle version qu'il faut signer, avec son propre lien."
#: What the employee's page says when his signature cannot be made for a
#: reason that is the server's, not his: never the reason itself - a path, a
#: setting's name - which goes to the server's log.
DOCUMENT_CHANGED = (
    "Le document à signer ne correspond plus à celui qui a été figé : rien n'a été signé. Prévenez votre employeur."
)
NOT_SIGNED = "La signature n'a pas pu être enregistrée : rien n'a été signé. Prévenez votre employeur."
#: Refused to a code by e-mail while the one the employer handed over can
#: still be typed: anyone holding the link could otherwise void it.
HANDED_OVER_CODE_WAITING = "Votre employeur vous a donné un code : tapez-le ci-dessous (il vaut 15 minutes)."
#: « Contresigner » posted before the employee signed (a page drawn earlier).
COUNTERSIGN_TOO_EARLY = "Ce relevé n'est pas encore signé du côté salarié : il se contresigne ensuite."
#: Where the SHA-256 of the employer's drawing is recorded: the COUNTERSIGNED
#: event's detail - chained, and sealed in the countersignature's /Reason. A
#: column would have been a migration of a database the owner had just
#: migrated (28/09), for a value the event and the signed PDF already hold.
EMPLOYER_DRAWING = "employer_signature_sha256"


# -- Errors: each a French sentence meant to be shown as it is --------------------------------------------------


class RequestError(signing.SigningError):
    """A step refused, with what to say."""


class NotSignable(RequestError):
    """The month cannot be sent: not saved, or no establishment name."""


class RequestStateError(RequestError):
    """The request is not at the step this action belongs to."""


class IdentificationRequired(RequestError):
    """The employee has not verified a code in this session, for this request."""


class CodeError(RequestError):
    """A code refused - wrong, expired, used up - or none may be sent now."""


class LinkError(RequestError):
    """The link reaches nothing: `status` is 404 (unknown) or 410 (gone)."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


# -- Small things -----------------------------------------------------------------------------------------------


def _now(now=None) -> dt.datetime:
    return now if now is not None else timezone.now()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def link_hash(token) -> str:
    """The hash a link is found by - "" for what is no token at all (not a
    string, empty, far longer than one): nothing is hashed or looked up for
    it, and it reaches nothing."""
    if not isinstance(token, str) or not token or len(token) > TOKEN_MAX_LENGTH:
        return ""
    return hash_token(token)


def statement_text(request: SignatureRequest, version: str | None = None) -> str:
    """« Je certifie que ce relevé correspond aux heures que j'ai effectuées
    en juin 2026. » - the version the request records, else the current."""
    version = version or request.statement_version or CURRENT_STATEMENT
    template = STATEMENTS.get(version, STATEMENTS[CURRENT_STATEMENT])
    return template.format(month=request.month_snapshot.get("label") or month_label(request.timesheet.month))


def absolute_link(path: str, build_absolute_uri=None) -> str:
    """The link to send: `settings.SITE_URL` + `path` when the site's
    address is set, else what the page's own request says
    (`request.build_absolute_uri`, passed in)."""
    site = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    if site:
        return site + path
    if build_absolute_uri is not None:
        return build_absolute_uri(path)
    return path


def _establishment():
    return Establishment.objects.filter(pk=Establishment.SINGLETON_PK).first()


# -- The snapshot of the month ----------------------------------------------------------------------------------


def _day_snapshot(day) -> dict:
    cells = pdf.note_cells(day)
    return {
        "date": day.iso,
        "name": day.name,
        "weekday": day.weekday_name,
        "number": day.date.day,
        "kind": day.kind,
        "kind_label": day.label,
        "hours": day.hours_text,
        # The cell exactly as the PDF prints it - cut with « … », « ? » for
        # what cp1252 lacks: what he reads is what he signs.
        "note": "".join(text for text, _grey in cells),
        "note_cells": [{"text": text, "muted": grey != pdf.BLACK} for text, grey in cells],
        "holiday": day.holiday,
        "worked": day.is_work,
        "rest": day.is_rest,
        "absence": day.is_absence,
    }


def month_snapshot(sheet, establishment) -> dict:
    """What the employee's page shows, as JSON, from the SAME `MonthSheet`
    the frozen PDF is drawn from: the establishment, the employee's name and
    nothing else about him, every day with its hours and its « Motif /
    note » cell exactly as the PDF prints it (`pdf.note_cells`), each week's
    total and the summary's lines (`pdf.summary_items`). Version 2 (version
    1 kept the note whole, and the page showed what the PDF had cut)."""
    hours, days = pdf.summary_items(sheet)
    name = " ".join(str(getattr(establishment, "name", "") or "").split())
    return {
        "version": 2,
        "establishment": {"name": name, "address": list(getattr(establishment, "address_lines", []) or [])},
        "employee": {"name": sheet.employee_name},
        "month": sheet.slug,
        "title": sheet.title,
        "label": sheet.label,
        "weekly_hours": sheet.weekly_hours_text,
        "weeks": [
            {
                "label": week.label,
                "total": week.total_text,
                "partial": week.partial,
                "days": [_day_snapshot(day) for day in week.days],
            }
            for week in sheet.weeks
        ],
        "summary": {"hours": [list(item) for item in hours], "days": [list(item) for item in days]},
    }


# -- The event chain --------------------------------------------------------------------------------------------


def _clean_ip(ip) -> str | None:
    """The address EXACTLY as `SignatureEvent.ip` (a GenericIPAddressField)
    will store it, since what is hashed must be what is read back: an IPv6
    through Django's own `clean_ipv6_address` - an IPv4-mapped one, which a
    server listening on [::] reports for every IPv4 client, is
    « ::ffff:203.0.113.7 » there and « ::ffff:cb00:7107 » in Python's
    `compressed`, and a scope (« %eth0 ») is dropped. None for what is no
    address."""
    if not ip:
        return None
    text = str(ip).strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if address.version == 6:
        try:
            return clean_ipv6_address(text)
        except ValidationError:
            return None
    return str(address)


def _clean_user_agent(user_agent) -> str:
    return " ".join(str(user_agent or "").split())[:USER_AGENT_LENGTH]


def _clean_detail(detail) -> dict:
    """Small, JSON-plain and stable: what is hashed is exactly what the
    JSONField gives back."""
    return json.loads(json.dumps(detail or {}, default=str, ensure_ascii=False))


def _moment(at: dt.datetime) -> str:
    return at.astimezone(dt.UTC).isoformat(timespec="microseconds")


def event_hash(request_uuid, previous_hash: str, at, kind: str, ip, user_agent: str, detail: dict) -> str:
    """SHA-256 of the previous hash and the event's canonical content (sorted
    JSON: the request, the moment in UTC to the microsecond, the kind, the
    IP, the device, the detail)."""
    content = json.dumps(
        {
            "request": str(request_uuid),
            "previous": previous_hash,
            "at": _moment(at),
            "kind": str(kind),
            "ip": ip or "",
            "user_agent": user_agent or "",
            "detail": detail or {},
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def log_event(request: SignatureRequest, kind: str, *, at=None, ip=None, user_agent="", detail=None) -> SignatureEvent:
    """Append an event to the request's chain, in one transaction with the
    row's `last_event_hash` (SQLite takes the write lock when the
    transaction starts - settings' IMMEDIATE mode - so two appends cannot
    both read the same last event)."""
    if kind not in Kind.values:
        raise ValueError(f"Événement inconnu : {kind!r}")
    at = _now(at)
    ip = _clean_ip(ip)
    user_agent = _clean_user_agent(user_agent)
    detail = _clean_detail(detail)
    with transaction.atomic():
        last = SignatureEvent.objects.filter(request=request).order_by("-id").values_list("hash", flat=True).first()
        previous = last or GENESIS
        event = SignatureEvent.objects.create(
            request=request,
            at=at,
            kind=kind,
            ip=ip,
            user_agent=user_agent,
            detail=detail,
            previous_hash=previous,
            hash=event_hash(request.uuid, previous, at, kind, ip, user_agent, detail),
        )
        SignatureRequest.objects.filter(pk=request.pk).update(last_event_hash=event.hash)
    request.last_event_hash = event.hash
    return event


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    count: int
    message: str
    broken_at: int | None = None  # 1-based position of the first event that does not add up


#: The signatures that seal the journal before them - the event recording
#: each, and how a sentence names it.
_SEALING = (
    (signing.EMPLOYEE_FIELD, Kind.EMPLOYEE_SIGNED, "la signature du salarié"),
    (signing.EMPLOYER_FIELD, Kind.COUNTERSIGNED, "la contresignature de l'employeur"),
)


def _sealed_heads(request: SignatureRequest) -> tuple[dict, str]:
    """({field: the journal's head its signature states}, "") read from the
    newest signed document - or ({}, why it could not be read)."""
    for name, digest in (
        (private_files.FINAL, request.final_pdf_sha256),
        (private_files.EMPLOYEE_SIGNED, request.employee_pdf_sha256),
    ):
        if not digest:
            continue
        try:
            data = private_files.read_checked(request.uuid, name, digest)
        except private_files.AlteredFileError:
            return {}, f"le fichier {name} ne correspond plus à son empreinte"
        except FileNotFoundError:
            return {}, f"le fichier {name} est introuvable"
        reasons = signing.signed_reasons(data)
        return {field: reason.journal for field, reason in reasons.items() if reason.journal}, ""
    return {}, ""


def verify_event_chain(request: SignatureRequest, events=None) -> ChainCheck:
    """Recompute every hash of the request's log, in order; check the head
    the request row keeps; and check that the head each signature SEALED -
    the journal's last hash when it was made, in its /Reason, under its
    timestamp - is still in the chain, before the event recording that
    signature. The first two catch an event edited, removed or cut off by
    somebody who did not work every hash out again; only the third resists
    whoever can rewrite the database, and only up to the last signature.

    `events`, the request's events in order (prefetched by the owner's
    page), saves reading them again - and the row's own `last_event_hash`,
    just read with it, is then the head compared."""
    if events is None:
        events = list(SignatureEvent.objects.filter(request=request).order_by("id")) if request.pk else []
        head = (
            SignatureRequest.objects.filter(pk=request.pk).values_list("last_event_hash", flat=True).first()
            if (request.pk)
            else request.last_event_hash
        )
    else:
        events = list(events)
        head = request.last_event_hash
    previous = GENESIS
    for position, event in enumerate(events, start=1):
        expected = event_hash(request.uuid, previous, event.at, event.kind, event.ip, event.user_agent, event.detail)
        if event.previous_hash != previous or event.hash != expected:
            return ChainCheck(
                False,
                len(events),
                f"Journal altéré : l'événement n° {position} ({event.get_kind_display()}) ne correspond plus à "
                "son empreinte ou à celle de l'événement précédent - il a été modifié, ou un événement a été "
                "retiré avant lui.",
                position,
            )
        previous = event.hash
    if (head or "") != (events[-1].hash if events else ""):
        return ChainCheck(
            False,
            len(events),
            "Journal altéré : le dernier événement enregistré n'est pas celui que la demande a noté - des "
            "événements ont été retirés à la fin, ou le journal a été réécrit.",
            len(events) or None,
        )
    if not events:
        return ChainCheck(True, 0, "Journal vide.")
    intact = f"Journal intègre : {len(events)} événements, chaînés par leurs empreintes"
    heads, unreadable = _sealed_heads(request)
    if unreadable:
        return ChainCheck(
            True,
            len(events),
            f"{intact}. Son scellement dans le document signé n'a pas pu être contrôlé : {unreadable}.",
        )
    position = {event.hash: index for index, event in enumerate(events)}
    sealed_by = ""
    for field, kind, words in _SEALING:
        stated = heads.get(field)
        if not stated:
            continue
        recorded = next((index for index, event in enumerate(events) if event.kind == kind), None)
        if stated not in position or recorded is None or position[stated] >= recorded:
            return ChainCheck(
                False,
                len(events),
                f"Journal altéré : les événements d'avant {words} ne sont plus ceux que le document signé a scellés "
                "- le journal a été réécrit.",
            )
        sealed_by = words
    if sealed_by:
        return ChainCheck(
            True, len(events), f"{intact} ; ceux d'avant {sealed_by} sont scellés dans le document signé."
        )
    return ChainCheck(True, len(events), f"{intact}.")


@dataclass(frozen=True)
class EventLine:
    """An event as a person reads it (the proof file, the owner's history)."""

    when: str  # « 02/07/2026 à 10:24:13 », Paris
    title: str  # what happened
    where: str  # « adresse IP 203.0.113.7 · appareil : … », or ""
    details: tuple[str, ...]  # what the detail says, in words


def _method_words(method) -> str:
    # The labels are plain strings (no gettext_lazy): the stubs' `str | _StrPromise` is wider than what is there.
    return cast(str, dict(Identification.choices).get(str(method), str(method)))


def _details(event: SignatureEvent) -> tuple[str, ...]:
    detail = event.detail or {}
    lines = []
    if event.kind == Kind.CREATED:
        if detail.get("version"):
            lines.append(f"version {detail['version']} du mois")
        if detail.get("document_sha256"):
            lines.append(f"empreinte du document figé : {detail['document_sha256']}")
    elif event.kind in (Kind.CODE_GIVEN, Kind.CODE_SENT):
        if detail.get("to"):
            lines.append(f"envoyé à {detail['to']}")
    elif event.kind == Kind.CODE_FAILED:
        if detail.get("reason"):
            lines.append(str(detail["reason"]))
    elif event.kind == Kind.CODE_VERIFIED:
        # Which code he typed: what the signature's « identifié par » rests on.
        if detail.get("method"):
            lines.append(f"méthode : {_method_words(detail['method'])}")
    elif event.kind == Kind.EMPLOYEE_SIGNED:
        if detail.get("identification"):
            lines.append(f"identification : {_method_words(detail['identification'])}")
        if detail.get("reservation"):
            lines.append("avec réserves")
        if detail.get("signed_sha256"):
            lines.append(f"empreinte du document signé : {detail['signed_sha256']}")
        if detail.get("timestamp_authority"):
            lines.append(f"horodaté par {detail['timestamp_authority']}")
    elif event.kind == Kind.COUNTERSIGNED:
        if detail.get("final_sha256"):
            lines.append(f"empreinte du document contresigné : {detail['final_sha256']}")
        if detail.get(EMPLOYER_DRAWING):
            lines.append(f"signature dessinée de l'employeur : SHA-256 {detail[EMPLOYER_DRAWING]}")
        if detail.get("timestamp_authority"):
            lines.append(f"horodaté par {detail['timestamp_authority']}")
    elif event.kind == Kind.TIMESTAMP_FAILED:
        servers = detail.get("servers") or []
        if servers:
            lines.append("aucune réponse de : " + ", ".join(str(server) for server in servers))
    elif event.kind == Kind.MAIL_FAILED:
        if detail.get("what"):
            lines.append(f"{detail['what']} non envoyé")
        if detail.get("error"):
            lines.append(str(detail["error"]))
    elif event.kind in (Kind.LINK_SENT, Kind.COPY_SENT):
        if detail.get("to"):
            lines.append(f"à {detail['to']}")
    elif event.kind in (Kind.CANCELLED, Kind.SUPERSEDED):
        if detail.get("reason"):
            lines.append(f"motif : {detail['reason']}")
    elif event.kind == Kind.DOWNLOADED:
        if detail.get("file"):
            lines.append(str(detail["file"]))
    elif event.kind == Kind.VERIFIED:
        if detail.get("verdict"):
            lines.append(str(detail["verdict"]))
    return tuple(lines)


def describe_event(event: SignatureEvent) -> EventLine:
    local = event.at.astimezone(signing.PARIS)
    where = []
    if event.ip:
        where.append(f"adresse IP {event.ip}")
    if event.user_agent:
        where.append(f"appareil : {event.user_agent}")
    return EventLine(
        when=f"{local:%d/%m/%Y} à {local:%H:%M:%S}",
        title=event.get_kind_display(),
        where=" · ".join(where),
        details=_details(event),
    )


# -- Expiry and the lock ----------------------------------------------------------------------------------------


def settle_expiry(request: SignatureRequest, now=None) -> SignatureRequest:
    """A waiting request past its date becomes « expirée », once, with its
    event and its proof file written again. Anything else is left alone."""
    now = _now(now)
    if request.status == Status.PENDING and now >= request.expires_at:
        if SignatureRequest.objects.filter(pk=request.pk, status=Status.PENDING).update(status=Status.EXPIRED):
            log_event(request, Kind.EXPIRED, at=now, detail={"expires_at": _moment(request.expires_at)})
            request.refresh_from_db()
            store_proof(request, now=now)
        else:
            request.refresh_from_db()
    return request


def open_request(timesheet: Timesheet | None, now=None) -> SignatureRequest | None:
    """The request holding the month - waiting, signed by the employee, or
    finished - or None."""
    if timesheet is None or timesheet.pk is None:
        return None
    for request in timesheet.signature_requests.filter(status__in=SignatureRequest.LOCKING_STATUSES):
        request = settle_expiry(request, now)
        if request.locks_month:
            return request
    return None


def month_is_locked(timesheet: Timesheet | None, now=None) -> bool:
    """Whether the month's editor is read-only: a request holds it."""
    return open_request(timesheet, now) is not None


def requests_for(timesheet: Timesheet | None, now=None) -> list[SignatureRequest]:
    """Every version sent for this month, newest first, expiry settled."""
    if timesheet is None or timesheet.pk is None:
        return []
    return [settle_expiry(request, now) for request in timesheet.signature_requests.order_by("-version")]


# -- Creating and correcting ------------------------------------------------------------------------------------


def create_request(employee, month: date, *, now=None, ip=None, user_agent="") -> tuple[SignatureRequest, str]:
    """Freeze the saved month and make its request; returns the row and the
    link's secret token - shown or mailed once, stored nowhere."""
    now = _now(now)
    first = first_of_month(month)
    timesheet = Timesheet.objects.filter(employee=employee, month=first).first()
    if timesheet is None:
        raise NotSignable(signing.UNSAVED_MONTH)
    establishment = _establishment()
    problem = signing.setup_problem(establishment)
    if problem:
        raise NotSignable(problem)
    if open_request(timesheet, now) is not None:
        raise RequestStateError(
            f"Le mois de {month_label(first)} a déjà une demande de signature en cours : « Corriger ce mois » "
            "l'annule avant d'en envoyer une nouvelle version."
        )
    sheet = month_sheet(employee, first)
    frozen = signing.freeze(sheet, establishment)
    token = secrets.token_urlsafe(32)
    try:
        with transaction.atomic():
            last = timesheet.signature_requests.order_by("-version").values_list("version", flat=True).first()
            request = SignatureRequest.objects.create(
                timesheet=timesheet,
                version=(last or 0) + 1,
                created_at=now,
                expires_at=now + LINK_VALIDITY,
                token_hash=hash_token(token),
                month_snapshot=month_snapshot(sheet, establishment),
                document_sha256=private_files.sha256(frozen),
            )
            private_files.write(request.uuid, private_files.DOCUMENT, frozen)
            log_event(
                request,
                Kind.CREATED,
                at=now,
                ip=ip,
                user_agent=user_agent,
                detail={"version": request.version, "month": sheet.slug, "document_sha256": request.document_sha256},
            )
            # The public page finds this tenant by the link's hash.
            # Written here, it rolls this request back if it fails; a
            # rollback after it leaves a hash no request holds: « lien
            # inconnu », as it would be anyway.
            links.register(request.token_hash)
    except IntegrityError:
        # Another request was made for the month in the same instant.
        raise RequestStateError(f"Le mois de {month_label(first)} a déjà une demande de signature en cours.") from None
    store_proof(request, now=now)
    return request, token


def renew_link(request: SignatureRequest, *, now=None, ip=None, user_agent="") -> str:
    """A new secret link for a request that still holds its month (the old
    one stops working), valid `LINK_VALIDITY` from now."""
    now = _now(now)
    request = settle_expiry(request, now)
    if not request.locks_month:
        raise RequestStateError("Cette demande n'est plus en cours : envoyez le mois de nouveau pour obtenir un lien.")
    token = secrets.token_urlsafe(32)
    old_hash = request.token_hash
    request.token_hash = hash_token(token)
    request.expires_at = now + LINK_VALIDITY
    # The new link is indexed before the row names it, the old one forgotten
    # once it no longer does: a hash left behind by a failure
    # reaches no request - « lien inconnu », as it should.
    links.register(request.token_hash)
    request.save(update_fields=["token_hash", "expires_at"])
    log_event(
        request,
        Kind.LINK_RENEWED,
        at=now,
        ip=ip,
        user_agent=user_agent,
        detail={"expires_at": _moment(request.expires_at)},
    )
    transaction.on_commit(partial(links.forget, old_hash), robust=True)
    return token


def cancel_request(
    request: SignatureRequest, reason: str = "", *, now=None, ip=None, user_agent=""
) -> SignatureRequest:
    """« Annuler la demande »: a waiting or half-signed request, files kept."""
    now = _now(now)
    request = settle_expiry(request, now)
    if request.status == Status.COMPLETE:
        raise RequestStateError(
            "Ce relevé est signé et contresigné : pour le modifier, « Corriger ce mois » le remplace par une "
            "nouvelle version."
        )
    if request.status not in (Status.PENDING, Status.EMPLOYEE_SIGNED):
        raise RequestStateError("Cette demande n'est plus en cours : il n'y a rien à annuler.")
    reason = " ".join(str(reason or "").split())[:500]
    updated = SignatureRequest.objects.filter(pk=request.pk, status=request.status).update(
        status=Status.CANCELLED, cancelled_reason=reason
    )
    if not updated:
        raise RequestStateError("Cette demande vient de changer : rechargez la page.")
    log_event(request, Kind.CANCELLED, at=now, ip=ip, user_agent=user_agent, detail={"reason": reason})
    request.refresh_from_db()
    store_proof(request, now=now)
    return request


def reopen_month(
    timesheet: Timesheet, reason: str = "", *, now=None, ip=None, user_agent=""
) -> SignatureRequest | None:
    """« Corriger ce mois »: the request holding the month is cancelled
    (waiting, or signed by the employee only) or superseded (finished) -
    its files kept - and the month can be edited again. None when no
    request held it."""
    now = _now(now)
    request = open_request(timesheet, now)
    if request is None:
        return None
    reason = " ".join(str(reason or "").split())[:500] or "mois corrigé par l'employeur"
    if request.status == Status.COMPLETE:
        new_status, kind = Status.SUPERSEDED, Kind.SUPERSEDED
    else:
        new_status, kind = Status.CANCELLED, Kind.CANCELLED
    updated = SignatureRequest.objects.filter(pk=request.pk, status=request.status).update(
        status=new_status, cancelled_reason=reason
    )
    if not updated:
        raise RequestStateError("Cette demande vient de changer : rechargez la page.")
    log_event(request, kind, at=now, ip=ip, user_agent=user_agent, detail={"reason": reason})
    request.refresh_from_db()
    store_proof(request, now=now)
    return request


# -- The link ---------------------------------------------------------------------------------------------------


def resolve_link(token, now=None) -> SignatureRequest:
    """The request a link reaches, or LinkError (404 unknown, 410 gone).
    Nothing about any other request is ever said. Looked up in the BOUND
    tenant: the public views bind the one the link's hash is indexed under
    first (staff/public_views.py)."""
    now = _now(now)
    token_hash = link_hash(token)
    if not token_hash:
        raise LinkError(404, UNKNOWN_LINK)
    request = SignatureRequest.objects.select_related("timesheet__employee").filter(token_hash=token_hash).first()
    if request is None:
        raise LinkError(404, UNKNOWN_LINK)
    request = settle_expiry(request, now)
    if request.status == Status.CANCELLED:
        raise LinkError(410, CANCELLED_LINK)
    if request.status == Status.SUPERSEDED:
        raise LinkError(410, SUPERSEDED_LINK)
    if request.status == Status.EXPIRED or now >= request.expires_at:
        raise LinkError(410, EXPIRED_LINK)
    return request


def index_links() -> tuple[int, int]:
    """Make the accounts database's index of this tenant's links hold
    exactly the hashes its requests hold (bound): the missing ones are
    registered, the ones no request holds any more are forgotten - for a
    tenant whose database was adopted or put back from a copy. Returns
    (added, removed)."""
    tenant = require_tenant()
    held = set(SignatureRequest.objects.values_list("token_hash", flat=True))
    indexed = set(SigningLink.objects.filter(tenant=tenant).values_list("token_hash", flat=True))
    missing = sorted(held - indexed)
    for token_hash in missing:
        links.register(token_hash)
    stale = sorted(indexed - held)
    links.forget(*stale)
    return len(missing), len(stale)


def _opened_key(request) -> str:
    return f"staff-signature-opened-{request.uuid}"


def note_link_opened(request: SignatureRequest, session, *, ip=None, user_agent="", now=None) -> None:
    """« Lien ouvert », once per session."""
    key = _opened_key(request)
    if session.get(key):
        return
    log_event(request, Kind.LINK_OPENED, at=now, ip=ip, user_agent=user_agent)
    session[key] = True


# -- The one-time code ------------------------------------------------------------------------------------------


def code_hash(request: SignatureRequest, code: str) -> str:
    """HMAC-SHA256 of the code with SECRET_KEY, bound to the request."""
    message = f"{request.uuid}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _codes_in_the_last_hour(request: SignatureRequest, now, method: str) -> int:
    """The codes of `method` issued in the hour before `now`. Each way has
    its own three: counted together, anyone holding only the link could use
    up the hour with « Recevoir un code par e-mail » and the employer was
    refused the code he hands over himself. A mail that could not carry its
    code counts, so a broken mail server cannot be hammered."""
    since = now - timedelta(hours=1)
    events = SignatureEvent.objects.filter(request=request, at__gt=since, at__lte=now)
    if method == Identification.CODE_HANDED_OVER:
        return events.filter(kind=Kind.CODE_GIVEN).count()
    sent = events.filter(kind=Kind.CODE_SENT).count()
    failed_mails = sum(
        1
        for detail in events.filter(kind=Kind.MAIL_FAILED).values_list("detail", flat=True)
        if (detail or {}).get("what") == "code"
    )
    return sent + failed_mails


def waiting_code_method(request: SignatureRequest, now=None) -> str:
    """The method of the code that can still be typed - issued, within its
    15 minutes, tries left - or "" when none can."""
    now = _now(now)
    if not request.code_hash or request.code_sent_at is None:
        return ""
    if now > request.code_sent_at + CODE_VALIDITY or request.code_attempts >= CODE_MAX_ATTEMPTS:
        return ""
    return request.code_method or ""


def _waiting(request: SignatureRequest, now) -> SignatureRequest:
    request = settle_expiry(request, now)
    if request.status == Status.PENDING:
        return request
    if request.status == Status.EMPLOYEE_SIGNED or request.status == Status.COMPLETE:
        raise RequestStateError("Ce relevé est déjà signé.")
    if request.status == Status.EXPIRED:
        raise RequestStateError(EXPIRED_LINK)
    if request.status == Status.SUPERSEDED:
        raise RequestStateError(SUPERSEDED_LINK)
    raise RequestStateError(CANCELLED_LINK)


def issue_code(request: SignatureRequest, method: str, *, now=None, ip=None, user_agent="", log: bool = True) -> str:
    """A new six-digit code for the request, replacing any previous one.
    `method` is `Identification.CODE_BY_EMAIL` (the caller mails it and logs
    `code_sent` - staff/signature_mail.py, which passes `log=False`) or
    `CODE_HANDED_OVER` (shown ONCE to the owner: logged `code_given` here).
    At most `CODES_PER_HOUR` an hour of each. The code itself is returned
    and never stored nor logged.

    Its method is kept beside it (`code_method`) and becomes the request's
    `identification` only once that code is typed (`check_code`): a code
    merely issued - by anyone holding the link - never rewrites how he was
    identified. A code by e-mail never replaces one the employer handed over
    that can still be typed (`HANDED_OVER_CODE_WAITING`); the employer's
    replaces anything."""
    if method not in Identification.values:
        raise ValueError(f"Méthode d'identification inconnue : {method!r}")
    now = _now(now)
    request = _waiting(request, now)
    request.refresh_from_db(fields=["code_hash", "code_sent_at", "code_attempts", "code_method"])
    if method == Identification.CODE_BY_EMAIL and waiting_code_method(request, now) == Identification.CODE_HANDED_OVER:
        raise CodeError(HANDED_OVER_CODE_WAITING)
    if _codes_in_the_last_hour(request, now, method) >= CODES_PER_HOUR:
        raise CodeError(
            f"Déjà {CODES_PER_HOUR} codes demandés dans l'heure : attendez un peu avant d'en demander un autre."
        )
    code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
    request.code_hash = code_hash(request, code)
    request.code_sent_at = now
    request.code_attempts = 0
    request.code_method = method
    request.save(update_fields=["code_hash", "code_sent_at", "code_attempts", "code_method"])
    if log and method == Identification.CODE_HANDED_OVER:
        log_event(request, Kind.CODE_GIVEN, at=now, ip=ip, user_agent=user_agent)
    elif log:
        log_event(request, Kind.CODE_SENT, at=now, ip=ip, user_agent=user_agent)
    return code


def withdraw_code(request: SignatureRequest) -> None:
    """Forget the current code (an e-mail that could not carry it)."""
    request.code_hash = ""
    request.save(update_fields=["code_hash"])


def _session_key(request) -> str:
    return f"staff-signature-identified-{request.uuid}"


def is_identified(session, request: SignatureRequest) -> bool:
    """Whether THIS session verified a code for THIS request."""
    if request.code_verified_at is None:
        return False
    return session.get(_session_key(request)) == _moment(request.code_verified_at)


#: Said when a guess arriving at the same moment used the code first.
CODE_JUST_USED = "Ce code vient d'être utilisé : demandez un nouveau code."
#: How often a guess whose reservation lost the race reads the row again: the
#: next reading refuses it (no tries left, no code) or finds a new code.
_RESERVATION_ROUNDS = 3


def check_code(request: SignatureRequest, typed, session, *, now=None, ip=None, user_agent="") -> None:
    """Check the code the employee typed; on success remember it in his
    session for this request only and use the code up. Every refusal is a
    `CodeError` and a `code_failed` event.

    **A try is reserved before the code is compared** (security audit
    SIGN-1): one conditional UPDATE - this very code, fewer than
    `CODE_MAX_ATTEMPTS` tries so far - counts it, and only a guess that got
    one is compared. Read, compared, then written back as read + 1, guesses
    sent together all read the same count: 40 at once were 40 comparisons
    and the count ended at 1. The code is used up the same way (an UPDATE
    filtered on it): two right guesses at once identify once. Each write is
    its own short transaction - IMMEDIATE (settings), so it waits for the
    write lock instead of failing « database is locked »."""
    now = _now(now)
    request = _waiting(request, now)
    digits = "".join(str(typed or "").split())

    def refuse(message, reason):
        log_event(request, Kind.CODE_FAILED, at=now, ip=ip, user_agent=user_agent, detail={"reason": reason})
        raise CodeError(message)

    for _round in range(_RESERVATION_ROUNDS):
        request.refresh_from_db(fields=["code_hash", "code_sent_at", "code_attempts", "code_method", "identification"])
        if not request.code_hash:
            refuse("Aucun code en cours : demandez un nouveau code.", "aucun code en cours")
        if now > request.code_sent_at + CODE_VALIDITY:
            refuse("Ce code a expiré (il vaut 15 minutes) : demandez-en un nouveau.", "code expiré")
        if request.code_attempts >= CODE_MAX_ATTEMPTS:
            refuse("Trop d'essais avec ce code : demandez un nouveau code.", "trop d'essais")
        stored, method = request.code_hash, request.code_method
        with transaction.atomic():
            reserved = SignatureRequest.objects.filter(
                pk=request.pk, code_hash=stored, code_attempts__lt=CODE_MAX_ATTEMPTS
            ).update(code_attempts=F("code_attempts") + 1)
        if reserved:
            break
    else:
        refuse("Trop d'essais avec ce code : demandez un nouveau code.", "trop d'essais")
    # This try's number, or a later one when guesses arrived together: the
    # tries left are never said to be more than they are.
    attempts = SignatureRequest.objects.filter(pk=request.pk).values_list("code_attempts", flat=True).first() or 0
    request.code_attempts = attempts
    if hmac.compare_digest(code_hash(request, digits), stored):
        # He is identified by the code he typed - its method, and only now.
        # A verification elsewhere later changes `code_verified_at`, which
        # takes this session's identification away (`is_identified`).
        identification = method or request.identification
        with transaction.atomic():
            used = SignatureRequest.objects.filter(pk=request.pk, code_hash=stored).update(
                code_hash="", code_verified_at=now, identification=identification
            )
        if not used:
            refuse(CODE_JUST_USED, "code déjà utilisé")
        request.code_hash = ""
        request.code_verified_at = now
        request.identification = identification
        log_event(
            request, Kind.CODE_VERIFIED, at=now, ip=ip, user_agent=user_agent, detail={"method": request.identification}
        )
        session[_session_key(request)] = _moment(now)
        return
    left = CODE_MAX_ATTEMPTS - attempts
    if left <= 0:
        with transaction.atomic():
            SignatureRequest.objects.filter(pk=request.pk, code_hash=stored).update(code_hash="")
        request.code_hash = ""
        refuse(
            "Code erroné, et c'était le dernier essai : demandez un nouveau code.",
            f"code erroné, essai {attempts} sur {CODE_MAX_ATTEMPTS}",
        )
    refuse(
        f"Code erroné. Encore {left} essai{'s' if left > 1 else ''} avec ce code.",
        f"code erroné, essai {attempts} sur {CODE_MAX_ATTEMPTS}",
    )


# -- Signing ----------------------------------------------------------------------------------------------------


def sign_for_employee(
    request: SignatureRequest,
    png: bytes,
    *,
    session,
    statement_accepted: bool,
    reservation: str = "",
    now=None,
    ip=None,
    user_agent="",
) -> SignatureRequest:
    """The employee signs: identified in this session, the certification
    ticked, his drawing (raw PNG bytes - `signing.signature_png_from_data_url`
    decodes what the page posts). Raises a French `SigningError` and stores
    nothing when anything is missing, when no timestamp server answers (the
    event says so), or when the request moved meanwhile.

    It is called from the employee's page only, so what it raises is said
    to HIM: his drawing refused, the box not ticked, the step, no timestamp
    - each in its own words; anything else that is the server's (a file
    gone, keys that do not open, a name a certificate cannot hold) is
    `DOCUMENT_CHANGED` or `NOT_SIGNED`, its detail in the server's log -
    never a path or a setting's name on a page anyone with the link can
    open.

    What he signed is recorded where the database alone cannot rewrite it:
    his reservations inside his signature (`signing.sign_as_employee`), with
    the journal's head at that moment - the events before it are sealed
    there (`verify_event_chain`) - and in the signed event, with the text he
    certified and the authority that issued his certificate."""
    now = _now(now)
    request = _waiting(request, now)
    if not is_identified(session, request):
        raise IdentificationRequired(
            "Identifiez-vous d'abord avec le code à usage unique, sur cette page, avant de signer."
        )
    if not statement_accepted:
        raise RequestError(f"Cochez « {statement_text(request, CURRENT_STATEMENT)} » pour signer.")
    reservation = str(reservation or "").strip()[:2000]
    image = signing.clean_signature_png(png)
    try:
        frozen = private_files.read_checked(request.uuid, private_files.DOCUMENT, request.document_sha256)
    except (private_files.AlteredFileError, FileNotFoundError) as error:
        logger.warning("Demande %s : le document figé ne peut pas être signé (%s)", request.uuid, error)
        raise RequestError(DOCUMENT_CHANGED) from None
    employee = request.timesheet.employee
    journal = SignatureRequest.objects.filter(pk=request.pk).values_list("last_event_hash", flat=True).first() or ""
    try:
        signed = signing.sign_as_employee(
            frozen,
            employee,
            image,
            now,
            document_id=request.document_id,
            establishment=_establishment(),
            reservation=reservation,
            journal=journal,
        )
    except signing.TimestampUnavailable as error:
        log_event(
            request,
            Kind.TIMESTAMP_FAILED,
            at=now,
            ip=ip,
            user_agent=user_agent,
            detail={"servers": [server for server, _reason in error.failures], "step": "salarié"},
        )
        raise
    except signing.SignatureImageError:
        raise
    except signing.SigningError as error:
        logger.warning("Demande %s : la signature du salarié n'a pas pu être faite (%s)", request.uuid, error)
        raise RequestError(NOT_SIGNED) from None
    signed_sha = private_files.sha256(signed.pdf)
    image_sha = private_files.sha256(image)
    with transaction.atomic():
        moved = SignatureRequest.objects.filter(pk=request.pk, status=Status.PENDING).update(
            status=Status.EMPLOYEE_SIGNED,
            employee_signed_at=now,
            employee_pdf_sha256=signed_sha,
            signature_png_sha256=image_sha,
            employee_timestamp_at=signed.timestamp,
            employee_timestamp_authority=signed.authority[:255],
            reservation=reservation,
            statement_version=CURRENT_STATEMENT,
            code_hash="",
        )
        if not moved:
            raise RequestStateError("Ce relevé vient d'être signé ou annulé : rechargez la page.")
        private_files.write(request.uuid, private_files.SIGNATURE_IMAGE, image)
        private_files.write(request.uuid, private_files.EMPLOYEE_SIGNED, signed.pdf)
        log_event(
            request,
            Kind.EMPLOYEE_SIGNED,
            at=now,
            ip=ip,
            user_agent=user_agent,
            detail={
                "signed_sha256": signed_sha,
                "signature_png_sha256": image_sha,
                "statement_version": CURRENT_STATEMENT,
                "statement": statement_text(request, CURRENT_STATEMENT),
                # His own words, as his signature carries them ("" for none).
                "reservation": reservation,
                "timestamp_authority": signed.authority,
                "timestamp": _moment(signed.timestamp) if signed.timestamp else None,
                "identification": request.identification,
                "authority_sha256": signed.issuer_sha256,
                "journal": journal,
            },
        )
    request.refresh_from_db()
    store_proof(request, now=now)
    return request


def countersignable(request: SignatureRequest) -> SignatureRequest:
    """The request read again, or `RequestStateError` when it is not signed
    by the employee alone - the step a « Contresigner » belongs to, checked
    before anything about the drawing it posts."""
    request.refresh_from_db()
    if request.status != Status.EMPLOYEE_SIGNED:
        raise RequestStateError(
            COUNTERSIGN_TOO_EARLY if request.status == Status.PENDING else "Cette demande n'est pas à contresigner."
        )
    return request


def countersign_request(
    request: SignatureRequest, png: bytes | None, *, now=None, ip=None, user_agent=""
) -> SignatureRequest:
    """The employer countersigns a request the employee signed, with his own
    drawn signature - `png`, raw PNG bytes (`signing.signature_png_from_data_url`
    decodes what the page posts). The step is checked first, so a page drawn
    before the employee signed is told that rather than to draw; then the
    drawing is required and checked as the employee's is
    (`signing.countersign`), and refused - `SignatureImageError`, in its own
    words - with nothing stored. Kept as `employer_signature.png`, its
    SHA-256 recorded in the COUNTERSIGNED event (`EMPLOYER_DRAWING`) and
    sealed in the countersignature with the journal's head. The link gets a
    new fortnight so the employee can fetch his final copy."""
    now = _now(now)
    countersignable(request)
    if not png:
        raise signing.SignatureImageError(signing.EMPLOYER_DRAWING_MISSING)
    try:
        employee_signed = private_files.read_checked(
            request.uuid, private_files.EMPLOYEE_SIGNED, request.employee_pdf_sha256
        )
    except (private_files.AlteredFileError, FileNotFoundError) as error:
        raise RequestError(
            f"Le document signé avant contreseing a changé sur le disque ({error}) : rien n'a été contresigné."
        ) from None
    journal = SignatureRequest.objects.filter(pk=request.pk).values_list("last_event_hash", flat=True).first() or ""
    try:
        signed = signing.countersign(
            employee_signed, _establishment(), png, now, document_id=request.document_id, journal=journal
        )
    except signing.TimestampUnavailable as error:
        log_event(
            request,
            Kind.TIMESTAMP_FAILED,
            at=now,
            ip=ip,
            user_agent=user_agent,
            detail={"servers": [server for server, _reason in error.failures], "step": "employeur"},
        )
        raise
    except signing.SignatureImageError:
        raise
    except signing.SigningError as error:
        raise RequestError(str(error)) from None
    final_sha = private_files.sha256(signed.pdf)
    with transaction.atomic():
        moved = SignatureRequest.objects.filter(pk=request.pk, status=Status.EMPLOYEE_SIGNED).update(
            status=Status.COMPLETE,
            employer_signed_at=now,
            final_pdf_sha256=final_sha,
            employer_timestamp_at=signed.timestamp,
            employer_timestamp_authority=signed.authority[:255],
            expires_at=max(request.expires_at, now + LINK_VALIDITY),
        )
        if not moved:
            raise RequestStateError("Cette demande vient de changer : rechargez la page.")
        # The picture `signing.countersign` kept and sealed - not the one
        # posted, whose chunks it dropped.
        private_files.write(request.uuid, private_files.EMPLOYER_SIGNATURE_IMAGE, signed.drawing)
        private_files.write(request.uuid, private_files.FINAL, signed.pdf)
        log_event(
            request,
            Kind.COUNTERSIGNED,
            at=now,
            ip=ip,
            user_agent=user_agent,
            detail={
                "final_sha256": final_sha,
                EMPLOYER_DRAWING: signed.drawing_sha256,
                "timestamp_authority": signed.authority,
                "timestamp": _moment(signed.timestamp) if signed.timestamp else None,
                "authority_sha256": signed.issuer_sha256,
                "journal": journal,
            },
        )
    request.refresh_from_db()
    store_proof(request, now=now)
    return request


def employer_drawing_sha256(request: SignatureRequest, events=None) -> str:
    """The SHA-256 of the employer's drawn signature, as the COUNTERSIGNED
    event recorded it - "" before the countersignature, and for a request
    countersigned before 28/09, which has none. `events`, the request's
    events (prefetched by the owner's page), saves reading them again."""
    events = list(request.events.all() if events is None else events)
    countersigned = next((event for event in reversed(events) if event.kind == Kind.COUNTERSIGNED), None)
    if countersigned is None:
        return ""
    return str((countersigned.detail or {}).get(EMPLOYER_DRAWING) or "")


# -- Reading back -----------------------------------------------------------------------------------------------


def latest_document(request: SignatureRequest) -> tuple[str, bytes]:
    """The newest PDF of the request - countersigned, else signed by the
    employee, else as frozen - checked against its recorded hash."""
    for name, digest in (
        (private_files.FINAL, request.final_pdf_sha256),
        (private_files.EMPLOYEE_SIGNED, request.employee_pdf_sha256),
        (private_files.DOCUMENT, request.document_sha256),
    ):
        if digest:
            return name, private_files.read_checked(request.uuid, name, digest)
    raise FileNotFoundError(f"La demande {request.uuid} n'a aucun document.")


def record_download(request: SignatureRequest, name: str, *, now=None, ip=None, user_agent="") -> None:
    if name not in private_files.FILE_NAMES:
        raise ValueError(f"Fichier inconnu : {name!r}")
    log_event(request, Kind.DOWNLOADED, at=now, ip=ip, user_agent=user_agent, detail={"file": name})


def verify_request(request: SignatureRequest, *, now=None, ip=None, user_agent="") -> signing.Verification:
    """« Vérifier »: `signing.verify` on the newest document, logged."""
    try:
        name, data = latest_document(request)
    except (private_files.AlteredFileError, FileNotFoundError) as error:
        result = signing.Verification(error=str(error))
        name = ""
    else:
        result = signing.verify(data)
    log_event(
        request,
        Kind.VERIFIED,
        at=now,
        ip=ip,
        user_agent=user_agent,
        detail={"file": name, "ok": result.ok, "verdict": result.verdict[:500]},
    )
    return result


def store_proof(request: SignatureRequest, *, now=None) -> bytes:
    """Write the proof file again (staff/proof.py) and record its hash."""
    from . import proof

    data = proof.proof_pdf(request, now=now)
    request.proof_sha256 = private_files.write(request.uuid, private_files.PROOF, data)
    SignatureRequest.objects.filter(pk=request.pk).update(proof_sha256=request.proof_sha256)
    return data
