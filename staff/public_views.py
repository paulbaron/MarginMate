"""The employee's pages: /personnel/signer/<token>/… - the ONE part of the
application meant to stay reachable without an account, forever. The owner
sends the link (by e-mail, SMS, WhatsApp…); the employee opens it on his
phone, reads his month, identifies himself with a one-time code and signs.

What protects it, and what each view keeps to:

* **Public, and bound to the link's tenant** (`_for_the_link`): every view
  is `@login_not_required` (everything else is denied to a visitor with no
  account), and the tenant is found by the link's hash in the accounts
  database (`accounts.links.resolve`) - unknown there is the « lien
  inconnu » page - then bound for the whole view, which reads and writes
  that tenant's database only. Whoever is logged in on the
  browser changes nothing: TenantMiddleware binds nothing of a visitor's on
  a public view, so a manager of another bar lending his phone, or an owner
  of two bars, opens and signs the link in the link's own tenant.
* **The link's secret** (`signature_requests.resolve_link`): only its SHA-256
  is stored. Unknown is a plain French page answering 404; expired,
  cancelled or superseded, 410 - never a traceback, never a word about any
  other request. Everything a view shows or serves comes from the ONE
  request the token reaches: its snapshot of the month, its own files (never
  a name or a path from the address), nothing of the rest of the application
  - these pages do not extend base.html, whose navigation and badges belong
  to the owner, and are rendered without the context processors (no
  messages of the owner's session, no counts of anything).
* **The one-time code** (`check_code`), remembered in HIS session for THIS
  request only, for an hour (`is_identified`); the session's key changes
  once it is verified. Signing needs it; reading the month and the PDF do not - and
  the PDF stays so on purpose (security audit ANON-6, 29/09): the page the
  link opens shows everything the PDF holds, and once he has signed no code
  can be issued, while « Voir le PDF » and his copy stay offered until the
  link expires - a code on document/ would hide nothing and lock him out
  of what he signed (staff/tests/test_link_only_reading.py). The token in
  the server's logs is the logging filter's to shorten (config).
* **Nothing technical reaches them**: a signature the server cannot make is
  `signature_requests.DOCUMENT_CHANGED` or `NOT_SIGNED`, its detail in the
  log - and anything unforeseen in `submit` is the same sentence, never
  Django's error page (under DEBUG it prints paths and settings).
* **CSRF on every POST** (Django's middleware; `csrf_failure` answers a
  refused post on these pages in French). Every POST redirects back to the
  page (its notices ride in the session, keyed by the request) - except a
  refused signature, drawn back with what was typed and the drawing.
* **Headers**: `Cache-Control: no-store` (a shared phone), `Referrer-Policy:
  same-origin` (the token is in the address; `REFERRER_POLICY` says why not
  « no-referrer »), `X-Robots-Tag: noindex`, and a
  Content-Security-Policy allowing this site's own script and stylesheet
  only. The PDF shown inline (« Voir le PDF ») may be framed by this site
  only (`SAMEORIGIN`: Chrome's viewer draws it in a frame of its own); every
  page keeps DENY.

The drawn signature is `static/js/signature_pad.js`, written for this page,
no dependency: it posts a PNG (a data URL) and nothing of the strokes.
"""

from __future__ import annotations

import functools
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_sameorigin

from accounts import links
from accounts.tenancy import bound_tenant

from . import pdf, private_files, signature_mail, signing
from . import signature_requests as workflow
from .models import SignatureRequest
from .signature_views import client

logger = logging.getLogger(__name__)

Status = SignatureRequest.Status

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)

# The fields of the page's forms.
CODE_FIELD = "code"
SIGNATURE_FIELD = "signature"
STATEMENT_FIELD = "certification"
RESERVED_FIELD = "avec_reserves"
RESERVATION_FIELD = "reserves"

IDENTIFY_FIRST = "Validez d'abord votre code (étape 1)."
RESERVATION_EMPTY = "Écrivez vos réserves, ou décochez « Je signe avec des réserves »."
RESERVATION_UNTICKED = "Cochez « Je signe avec des réserves » pour joindre vos réserves, ou effacez le texte."
ALREADY_SIGNED = "Ce relevé est déjà signé."
NO_CODE_BY_MAIL = "Le code ne peut pas vous être envoyé par e-mail : demandez-le à votre employeur."
MAIL_FAILED = "L'e-mail n'a pas pu partir. Réessayez dans quelques minutes, ou demandez le code à votre employeur."
CODE_VERIFIED = "Code vérifié : vous pouvez signer."
#: Asking again cancels the code on its way: said where it is asked, and in
#: the mail carrying it (signature_mail.send_code).
CODE_REPLACES = "Il remplace tout code demandé avant."
NOT_SIGNED_YET = "Ce relevé n'est pas encore signé."
ALTERED_DOCUMENT = (
    "Le document enregistré ne correspond plus à celui qui a été figé : il ne peut pas être affiché. Prévenez "
    "votre employeur."
)
PAGE_EXPIRED = (
    "La page a expiré, ou votre navigateur refuse les cookies de ce site. Rouvrez le lien et recommencez ; si cela "
    "se reproduit, autorisez les cookies pour ce site."
)
HEADINGS = {
    403: "La page a expiré",
    404: "Lien de signature inconnu",
    410: "Ce lien ne sert plus",
}


# -- Rendering --------------------------------------------------------------------------------------------------


#: The token is in the address: the page's own requests may carry it back to
#: this site, never anywhere else. NOT « no-referrer »: a browser then sends
#: `Origin: null` with the page's POSTs, and Django's CSRF check refuses
#: every one of them (found by staff/tests/test_sign_browser.py).
REFERRER_POLICY = "same-origin"


def _hardened(response: HttpResponse, *, html: bool = True) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = REFERRER_POLICY
    response["X-Robots-Tag"] = "noindex, nofollow"
    if html:
        response["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    return response


def _page(request, template: str, context: dict, status: int = 200) -> HttpResponse:
    """A public page, rendered WITHOUT the context processors: nothing of
    the owner's application - his messages, the navigation's counts - can
    reach it. `{% csrf_token %}` still works: the token is given here."""
    context = {**context, "csrf_token": get_token(request)}
    return _hardened(HttpResponse(render_to_string(template, context), status=status))


def _error(request, message: str, status: int) -> HttpResponse:
    return _page(
        request,
        "staff/sign_error.html",
        {"heading": HEADINGS.get(status, "Page indisponible"), "message": message},
        status,
    )


def _for_the_link(view):
    """A public view of a link: `@login_not_required`, and run bound to the
    tenant the link's hash is indexed under (`accounts.links.resolve`). A
    hash indexed
    nowhere - or under a tenant since closed - is « lien inconnu ».
    Nothing else is bound here, whoever is logged in on the browser
    (TenantMiddleware binds nothing on a public view). The binding ends with
    the view: the page is rendered to a string inside it, never lazily
    after."""

    @functools.wraps(view)
    def run(request, token, *args, **kwargs):
        tenant = links.resolve(workflow.link_hash(token))
        if tenant is None:
            return _error(request, workflow.UNKNOWN_LINK, 404)
        with bound_tenant(tenant):
            return view(request, token, *args, **kwargs)

    return login_not_required(run)


def _resolve(request, token):
    """(the request the token reaches, None) - or (None, the page saying why not)."""
    try:
        return workflow.resolve_link(token), None
    except workflow.LinkError as error:
        return None, _error(request, str(error), error.status)


def _page_url(token: str, anchor: str = "") -> str:
    """The page - at `anchor`: after a code is asked for or typed, the answer
    lands on « 1. Votre code » with the signing frame right under it, not at
    the top of a month to scroll through again (seen at 375 px)."""
    return reverse("staff:sign", args=[token]) + (f"#{anchor}" if anchor else "")


CODE_ANCHOR = "code"


# -- Notices: what an action says, carried to the page it redirects to ------------------------------------------


def _notices_key(sign_request) -> str:
    return f"staff-signature-notices-{sign_request.uuid}"


def _notify(request, sign_request, level: str, text: str) -> None:
    key = _notices_key(sign_request)
    request.session[key] = [*request.session.get(key, []), [level, text]]


def _take_notices(request, sign_request) -> list:
    return request.session.pop(_notices_key(sign_request), [])


def mask_address(address: str) -> str:
    """« j•••t@example.invalid »: enough for him to know which box to look
    in, not his whole address on a page anyone with the link can open."""
    local, at, domain = (address or "").strip().partition("@")
    if not at or not local:
        return ""
    shown = local[0] + "•••" + (local[-1] if len(local) > 2 else "")
    return f"{shown}@{domain}"


# -- The page ---------------------------------------------------------------------------------------------------


def _sign_page(request, token: str, sign_request: SignatureRequest, *, error: str = "", posted=None, status=200):
    snapshot = sign_request.month_snapshot or {}
    employee_name = (snapshot.get("employee") or {}).get("name") or ""
    context = {
        "month": snapshot,
        "establishment": snapshot.get("establishment") or {},
        "title": f"Relevé d'heures — {snapshot.get('title', '')} — {employee_name}",
        "state": {
            Status.PENDING: "pending",
            Status.EMPLOYEE_SIGNED: "employee_signed",
            Status.COMPLETE: "complete",
        }.get(sign_request.status, ""),
        "identified": workflow.is_identified(request.session, sign_request),
        # A code that can still be typed: typing it comes first, and asking
        # again says it voids this one (« code_email »), or is not offered
        # while the employer's own waits (« code_remis »).
        "code_waiting": workflow.waiting_code_method(sign_request),
        "code_verified": CODE_VERIFIED,
        "can_email": signature_mail.can_email(sign_request),
        "masked_address": mask_address(sign_request.timesheet.employee.email),
        "statement": workflow.statement_text(sign_request, workflow.CURRENT_STATEMENT),
        "signed_statement": workflow.statement_text(sign_request) if sign_request.employee_signed_at else "",
        "expires": signing.french_moment(sign_request.expires_at).split(" à ")[0],
        "signed_at": signing.french_moment(sign_request.employee_signed_at) if sign_request.employee_signed_at else "",
        "countersigned_at": (
            signing.french_moment(sign_request.employer_signed_at) if sign_request.employer_signed_at else ""
        ),
        "reservation": sign_request.reservation,
        "document_id": sign_request.document_id,
        "retention_years": getattr(settings, "STAFF_SIGNATURE_RETENTION_YEARS", 5),
        "notices": _take_notices(request, sign_request),
        "error": error,
        "posted": posted or {},
        "fields": {
            "code": CODE_FIELD,
            "signature": SIGNATURE_FIELD,
            "statement": STATEMENT_FIELD,
            "reserved": RESERVED_FIELD,
            "reservation": RESERVATION_FIELD,
        },
        "urls": {
            "page": _page_url(token),
            "send_code": reverse("staff:sign_send_code", args=[token]),
            "check_code": reverse("staff:sign_check_code", args=[token]),
            "submit": reverse("staff:sign_submit", args=[token]),
            "document": reverse("staff:sign_document", args=[token]),
            "copy": reverse("staff:sign_copy", args=[token]),
        },
    }
    return _page(request, "staff/sign.html", context, status)


@_for_the_link
def sign(request, token):
    """The month to read, « Voir le PDF », the code, the drawing - or, once
    signed, his copy. Opening it is « Lien ouvert », once an hour per
    device."""
    sign_request, failure = _resolve(request, token)
    if failure:
        return failure
    workflow.note_link_opened(sign_request, **client(request))
    return _sign_page(request, token, sign_request)


@_for_the_link
def send_code(request, token):
    """« Recevoir un code par e-mail »."""
    sign_request, failure = _resolve(request, token)
    if failure:
        return failure
    back = redirect(_page_url(token, CODE_ANCHOR))
    if request.method != "POST":
        return back
    if not signature_mail.can_email(sign_request):
        _notify(request, sign_request, "error", NO_CODE_BY_MAIL)
        return back
    try:
        outcome = signature_mail.send_code(sign_request, **client(request))
    except signing.SigningError as error:
        _notify(request, sign_request, "error", str(error))
        return back
    if outcome.sent:
        _notify(
            request,
            sign_request,
            "success",
            f"Code envoyé à {mask_address(sign_request.timesheet.employee.email)} : il vaut 15 minutes. "
            f"{CODE_REPLACES} S'il n'arrive pas, regardez aussi dans les courriers indésirables.",
        )
    else:
        _notify(request, sign_request, "error", MAIL_FAILED)
    return back


@_for_the_link
def check_code(request, token):
    """« J'ai un code »: checked, and on success remembered in this session
    for this request - whose key then changes."""
    sign_request, failure = _resolve(request, token)
    if failure:
        return failure
    back = redirect(_page_url(token, CODE_ANCHOR))
    if request.method != "POST":
        return back
    try:
        workflow.check_code(sign_request, request.POST.get(CODE_FIELD, ""), request.session, **client(request))
    except signing.SigningError as error:
        _notify(request, sign_request, "error", str(error))
        return back
    # A session fixed before the code was typed is not the one that signs.
    # (No notice: the page says CODE_VERIFIED itself while he is identified.)
    request.session.cycle_key()
    return back


@_for_the_link
def submit(request, token):
    """« Signer le relevé »: identified in this session, the drawing checked,
    his reservations if he gave some, the certification ticked - then signed
    and timestamped. A refusal draws the page back with what he typed and
    his drawing; no timestamp server answering is a 503 and stores nothing."""
    sign_request, failure = _resolve(request, token)
    if failure:
        return failure
    back = redirect(_page_url(token))
    if request.method != "POST":
        return back
    if sign_request.status != Status.PENDING:
        _notify(request, sign_request, "info", ALREADY_SIGNED)
        return back
    posted = {
        "statement": request.POST.get(STATEMENT_FIELD) == "1",
        "reserved": request.POST.get(RESERVED_FIELD) == "1",
        "reservation": request.POST.get(RESERVATION_FIELD, ""),
        "signature": request.POST.get(SIGNATURE_FIELD, ""),
    }

    def refuse(message: str, status: int = 200):
        return _sign_page(request, token, sign_request, error=message, posted=posted, status=status)

    if not workflow.is_identified(request.session, sign_request):
        posted["signature"] = ""
        return refuse(IDENTIFY_FIRST, 403)
    try:
        png = signing.signature_png_from_data_url(posted["signature"])
        signing.clean_signature_png(png)
    except signing.SignatureImageError as error:
        # Not drawn back: what was posted is no drawing to show.
        posted["signature"] = ""
        return refuse(str(error))
    reservation = posted["reservation"].strip()
    if posted["reserved"] and not reservation:
        return refuse(RESERVATION_EMPTY)
    if reservation and not posted["reserved"]:
        return refuse(RESERVATION_UNTICKED)
    try:
        workflow.sign_for_employee(
            sign_request,
            png,
            session=request.session,
            statement_accepted=posted["statement"],
            reservation=reservation,
            **client(request),
        )
    except signing.TimestampUnavailable as error:
        return refuse(str(error), 503)
    except signing.SigningError as error:
        return refuse(str(error))
    except Exception:
        # Anything nobody foresaw: a sentence on his page, the traceback in
        # the server's log - never Django's error page (whose technical form,
        # under DEBUG, shows paths and settings) on a page reachable by link.
        logger.exception("Signature du salarié : erreur imprévue (demande %s)", sign_request.uuid)
        sign_request.refresh_from_db()
        if sign_request.status != Status.PENDING:
            return back  # it did sign, and what failed came after
        return refuse(workflow.NOT_SIGNED, 500)
    return back


def _employee_name(sign_request) -> str:
    return (sign_request.month_snapshot.get("employee") or {}).get("name") or ""


@xframe_options_sameorigin
@_for_the_link
def document(request, token):
    """« Voir le PDF »: the document frozen when it was sent, shown in the
    browser - checked against its hash first."""
    sign_request, failure = _resolve(request, token)
    if failure:
        return failure
    try:
        data = private_files.read_checked(sign_request.uuid, private_files.DOCUMENT, sign_request.document_sha256)
    except (private_files.AlteredFileError, FileNotFoundError):
        return _error(request, ALTERED_DOCUMENT, 500)
    workflow.note_download(sign_request, private_files.DOCUMENT, **client(request))
    response = HttpResponse(data, content_type="application/pdf")
    label = sign_request.month_snapshot.get("label") or ""
    response["Content-Disposition"] = pdf.disposition(
        f"Relevé d'heures {_employee_name(sign_request)} {label}.pdf", inline=True
    )
    return _hardened(response, html=False)


@_for_the_link
def copy(request, token):
    """His signed copy - the countersigned one once it exists."""
    sign_request, failure = _resolve(request, token)
    if failure:
        return failure
    if sign_request.status not in (Status.EMPLOYEE_SIGNED, Status.COMPLETE):
        return _error(request, NOT_SIGNED_YET, 404)
    try:
        name, data = workflow.latest_document(sign_request)
    except (private_files.AlteredFileError, FileNotFoundError):
        return _error(request, ALTERED_DOCUMENT, 500)
    workflow.note_download(sign_request, name, **client(request))
    if name == private_files.FINAL:
        filename = signature_mail.final_copy_name(sign_request)
    else:
        label = sign_request.month_snapshot.get("label") or ""
        filename = f"Relevé d'heures {_employee_name(sign_request)} {label} signé, avant contreseing.pdf"
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = pdf.disposition(filename)
    return _hardened(response, html=False)


@login_not_required
def unknown(request, rest=""):
    """Any other address under signer/: a link that reaches nothing."""
    return _error(request, workflow.UNKNOWN_LINK, 404)


def _public_prefix() -> str:
    return reverse("staff:sign_unknown", kwargs={"rest": ""})


def csrf_failure(request, reason=""):
    """`settings.CSRF_FAILURE_VIEW`: a refused post on the employee's pages
    is said in French, in their own page; anywhere else, Django's own."""
    if request.path.startswith(_public_prefix()):
        return _error(request, PAGE_EXPIRED, 403)
    from django.views.csrf import csrf_failure as django_csrf_failure

    return django_csrf_failure(request, reason=reason)
