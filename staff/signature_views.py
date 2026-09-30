"""« Signature », the owner's side of the monthly signature: a section of
the month's page (`staff:month`) and the actions it posts to. The workflow
is staff/signature_requests.py; a view here reads the request, calls it, and
says in French what happened - every refusal is a `signing.SigningError`
carrying its sentence.

What the section offers depends on the month:

* **not saved** - nothing to send: a planning is not a record
  (`signing.UNSAVED_MONTH`); **no establishment name** - its sentence;
* **saved, nothing holding it** - « Envoyer pour signature »: by e-mail when
  a mail server is configured and the employee has an address, else only
  once the owner ticks « Je transmettrai le lien moi-même »;
* **a request holding it** - its state, « Nouveau lien », the « Code à
  transmettre par un autre canal que le lien », « Contresigner… » once the
  employee signed - a drawing pad, static/js/signature_pad.js as on the
  employee's page: the owner draws his own signature, which the
  countersignature requires (28/09) -, « Annuler la demande », « Corriger ce
  mois », « Vérifier », the files (both drawings among them) and the journal;
  the month's grid is then read-only (`timesheet._store` refuses a write
  whatever the page), and the versions before it stay listed with their
  files;
* **every version, whatever its state** - a discreet « Supprimer… »: two
  pages of their own (`signature_delete`, `signature_delete_confirm`), the
  rules in staff/signature_deletion.py. Step 1 says what goes and why it is
  dangerous, and passes only with the box ticked and the phrase typed; step
  2, « Dernière vérification », deletes - with the token step 1 signed.

**Three things are shown once and stored nowhere**: the link made by
« Envoyer » or « Nouveau lien » (only its SHA-256 is kept) and the code to
hand over (only its HMAC). So those three POSTs answer with the month's page
itself - the link or the code in it - rather than a redirect: carried to the
next GET they would have to sit in the session, i.e. in the database or a
cookie. « Vérifier » answers the same way, with its report. So does a
« Contresigner » refused for its drawing or for want of a timestamp: the
sentence beside the pad, the pad open, the drawing painted back when it was
one. Every other action redirects to the month's section with a message.

The owner's pages are behind the login (accounts/). The employee's page is
staff/public_views.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.db.models import Prefetch, prefetch_related_objects
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.tenancy import integrations_allowed
from invoices.integrations import TO_CONFIGURE

from . import pdf, private_files, signature_mail, signing
from . import signature_deletion as deletion
from . import signature_requests as workflow
from .models import Employee, Establishment, SignatureEvent, SignatureRequest, Timesheet
from .timesheet import month_label, month_sheet, month_title

logger = logging.getLogger(__name__)

Status = SignatureRequest.Status

#: The box the owner ticks when the link cannot go by e-mail.
HAND_OVER = "transmettre"
SEND_WITHOUT_MAIL = (
    "Cochez « Je transmettrai le lien moi-même » pour envoyer ce mois : {why}, le lien ne peut pas partir par e-mail."
)
NO_MAIL_SERVER = "aucun serveur d'e-mail n'est configuré"
#: What timesheet.js asks before « Envoyer » leaves a grid changed and not saved.
SEND_WARNING = (
    "Des modifications de la grille ne sont pas enregistrées : c'est le mois enregistré, sans elles, qui serait "
    "envoyé pour signature. Continuer quand même ?"
)
REASON = "motif"
#: The pad's hidden field: the employer's drawing, a PNG data URL.
DRAWING = "signature"
NOTHING_TO_REOPEN = "Ce mois n'est pas en cours de signature : il se modifie déjà."
#: Said once in the section: what the employee's page promises him, and that
#: keeping the promise is a command to run (staff_purge_signatures).
RETENTION_NOTE = (
    "Les signatures sont conservées {years} ans après la fin du mois, comme la page de signature l'annonce ; les "
    "effacer ensuite est à faire : manage.py staff_purge_signatures (--dry-run d'abord), rien ne le lance "
    "automatiquement."
)
#: RETENTION_NOTE in a tenant that runs no command on the server (a hosted
#: bar, accounts.tenancy.integrations_allowed): the words every other page
#: uses for what it cannot do yet (invoices/integrations.py).
RETENTION_NOTE_TO_CONFIGURE = (
    "Les signatures sont conservées {years} ans après la fin du mois, comme la page de signature l'annonce ; leur "
    f"effacement ensuite est {TO_CONFIGURE}."
)

#: A request's state as a pill: the colours the rest of the application gives them.
PILLS = {
    Status.PENDING: "status-PENDING",
    Status.EMPLOYEE_SIGNED: "status-RUNNING",
    Status.COMPLETE: "status-COMPLETE",
}
#: The pill's words where the model's would name « le salarié » beside the
#: person's own name, which the section's sentences use: « En attente de la
#: signature du salarié » over « En attente de la signature de DURAND
#: Jeanne ». The model's labels stay the record's words (the proof file, the
#: admin) - changed there they would be a migration for a word.
PANEL_STATUS = {
    Status.PENDING: "En attente de signature",
    Status.EMPLOYEE_SIGNED: "Signée, à contresigner",
}


@dataclass(frozen=True)
class OwnerFile:
    """One of a request's files as the owner downloads it."""

    slug: str  # in the address: …/signature/1/fichier/<slug>/
    name: str  # its name in the private folder (staff.private_files)
    digest: str  # the request's field holding its SHA-256 - or workflow.EMPLOYER_DRAWING
    label: str  # the link's words: {who}
    filename: str  # the file's name: {who} {month} v{version}
    content_type: str

    def sha256(self, request: SignatureRequest, events=None) -> str:
        """The SHA-256 recorded for this file of `request`, "" when it has
        none. The employer's drawing has no column: its hash is in the
        COUNTERSIGNED event (`events`, when the page prefetched them)."""
        if self.digest == workflow.EMPLOYER_DRAWING:
            return workflow.employer_drawing_sha256(request, events)
        return getattr(request, self.digest)


OWNER_FILES = (
    OwnerFile(
        "document",
        private_files.DOCUMENT,
        "document_sha256",
        "Document figé (PDF)",
        "Relevé d'heures {who} {month} v{version} - figé avant signature.pdf",
        "application/pdf",
    ),
    OwnerFile(
        "signe-salarie",
        private_files.EMPLOYEE_SIGNED,
        "employee_pdf_sha256",
        "Signé par {who} (PDF)",
        "Relevé d'heures {who} {month} v{version} - signé, avant contreseing.pdf",
        "application/pdf",
    ),
    OwnerFile(
        "signe",
        private_files.FINAL,
        "final_pdf_sha256",
        "Signé et contresigné (PDF)",
        "Relevé d'heures {who} {month} v{version} - signé et contresigné.pdf",
        "application/pdf",
    ),
    OwnerFile(
        "preuve",
        private_files.PROOF,
        "proof_sha256",
        "Dossier de preuve (PDF)",
        "Dossier de preuve {who} {month} v{version}.pdf",
        "application/pdf",
    ),
    OwnerFile(
        "dessin",
        private_files.SIGNATURE_IMAGE,
        "signature_png_sha256",
        "Signature dessinée de {who} (PNG)",
        "Signature dessinée {who} {month} v{version}.png",
        "image/png",
    ),
    OwnerFile(
        "dessin-employeur",
        private_files.EMPLOYER_SIGNATURE_IMAGE,
        workflow.EMPLOYER_DRAWING,
        "Signature dessinée de l'employeur (PNG)",
        "Signature dessinée de l'employeur - relevé {who} {month} v{version}.png",
        "image/png",
    ),
)
FILES_BY_SLUG = {spec.slug: spec for spec in OWNER_FILES}


# -- Small things -----------------------------------------------------------------------------------------------


def client(request) -> dict:
    """Who acted, for the request's journal: the address the request came
    from and the browser it says it is."""
    return {"ip": request.META.get("REMOTE_ADDR"), "user_agent": request.META.get("HTTP_USER_AGENT", "")}


def _establishment():
    return Establishment.objects.filter(pk=Establishment.SINGLETON_PK).first()


def _section(person: Employee, month: date) -> str:
    return f"{reverse('staff:month', args=[person.pk, month])}#signature"


def _back(person: Employee, month: date):
    return redirect(_section(person, month))


def _employee(pk) -> Employee:
    return get_object_or_404(Employee, pk=pk)


def _request_of(person: Employee, month: date, version) -> SignatureRequest:
    return get_object_or_404(
        SignatureRequest.objects.select_related("timesheet__employee"),
        timesheet__employee=person,
        timesheet__month=month,
        version=version,
    )


def _mail_to(person: Employee) -> bool:
    """Whether the link, the code and the copy can go by e-mail to him."""
    return signature_mail.mail_configured() and bool((person.email or "").strip())


def _why_no_mail(person: Employee) -> str:
    if not signature_mail.mail_configured():
        return NO_MAIL_SERVER
    return f"{person.display_name} n'a pas d'adresse e-mail"


def _until(moment) -> str:
    """« 16/07/2026 »: a day, in Paris."""
    return signing.french_moment(moment).split(" à ")[0]


def _of(month: date) -> str:
    """« de juin 2026 », « d'août 2026 »."""
    return month_title(month).removeprefix("Mois ")


def _signer_name(sign_request: SignatureRequest, person: Employee) -> str:
    """The name the version was sent under (its snapshot): « DURAND
    Jeanne »."""
    return ((sign_request.month_snapshot or {}).get("employee") or {}).get("name") or person.display_name


# -- What the section shows -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FileLink:
    label: str
    url: str


@dataclass
class RequestBlock:
    """One version of the month sent for signature, as the section draws it."""

    request: SignatureRequest
    person: Employee
    month: date
    events: list = field(default_factory=list)
    chain: workflow.ChainCheck | None = None
    files: list = field(default_factory=list)
    # Shown once, in the answer to the POST that made them.
    new_link: str = ""
    link_note: str = ""
    link_level: str = "info"
    handed_code: str = ""
    verification: signing.Verification | None = None
    verified_at: str = ""
    fingerprint: str = ""  # the authority's, beside a verification report
    # A « Contresigner » refused: what to say beside the pad, and the drawing
    # to paint back on it ("" when what was posted was no drawing).
    countersign_error: str = ""
    countersign_drawing: str = ""

    @property
    def drawing_field(self) -> str:
        return DRAWING

    @property
    def verification_level(self) -> str:
        """The report's colour: green when every signature holds, red when
        one does not or the file could not be read, plain before any."""
        result = self.verification
        if result is None:
            return ""
        if result.ok:
            return "success"
        return "error" if (result.error or result.problems) else ""

    def _url(self, name: str) -> str:
        return reverse(name, args=[self.person.pk, self.month, self.request.version])

    @property
    def version(self) -> int:
        return self.request.version

    @property
    def status(self) -> str:
        return PANEL_STATUS.get(self.request.status) or self.request.get_status_display()

    @property
    def pill(self) -> str:
        return PILLS.get(self.request.status, "status-CANCELLED")

    @property
    def pending(self) -> bool:
        return self.request.status == Status.PENDING

    @property
    def employee_signed(self) -> bool:
        return self.request.status == Status.EMPLOYEE_SIGNED

    @property
    def complete(self) -> bool:
        return self.request.status == Status.COMPLETE

    @property
    def holds_month(self) -> bool:
        return self.request.locks_month

    @property
    def cancellable(self) -> bool:
        return self.request.status in (Status.PENDING, Status.EMPLOYEE_SIGNED)

    @property
    def signed_file(self) -> bool:
        return bool(self.request.employee_pdf_sha256)

    @property
    def created(self) -> str:
        return signing.french_moment(self.request.created_at)

    @property
    def expires(self) -> str:
        return _until(self.request.expires_at)

    @property
    def employee_signed_at(self) -> str:
        return signing.french_moment(self.request.employee_signed_at) if self.request.employee_signed_at else ""

    @property
    def employer_signed_at(self) -> str:
        return signing.french_moment(self.request.employer_signed_at) if self.request.employer_signed_at else ""

    @property
    def mail_to(self) -> bool:
        return _mail_to(self.person)

    @property
    def link_url(self) -> str:
        return self._url("staff:signature_link")

    @property
    def code_url(self) -> str:
        return self._url("staff:signature_code")

    @property
    def countersign_url(self) -> str:
        return self._url("staff:signature_countersign")

    @property
    def cancel_url(self) -> str:
        return self._url("staff:signature_cancel")

    @property
    def verify_url(self) -> str:
        return self._url("staff:signature_verify")

    @property
    def delete_url(self) -> str:
        return self._url("staff:signature_delete")


@dataclass
class SignaturePanel:
    """The month's « Signature » section."""

    person: Employee
    month: date
    saved: bool
    problem: str  # why nothing can be sent: not saved, no establishment name
    current: RequestBlock | None  # the request holding the month
    earlier: list  # the other versions, newest first
    key_warning: str
    adobe: str = signing.ADOBE_UNKNOWN_VALIDITY

    @property
    def locked(self) -> bool:
        return self.current is not None

    @property
    def mail_to(self) -> bool:
        return _mail_to(self.person)

    @property
    def address(self) -> str:
        return (self.person.email or "").strip()

    @property
    def why_no_mail(self) -> str:
        return _why_no_mail(self.person)

    @property
    def mail_configured(self) -> bool:
        return signature_mail.mail_configured()

    @property
    def send_url(self) -> str:
        return reverse("staff:signature_send", args=[self.person.pk, self.month])

    @property
    def reopen_url(self) -> str:
        return reverse("staff:month_reopen", args=[self.person.pk, self.month])

    @property
    def send_warning(self) -> str:
        return SEND_WARNING

    @property
    def hand_over(self) -> str:
        return HAND_OVER

    @property
    def reason_field(self) -> str:
        return REASON

    @property
    def any_signature(self) -> bool:
        blocks = [self.current, *self.earlier] if self.current else self.earlier
        return any(block.signed_file for block in blocks)

    @property
    def retention_note(self) -> str:
        """Once anything was sent: the employee's page promises the deletion
        after the retention years, and nothing runs it by itself - the owner
        is the one told it is his to run. A hosted bar runs no command on
        the server: it is told « à configurer »."""
        if not (self.current or self.earlier):
            return ""
        note = RETENTION_NOTE if integrations_allowed() else RETENTION_NOTE_TO_CONFIGURE
        return note.format(years=getattr(settings, "STAFF_SIGNATURE_RETENTION_YEARS", 5))


def _block(request: SignatureRequest, person: Employee, month: date, shown: dict) -> RequestBlock:
    block = RequestBlock(request, person, month)
    # Prefetched by `panel`, in order: one list for the journal drawn and the
    # chain checked - read once per page, not three times per version.
    events = list(request.events.all())
    block.events = [workflow.describe_event(event) for event in events]
    block.chain = workflow.verify_event_chain(request, events)
    who = _signer_name(request, person)
    block.files = [
        FileLink(
            spec.label.format(who=who),
            reverse("staff:signature_file", args=[person.pk, month, request.version, spec.slug]),
        )
        for spec in OWNER_FILES
        if spec.sha256(request, events)
    ]
    if shown.get("version") == request.version:
        for key in (
            "new_link",
            "link_note",
            "link_level",
            "handed_code",
            "verification",
            "verified_at",
            "countersign_error",
            "countersign_drawing",
        ):
            if key in shown:
                setattr(block, key, shown[key])
        if block.verification is not None:
            block.fingerprint = signing.authority_fingerprint()
    return block


def panel(person: Employee, sheet, **shown) -> SignaturePanel:
    """The section for `sheet`'s month. `shown` is what one answer draws
    once - `version` and `new_link`, `link_note`, `link_level`,
    `handed_code`, `verification`, `verified_at`, `countersign_error`,
    `countersign_drawing` - on that version's block."""
    month = sheet.month
    establishment = _establishment()
    problem = signing.UNSAVED_MONTH if not sheet.saved else signing.setup_problem(establishment)
    requests = workflow.requests_for(sheet.timesheet)
    # Every version's events in one query, whatever the number of versions
    # (`requests_for` settles expiry first: a refresh there drops a stale
    # cache, and this comes after it).
    prefetch_related_objects(requests, Prefetch("events", queryset=SignatureEvent.objects.order_by("id")))
    current = next((request for request in requests if request.locks_month), None)
    return SignaturePanel(
        person=person,
        month=month,
        saved=sheet.saved,
        problem=problem,
        current=_block(current, person, month, shown) if current else None,
        earlier=[_block(request, person, month, shown) for request in requests if request is not current],
        key_warning=signing.key_warning(),
    )


def _render(request, person: Employee, month: date, **shown):
    """The month's page, drawing what this answer shows once."""
    from .views import render_month

    sheet = month_sheet(person, month)
    return render_month(request, person, sheet, signature=panel(person, sheet, **shown))


# -- The actions ------------------------------------------------------------------------------------------------


#: A signed file the mail was to carry is missing from the private folder.
FINAL_COPY_MISSING = (
    "l'exemplaire signé est introuvable dans le dossier privé de l'espace : restaurez-le depuis la sauvegarde."
)


def _mail_error(error) -> str:
    """Why a mail could not carry its file: the app's French sentence, or,
    for a file gone from disk, one of its own - a FileNotFoundError's words
    are the file's full path on the server (security audit LB-3)."""
    if isinstance(error, FileNotFoundError):
        logger.warning("Signature : fichier introuvable pour l'e-mail (%s)", error)
        return FINAL_COPY_MISSING
    return str(error)


def _link_shown(request, person: Employee, sign_request: SignatureRequest, token: str) -> dict:
    """The link, absolute, mailed when it can be - and what to say under it.
    Mailed as what it is for: « à signer » while he has not signed; with his
    final copy attached once countersigned; not at all in between - « vérifiez
    et signez » over a month he signed would be a lie."""
    link = workflow.absolute_link(reverse("staff:sign", args=[token]), request.build_absolute_uri)
    status = sign_request.status
    if not _mail_to(person) or status == Status.EMPLOYEE_SIGNED:
        return {
            "new_link": link,
            "link_note": f"Transmettez ce lien à {person.display_name} vous-même (SMS, messagerie, en main propre) : "
            "il ne s'affichera plus.",
            "link_level": "info",
        }
    try:
        if status == Status.COMPLETE:
            outcome = signature_mail.send_final_copy(sign_request, link, **client(request))
        else:
            outcome = signature_mail.send_link(sign_request, link, **client(request))
    except (signing.SigningError, private_files.AlteredFileError, FileNotFoundError) as error:
        return {
            "new_link": link,
            "link_note": f"L'e-mail n'est pas parti : {_mail_error(error)}",
            "link_level": "warning",
        }
    if outcome.sent:
        note = f"{outcome.message} Vous pouvez aussi le copier pour l'envoyer autrement (SMS, messagerie)."
    else:
        note = outcome.message
    return {"new_link": link, "link_note": note, "link_level": "success" if outcome.sent else "warning"}


def signature_send(request, pk, month):
    """« Envoyer pour signature »: the saved month frozen, its request made,
    the link mailed when it can be - and shown on the page whatever happens,
    once."""
    person = _employee(pk)
    if request.method != "POST":
        return _back(person, month)
    if not Timesheet.objects.filter(employee=person, month=month).exists():
        messages.error(request, signing.UNSAVED_MONTH)
        return _back(person, month)
    if not _mail_to(person) and request.POST.get(HAND_OVER) != "1":
        messages.error(request, SEND_WITHOUT_MAIL.format(why=_why_no_mail(person)))
        return _back(person, month)
    try:
        sign_request, token = workflow.create_request(person, month, **client(request))
    except signing.SigningError as error:
        messages.error(request, str(error))
        return _back(person, month)
    messages.success(
        request,
        f"{month_title(month)} envoyé pour signature (version {sign_request.version}, document n° "
        f"{sign_request.document_id}) : il ne se modifie plus tant que la demande est en cours.",
    )
    shown = _link_shown(request, person, sign_request, token)
    return _render(request, person, month, version=sign_request.version, **shown)


def signature_link(request, pk, month, version):
    """« Nouveau lien »: the old one stops working; the new one is shown once
    (and mailed when it can be)."""
    person = _employee(pk)
    sign_request = _request_of(person, month, version)
    if request.method != "POST":
        return _back(person, month)
    try:
        token = workflow.renew_link(sign_request, **client(request))
    except signing.SigningError as error:
        messages.error(request, str(error))
        return _back(person, month)
    messages.success(request, f"Nouveau lien pour la version {sign_request.version} : l'ancien ne fonctionne plus.")
    shown = _link_shown(request, person, sign_request, token)
    return _render(request, person, month, version=sign_request.version, **shown)


def signature_code(request, pk, month, version):
    """« Code à transmettre par un autre canal que le lien »: a one-time code
    shown ONCE to the owner, who gives it to the employee himself - by voice,
    SMS, never with the link (the proof file says so, `code_remis`)."""
    person = _employee(pk)
    sign_request = _request_of(person, month, version)
    if request.method != "POST":
        return _back(person, month)
    try:
        code = workflow.issue_code(sign_request, SignatureRequest.Identification.CODE_HANDED_OVER, **client(request))
    except signing.SigningError as error:
        messages.error(request, str(error))
        return _back(person, month)
    return _render(request, person, month, version=sign_request.version, handed_code=f"{code[:3]} {code[3:]}")


def _drawn_back(posted: str, png: bytes | None) -> str:
    """What was posted, to paint back on the pad - only when it is a drawing
    the employee's checks keep; never something else echoed into the page."""
    if not png:
        return ""
    try:
        signing.clean_signature_png(png)
    except signing.SignatureImageError:
        return ""
    return posted


def signature_countersign(request, pk, month, version):
    """« Contresigner »: the employer's drawn signature - the pad's hidden
    field, required - on the employee's, and his final copy, by e-mail when
    it can go, else through his link.

    The step is checked first: a page drawn before the employee signed, or
    after the request moved on, is told so at the top of the month (a
    redirect). Past that, a refusal - nothing drawn (the script stops that
    first; a page drawn before the pad existed posts nothing), a drawing the
    checks refuse, no timestamp - redraws the page with the sentence beside
    the pad and the pad open, the drawing painted back when it was one: never
    a 500, and nothing drawn twice."""
    person = _employee(pk)
    sign_request = _request_of(person, month, version)
    if request.method != "POST":
        return _back(person, month)
    posted = request.POST.get(DRAWING, "")
    png = None
    try:
        workflow.countersignable(sign_request)
        png = signing.signature_png_from_data_url(posted) if posted.strip() else None
        sign_request = workflow.countersign_request(sign_request, png, **client(request))
    except workflow.RequestStateError as error:
        messages.error(request, str(error))
        return _back(person, month)
    except signing.SigningError as error:
        response = _render(
            request,
            person,
            month,
            version=sign_request.version,
            countersign_error=str(error),
            countersign_drawing=_drawn_back(posted, png),
        )
        if isinstance(error, signing.TimestampUnavailable):
            response.status_code = 503
        return response
    messages.success(
        request,
        f"Contresigné : le relevé {_of(month)} est signé des deux côtés (version {sign_request.version}).",
    )
    if signature_mail.can_email(sign_request):
        try:
            outcome = signature_mail.send_final_copy(sign_request, **client(request))
        except (signing.SigningError, private_files.AlteredFileError, FileNotFoundError) as error:
            messages.warning(request, f"Son exemplaire final n'a pas été envoyé : {_mail_error(error)}")
        else:
            if outcome.sent:
                messages.success(request, f"Son exemplaire final est parti par e-mail à {person.email.strip()}.")
            else:
                messages.warning(request, outcome.message)
    else:
        messages.info(request, f"Son lien lui donne son exemplaire final jusqu'au {_until(sign_request.expires_at)}.")
    return _back(person, month)


def signature_cancel(request, pk, month, version):
    """« Annuler la demande »: a request waiting, or signed by the employee
    only. Its link stops working, its files are kept, and the month is
    editable again."""
    person = _employee(pk)
    sign_request = _request_of(person, month, version)
    if request.method != "POST":
        return _back(person, month)
    try:
        workflow.cancel_request(sign_request, request.POST.get(REASON, ""), **client(request))
    except signing.SigningError as error:
        messages.error(request, str(error))
        return _back(person, month)
    messages.success(
        request,
        f"Demande annulée (version {sign_request.version}) : son lien ne fonctionne plus, et le mois se modifie "
        "de nouveau.",
    )
    return _back(person, month)


def month_reopen(request, pk, month):
    """« Corriger ce mois »: the request holding the month is cancelled - or,
    finished, superseded - its files kept, and the month can be edited and
    sent again as a new version."""
    person = _employee(pk)
    if request.method != "POST":
        return _back(person, month)
    timesheet = Timesheet.objects.filter(employee=person, month=month).first()
    try:
        reopened = (
            workflow.reopen_month(timesheet, request.POST.get(REASON, ""), **client(request)) if timesheet else None
        )
    except signing.SigningError as error:
        messages.error(request, str(error))
        return _back(person, month)
    if reopened is None:
        messages.info(request, NOTHING_TO_REOPEN)
    elif reopened.status == Status.SUPERSEDED:
        messages.success(
            request,
            f"Mois rouvert : la version {reopened.version}, signée et contresignée, est remplacée et conservée. "
            "Corrigez le mois, puis envoyez la nouvelle version.",
        )
    else:
        messages.success(
            request,
            f"Mois rouvert : la version {reopened.version} est annulée, son lien ne fonctionne plus. Corrigez le "
            "mois, puis envoyez la nouvelle version.",
        )
    return _back(person, month)


def signature_verify(request, pk, month, version):
    """« Vérifier »: `signing.verify` on the newest document of the version,
    offline, logged - and its report on the page."""
    person = _employee(pk)
    sign_request = _request_of(person, month, version)
    if request.method != "POST":
        return _back(person, month)
    result = workflow.verify_request(sign_request, **client(request))
    return _render(
        request,
        person,
        month,
        version=sign_request.version,
        verification=result,
        verified_at=signing.french_moment(timezone.now()),
    )


def signature_file(request, pk, month, version, file):
    """One of a version's files, checked against the hash its row keeps and
    logged (« Document téléchargé »). The proof file is written again first,
    so it holds every event - its own download included. A file changed on
    disk is refused with the sentence saying so, never served."""
    person = _employee(pk)
    sign_request = _request_of(person, month, version)
    spec = FILES_BY_SLUG.get(file)
    digest = spec.sha256(sign_request) if spec is not None else ""
    if not digest:
        raise Http404("Pas de tel fichier pour cette demande.")
    try:
        if spec.name == private_files.PROOF:
            workflow.record_download(sign_request, spec.name, **client(request))
            data = workflow.store_proof(sign_request)
        else:
            data = private_files.read_checked(sign_request.uuid, spec.name, digest)
            workflow.record_download(sign_request, spec.name, **client(request))
    except private_files.AlteredFileError as error:
        messages.error(request, f"{error} Il n'a pas été téléchargé.")
        return _back(person, month)
    except FileNotFoundError:
        messages.error(
            request,
            f"Le fichier {spec.name} de la version {sign_request.version} est introuvable dans le dossier privé "
            "de l'espace : restaurez-le depuis la sauvegarde.",
        )
        return _back(person, month)
    snapshot = sign_request.month_snapshot or {}
    filename = spec.filename.format(
        who=_signer_name(sign_request, person),
        month=snapshot.get("label") or "",
        version=sign_request.version,
    )
    response = HttpResponse(data, content_type=spec.content_type)
    response["Content-Disposition"] = pdf.disposition(filename)
    response["Cache-Control"] = "private, no-store"
    return response


# -- « Supprimer… », in two steps -------------------------------------------------------------------------------


#: What a file of the request's folder is, in the owner's words.
FILE_LABELS = {spec.name: spec.label for spec in OWNER_FILES}


@dataclass(frozen=True)
class DoomedFile:
    name: str  # in the private folder
    label: str  # what it is


@dataclass
class DeletionPage:
    """The two pages of « Supprimer… »: step 1 (what goes, why it is
    dangerous, the box and the phrase) and step 2 (« Dernière
    vérification », its token). What was posted to a refused step 1 is
    drawn back (`typed`, `understood`, `with_hours`) with its `errors`."""

    request: SignatureRequest
    person: Employee
    month: date
    preview: deletion.Preview
    errors: dict = field(default_factory=dict)
    typed: str = ""
    understood: bool = False
    with_hours: bool = False
    token: str = ""

    understood_field: str = deletion.UNDERSTOOD_FIELD
    phrase_field: str = deletion.PHRASE_FIELD
    hours_field: str = deletion.HOURS_FIELD
    token_field: str = deletion.TOKEN_FIELD
    minutes: int = deletion.TOKEN_MAX_AGE // 60

    @property
    def version(self) -> int:
        return self.request.version

    @property
    def who(self) -> str:
        return self.person.display_name

    @property
    def status(self) -> str:
        return PANEL_STATUS.get(self.request.status) or self.request.get_status_display()

    @property
    def pill(self) -> str:
        return PILLS.get(self.request.status, "status-CANCELLED")

    @property
    def month_label(self) -> str:
        return month_label(self.month)

    @property
    def of_month(self) -> str:
        """« de juin 2026 », « d'août 2026 »."""
        return _of(self.month)

    @property
    def month_title(self) -> str:
        return month_title(self.month)

    @property
    def created(self) -> str:
        return signing.french_moment(self.request.created_at)

    @property
    def expires(self) -> str:
        return _until(self.request.expires_at)

    @property
    def employee_signed_at(self) -> str:
        return signing.french_moment(self.request.employee_signed_at) if self.request.employee_signed_at else ""

    @property
    def employer_signed_at(self) -> str:
        return signing.french_moment(self.request.employer_signed_at) if self.request.employer_signed_at else ""

    @property
    def holds_month(self) -> bool:
        return self.request.locks_month

    @property
    def signed(self) -> bool:
        return bool(self.request.employee_pdf_sha256)

    @property
    def files(self) -> list[DoomedFile]:
        who = _signer_name(self.request, self.person)
        return [
            DoomedFile(name, FILE_LABELS[name].format(who=who) if name in FILE_LABELS else "fichier inattendu")
            for name in self.preview.files
        ]

    @property
    def has_proof(self) -> bool:
        return private_files.PROOF in self.preview.files

    @property
    def events(self) -> int:
        return self.preview.events

    @property
    def last_version(self) -> bool:
        return self.preview.last_version

    @property
    def days(self) -> int:
        return self.preview.days

    @property
    def phrase(self) -> str:
        return deletion.deletion_phrase(self.month)

    @property
    def retention_years(self) -> int:
        return int(getattr(settings, "STAFF_SIGNATURE_RETENTION_YEARS", 5))

    @property
    def back_url(self) -> str:
        return _section(self.person, self.month)

    @property
    def step1_url(self) -> str:
        return reverse("staff:signature_delete", args=[self.person.pk, self.month, self.version])

    @property
    def step2_url(self) -> str:
        return reverse("staff:signature_delete_confirm", args=[self.person.pk, self.month, self.version])


def signature_delete(request, pk, month, version):
    """« Supprimer… », step 1. GET: what goes - the version, its state and
    dates, its files by name, its journal, the proof - and why it is
    dangerous. POST: « Je comprends que la suppression est définitive »
    ticked and the phrase typed (and the hours option, offered on the
    month's last version only) - then a redirect to step 2 carrying the
    token they earn. Refused, the page is drawn again as posted with what
    is missing. Nothing is deleted here, whatever is posted."""
    person = _employee(pk)
    sign_request = workflow.settle_expiry(_request_of(person, month, version))
    page = DeletionPage(sign_request, person, month, deletion.preview(sign_request))
    if request.method == "POST":
        page.typed = request.POST.get(deletion.PHRASE_FIELD, "")[:200]
        page.understood = request.POST.get(deletion.UNDERSTOOD_FIELD) == "1"
        page.with_hours = request.POST.get(deletion.HOURS_FIELD) == "1"
        if not page.understood:
            page.errors["understood"] = deletion.UNDERSTOOD_MISSING
        if not deletion.phrase_matches(page.typed, month):
            page.errors["phrase"] = deletion.PHRASE_WRONG.format(phrase=page.phrase)
        if page.with_hours and not page.last_version:
            page.errors["hours"] = deletion.HOURS_NOT_LAST.format(month=page.month_label)
        if not page.errors:
            token = deletion.confirmation_token(sign_request, with_hours=page.with_hours)
            return redirect(f"{page.step2_url}?{urlencode({deletion.TOKEN_FIELD: token})}")
    return render(request, "staff/signature_delete.html", {"page": page})


def _said_deleted(outcome: deletion.Deleted) -> str:
    events = f"{outcome.events} événement{'s' if outcome.events > 1 else ''}"
    count = len(outcome.files)
    files = f"ses {count} fichiers" if count > 1 else ("son fichier" if count else "aucun fichier")
    after = ""
    if outcome.hours_deleted:
        after = " ; les heures enregistrées du mois aussi : il reprend la semaine type"
    elif outcome.held_month:
        after = " ; le mois se modifie de nouveau"
    return (
        f"Version {outcome.version} {_of(outcome.month)} supprimée, avec son journal ({events}) et {files}{after}. "
        "Une trace en est gardée dans deletions.log, dans le dossier privé."
    )


def signature_delete_confirm(request, pk, month, version):
    """« Supprimer… », step 2 - « Dernière vérification ». Both the GET
    (the page, one red button) and the POST (the deletion) read the token
    step 1 signed: under ten minutes old, for THIS version, nothing changed
    since (its state, its documents); the hours option only while no other
    version remains. Anything else goes back to step 1 with the sentence
    saying why, and nothing is deleted. A version that no longer exists -
    the button pressed twice - is said at the month's section."""
    person = _employee(pk)
    sign_request = (
        SignatureRequest.objects.select_related("timesheet__employee")
        .filter(timesheet__employee=person, timesheet__month=month, version=version)
        .first()
    )
    if sign_request is None:
        messages.error(request, deletion.GONE)
        return _back(person, month)
    sign_request = workflow.settle_expiry(sign_request)
    step1 = reverse("staff:signature_delete", args=[person.pk, month, version])
    posted = request.method == "POST"
    token = (request.POST if posted else request.GET).get(deletion.TOKEN_FIELD, "")
    try:
        confirmation = deletion.read_confirmation(token, sign_request)
        if confirmation.with_hours and not deletion.is_last_version(sign_request):
            raise deletion.hours_refused(month)
    except deletion.DeletionRefused as error:
        messages.error(request, str(error))
        return redirect(step1)
    if not posted:
        page = DeletionPage(
            sign_request,
            person,
            month,
            deletion.preview(sign_request),
            with_hours=confirmation.with_hours,
            token=token.strip(),
        )
        return render(request, "staff/signature_delete_confirm.html", {"page": page})
    try:
        outcome = deletion.delete_signature_request(
            sign_request,
            how=deletion.PAGE,
            with_hours=confirmation.with_hours,
            expected=confirmation.expected,
            ip=client(request)["ip"],
        )
    except deletion.DeletionRefused as error:
        messages.error(request, str(error))
        if not SignatureRequest.objects.filter(pk=sign_request.pk).exists():
            return _back(person, month)
        return redirect(step1)
    messages.success(request, _said_deleted(outcome))
    if outcome.files_error:
        messages.warning(request, outcome.files_error)
    return _back(person, month)
