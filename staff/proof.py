"""The proof file of a signature request (« dossier de preuve »): what a
person - a judge, a labour inspector, the employee - reads to know who
signed which document, how he was identified, when, and what happened to
the request, in order.

The appeal courts' decisions of 2025 (research report) discarded proof
files that were strings of codes nobody could read, and documents nothing
visibly tied to their proof. So this one is French prose, laid out like the
timesheet, and carries the document's ID - the same ID the stamp of each
signature prints - on every page. It is written with `staff.pdf`'s own
writer (`pdf.Canvas`, `pdf.write_document`: no PDF library), again after
every step (`signature_requests.store_proof`), and its own hash is kept on
the request.

It says what this signature IS and nothing more: a simple electronic
signature (eIDAS art. 25, Code civil 1366-1367). The words « qualifiée »,
« avancée » and « équivalente à une signature manuscrite » are nowhere in
it, and a test holds it to that.

What it prints of a signature comes from where the database alone cannot
rewrite it, and the row is checked against that (review, 28/09): his
reservations from his own signed document (their /Reason, under the
timestamp), the method he was identified by and the text he certified from
the signed event, the SHA-256 of the employer's drawn signature from the
countersignature that seals it (« Signature dessinée de l'employeur :
SHA-256 … », the file kept checked against it - nothing of the kind for a
request countersigned before 28/09, whose proof reads as it did), the
authority that issued the certificates as recorded when
each signature was made - never the keys folder of today - and any value of
the row that no longer matches its journal is named as an anomaly. What the
journal's integrity shows is said as it is: consistent, sealed up to the last
signature, and not by itself proof against whoever can rewrite the database.
"""

from __future__ import annotations

from django.utils import timezone

from . import pdf, signing
from .models import SignatureEvent, SignatureRequest

TOP = pdf.PAGE_HEIGHT - 42.0
BOTTOM = 52.0
FOOTER_BASELINE = 28.0
WIDTH = pdf.RIGHT - pdf.LEFT
TEXT_SIZE = 9.0
SMALL_SIZE = 8.0
LINE = 12.0
SMALL_LINE = 10.5
INDENT = 14.0

NOT_YET = "— (pas encore)"

NATURE = (
    "Il s'agit d'une signature électronique simple au sens du règlement (UE) n° 910/2014 dit « eIDAS » "
    "(article 25) et des articles 1366 et 1367 du Code civil. Ce dossier rassemble ce qui permet d'en établir la "
    "fiabilité : la méthode d'identification du salarié, le document signé et l'empreinte de chacune de ses "
    "versions, les horodatages délivrés par un service tiers, et le journal des événements, chaîné par empreintes. "
    "Le document signé et ce dossier portent le même identifiant, imprimé dans le cachet de chaque signature."
)
#: What the chain shows, and no more (review, 28/09: « un événement modifié,
#: retiré ou ajouté après coup ne correspond plus » was true only of somebody
#: who did not work every hash out again).
JOURNAL_INTEGRITY = (
    "Chaque événement porte l'empreinte du précédent et la sienne, calculée sur son contenu : un événement modifié, "
    "retiré ou coupé à la fin ne correspond plus, à moins que toutes les empreintes qui le suivent aient été "
    "recalculées. Cette chaîne ne résiste pas, à elle seule, à qui peut réécrire la base de données. Ce qui est "
    "ancré hors d'elle : les documents signés, leurs empreintes et leurs horodatages par un service tiers, et "
    "l'empreinte du dernier événement au moment de chaque signature, scellée dans la signature elle-même - les "
    "événements d'avant la dernière signature ne peuvent donc plus être changés sans que cela se voie ; ceux qui "
    "la suivent restent sous la seule garde de la base."
)


class _Pages:
    """A flow of lines down A4 pages, a new page when one is full."""

    def __init__(self):
        self.pages: list[pdf.Canvas] = []
        self._new_page()

    def _new_page(self):
        self.canvas = pdf.Canvas()
        self.pages.append(self.canvas)
        self.y = TOP

    def need(self, height: float):
        if self.y - height < BOTTOM:
            self._new_page()

    def title(self, text: str, subtitle: str):
        self.y -= 16
        self.canvas.text(pdf.LEFT, self.y, pdf.fit(pdf.printable(text), WIDTH, pdf.BOLD, 15), font=pdf.BOLD, size=15)
        self.y -= 16
        for line in pdf.wrap(pdf.printable(subtitle), WIDTH, pdf.REGULAR, 10):
            self.canvas.text(pdf.LEFT, self.y, line, size=10, grey=pdf.GREY)
            self.y -= 12
        self.y -= 4

    def heading(self, text: str):
        self.need(46)
        # Air above a heading, except at the top of a page.
        self.y -= 14 if self.y >= TOP else 24
        self.canvas.text(pdf.LEFT, self.y, pdf.printable(text), font=pdf.BOLD, size=11)
        self.y -= 5
        self.canvas.line(pdf.LEFT, self.y, pdf.RIGHT, self.y, width=0.6, grey=pdf.BOX_GREY)
        self.y -= 4

    def item(self, label: str, value: str):
        """« Label : value », the value in bold. A value with a word too wide
        to sit beside the label - a hash - goes whole on the line below:
        wrapped beside it, a hash was cut with « … », and a proof file
        printing part of a fingerprint proves nothing."""
        label = pdf.printable(label) + " : "
        value = pdf.printable(value) or "—"
        label_width = pdf.text_width(label, pdf.REGULAR, TEXT_SIZE)
        widest_word = max(pdf.text_width(word, pdf.BOLD, TEXT_SIZE) for word in value.split(" "))
        if label_width > WIDTH * 0.45 or widest_word > WIDTH - label_width:
            first_room, rest_room, rest_x = WIDTH, WIDTH - INDENT, pdf.LEFT + INDENT
            lines = [label.rstrip()] + pdf.wrap(value, rest_room, pdf.BOLD, TEXT_SIZE)
            self.need(LINE * len(lines))
            self.y -= LINE
            self.canvas.text(pdf.LEFT, self.y, pdf.fit(lines[0], first_room, pdf.REGULAR, TEXT_SIZE))
            for line in lines[1:]:
                self.y -= LINE
                self.canvas.text(rest_x, self.y, line, font=pdf.BOLD)
            return
        lines = pdf.wrap(value, WIDTH - label_width, pdf.BOLD, TEXT_SIZE)
        self.need(LINE * len(lines))
        self.y -= LINE
        self.canvas.text(pdf.LEFT, self.y, label)
        self.canvas.text(pdf.LEFT + label_width, self.y, lines[0], font=pdf.BOLD)
        for line in lines[1:]:
            self.y -= LINE
            self.canvas.text(pdf.LEFT + label_width, self.y, line, font=pdf.BOLD)

    def paragraph(
        self,
        text: str,
        *,
        size: float = TEXT_SIZE,
        grey: float = pdf.BLACK,
        x: float = pdf.LEFT,
        font: str = pdf.REGULAR,
    ):
        leading = LINE if size >= TEXT_SIZE else SMALL_LINE
        lines = pdf.wrap(pdf.printable(text), pdf.RIGHT - x, font, size)
        self.need(leading * len(lines))
        for line in lines:
            self.y -= leading
            self.canvas.text(x, self.y, line, font=font, size=size, grey=grey)

    def gap(self, height: float = 4):
        self.y -= height

    def finish(self, footer: str) -> list[bytes]:
        count = len(self.pages)
        for number, canvas in enumerate(self.pages, start=1):
            text = pdf.fit(pdf.printable(f"{footer} — page {number} / {count}"), WIDTH, pdf.REGULAR, SMALL_SIZE)
            canvas.line(pdf.LEFT, FOOTER_BASELINE + 10, pdf.RIGHT, FOOTER_BASELINE + 10, width=0.4)
            canvas.text(pdf.LEFT, FOOTER_BASELINE, text, size=SMALL_SIZE, grey=pdf.GREY)
        return [canvas.content() for canvas in self.pages]


def _moment(value) -> str:
    return signing.french_moment(value) if value else NOT_YET


def _last(events: list[SignatureEvent], kind: str) -> SignatureEvent | None:
    return next((event for event in reversed(events) if event.kind == kind), None)


def _employer_drawing(request: SignatureRequest, events: list[SignatureEvent]) -> tuple[str, list[tuple[str, bool]]]:
    """The SHA-256 of the employer's drawn signature - as his
    countersignature seals it (its /Reason, under its timestamp) when that
    document can be read, else as the COUNTERSIGNED event recorded it - and
    what to say under it: (sentence, is it an anomaly). ("", []) when there
    is none: not countersigned yet, or countersigned before 28/09.

    « The countersignature was read and seals nothing » is not « it could
    not be read » (review, 28/09): once its /Reason was read, the seal and
    the journal are compared whatever either says - a journal naming a
    drawing the countersignature does not seal, or no longer naming the one
    it does, is an anomaly, each said with whose hash is printed."""
    from . import private_files
    from .signature_requests import employer_drawing_sha256

    recorded = employer_drawing_sha256(request, events)
    sealed, read = "", False
    if request.final_pdf_sha256:
        try:
            final = private_files.read_checked(request.uuid, private_files.FINAL, request.final_pdf_sha256)
        except (private_files.AlteredFileError, FileNotFoundError):
            final = None
        if final is not None:
            reason = signing.signed_reasons(final).get(signing.EMPLOYER_FIELD)
            if reason is not None:
                sealed, read = reason.drawing, True
    digest = sealed or recorded
    if not digest:
        return "", []  # a request countersigned before 28/09 reads as it did, byte for byte
    notes = []
    if read and sealed != recorded:
        if not sealed:
            notes.append(
                (
                    (
                        "Anomalie : la contresignature ne scelle aucune signature dessinée, le journal en nomme une - le "
                        "journal a été altéré. L'empreinte ci-dessus est celle du journal."
                    ),
                    True,
                )
            )
        elif not recorded:
            notes.append(
                (
                    (
                        "Anomalie : le journal ne nomme pas la signature dessinée que la contresignature a scellée - le "
                        "journal a été altéré. L'empreinte ci-dessus est celle de la contresignature."
                    ),
                    True,
                )
            )
        else:
            notes.append(
                (
                    (
                        f"Anomalie : le journal a enregistré une autre empreinte ({recorded}) que celle que la "
                        "contresignature a scellée - le journal a été altéré. L'empreinte ci-dessus est celle de la "
                        "contresignature."
                    ),
                    True,
                )
            )
    elif read:
        notes.append(
            ("Son empreinte est scellée dans la contresignature elle-même, couverte par son horodatage.", False)
        )
    else:
        notes.append(
            (
                (
                    "Telle que le journal l'a enregistrée, à l'événement de la contresignature : le document contresigné n'a "
                    "pas pu être relu."
                ),
                False,
            )
        )
    try:
        private_files.read_checked(request.uuid, private_files.EMPLOYER_SIGNATURE_IMAGE, digest)
    except private_files.AlteredFileError:
        notes.append(("Anomalie : le fichier employer_signature.png ne correspond plus à cette empreinte.", True))
    except FileNotFoundError:
        notes.append(("Anomalie : le fichier employer_signature.png est introuvable.", True))
    return digest, notes


def _signed_reservation(request: SignatureRequest) -> str | None:
    """His reservations as his own signature carries them (its /Reason,
    under its timestamp) - None when that document cannot be read."""
    from . import private_files

    if not request.employee_pdf_sha256:
        return None
    try:
        data = private_files.read_checked(request.uuid, private_files.EMPLOYEE_SIGNED, request.employee_pdf_sha256)
    except (private_files.AlteredFileError, FileNotFoundError):
        return None
    reason = signing.signed_reasons(data).get(signing.EMPLOYEE_FIELD)
    return reason.reservation if reason is not None else None


#: The row's fields a signed event also records, as a sentence names them.
_RECORDED = (
    (SignatureEvent.Kind.EMPLOYEE_SIGNED, "signed_sha256", "employee_pdf_sha256", "empreinte du document signé"),
    (
        SignatureEvent.Kind.EMPLOYEE_SIGNED,
        "signature_png_sha256",
        "signature_png_sha256",
        "empreinte de la signature dessinée",
    ),
    (SignatureEvent.Kind.EMPLOYEE_SIGNED, "statement_version", "statement_version", "version du texte certifié"),
    (SignatureEvent.Kind.EMPLOYEE_SIGNED, "identification", "identification", "méthode d'identification"),
    (SignatureEvent.Kind.EMPLOYEE_SIGNED, "reservation", "reservation", "réserves"),
    (SignatureEvent.Kind.COUNTERSIGNED, "final_sha256", "final_pdf_sha256", "empreinte du document contresigné"),
)


def _row_against_journal(request: SignatureRequest, events: list[SignatureEvent]) -> list[str]:
    """What the request's row says that its signed events no longer do -
    the row is a database column, the events are chained and sealed in the
    signed documents (`signature_requests.verify_event_chain`)."""
    differing = []
    for kind, key, field, words in _RECORDED:
        event = _last(events, kind)
        if event is None or key not in (event.detail or {}):
            continue
        recorded = event.detail[key]
        if isinstance(recorded, bool):  # a request signed before the words were kept
            continue
        if str(recorded or "") != str(getattr(request, field) or ""):
            differing.append(words)
    for kind, field, words in (
        (SignatureEvent.Kind.EMPLOYEE_SIGNED, "employee_signed_at", "date de la signature"),
        (SignatureEvent.Kind.COUNTERSIGNED, "employer_signed_at", "date de la contresignature"),
    ):
        event = _last(events, kind)
        if event is not None and getattr(request, field) != event.at:
            differing.append(words)
    return differing


def _identification(request: SignatureRequest, method: str, events: list[SignatureEvent]) -> list[str]:
    """What identified the employee, said honestly - including its limits.
    `method` is the one of the code he typed (`check_code`)."""
    if request.code_verified_at is None or not method:
        return ["Aucun code à usage unique n'a encore été vérifié pour cette demande."]
    if method == SignatureRequest.Identification.CODE_HANDED_OVER:
        return ["L'identification repose donc sur cette remise, faite par l'employeur lui-même."]
    lines = ["Le code à usage unique a été envoyé à l'adresse e-mail du salarié enregistrée par l'employeur."]
    if any(event.kind == SignatureEvent.Kind.LINK_SENT for event in events):
        lines.append(
            "Le lien de signature a aussi été envoyé à cette adresse : l'identification repose sur l'accès à cette "
            "boîte e-mail."
        )
    return lines


def proof_pdf(request: SignatureRequest, *, now=None) -> bytes:
    """The proof file of `request` as it stands, as the bytes of a PDF."""
    from .signature_requests import describe_event, statement_text, verify_event_chain

    now = now or timezone.now()
    snapshot = request.month_snapshot or {}
    employee_name = snapshot.get("employee", {}).get("name") or request.timesheet.employee.display_name
    establishment_name = snapshot.get("establishment", {}).get("name") or ""
    month = snapshot.get("label") or ""
    events = list(SignatureEvent.objects.filter(request=request).order_by("id")) if request.pk else []

    pages = _Pages()
    pages.title(
        "Dossier de preuve — signature électronique",
        f"Relevé d'heures de {employee_name} — {month} — version {request.version}",
    )

    pages.heading("Le document")
    pages.item("Identifiant du document", request.document_id)
    pages.item("Établissement (employeur)", establishment_name)
    pages.item("Salarié", employee_name)
    pages.item("Mois", f"{month} (version {request.version} de ce mois)")
    pages.item("État de la demande", request.get_status_display())
    pages.item("Demande créée le", _moment(request.created_at))
    pages.item("Lien de signature valable jusqu'au", _moment(request.expires_at))
    if request.cancelled_reason:
        pages.item("Motif de l'annulation ou du remplacement", request.cancelled_reason)

    employer_drawing, employer_drawing_notes = _employer_drawing(request, events)
    pages.heading("Empreintes SHA-256 des fichiers conservés")
    pages.item("Document figé, avant signature (document.pdf)", request.document_sha256)
    pages.item("Signature dessinée par le salarié (signature.png)", request.signature_png_sha256 or NOT_YET)
    pages.item("Document signé par le salarié (signed_employee.pdf)", request.employee_pdf_sha256 or NOT_YET)
    # A request countersigned before 28/09 has no drawing of the employer's:
    # its proof says what it said, nothing about a file that never existed.
    if employer_drawing or not request.employer_signed_at:
        pages.item("Signature dessinée par l'employeur (employer_signature.png)", employer_drawing or NOT_YET)
    pages.item("Document contresigné par l'employeur (signed_final.pdf)", request.final_pdf_sha256 or NOT_YET)
    pages.paragraph(
        "Chaque fichier est conservé par l'application. Son empreinte, recalculée, doit être identique à celle "
        "indiquée ici : un seul octet changé en donne une autre.",
        size=SMALL_SIZE,
        grey=pdf.GREY,
    )

    signed_event = _last(events, SignatureEvent.Kind.EMPLOYEE_SIGNED) if request.employee_signed_at else None
    recorded = signed_event.detail if signed_event is not None else {}

    # Once he signed, how he was identified is what the signed event recorded
    # - chained, and sealed by the countersignature - not the row's column.
    method = str(recorded.get("identification") or request.identification or "")
    pages.heading("Identification du salarié")
    pages.item("Méthode", dict(SignatureRequest.Identification.choices).get(method, NOT_YET) if method else NOT_YET)
    pages.item("Code vérifié le", _moment(request.code_verified_at))
    for line in _identification(request, method, events):
        pages.paragraph(line, size=SMALL_SIZE, grey=pdf.GREY)

    pages.heading("Ce que le salarié a certifié")
    if request.employee_signed_at:
        statement = recorded.get("statement") if isinstance(recorded.get("statement"), str) else ""
        pages.item(f"Texte accepté (version {request.statement_version or '—'})", statement or statement_text(request))
        # His words as his own signature carries them (under its timestamp),
        # else as the signed event recorded them: never the row alone, which
        # anyone with the database can rewrite.
        in_signature = _signed_reservation(request)
        in_journal = recorded.get("reservation") if isinstance(recorded.get("reservation"), str) else None
        if in_signature is not None:
            reservation, source = in_signature, "sa signature elle-même, couverte par son horodatage"
        elif in_journal is not None:
            reservation, source = in_journal, "le journal, à l'événement de sa signature"
        else:
            reservation, source = request.reservation, ""
        pages.item("Réserves", reservation or "aucune")
        if source:
            pages.paragraph(f"Telles qu'elles figurent dans {source}.", size=SMALL_SIZE, grey=pdf.GREY)
        if (request.reservation or "") != (reservation or ""):
            pages.paragraph(
                f"Anomalie : les réserves que la demande enregistre (« {request.reservation or 'aucune'} ») ne sont "
                f"plus celles qui ont été signées - elles ont été altérées. Le texte ci-dessus est celui de {source}.",
                font=pdf.BOLD,
            )
    else:
        pages.paragraph("Le salarié n'a pas encore signé.")

    pages.heading("Signatures et horodatages")
    pages.item("Signature du salarié", _moment(request.employee_signed_at))
    pages.item(
        "Horodatage de sa signature",
        f"{_moment(request.employee_timestamp_at)} (heure de Paris), par {request.employee_timestamp_authority}"
        if request.employee_timestamp_at
        else NOT_YET,
    )
    pages.item("Contresignature de l'employeur", _moment(request.employer_signed_at))
    if employer_drawing:
        pages.item("Signature dessinée de l'employeur", f"SHA-256 {employer_drawing}")
        for note, anomaly in employer_drawing_notes:
            if anomaly:
                pages.paragraph(note, font=pdf.BOLD)
            else:
                pages.paragraph(note, size=SMALL_SIZE, grey=pdf.GREY)
    pages.item(
        "Horodatage de la contresignature",
        f"{_moment(request.employer_timestamp_at)} (heure de Paris), par {request.employer_timestamp_authority}"
        if request.employer_timestamp_at
        else NOT_YET,
    )
    pages.item(
        "Autorité interne qui a émis les certificats",
        f"Autorité interne de {establishment_name}" if establishment_name else "Autorité interne",
    )
    # The authority recorded when each signature was made - not the one on
    # disk today, which is another once the keys folder was lost or moved.
    issued = []
    for kind, whose in (
        (SignatureEvent.Kind.EMPLOYEE_SIGNED, "du salarié"),
        (SignatureEvent.Kind.COUNTERSIGNED, "de l'employeur"),
    ):
        event = _last(events, kind)
        fingerprint = str((event.detail or {}).get("authority_sha256") or "") if event is not None else ""
        if fingerprint:
            issued.append((whose, fingerprint))
    current = signing.authority_fingerprint()
    fingerprints = list(dict.fromkeys(fingerprint for _whose, fingerprint in issued))
    if len(fingerprints) > 1:
        for whose, fingerprint in issued:
            pages.item(f"Empreinte SHA-256 de son certificat (signature {whose})", fingerprint)
    elif fingerprints:
        pages.item("Empreinte SHA-256 de son certificat", fingerprints[0])
    else:
        pages.item("Empreinte SHA-256 de son certificat", (current if request.employee_signed_at else "") or NOT_YET)
    if fingerprints and current and current not in fingerprints:
        pages.paragraph(
            f"L'autorité interne de cette installation n'est plus celle-ci (empreinte actuelle : {current}) : ses clés "
            "ont été perdues ou remplacées depuis. Les signatures de ce document restent vérifiables : chacune porte "
            "ses propres certificats.",
            size=SMALL_SIZE,
            grey=pdf.GREY,
        )

    pages.heading(f"Journal des événements ({len(events)})")
    if not events:
        pages.paragraph("Aucun événement.")
    for event in events:
        line = describe_event(event)
        pages.need(LINE + SMALL_LINE * (1 + len(line.details)))
        pages.paragraph(f"{line.when} — {line.title}", font=pdf.BOLD)
        if line.where:
            pages.paragraph(line.where, size=SMALL_SIZE, grey=pdf.GREY, x=pdf.LEFT + INDENT)
        for detail in line.details:
            pages.paragraph(detail, size=SMALL_SIZE, grey=pdf.GREY, x=pdf.LEFT + INDENT)
        pages.gap(2)

    pages.heading("Intégrité du journal")
    # Read again, the head with them: a caller's row may be older than the log.
    chain = verify_event_chain(request)
    pages.paragraph(chain.message, font=pdf.BOLD)
    differing = _row_against_journal(request, events)
    if differing:
        pages.paragraph(
            "Anomalie : ces valeurs de la demande ne correspondent plus au journal, qui les a enregistrées au moment "
            f"de chaque signature : {', '.join(differing)}.",
            font=pdf.BOLD,
        )
    pages.item("Empreinte du dernier événement", request.last_event_hash or NOT_YET)
    pages.paragraph(JOURNAL_INTEGRITY, size=SMALL_SIZE, grey=pdf.GREY)

    pages.heading("Nature de cette signature")
    pages.paragraph(NATURE)

    footer = f"Dossier de preuve — document n° {request.document_id} — établi le {signing.french_moment(now)}"
    return pdf.write_document(pages.finish(footer), f"Dossier de preuve — {request.document_id}")
