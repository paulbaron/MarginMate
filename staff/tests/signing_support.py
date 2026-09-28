"""What the signature tests share: a private folder of their own, an
offline timestamp authority, a drawn signature - all generated, nothing
real. The repository is public: no key, certificate, signed PDF or signature
image is ever committed, so every one of them is made here, in a temp
folder, for the test that needs it.

`OfflineTimestamps` is how a test (the views' included) signs WITHOUT the
network: it patches `staff.signing.timestampers` to hand out pyHanko's
`DummyTimeStamper`, signed by a throwaway RSA authority (DummyTimeStamper is
RSA-only), and `staff.signing.timestamp_trust_roots` to trust that authority
- so `verify` says the timestamp is trusted, as it would for DigiCert's.

    class MyTests(SigningTestMixin, NoNetworkTestCase): ...

gives each test its own `STAFF_PRIVATE_DIR` and the offline timestamps."""

from __future__ import annotations

import datetime as dt
import io
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from asn1crypto import keys as asn1_keys
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.test import override_settings
from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo
from pyhanko.sign.timestamps import DummyTimeStamper

#: The offline authority's name, as `verify` reports it.
FAKE_TSA_NAME = "Horodatage de test (hors ligne)"


def _der(certificate) -> asn1_x509.Certificate:
    return asn1_x509.Certificate.load(certificate.public_bytes(serialization.Encoding.DER))


class FakeTimestampAuthority:
    """A self-signed RSA time-stamping authority, made once per process
    (RSA key generation is the slow part)."""

    _made = None

    def __init__(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, FAKE_TSA_NAME)])
        now = dt.datetime.now(dt.timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
            .sign(key, hashes.SHA256())
        )
        self.certificate = _der(certificate)
        self.key = asn1_keys.PrivateKeyInfo.load(
            key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        )

    @classmethod
    def shared(cls) -> "FakeTimestampAuthority":
        if cls._made is None:
            cls._made = cls()
        return cls._made

    def timestamper(self, fixed_dt: dt.datetime | None = None) -> DummyTimeStamper:
        stamper = DummyTimeStamper(tsa_cert=self.certificate, tsa_key=self.key, fixed_dt=fixed_dt)
        # What `staff.signing.authority_label` records as the authority.
        stamper.url = "http://horodatage.test"
        return stamper


class FailingTimestamper(DummyTimeStamper):
    """A timestamp server that does not answer."""

    def __init__(self, url="http://en-panne.test"):
        authority = FakeTimestampAuthority.shared()
        super().__init__(tsa_cert=authority.certificate, tsa_key=authority.key)
        self.url = url
        self.calls = 0

    async def async_timestamp(self, message_digest, md_algorithm):
        self.calls += 1
        raise OSError("Connection refused (test)")


class OfflineTimestamps:
    """Context manager (or `start()`/`stop()`): signing uses the fake
    authority - or `stampers`, when given - and `verify` trusts it."""

    def __init__(self, stampers=None, fixed_dt=None):
        self.authority = FakeTimestampAuthority.shared()
        self.stampers = stampers
        self.fixed_dt = fixed_dt
        self._patchers = []

    def start(self):
        stampers = self.stampers
        fixed = self.fixed_dt
        authority = self.authority

        def timestampers():
            return list(stampers) if stampers is not None else [authority.timestamper(fixed)]

        self._patchers = [
            mock.patch("staff.signing.timestampers", new=timestampers),
            mock.patch("staff.signing.timestamp_trust_roots", new=lambda: [authority.certificate]),
        ]
        for patcher in self._patchers:
            patcher.start()
        return self

    def stop(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._patchers = []

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


class SigningTestMixin:
    """Each test gets its own private folder (keys, signed files) and signs
    with the offline timestamps. Put it before the TestCase class."""

    def setUp(self):
        super().setUp()
        self.private_dir = Path(tempfile.mkdtemp(prefix="marginmate-signing-test-"))
        self.addCleanup(shutil.rmtree, self.private_dir, ignore_errors=True)
        settings_override = override_settings(STAFF_PRIVATE_DIR=self.private_dir, MARGINMATE_SIGNING_PASSPHRASE="")
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.timestamps = OfflineTimestamps().start()
        self.addCleanup(self.timestamps.stop)


#: The employee's invented stroke, and the employer's - another shape, so a
#: test tells the two drawings apart.
ZIGZAG = ((20, 150), (80, 40), (120, 160), (170, 60), (230, 150), (290, 70), (340, 140), (420, 90), (560, 110))
LOOPS = ((30, 120), (90, 50), (150, 150), (200, 60), (260, 140), (330, 40), (380, 160), (470, 70), (580, 130))


def drawn_signature(width=600, height=200, colour=(20, 30, 120, 255), points=ZIGZAG, pnginfo=None) -> bytes:
    """A PNG such as the page's canvas exports: a transparent background and
    one stroke - invented, of course. `pnginfo` adds chunks a canvas never
    writes (a text, a date), which the kept picture must not carry."""
    image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    scale_x, scale_y = width / 600, height / 200
    draw.line([(x * scale_x, y * scale_y) for x, y in points], fill=colour, width=7, joint="curve")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=pnginfo)
    return buffer.getvalue()


def png_metadata() -> PngInfo:
    """Chunks a canvas never writes - an author, a date, invented: what a
    crafted post could carry, and the kept picture must not."""
    info = PngInfo()
    info.add_text("Author", "Auteur imaginaire")
    info.add_text("CreationTime", "2026-07-03T18:05:00")
    return info


def employer_signature(**options) -> bytes:
    """The employer's drawn signature, as the owner's pad exports it."""
    return drawn_signature(points=LOOPS, **options)


def blank_canvas(width=600, height=200) -> bytes:
    """What a pad exports when nothing was drawn: transparent, no ink."""
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (255, 255, 255, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def data_url(png: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def countersign_without_a_drawing(request, *, now=None, ip=None, user_agent="Bureau"):
    """A request countersigned THE WAY IT WAS BEFORE 28/09 - no drawing in
    the « Employeur » box, none kept, none in the COUNTERSIGNED event nor in
    the countersignature's /Reason - step for step what
    `signature_requests.countersign_request` did then. Requests finished that
    way exist (one on the owner's database): they must keep displaying,
    verifying and producing their proof as they did."""
    from django.db import transaction
    from django.utils import timezone
    from pyhanko.sign import fields, signers

    from staff import pdf, private_files, signature_requests as workflow, signing
    from staff.models import Establishment, SignatureEvent, SignatureRequest

    now = now or timezone.now()
    request.refresh_from_db()
    assert request.status == SignatureRequest.Status.EMPLOYEE_SIGNED
    employee_signed = private_files.read_checked(
        request.uuid, private_files.EMPLOYEE_SIGNED, request.employee_pdf_sha256
    )
    journal = SignatureRequest.objects.filter(pk=request.pk).values_list("last_event_hash", flat=True).first() or ""
    establishment = Establishment.objects.get(pk=Establishment.SINGLETON_PK)
    identity = signing.employer_identity(establishment)
    style = signing._StampStyle(
        lines=(
            *signing._signer_lines("Contresigné électroniquement par", signing._establishment_name(establishment)),
            signing._line((pdf.printable(signing._moment_line(now)), pdf.REGULAR)),
            signing._line((pdf.printable(f"Document n° {request.document_id}"), pdf.REGULAR)),
        ),
    )
    meta = signers.PdfSignatureMetadata(
        field_name=signing.EMPLOYER_FIELD,
        md_algorithm="sha256",
        subfilter=fields.SigSeedSubFilter.PADES,
        name=identity.name,
        reason=f"{signing.REASON_EMPLOYER}{signing.JOURNAL_MARK}{journal}" if journal else signing.REASON_EMPLOYER,
    )
    signed = signing._sign(employee_signed, meta, identity, style, None)
    final_sha = private_files.sha256(signed.pdf)
    with transaction.atomic():
        SignatureRequest.objects.filter(pk=request.pk).update(
            status=SignatureRequest.Status.COMPLETE,
            employer_signed_at=now,
            final_pdf_sha256=final_sha,
            employer_timestamp_at=signed.timestamp,
            employer_timestamp_authority=signed.authority[:255],
            expires_at=max(request.expires_at, now + workflow.LINK_VALIDITY),
        )
        private_files.write(request.uuid, private_files.FINAL, signed.pdf)
        workflow.log_event(
            request, SignatureEvent.Kind.COUNTERSIGNED, at=now, ip=ip, user_agent=user_agent,
            detail={
                "final_sha256": final_sha,
                "timestamp_authority": signed.authority,
                "timestamp": workflow._moment(signed.timestamp) if signed.timestamp else None,
                "authority_sha256": signed.issuer_sha256,
                "journal": journal,
            },
        )
    request.refresh_from_db()
    workflow.store_proof(request, now=now)
    return request
