"""E-mail for the monthly signature - optional, off by default: nothing is
sent unless `settings.EMAIL_HOST` is set (« configured »), and the employee
has an address (`Employee.email`). Without it the owner hands the link and
the code over himself (WhatsApp, SMS, in person): the owner's page always
shows the link, whether it was mailed or not.

Three messages, plain text, French, short: the link (with the month and how
long it lasts), the one-time code, and the final copy (attached). Every
subject names the employer - the establishment as the request's snapshot
froze it, the bound tenant's own: in multi mode the mail server and the
sender are the platform's, shared by every bar, and the subject is what says
which bar is asking. Each send
is an event of the request's chain. **A send that fails is never a 500**:
it comes back as `MailOutcome(sent=False, message=…)` - a sentence for the
page - and a `mail_failed` event. A code that could not be sent is withdrawn
and still counts towards the three an hour, so a broken mail server cannot
be hammered from the employee's page.

**Capped** (security audit LB-4): the sender is the platform's, and a bar
chooses both the address (the employee's) and words that go in the mail
(its establishment's name) - « Nouveau lien » pressed twenty times sent
twenty mails. At most `REQUEST_MAILS_PER_HOUR` link or copy mails for one
request in an hour, and `TENANT_MAILS_PER_DAY` signature mails of any kind
(links, codes, copies, failed sends included) for the whole tenant in 24
hours, counted from the requests' own events. Over either, nothing is sent:
the owner's page still shows the link, to hand over himself; the
employee's page says to ask the employer for the code.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from . import private_files, signature_requests, signing
from .models import SignatureEvent, SignatureRequest

Kind = SignatureEvent.Kind

OTHER_CHANNEL = "transmettez-le par un autre moyen (SMS, messagerie, en main propre)."

#: Link or final-copy mails for ONE request in an hour: sent, a new link
#: sent, one retry - the codes have their own three an hour
#: (signature_requests.CODES_PER_HOUR).
REQUEST_MAILS_PER_HOUR = 3
#: Every signature mail of one tenant in 24 hours: a link, a code and a
#: final copy for twenty employees on the same day is 60.
TENANT_MAILS_PER_DAY = 60
#: The events that are a mail sent - or tried: a failed send counts, so a
#: broken server is not hammered either.
_MAIL_EVENTS = (Kind.LINK_SENT, Kind.CODE_SENT, Kind.COPY_SENT, Kind.MAIL_FAILED)
_REQUEST_MAIL_EVENTS = (Kind.LINK_SENT, Kind.COPY_SENT)

REQUEST_CAP_REACHED = (
    "Déjà {count} e-mails envoyés pour cette demande dans l'heure : {what} n'a pas été envoyé, "
    "transmettez-le vous-même (SMS, messagerie, en main propre)."
)
TENANT_CAP_REACHED = (
    "Déjà {count} e-mails de signature envoyés depuis 24 heures : {what} n'a pas été envoyé, "
    "transmettez-le vous-même (SMS, messagerie, en main propre)."
)
#: The employee's page, when the tenant's day is used up: the code comes
#: from the employer instead.
CODE_CAP_REACHED = "Le code ne peut plus être envoyé par e-mail aujourd'hui : demandez-le à votre employeur."


def _request_mails_in_the_last_hour(request: SignatureRequest, now) -> int:
    events = SignatureEvent.objects.filter(request=request, at__gt=now - timedelta(hours=1), at__lte=now)
    sent = events.filter(kind__in=_REQUEST_MAIL_EVENTS).count()
    failed = sum(
        1
        for detail in events.filter(kind=Kind.MAIL_FAILED).values_list("detail", flat=True)
        if (detail or {}).get("what") != "code"
    )
    return sent + failed


def _tenant_mails_in_the_last_day(now) -> int:
    """Every signature mail of the bound tenant (its own database)."""
    return SignatureEvent.objects.filter(kind__in=_MAIL_EVENTS, at__gt=now - timedelta(days=1), at__lte=now).count()


def cap_reached(request: SignatureRequest, what: str, *, now=None) -> str:
    """Why `what` (« le lien », « la copie signée », « le code ») may not be
    mailed now, or "" when it may."""
    now = now or timezone.now()
    count = _tenant_mails_in_the_last_day(now)
    if count >= TENANT_MAILS_PER_DAY:
        return TENANT_CAP_REACHED.format(count=count, what=what)
    if what != "le code":
        count = _request_mails_in_the_last_hour(request, now)
        if count >= REQUEST_MAILS_PER_HOUR:
            return REQUEST_CAP_REACHED.format(count=count, what=what)
    return ""


@dataclass(frozen=True)
class MailOutcome:
    sent: bool
    message: str  # French, for the page


def mail_configured() -> bool:
    return bool((getattr(settings, "EMAIL_HOST", "") or "").strip())


def _address(request: SignatureRequest) -> str:
    return (request.timesheet.employee.email or "").strip()


def can_email(request: SignatureRequest) -> bool:
    """Whether the link, the code and the copy can go by e-mail."""
    return mail_configured() and bool(_address(request))


def _unavailable(request: SignatureRequest, what: str) -> str:
    if not mail_configured():
        # The setting's name only where it can be acted on (multi mode: the
        # owner's tenant; the server is the platform's).
        setting = " (EMAIL_HOST)" if signing.server_settings_may_be_named() else ""
        return f"Aucun serveur d'e-mail n'est configuré{setting} : {what} n'a pas été envoyé, {OTHER_CHANNEL}"
    return f"{request.timesheet.employee.display_name} n'a pas d'adresse e-mail : {what} n'a pas été envoyé, {OTHER_CHANNEL}"


def _greeting(request: SignatureRequest) -> str:
    employee = request.timesheet.employee
    return f"Bonjour {employee.first_name.strip() or employee.display_name},"


def _establishment(request: SignatureRequest) -> str:
    return (request.month_snapshot.get("establishment") or {}).get("name") or "Votre employeur"


def _label(request: SignatureRequest) -> str:
    return request.month_snapshot.get("label") or ""


def _send(
    request, what: str, subject: str, body: str, *, attachments=(), now=None, ip=None, user_agent=""
) -> MailOutcome:
    address = _address(request)
    message = EmailMessage(
        subject=" ".join(subject.split()), body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=[address]
    )
    for name, content, mimetype in attachments:
        message.attach(name, content, mimetype)
    try:
        message.send(fail_silently=False)
    except (smtplib.SMTPException, OSError, ValueError) as error:
        signature_requests.log_event(
            request,
            Kind.MAIL_FAILED,
            at=now,
            ip=ip,
            user_agent=user_agent,
            detail={"what": what, "to": address, "error": type(error).__name__},
        )
        return MailOutcome(False, f"L'e-mail n'a pas pu être envoyé à {address} ({what}) : {OTHER_CHANNEL}")
    return MailOutcome(True, f"E-mail envoyé à {address}.")


def send_link(request: SignatureRequest, link: str, *, ip=None, user_agent="") -> MailOutcome:
    """The signing link (absolute: `signature_requests.absolute_link`)."""
    if not can_email(request):
        return MailOutcome(False, _unavailable(request, "le lien"))
    refused = cap_reached(request, "le lien")
    if refused:
        return MailOutcome(False, refused)
    until = signing.french_moment(request.expires_at).split(" à ")[0]
    body = (
        f"{_greeting(request)}\n\n"
        f"{_establishment(request)} vous demande de vérifier et de signer votre relevé d'heures de {_label(request)}.\n\n"
        f"Ouvrez ce lien pour le lire et le signer (il est valable jusqu'au {until}) :\n{link}\n\n"
        "Si un chiffre vous paraît faux, vous pourrez signer avec des réserves et les écrire.\n"
        "Ce lien vous est personnel : ne le transférez pas.\n"
    )
    outcome = _send(
        request,
        "lien",
        f"Relevé d'heures de {_label(request)} à signer — {_establishment(request)}",
        body,
        ip=ip,
        user_agent=user_agent,
    )
    if outcome.sent:
        signature_requests.log_event(
            request, Kind.LINK_SENT, ip=ip, user_agent=user_agent, detail={"to": _address(request)}
        )
    return outcome


def send_code(request: SignatureRequest, *, now=None, ip=None, user_agent="") -> MailOutcome:
    """A new one-time code, by e-mail to the employee's address. Raises
    `CodeError` when no e-mail can carry it (the page offers « J'ai un
    code » instead) and whatever `issue_code` refuses (three an hour, a
    request no longer waiting)."""
    if not can_email(request):
        raise signature_requests.CodeError(_unavailable(request, "le code"))
    # Before a code is issued: refused, the one waiting stays good.
    if cap_reached(request, "le code", now=now):
        raise signature_requests.CodeError(CODE_CAP_REACHED)
    code = signature_requests.issue_code(
        request, SignatureRequest.Identification.CODE_BY_EMAIL, now=now, ip=ip, user_agent=user_agent, log=False
    )
    body = (
        f"{_greeting(request)}\n\n"
        f"Votre code pour signer le relevé d'heures de {_label(request)} : {code}\n\n"
        "Il est valable 15 minutes et ne sert qu'une fois. Il remplace tout code demandé avant.\n"
        "Ne le communiquez à personne.\n"
    )
    outcome = _send(
        request,
        "code",
        f"Votre code pour signer le relevé de {_label(request)} — {_establishment(request)}",
        body,
        now=now,
        ip=ip,
        user_agent=user_agent,
    )
    if outcome.sent:
        signature_requests.log_event(
            request, Kind.CODE_SENT, at=now, ip=ip, user_agent=user_agent, detail={"to": _address(request)}
        )
        return MailOutcome(True, f"Code envoyé à {_address(request)} : il vaut 15 minutes.")
    signature_requests.withdraw_code(request)
    return outcome


def final_copy_name(request: SignatureRequest) -> str:
    """« Relevé d'heures DUPONT Jeanne juin 2026 signé.pdf »."""
    name = (request.month_snapshot.get("employee") or {}).get("name") or request.timesheet.employee.display_name
    return f"Relevé d'heures {name} {_label(request)} signé.pdf"


def send_final_copy(request: SignatureRequest, link: str | None = None, *, ip=None, user_agent="") -> MailOutcome:
    """The countersigned PDF, attached, once the request is finished."""
    if request.status != SignatureRequest.Status.COMPLETE:
        raise signature_requests.RequestStateError(
            "Le relevé n'est pas encore contresigné : il n'y a pas de copie finale."
        )
    if not can_email(request):
        return MailOutcome(False, _unavailable(request, "la copie signée"))
    refused = cap_reached(request, "la copie signée")
    if refused:
        return MailOutcome(False, refused)
    content = private_files.read_checked(request.uuid, private_files.FINAL, request.final_pdf_sha256)
    body = (
        f"{_greeting(request)}\n\n"
        f"Votre relevé d'heures de {_label(request)} est signé par vous et contresigné par {_establishment(request)}. "
        "Votre exemplaire est joint à ce message.\n"
    )
    if link:
        body += f"\nVous pouvez aussi le télécharger ici tant que le lien est valable :\n{link}\n"
    body += f"\nDocument n° {request.document_id}\n"
    outcome = _send(
        request,
        "copie signée",
        f"Votre relevé d'heures de {_label(request)} signé — {_establishment(request)}",
        body,
        attachments=[(final_copy_name(request), content, "application/pdf")],
        ip=ip,
        user_agent=user_agent,
    )
    if outcome.sent:
        signature_requests.log_event(
            request, Kind.COPY_SENT, ip=ip, user_agent=user_agent, detail={"to": _address(request)}
        )
    return outcome
