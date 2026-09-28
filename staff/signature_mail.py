"""E-mail for the monthly signature - optional, off by default: nothing is
sent unless `settings.EMAIL_HOST` is set (« configured »), and the employee
has an address (`Employee.email`). Without it the owner hands the link and
the code over himself (WhatsApp, SMS, in person): the owner's page always
shows the link, whether it was mailed or not.

Three messages, plain text, French, short: the link (with the month and how
long it lasts), the one-time code, and the final copy (attached). Each send
is an event of the request's chain. **A send that fails is never a 500**:
it comes back as `MailOutcome(sent=False, message=…)` - a sentence for the
page - and a `mail_failed` event. A code that could not be sent is withdrawn
and still counts towards the three an hour, so a broken mail server cannot
be hammered from the employee's page.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass

from django.conf import settings
from django.core.mail import EmailMessage

from . import private_files, signature_requests, signing
from .models import SignatureEvent, SignatureRequest

Kind = SignatureEvent.Kind

OTHER_CHANNEL = "transmettez-le par un autre moyen (SMS, messagerie, en main propre)."


@dataclass(frozen=True)
class MailOutcome:
    sent: bool
    message: str   # French, for the page


def mail_configured() -> bool:
    return bool((getattr(settings, "EMAIL_HOST", "") or "").strip())


def _address(request: SignatureRequest) -> str:
    return (request.timesheet.employee.email or "").strip()


def can_email(request: SignatureRequest) -> bool:
    """Whether the link, the code and the copy can go by e-mail."""
    return mail_configured() and bool(_address(request))


def _unavailable(request: SignatureRequest, what: str) -> str:
    if not mail_configured():
        return f"Aucun serveur d'e-mail n'est configuré (EMAIL_HOST) : {what} n'a pas été envoyé, {OTHER_CHANNEL}"
    return f"{request.timesheet.employee.display_name} n'a pas d'adresse e-mail : {what} n'a pas été envoyé, {OTHER_CHANNEL}"


def _greeting(request: SignatureRequest) -> str:
    employee = request.timesheet.employee
    return f"Bonjour {employee.first_name.strip() or employee.display_name},"


def _establishment(request: SignatureRequest) -> str:
    return (request.month_snapshot.get("establishment") or {}).get("name") or "Votre employeur"


def _label(request: SignatureRequest) -> str:
    return request.month_snapshot.get("label") or ""


def _send(request, what: str, subject: str, body: str, *, attachments=(), now=None, ip=None, user_agent="") -> MailOutcome:
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
            request, Kind.MAIL_FAILED, at=now, ip=ip, user_agent=user_agent,
            detail={"what": what, "to": address, "error": type(error).__name__},
        )
        return MailOutcome(False, f"L'e-mail n'a pas pu être envoyé à {address} ({what}) : {OTHER_CHANNEL}")
    return MailOutcome(True, f"E-mail envoyé à {address}.")


def send_link(request: SignatureRequest, link: str, *, ip=None, user_agent="") -> MailOutcome:
    """The signing link (absolute: `signature_requests.absolute_link`)."""
    if not can_email(request):
        return MailOutcome(False, _unavailable(request, "le lien"))
    until = signing.french_moment(request.expires_at).split(" à ")[0]
    body = (
        f"{_greeting(request)}\n\n"
        f"{_establishment(request)} vous demande de vérifier et de signer votre relevé d'heures de {_label(request)}.\n\n"
        f"Ouvrez ce lien pour le lire et le signer (il est valable jusqu'au {until}) :\n{link}\n\n"
        "Si un chiffre vous paraît faux, vous pourrez signer avec des réserves et les écrire.\n"
        "Ce lien vous est personnel : ne le transférez pas.\n"
    )
    outcome = _send(
        request, "lien", f"Relevé d'heures de {_label(request)} à signer — {_establishment(request)}", body,
        ip=ip, user_agent=user_agent,
    )
    if outcome.sent:
        signature_requests.log_event(request, Kind.LINK_SENT, ip=ip, user_agent=user_agent,
                                     detail={"to": _address(request)})
    return outcome


def send_code(request: SignatureRequest, *, now=None, ip=None, user_agent="") -> MailOutcome:
    """A new one-time code, by e-mail to the employee's address. Raises
    `CodeError` when no e-mail can carry it (the page offers « J'ai un
    code » instead) and whatever `issue_code` refuses (three an hour, a
    request no longer waiting)."""
    if not can_email(request):
        raise signature_requests.CodeError(_unavailable(request, "le code"))
    code = signature_requests.issue_code(
        request, SignatureRequest.Identification.CODE_BY_EMAIL, now=now, ip=ip, user_agent=user_agent, log=False
    )
    body = (
        f"{_greeting(request)}\n\n"
        f"Votre code pour signer le relevé d'heures de {_label(request)} : {code}\n\n"
        "Il est valable 15 minutes et ne sert qu'une fois. Il remplace tout code demandé avant.\n"
        "Ne le communiquez à personne.\n"
    )
    outcome = _send(request, "code", f"Votre code pour signer le relevé de {_label(request)}", body,
                    now=now, ip=ip, user_agent=user_agent)
    if outcome.sent:
        signature_requests.log_event(request, Kind.CODE_SENT, at=now, ip=ip, user_agent=user_agent,
                                     detail={"to": _address(request)})
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
        raise signature_requests.RequestStateError("Le relevé n'est pas encore contresigné : il n'y a pas de copie finale.")
    if not can_email(request):
        return MailOutcome(False, _unavailable(request, "la copie signée"))
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
        request, "copie signée", f"Votre relevé d'heures de {_label(request)} signé", body,
        attachments=[(final_copy_name(request), content, "application/pdf")], ip=ip, user_agent=user_agent,
    )
    if outcome.sent:
        signature_requests.log_event(request, Kind.COPY_SENT, ip=ip, user_agent=user_agent,
                                     detail={"to": _address(request)})
    return outcome
