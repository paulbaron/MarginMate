"""The cryptography of the monthly timesheet signature - pure of request and
view: keys, certificates, the frozen document, the two signatures, and
`verify`, which is what would be shown to a judge.

Legally this is a **simple electronic signature** (eIDAS art. 25, Code
civil 1366-1367) - never to be called « qualifiée », « avancée » or
« équivalente à une signature manuscrite ». Since the Cour de cassation's
ruling of 5 March 2026 the employer must prove its reliability if the
employee denies signing, so what this module produces is EVIDENCE: the exact
document (PAdES, SHA-256), who signed it (a certificate in the employee's
name), and when (an RFC 3161 timestamp from a third party - the one piece of
evidence the employer does not control).

**Keys** (`settings.STAFF_PRIVATE_DIR/keys/`, `staff.private_files`): one
internal authority per installation (« Autorité interne de <établissement> »),
one certificate for the employer (the establishment's name) and one per
employee, made at his first signature (CN « DUPONT Jeanne », O the
establishment). EC P-256, SHA-256, ten years. The private keys are PKCS#8 PEM,
encrypted with `settings.MARGINMATE_SIGNING_PASSPHRASE` when it is set - and
keys written in clear before it was set are encrypted at the next signature;
unset, `key_warning()` is the sentence the owner's pages show. A certificate
whose name no longer matches (an employee renamed) is replaced, the old one
moved to `keys/archive/`: it signed documents. The authority is never
replaced while its files exist. **Losing it does not make a signed PDF
unverifiable**: each signature embeds its certificates, and `verify` still
says what each signature covers is intact and whom it names - with « ne se
rattache à aucune autorité connue de cette installation (clés perdues ou
remplacées ?) » instead of « émise par l'autorité de l'établissement ». The
authority that issued each signer's certificate is recorded when he signs
(`Signed.issuer_sha256`, kept in the request's events), so the proof file
names it whatever is on disk later. A common name holds 64 BYTES of UTF-8
at most (X.520): a longer name is cut there, between two words
(`_limited`); the stamps print it whole.

**The document** (`freeze`): the month exactly as `staff.pdf` prints it,
plus BOTH empty signature fields in one incremental update, placed where the
page draws its boxes (`pdf.ELECTRONIC_SIGNATURE_BOXES`). Its first revision is
byte for byte `pdf.render_month_pdf(sheet, establishment, electronic=True)`:
the page « Personnel » prints, its two boxes saying « Signature électronique
du salarié / de l'employeur » instead of asking for a handwritten date and
« Lu et approuvé » (the download keeps those). A month nobody saved is never
frozen: it is the typical week, a planning, not a record.

**The signatures** (`sign_as_employee`, `countersign`): PAdES
(`ETSI.CAdES.detached`), SHA-256, each in its own incremental revision, the
employee first. Both stamps are ONE layout (`_stamp_style`), drawn with the
sheet's own writer (`pdf.Canvas`, Helvetica): the drawn signature above - the
employee's, then the employer's own (28/09: « I cannot draw my signature as
the employer ») - and under it who signed - named whole, on a line of its own
and smaller when it is long, a letter cp1252 lacks written as its nearest
(`_signer_lines`, `pdf.printable_name`) - the date and hour in Paris (« le
02/07/2026 à 10:24 »), « avec réserves » when he said so, and the document's
ID - the reference the proof file carries too. Each signature's /Reason
carries what the database alone must not be able to rewrite
(`employee_reason`, `employer_reason`, `signed_reasons`): the employee's
reservations in his own words, the SHA-256 of the employer's drawing, and
the request's journal head at that moment - covered by the signature and its
timestamp. The employer's drawing is REQUIRED and goes through the
employee's checks (`clean_signature_png`); a countersignature made before it
existed states none, and reads back as it did. Both are
timestamped: the servers of
`settings.STAFF_TIMESTAMP_URLS` are tried in order, and when none answers
the signature is REFUSED (`TimestampUnavailable`, « le service d'horodatage
ne répond pas, réessayez dans quelques minutes ») - a signature without a
third party's time is one the employer could have dated himself.
`timestampers()` is looked up at call time so a test injects pyHanko's
`DummyTimeStamper` (staff/tests/signing_support.py); no test ever reaches a
real server (tests.support.NoNetworkTestCase).

**Verification** (`verify`) is offline: our authority's certificates are
the trust roots for the signers, and the Mozilla list shipped with certifi
for the timestamp authorities (DigiCert's and Sectigo's roots are in it).
Adobe Reader shows « validité inconnue » for our certificates
(`ADOBE_UNKNOWN_VALIDITY`): a trust warning, not an alteration.
"""

from __future__ import annotations

import datetime as dt
import functools
import io
import logging
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from zoneinfo import ZoneInfo

from asn1crypto import keys as asn1_keys
from asn1crypto import pem
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.conf import settings
from django.utils import timezone
from PIL import Image, ImageChops
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.content import ResourceType
from pyhanko.pdf_utils.generic import pdf_name
from pyhanko.pdf_utils.images import PdfImage
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.layout import BoxConstraints
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import fields, signers
from pyhanko.sign.timestamps import requests_client
from pyhanko.sign.timestamps.api import TimeStamper
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.generic_cms import extract_tst_data_iter
from pyhanko.stamp.base import BaseStamp, BaseStampStyle
from pyhanko_certvalidator import ValidationContext
from pyhanko_certvalidator.registry import SimpleCertificateStore

from . import pdf, private_files

PARIS = ZoneInfo("Europe/Paris")

#: The two signature fields, created together when the document is frozen.
EMPLOYEE_FIELD = "Salarie"
EMPLOYER_FIELD = "Employeur"
#: Placed by the boxes of the document frozen for signing - the paper
#: sheet's frames, with « Signature électronique du salarié / de
#: l'employeur » in place of the handwritten instructions.
FIELD_BOXES = tuple(zip((EMPLOYEE_FIELD, EMPLOYER_FIELD), pdf.ELECTRONIC_SIGNATURE_BOXES))
#: « du salarié », « de l'employeur » - for the sentences `verify` writes.
FIELD_ROLES = {EMPLOYEE_FIELD: "du salarié", EMPLOYER_FIELD: "de l'employeur"}

CERTIFICATE_VALIDITY = dt.timedelta(days=3653)   # ten years, leap days included
#: X.520's upper bound for a common name and an organisation name.
NAME_LIMIT = 64
#: A timestamp server that does not answer in this many seconds is the next one's turn.
TIMESTAMP_TIMEOUT = 10

AUTHORITY_STEM = "authority"
EMPLOYER_STEM = "employer"
ARCHIVE = "archive"

KEY_WARNING = "clé de signature non chiffrée : définir MARGINMATE_SIGNING_PASSPHRASE avant la mise en ligne"
KEYS_STILL_CLEAR = (
    "clé de signature encore non chiffrée : MARGINMATE_SIGNING_PASSPHRASE est définie, elle le sera à la "
    "prochaine signature"
)
TIMESTAMP_REFUSED = "Le service d'horodatage ne répond pas, réessayez dans quelques minutes."
UNSAVED_MONTH = (
    "Ce mois n'est pas enregistré : c'est la semaine type, un planning et non le relevé des heures faites. "
    "Enregistrez-le avant de l'envoyer pour signature."
)
NO_ESTABLISHMENT_NAME = (
    "Donnez d'abord le nom de l'établissement (en haut de « Personnel ») : il est imprimé en tête des fiches et "
    "nomme l'employeur qui contresigne."
)
#: For the owner's page, once: what Adobe shows, and why it is not a problem.
ADOBE_UNKNOWN_VALIDITY = (
    "Adobe Reader affiche « validité de la signature inconnue » : les certificats viennent de l'autorité interne "
    "de l'établissement, qu'Adobe ne connaît pas. C'est un avertissement de confiance, pas une altération du "
    "document : « Vérifier » contrôle le document lui-même."
)
UNREADABLE = "Ce fichier n'est pas un PDF lisible : aucune signature n'a pu être vérifiée."
#: « Contresigner » with nothing drawn - or posted by a page drawn before the
#: pad existed: the countersignature carries the employer's own drawing.
EMPLOYER_DRAWING_MISSING = "Dessinez votre signature dans le cadre avant de contresigner."
#: A drawing with too little ink (a blank pad, one click: a dot), in the words
#: of whoever is drawing - the employee signs, the owner countersigns
#: (review, 28/09: the owner was told « avant de signer » beside
#: « Contresigner le relevé »). Not EMPLOYER_DRAWING_MISSING: after a refused
#: dot, a click on the blank pad keeps the server's sentence and adds the
#: script's, which would then print the same sentence twice.
DRAWING_EMPTY = "La signature est vide : dessinez-la dans le cadre avant de signer."
EMPLOYER_DRAWING_EMPTY = "La signature est vide : dessinez-la dans le cadre avant de contresigner."


# -- Errors: each carries a French sentence meant to be shown as it is -----------------------------------------


class SigningError(Exception):
    """Something the owner or the employee has to be told, in French."""


class SigningSetupError(SigningError):
    """The establishment is not ready to sign (no name)."""


class SigningKeyError(SigningError):
    """A private key cannot be opened (passphrase missing or wrong)."""


class DocumentError(SigningError):
    """The PDF is not one this application froze, or not at that step."""


class SignatureImageError(SigningError, ValueError):
    """The drawn signature posted is not one to put on a document."""


class TimestampUnavailable(SigningError):
    """Every timestamp server failed: the signature is refused, nothing is
    stored. `failures` is [(server, reason)] for the event log."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        super().__init__(TIMESTAMP_REFUSED)


# -- Dates ------------------------------------------------------------------------------------------------------


def french_moment(when: dt.datetime) -> str:
    """« 02/07/2026 à 10:24 », in Paris whatever the server's clock says."""
    if timezone.is_naive(when):
        when = timezone.make_aware(when)
    local = when.astimezone(PARIS)
    return f"{local:%d/%m/%Y} à {local:%H:%M}"


# -- Keys and certificates --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """A certificate and its private key - the authority's, the employer's
    or an employee's - with the certificates above it."""

    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey
    issuers: tuple[x509.Certificate, ...] = ()

    @property
    def name(self) -> str:
        return _attribute(self.certificate.subject, NameOID.COMMON_NAME)

    @property
    def sha256(self) -> str:
        return self.certificate.fingerprint(hashes.SHA256()).hex()

    @property
    def asn1_certificate(self) -> asn1_x509.Certificate:
        return _asn1(self.certificate)

    @property
    def asn1_key(self) -> asn1_keys.PrivateKeyInfo:
        return asn1_keys.PrivateKeyInfo.load(
            self.key.private_bytes(
                serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )


def _asn1(certificate: x509.Certificate) -> asn1_x509.Certificate:
    return asn1_x509.Certificate.load(certificate.public_bytes(serialization.Encoding.DER))


def _attribute(name: x509.Name, oid) -> str:
    values = name.get_attributes_for_oid(oid)
    return str(values[0].value) if values else ""


def _limited(text: str) -> str:
    """`text` on one line, in at most `NAME_LIMIT` BYTES of UTF-8 - what
    X.520 allows a common name and `cryptography` enforces, raising a plain
    ValueError past it: « Autorité interne de » and a 44-character name with
    one « é » are already 65. Cut between two words when it can be, never
    inside a character. The stamps print the names whole; only the
    certificate holds this shorter form."""
    value = " ".join(str(text).split())
    data = value.encode("utf-8")
    if len(data) <= NAME_LIMIT:
        return value
    cut = data[:NAME_LIMIT].decode("utf-8", "ignore")
    if value[len(cut):len(cut) + 1] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.strip()


def _name(common_name: str, organisation: str) -> x509.Name:
    attributes = [x509.NameAttribute(NameOID.COMMON_NAME, _limited(common_name))]
    if _limited(organisation):
        attributes.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, _limited(organisation)))
    return x509.Name(attributes)


def _establishment_name(establishment) -> str:
    return " ".join(str(getattr(establishment, "name", "") or "").split())


def setup_problem(establishment) -> str:
    """"" when the establishment can sign, else the French sentence saying
    what is missing - for the owner's page, before anything is sent."""
    return "" if _establishment_name(establishment) else NO_ESTABLISHMENT_NAME


def _passphrase() -> bytes | None:
    phrase = getattr(settings, "MARGINMATE_SIGNING_PASSPHRASE", "") or ""
    return phrase.encode("utf-8") if phrase else None


def _key_pem(key) -> bytes:
    phrase = _passphrase()
    encryption = serialization.BestAvailableEncryption(phrase) if phrase else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)


def _is_encrypted(data: bytes) -> bool:
    return b"-----BEGIN ENCRYPTED PRIVATE KEY-----" in data[:200]


def _load_key(path: Path):
    data = path.read_bytes()
    phrase = _passphrase()
    if _is_encrypted(data):
        if phrase is None:
            raise SigningKeyError(
                "La clé de signature est chiffrée : définissez MARGINMATE_SIGNING_PASSPHRASE (la phrase avec laquelle "
                "elle a été chiffrée) pour pouvoir signer."
            )
        try:
            return serialization.load_pem_private_key(data, password=phrase)
        except (ValueError, TypeError):
            raise SigningKeyError(
                "La clé de signature ne s'ouvre pas avec la phrase MARGINMATE_SIGNING_PASSPHRASE actuelle : remettez "
                "celle avec laquelle elle a été chiffrée."
            ) from None
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        raise SigningKeyError(f"La clé de signature {path.name} est illisible.") from None
    if phrase is not None:
        private_files.write_private(path, _key_pem(key))
    return key


def _key_files(folder: Path) -> list[Path]:
    return sorted(folder.rglob("*.key.pem")) if folder.is_dir() else []


def _encrypt_clear_keys() -> None:
    """Once a passphrase is set, every key still written in clear - the
    archived ones included - is written again, encrypted."""
    if _passphrase() is None:
        return
    for path in _key_files(private_files.keys_dir()):
        if not _is_encrypted(path.read_bytes()):
            _load_key(path)


def key_warning() -> str:
    """The one muted line the owner's pages show while the keys are, or may
    be, stored in clear - "" once they are encrypted. Reads the folder,
    never creates it."""
    if _passphrase() is None:
        return KEY_WARNING
    folder = Path(settings.STAFF_PRIVATE_DIR) / private_files.KEYS
    if any(not _is_encrypted(path.read_bytes()) for path in _key_files(folder)):
        return KEYS_STILL_CLEAR
    return ""


def _certificate(subject: x509.Name, public_key, issuer: Identity | None):
    """The certificate to sign, and the key that signs it (None: the
    authority signs itself)."""
    now = dt.datetime.now(dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.certificate.subject if issuer else subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + CERTIFICATE_VALIDITY)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    )
    if issuer is None:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True).add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        signing_key = None
    else:
        builder = (
            builder.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=True, key_encipherment=False, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.certificate.public_key()), critical=False
            )
        )
        signing_key = issuer.key
    return builder, signing_key


def _paths(stem: str) -> tuple[Path, Path]:
    folder = private_files.keys_dir()
    return folder / f"{stem}.key.pem", folder / f"{stem}.cert.pem"


def _archive(stem: str) -> None:
    """Move an identity's files aside, never delete them: the certificate
    signed documents, and the key is what it was issued for."""
    key_path, cert_path = _paths(stem)
    folder = private_files.keys_dir() / ARCHIVE
    folder.mkdir(parents=True, exist_ok=True)
    moment = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    base = stem.replace("/", "-")
    for path, suffix in ((key_path, "key.pem"), (cert_path, "cert.pem")):
        if path.exists():
            path.replace(folder / f"{base}-{moment}.{suffix}")


def _still_good(certificate: x509.Certificate, subject: x509.Name, issuer: Identity) -> bool:
    if certificate.subject != subject or certificate.issuer != issuer.certificate.subject:
        return False
    try:
        certificate.verify_directly_issued_by(issuer.certificate)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return certificate.not_valid_after_utc > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)


def _issue(stem: str, subject: x509.Name, issuer: Identity | None) -> Identity:
    key = ec.generate_private_key(ec.SECP256R1())
    builder, signing_key = _certificate(subject, key.public_key(), issuer)
    certificate = builder.sign(signing_key or key, hashes.SHA256())
    key_path, cert_path = _paths(stem)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_files.write_private(key_path, _key_pem(key))
    private_files.write_private(cert_path, certificate.public_bytes(serialization.Encoding.PEM))
    return Identity(certificate, key, (issuer.certificate, *issuer.issuers) if issuer else ())


def _identity(stem: str, subject: x509.Name, issuer: Identity | None) -> Identity:
    key_path, cert_path = _paths(stem)
    if key_path.is_file() and cert_path.is_file():
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        if issuer is None or _still_good(certificate, subject, issuer):
            return Identity(certificate, _load_key(key_path), (issuer.certificate, *issuer.issuers) if issuer else ())
        _archive(stem)
    elif key_path.exists() or cert_path.exists():
        # Half of a pair (a copy interrupted): neither half is usable alone.
        _archive(stem)
    return _issue(stem, subject, issuer)


# One process makes an identity at a time: two first signatures at the same
# second must not make two authorities.
_KEYS_LOCK = threading.RLock()


def authority(establishment) -> Identity:
    """The installation's internal authority, made on first use and kept
    while its files exist - whatever the establishment is renamed to."""
    name = _establishment_name(establishment)
    if not name:
        raise SigningSetupError(NO_ESTABLISHMENT_NAME)
    with _KEYS_LOCK:
        _encrypt_clear_keys()
        return _identity(AUTHORITY_STEM, _name(f"Autorité interne de {name}", name), None)


def employer_identity(establishment) -> Identity:
    """The employer's certificate: the establishment's name, issued by the
    authority - made again, the old one archived, when the name changed."""
    name = _establishment_name(establishment)
    with _KEYS_LOCK:
        issuer = authority(establishment)
        return _identity(EMPLOYER_STEM, _name(name, name), issuer)


def employee_identity(employee, establishment) -> Identity:
    """The employee's certificate (« DUPONT Jeanne », O = the
    establishment), made at his first signature - and made again when his
    name or the establishment's changed."""
    if employee.pk is None:
        raise ValueError("Un salarié non enregistré n'a pas de certificat.")
    name = _establishment_name(establishment)
    with _KEYS_LOCK:
        issuer = authority(establishment)
        return _identity(f"employees/{employee.pk}", _name(employee.display_name, name), issuer)


def authority_certificates() -> list[x509.Certificate]:
    """Every authority this installation has had (the current one and any
    kept in the archive): the trust roots `verify` checks signers against.
    Reads the folder, never creates anything."""
    folder = Path(settings.STAFF_PRIVATE_DIR) / private_files.KEYS
    paths = [folder / f"{AUTHORITY_STEM}.cert.pem", *sorted((folder / ARCHIVE).glob(f"{AUTHORITY_STEM}-*.cert.pem"))]
    certificates = []
    for path in paths:
        if path.is_file():
            try:
                certificates.append(x509.load_pem_x509_certificate(path.read_bytes()))
            except ValueError:
                continue
    return certificates


def authority_fingerprint() -> str:
    """The current authority's SHA-256 fingerprint (hex), "" before the first
    signature - what the owner's page and the proof file show."""
    folder = Path(settings.STAFF_PRIVATE_DIR) / private_files.KEYS
    path = folder / f"{AUTHORITY_STEM}.cert.pem"
    if not path.is_file():
        return ""
    return x509.load_pem_x509_certificate(path.read_bytes()).fingerprint(hashes.SHA256()).hex()


# -- The drawn signature ----------------------------------------------------------------------------------------

MAX_SIGNATURE_BYTES = 300 * 1024
MAX_SIGNATURE_SIZE = (1200, 400)
#: Fewer inked pixels than this is an empty canvas, or a tap on it.
MIN_INK_PIXELS = 20
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def signature_png_from_data_url(text) -> bytes:
    """The PNG a page's canvas posts (`data:image/png;base64,…`), decoded -
    or SignatureImageError. Its size is checked BEFORE decoding."""
    import base64
    import binascii

    if not isinstance(text, str) or not text.strip():
        raise SignatureImageError("La signature n'a pas été reçue : dessinez-la dans le cadre, puis signez.")
    prefix, _comma, encoded = text.strip().partition(",")
    if prefix.lower() != "data:image/png;base64" or not encoded:
        raise SignatureImageError("La signature doit être une image PNG dessinée dans le cadre.")
    if len(encoded) > MAX_SIGNATURE_BYTES * 4 // 3 + 4:
        raise SignatureImageError("La signature dessinée est trop lourde (300 Ko au plus) : effacez-la et recommencez.")
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise SignatureImageError("La signature reçue est illisible : dessinez-la de nouveau.") from None


def clean_signature_png(data: bytes, *, empty: str = DRAWING_EMPTY) -> bytes:
    """The drawn signature as it is kept: a PNG of at most 300 KB and
    1200 × 400 pixels, decoded by Pillow and encoded again - transparency
    kept, every other chunk (text, dates, profiles) dropped - and refused
    when nothing was drawn, in the words `empty` gives (the employee's by
    default; `countersign` passes the owner's). The stroke data a pad
    records is never received: a static image, not a biometric one
    (research report, « RGPD »)."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise SignatureImageError("La signature n'a pas été reçue : dessinez-la dans le cadre, puis signez.")
    if len(data) > MAX_SIGNATURE_BYTES:
        raise SignatureImageError("La signature dessinée est trop lourde (300 Ko au plus) : effacez-la et recommencez.")
    if not bytes(data).startswith(_PNG_MAGIC):
        raise SignatureImageError("La signature doit être une image PNG dessinée dans le cadre.")
    try:
        image = Image.open(io.BytesIO(data))
        if image.format != "PNG":
            raise SignatureImageError("La signature doit être une image PNG dessinée dans le cadre.")
        width, height = image.size
        if width > MAX_SIGNATURE_SIZE[0] or height > MAX_SIGNATURE_SIZE[1]:
            raise SignatureImageError(
                f"La signature dessinée est trop grande ({width} × {height} pixels, "
                f"{MAX_SIGNATURE_SIZE[0]} × {MAX_SIGNATURE_SIZE[1]} au plus)."
            )
        image.load()
    except SignatureImageError:
        raise
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise SignatureImageError("L'image de la signature est illisible : dessinez-la de nouveau.") from None
    image = image.convert("RGBA")
    if _ink(image) < MIN_INK_PIXELS:
        raise SignatureImageError(empty)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _ink_mask(image: Image.Image) -> Image.Image:
    """White where something visible and dark was drawn."""
    visible = image.getchannel("A").point(lambda alpha: 255 if alpha > 16 else 0)
    dark = image.convert("RGB").convert("L").point(lambda value: 255 if value < 230 else 0)
    return ImageChops.multiply(visible, dark)


def _ink(image: Image.Image) -> int:
    return _ink_mask(image).histogram()[255]


def _stamp_drawing(png: bytes) -> Image.Image | None:
    """The drawn signature cropped to its ink, so it fills the stamp's upper
    part rather than a canvas's empty margins. `png` is a picture
    `clean_signature_png` kept."""
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    box = _ink_mask(image).getbbox()
    if box is None:
        return None
    margin = 4
    left, top, right, bottom = box
    return image.crop(
        (max(left - margin, 0), max(top - margin, 0), min(right + margin, image.width), min(bottom + margin, image.height))
    )


# -- The stamp: the sheet's own writer inside the signature box --------------------------------------------------

STAMP_TEXT_SIZE = 7.0
STAMP_LEADING = 8.6
STAMP_DESCENT = 2.0
STAMP_GAP = 3.5
#: A name too long for its line is set smaller, half a point at a time, down
#: to this, before it is wrapped - and cut only past two lines of it.
STAMP_MIN_TEXT_SIZE = 5.0
STAMP_SIZE_STEP = 0.5
_STAMP_WIDTH = FIELD_BOXES[0][1].stamp_rect[2] - FIELD_BOXES[0][1].stamp_rect[0]


def _signer_lines(lead: str, name: str, width: float = _STAMP_WIDTH) -> tuple:
    """« Signé électroniquement par DUPONT Jeanne » as the stamp's lines -
    `(segments, size)` each - naming the signer WHOLE: on one line when it
    fits; else the lead on its own line and the name under it, set smaller
    step by step down to `STAMP_MIN_TEXT_SIZE`; else the name over two lines
    at the size that holds it; past that, the second line cut. Before, the
    name shared one line with the lead and was cut with « … » at the box's
    edge - the employer's past about 27 capitals."""
    lead = pdf.printable(lead)
    name = pdf.printable_name(name)
    whole = f"{lead} {name}"
    if pdf.text_width(whole, pdf.BOLD, STAMP_TEXT_SIZE) <= width:
        return ((((whole, pdf.BOLD),), STAMP_TEXT_SIZE),)
    lines = [(((lead, pdf.BOLD),), STAMP_TEXT_SIZE)]
    size = STAMP_TEXT_SIZE
    while size >= STAMP_MIN_TEXT_SIZE:
        if pdf.text_width(name, pdf.BOLD, size) <= width:
            return (*lines, (((name, pdf.BOLD),), size))
        size -= STAMP_SIZE_STEP
    size = STAMP_TEXT_SIZE
    while True:
        wrapped = pdf.wrap(name, width, pdf.BOLD, size)
        if (len(wrapped) <= 2 and " ".join(wrapped) == name) or size <= STAMP_MIN_TEXT_SIZE:
            break
        size -= STAMP_SIZE_STEP
    if len(wrapped) > 2 or " ".join(wrapped) != name:
        first = wrapped[0]
        if name.startswith(first):
            wrapped = [first, pdf.fit(name[len(first):].strip(), width, pdf.BOLD, size)]
        else:   # one word wider than the line on its own
            wrapped = [pdf.fit(name, width, pdf.BOLD, size)]
    return (*lines, *((((line, pdf.BOLD),), size) for line in wrapped))


def _line(*segments, size: float = STAMP_TEXT_SIZE) -> tuple:
    """One of the stamp's lines: its (text, font) segments - `printable`
    already - and its size."""
    return (tuple(segments), size)


def _font(font: str) -> generic.DictionaryObject:
    return generic.DictionaryObject(
        {
            pdf_name("/Type"): pdf_name("/Font"),
            pdf_name("/Subtype"): pdf_name("/Type1"),
            pdf_name("/BaseFont"): pdf_name(f"/{font}"),
            pdf_name("/Encoding"): pdf_name("/WinAnsiEncoding"),
        }
    )


@dataclass(frozen=True)
class _StampStyle(BaseStampStyle):
    """Lines - `(segments, size)`, each segment (text, font) - bottom-aligned,
    and the drawing in what is left above them."""

    lines: tuple = ()
    drawing: object = None
    border_width: int = 0

    def create_stamp(self, writer, box, text_params):
        return _Stamp(writer=writer, style=self, box=box)


def _baselines(lines) -> tuple[list[float], float]:
    """Each line's baseline, from the foot of the stamp up, and the top of
    the highest line's capitals: a line sits one leading - proportional to
    its own size - above the one under it."""
    baselines = [0.0] * len(lines)
    baseline = STAMP_DESCENT
    for index in range(len(lines) - 1, -1, -1):
        if index < len(lines) - 1:
            baseline += lines[index][1] * STAMP_LEADING / STAMP_TEXT_SIZE
        baselines[index] = baseline
    top = baselines[0] + lines[0][1] if lines else 0.0
    return baselines, top


class _Stamp(BaseStamp):
    def _render_inner_content(self):
        width, height = self.box.width, self.box.height
        for font, resource in pdf.FONT_RESOURCES.items():
            self.set_resource(category=ResourceType.FONT, name=pdf_name(f"/{resource}"), value=_font(font))
        canvas = pdf.Canvas()
        lines = self.style.lines
        baselines, text_top = _baselines(lines)
        for (segments, size), baseline in zip(lines, baselines):
            x = 0.0
            for text, font in segments:
                # `printable` already (`_line`): not made so again here, which
                # would strip the spaces a segment starts or ends with.
                shown = pdf.fit(text, max(width - x, 0), font, size)
                x += canvas.text(x, baseline, shown, font=font, size=size)
                if shown != text:
                    break
        commands = [b"q", canvas.content() if lines else b""]
        drawing = self.style.drawing
        area_bottom = text_top + STAMP_GAP
        area_height = height - area_bottom
        if drawing is not None and area_height > 4:
            scale = min(width / drawing.width, area_height / drawing.height)
            image = PdfImage(
                drawing, writer=self.writer, box=BoxConstraints(width=drawing.width * scale, height=drawing.height * scale)
            )
            rendered = image.render()
            self.import_resources(image.resources)
            commands.append(b"q 1 0 0 1 0 %g cm %s Q" % (round(area_bottom, 2), rendered))
        commands.append(b"Q")
        return commands


def _stamp_style(lead: str, signer: str, moment: list, document_id: str, png: bytes | None) -> _StampStyle:
    """THE layout of both stamps - the employee's and the employer's: the
    drawn signature above (`_Stamp`: as wide or as tall as what is left over
    the text allows, inside the box), and at the foot « `lead` `signer` »
    named whole (`_signer_lines`), the moment - `moment`'s segments,
    `printable` already - and the document's ID. One definition, so the two
    boxes of one sheet cannot come out laid out two ways."""
    return _StampStyle(
        lines=(
            *_signer_lines(lead, signer),
            _line(*moment),
            _line((pdf.printable(f"Document n° {document_id}"), pdf.REGULAR)),
        ),
        drawing=_stamp_drawing(png) if png else None,
    )


# -- Freezing and signing ---------------------------------------------------------------------------------------


def freeze(sheet, establishment) -> bytes:
    """The month to sign: `staff.pdf`'s timesheet of a SAVED month - its
    boxes saying « Signature électronique du salarié / de l'employeur »
    rather than asking for a handwritten « Lu et approuvé »
    (`render_month_pdf(electronic=True)`, called with it from here only) -
    and in one incremental update both empty signature fields where the page
    draws its boxes. Refused for a month nobody saved (a planning is not a
    record) and for an establishment with no name (nobody to countersign).
    A document frozen before those words existed keeps the paper ones: it
    is never drawn again, and signs the same (its fields are where they
    were)."""
    if not getattr(sheet, "saved", False):
        raise DocumentError(UNSAVED_MONTH)
    problem = setup_problem(establishment)
    if problem:
        raise SigningSetupError(problem)
    rendered = pdf.render_month_pdf(sheet, establishment, electronic=True)
    writer = IncrementalPdfFileWriter(io.BytesIO(rendered))
    for name, box in FIELD_BOXES:
        fields.append_signature_field(writer, fields.SigFieldSpec(sig_field_name=name, on_page=0, box=box.stamp_rect))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _signature_fields(pdf_bytes: bytes) -> dict:
    """{field name: filled?} - or DocumentError for bytes that are no PDF."""
    try:
        reader = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
        return {name: value is not None for name, value, _ref in fields.enumerate_sig_fields(reader)}
    except Exception:
        raise DocumentError(UNREADABLE) from None


def _require(pdf_bytes: bytes, field_name: str, after: str | None = None) -> None:
    found = _signature_fields(pdf_bytes)
    if field_name not in found:
        raise DocumentError(
            f"Ce document n'a pas de champ de signature « {field_name} » : ce n'est pas un document figé par "
            "l'application pour être signé."
        )
    if found[field_name]:
        raise DocumentError(f"Ce document porte déjà la signature {FIELD_ROLES.get(field_name, field_name)}.")
    if after is not None and not found.get(after):
        raise DocumentError("Ce document ne porte pas encore la signature du salarié : l'employeur contresigne en second.")


def timestampers() -> list[TimeStamper]:
    """The RFC 3161 servers of `settings.STAFF_TIMESTAMP_URLS`, in order.
    Looked up at call time, so a test patches it (signing_support)."""
    return [
        requests_client.RequestsHTTPTimeStamper(url, timeout=TIMESTAMP_TIMEOUT)
        for url in getattr(settings, "STAFF_TIMESTAMP_URLS", [])
    ]


def authority_label(stamper) -> str:
    """How a timestamp server is named in the record: its address."""
    return str(getattr(stamper, "url", "") or type(stamper).__name__)


class _Attempt(TimeStamper):
    """One server's turn: remembers why it failed, so a failure of the
    SERVER (the next one is tried) is told apart from any other error (a
    bug, a broken document: raised as it is). A test's network guard
    (AssertionError) is never taken for a server down."""

    def __init__(self, inner):
        super().__init__(include_nonce=getattr(inner, "include_nonce", True))
        self.inner = inner
        self.failure = None

    async def async_timestamp(self, message_digest, md_algorithm):
        try:
            return await self.inner.async_timestamp(message_digest, md_algorithm)
        except AssertionError:
            raise
        except Exception as error:
            self.failure = error
            raise


def _timestamp_of(pdf_bytes: bytes, field_name: str) -> dt.datetime | None:
    reader = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
    for embedded in reader.embedded_signatures:
        if embedded.field_name == field_name:
            for signed_data in extract_tst_data_iter(embedded.signer_info, signed=False):
                return signed_data["encap_content_info"]["content"].parsed["gen_time"].native
    return None


@dataclass(frozen=True)
class Signed:
    """A signature's result: the new PDF, the timestamp server that
    answered and the time its token states, who signed, and the SHA-256
    fingerprint of the internal authority that issued his certificate -
    recorded with the request's events, since the authority on disk may be
    another one by the time anybody reads the proof again. A
    countersignature also returns the employer's drawing as it is to be kept
    (`clean_signature_png`'s picture) and its SHA-256 - the one its /Reason
    seals."""

    pdf: bytes
    authority: str
    timestamp: dt.datetime | None
    signer: str
    issuer_sha256: str = ""
    drawing: bytes = b""
    drawing_sha256: str = ""


# -- What the signature itself says (its /Reason) ---------------------------------------------------------------
#
# The signature dictionary is inside the signed byte range, so what its
# /Reason says is covered by the signature and by the timestamp token over
# it: the employee's reservations in his own words - otherwise they lived in
# a database column only, rewritable with nothing noticing -, the SHA-256 of
# the employer's drawing (kept as a file and in an event, no column), and the
# journal's last hash at the moment of signing, which seals every event
# before it (`signature_requests.verify_event_chain`).

REASON_CERTIFIED = "Relevé d'heures certifié exact par le salarié"
REASON_RESERVED = "Relevé d'heures signé par le salarié, avec réserves : « "
REASON_RESERVED_END = " »"
REASON_EMPLOYER = "Contresignature de l'employeur"
DRAWING_MARK = " — signature dessinée SHA-256 "
JOURNAL_MARK = " — journal "
_JOURNAL = re.compile(r"(?s)^(.*) — journal ([0-9a-f]{64})$")
_EMPLOYER_DRAWING = re.compile(rf"^{re.escape(REASON_EMPLOYER)} — signature dessinée SHA-256 ([0-9a-f]{{64}})$")


@dataclass(frozen=True)
class SignedReason:
    """A signature's /Reason, read back: `reservation` is the employee's own
    words ("" when he gave none; None on a signature that is not his),
    `drawing` the SHA-256 of the employer's drawing a countersignature seals
    ("" when it states none - every other signature, and the
    countersignatures made before 28/09), and `journal` the chain's head it
    sealed ("" when it states none)."""

    text: str
    reservation: str | None
    journal: str
    drawing: str = ""


def _reason(base: str, journal: str) -> str:
    return f"{base}{JOURNAL_MARK}{journal}" if journal else base


def employee_reason(reservation: str = "", journal: str = "") -> str:
    reservation = str(reservation or "").strip()
    base = f"{REASON_RESERVED}{reservation}{REASON_RESERVED_END}" if reservation else REASON_CERTIFIED
    return _reason(base, journal)


def employer_reason(drawing_sha256: str, journal: str = "") -> str:
    """« Contresignature de l'employeur — signature dessinée SHA-256 … —
    journal … »."""
    return _reason(f"{REASON_EMPLOYER}{DRAWING_MARK}{drawing_sha256}" if drawing_sha256 else REASON_EMPLOYER, journal)


def read_reason(text) -> SignedReason:
    text = str(text or "")
    base, journal, drawing = text, "", ""
    match = _JOURNAL.match(text)
    if match:
        base, journal = match.group(1), match.group(2)
    if base == REASON_CERTIFIED:
        reservation = ""
    elif base.startswith(REASON_RESERVED) and base.endswith(REASON_RESERVED_END):
        reservation = base[len(REASON_RESERVED):-len(REASON_RESERVED_END)]
    else:
        reservation = None
        drawn = _EMPLOYER_DRAWING.match(base)
        if drawn:
            drawing = drawn.group(1)
    return SignedReason(text, reservation, journal, drawing)


def signed_reasons(pdf_bytes: bytes) -> dict[str, SignedReason]:
    """{field: what its signature's /Reason says}, for every signature of
    `pdf_bytes` - {} for bytes that are no PDF. Never raises."""
    try:
        reader = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
        return {
            str(embedded.field_name): read_reason(embedded.sig_object.get("/Reason", ""))
            for embedded in reader.embedded_signatures
        }
    except Exception:
        return {}


def _sign(pdf_bytes: bytes, meta, identity: Identity, style, stampers) -> Signed:
    stampers = list(timestampers() if stampers is None else stampers)
    if not stampers:
        raise TimestampUnavailable([])
    signer = signers.SimpleSigner(
        signing_cert=identity.asn1_certificate,
        signing_key=identity.asn1_key,
        cert_registry=SimpleCertificateStore.from_certs([_asn1(issuer) for issuer in identity.issuers]),
    )
    failures = []
    for stamper in stampers:
        attempt = _Attempt(stamper)
        output = io.BytesIO()
        try:
            signers.PdfSigner(meta, signer=signer, timestamper=attempt, stamp_style=style).sign_pdf(
                IncrementalPdfFileWriter(io.BytesIO(pdf_bytes), strict=False), output=output
            )
        except Exception:
            if attempt.failure is None:
                raise
            failures.append((authority_label(stamper), str(attempt.failure) or type(attempt.failure).__name__))
            continue
        signed = output.getvalue()
        root = identity.issuers[-1] if identity.issuers else identity.certificate
        return Signed(
            signed, authority_label(stamper), _timestamp_of(signed, meta.field_name), identity.name,
            root.fingerprint(hashes.SHA256()).hex(),
        )
    raise TimestampUnavailable(failures)


def _moment_line(when: dt.datetime) -> str:
    return f"le {french_moment(when)} (heure de Paris)"


def sign_as_employee(
    pdf_bytes: bytes,
    employee,
    png: bytes | None,
    when: dt.datetime,
    *,
    document_id: str,
    establishment,
    reservation: str = "",
    journal: str = "",
    stampers=None,
) -> Signed:
    """The employee's signature on the frozen document, in the « Salarie »
    field: his certificate (made at his first signature), his drawn
    signature, a timestamp. `when` is the moment printed on the stamp. His
    reservations, in his own words, and `journal` - the request's chain's
    head at this moment - go into the signature's /Reason, which the
    signature and its timestamp cover (`employee_reason`). The stamp names
    him whole, as the sheet does (`_signer_lines`); his certificate may
    hold a shorter form of a very long name (`_limited`)."""
    _require(pdf_bytes, EMPLOYEE_FIELD)
    identity = employee_identity(employee, establishment)
    with_reservations = bool((reservation or "").strip())
    moment = [(pdf.printable(_moment_line(when)), pdf.REGULAR)]
    if with_reservations:
        moment += [(" – ", pdf.REGULAR), (pdf.printable("avec réserves"), pdf.BOLD)]
    style = _stamp_style("Signé électroniquement par", employee.display_name, moment, document_id, png)
    meta = signers.PdfSignatureMetadata(
        field_name=EMPLOYEE_FIELD,
        md_algorithm="sha256",
        subfilter=fields.SigSeedSubFilter.PADES,
        name=identity.name,
        reason=employee_reason(reservation, journal),
    )
    return _sign(pdf_bytes, meta, identity, style, stampers)


def countersign(
    pdf_bytes: bytes,
    establishment,
    png: bytes | None,
    when: dt.datetime,
    *,
    document_id: str,
    journal: str = "",
    stampers=None,
) -> Signed:
    """The employer's signature, in the « Employeur » field, once the
    employee has signed, WITH HIS DRAWN SIGNATURE: `png` as his pad posted
    it, required (`EMPLOYER_DRAWING_MISSING`) and checked as the employee's
    is (`clean_signature_png`: size, dimensions, ink, encoded again, every
    other chunk dropped; too little ink says `EMPLOYER_DRAWING_EMPTY`, « avant
    de contresigner ») - refused before anything is signed or timestamped.
    The picture kept is drawn in « L'employeur » by the stamps' one layout
    (`_stamp_style`), and its SHA-256 goes into the /Reason with `journal`,
    the chain's head at this moment (`employer_reason`): the database alone
    can rewrite neither. Returned with the signature (`Signed.drawing`,
    `drawing_sha256`), so what is kept is what was sealed."""
    _require(pdf_bytes, EMPLOYER_FIELD, after=EMPLOYEE_FIELD)
    if not png:
        raise SignatureImageError(EMPLOYER_DRAWING_MISSING)
    drawing = clean_signature_png(png, empty=EMPLOYER_DRAWING_EMPTY)
    drawing_sha256 = private_files.sha256(drawing)
    identity = employer_identity(establishment)
    style = _stamp_style(
        "Contresigné électroniquement par",
        _establishment_name(establishment),
        [(pdf.printable(_moment_line(when)), pdf.REGULAR)],
        document_id,
        drawing,
    )
    meta = signers.PdfSignatureMetadata(
        field_name=EMPLOYER_FIELD,
        md_algorithm="sha256",
        subfilter=fields.SigSeedSubFilter.PADES,
        name=identity.name,
        reason=employer_reason(drawing_sha256, journal),
    )
    signed = _sign(pdf_bytes, meta, identity, style, stampers)
    return replace(signed, drawing=drawing, drawing_sha256=drawing_sha256)


# -- Verification -----------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _certifi_roots() -> tuple:
    import certifi

    with open(certifi.where(), "rb") as handle:
        return tuple(asn1_x509.Certificate.load(der) for _kind, _headers, der in pem.unarmor(handle.read(), multiple=True))


def timestamp_trust_roots() -> list:
    """The roots a timestamp authority must chain to: the Mozilla list
    certifi ships (DigiCert's and Sectigo's timestamp roots are in it) -
    offline, the same on every machine. Patched by the tests."""
    return list(_certifi_roots())


@contextmanager
def _quiet_validation_logs():
    """pyHanko logs a traceback for every certificate it cannot trust;
    `verify` says so in French instead."""
    loggers = [logging.getLogger(name) for name in ("pyhanko", "pyhanko_certvalidator")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for logger, level in zip(loggers, levels):
            logger.setLevel(level)


@dataclass(frozen=True)
class SignatureCheck:
    """One signature, as `verify` found it."""

    field: str
    signer: str
    intact: bool
    valid: bool
    trusted: bool
    timestamp: dt.datetime | None
    timestamp_trusted: bool
    timestamp_authority: str
    modification: str       # pyHanko's ModificationLevel: NONE, FORM_FILLING, ANNOTATIONS, OTHER
    coverage: str           # ENTIRE_FILE, ENTIRE_REVISION, …
    problems: tuple[str, ...] = ()

    @property
    def role(self) -> str:
        return FIELD_ROLES.get(self.field, f"« {self.field} »")

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class Verification:
    """What `verify` says of a PDF: each signature, the fields still empty,
    and `verdict` - the whole in French words."""

    signatures: tuple[SignatureCheck, ...] = ()
    unsigned_fields: tuple[str, ...] = ()
    error: str = ""

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(problem for check in self.signatures for problem in check.problems)

    @property
    def intact(self) -> bool:
        return bool(self.signatures) and all(check.intact for check in self.signatures)

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.signatures) and not self.problems

    @property
    def verdict(self) -> str:
        if self.error:
            return self.error
        if not self.signatures:
            return "Aucune signature : c'est le document tel qu'il a été figé, avant toute signature."
        parts = []
        for check in self.signatures:
            verb = {EMPLOYEE_FIELD: "signé", EMPLOYER_FIELD: "contresigné"}.get(check.field, "signé")
            when = f", horodaté le {french_moment(check.timestamp)}" if check.timestamp else ""
            parts.append(f"{verb} par {check.signer or '—'}{when}")
        if self.problems:
            text = "Anomalie : " + " ; ".join(self.problems) + "."
            if all(check.intact and check.valid for check in self.signatures):
                # What each signature covers is still what was signed: say so,
                # and whom they name - an unknown authority is not a forgery.
                text += " Ce que couvre chaque signature est intact : " + " ; ".join(parts) + "."
            return text
        text = "Document intact : " + " ; ".join(parts) + "."
        text += (
            " Chaque signature correspond au document, a été émise par l'autorité interne de l'établissement et "
            "horodatée par un service tiers."
        )
        if EMPLOYER_FIELD in self.unsigned_fields:
            text += " En attente de la contresignature de l'employeur."
        return text


_ACCEPTED_MODIFICATIONS = {"NONE", "FORM_FILLING"}


def _check(embedded, status) -> SignatureCheck:
    field_name = str(embedded.field_name)
    role = FIELD_ROLES.get(field_name, f"« {field_name} »")
    signer = str(status.signing_cert.subject.native.get("common_name", "")) if status.signing_cert else ""
    stamp = status.timestamp_validity
    modification = getattr(status.modification_level, "name", "") if status.modification_level is not None else ""
    coverage = getattr(status.coverage, "name", "") if status.coverage is not None else ""
    problems = []
    if not status.intact:
        problems.append(f"le document a été modifié après la signature {role}, qui ne correspond plus à son contenu")
    elif not status.valid:
        problems.append(f"la signature {role} est invalide")
    elif not status.trusted:
        # Intact and valid, from an authority this installation does not
        # know (any more): its keys lost or replaced since - not a forgery
        # by itself, and the verdict still says what the signature covers.
        problems.append(
            f"le certificat {role} ne se rattache à aucune autorité connue de cette installation (clés perdues ou "
            "remplacées ?)"
        )
    if stamp is None:
        problems.append(f"la signature {role} n'est pas horodatée")
    elif not (stamp.intact and stamp.valid):
        problems.append(f"l'horodatage de la signature {role} est invalide")
    elif not stamp.trusted:
        problems.append(f"l'horodatage de la signature {role} vient d'une autorité d'horodatage non reconnue")
    if status.intact and (
        modification not in _ACCEPTED_MODIFICATIONS
        or not getattr(status, "docmdp_ok", True)
        or coverage not in ("ENTIRE_FILE", "ENTIRE_REVISION")
    ):
        problems.append(
            f"le document a été modifié après la signature {role}, par autre chose que la contresignature prévue"
        )
    stamp_authority = ""
    if stamp is not None and stamp.signing_cert is not None:
        stamp_authority = str(stamp.signing_cert.subject.native.get("common_name", "") or stamp.signing_cert.subject.human_friendly)
    return SignatureCheck(
        field=field_name,
        signer=signer,
        intact=bool(status.intact),
        valid=bool(status.valid),
        trusted=bool(status.trusted),
        timestamp=stamp.timestamp if stamp is not None else None,
        timestamp_trusted=bool(stamp is not None and stamp.intact and stamp.valid and stamp.trusted),
        timestamp_authority=stamp_authority,
        modification=modification,
        coverage=coverage,
        problems=tuple(problems),
    )


def verify(pdf_bytes: bytes) -> Verification:
    """Every signature of `pdf_bytes`, checked offline: intact (the bytes
    it covers are the ones signed), valid (the cryptography holds), trusted
    (issued by this installation's authority), its timestamp and whether a
    known authority issued it, and what was changed after it. Never raises
    on a bad file: `error` says it."""
    try:
        reader = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
        embedded_signatures = list(reader.embedded_signatures)
        empty = tuple(
            str(name) for name, value, _ref in fields.enumerate_sig_fields(reader) if value is None
        )
    except Exception:
        return Verification(error=UNREADABLE)
    signer_roots = [_asn1(certificate) for certificate in authority_certificates()]
    stamp_roots = timestamp_trust_roots()
    checks = []
    with _quiet_validation_logs():
        for embedded in embedded_signatures:
            try:
                status = validate_pdf_signature(
                    embedded,
                    signer_validation_context=ValidationContext(trust_roots=signer_roots, allow_fetching=False),
                    ts_validation_context=ValidationContext(trust_roots=stamp_roots, allow_fetching=False),
                )
            except Exception:
                role = FIELD_ROLES.get(str(embedded.field_name), f"« {embedded.field_name} »")
                checks.append(
                    SignatureCheck(
                        field=str(embedded.field_name), signer="", intact=False, valid=False, trusted=False,
                        timestamp=None, timestamp_trusted=False, timestamp_authority="", modification="",
                        coverage="", problems=(f"la signature {role} est illisible",),
                    )
                )
                continue
            checks.append(_check(embedded, status))
    return Verification(signatures=tuple(checks), unsigned_fields=empty)
