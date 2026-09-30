"""What the consignes tests share: a factory per model, tiny files, and a way
to start from an empty app.

Every name, number, date, amount and count here is INVENTED - the bons'
designations, BL numbers and ticket numbers included: the repository is
public, and the owner's real tickets (read for their STRUCTURE only) carry
his account, his driver and his deliveries. The designations are made up so
that the seeded motifs recognise them (« FÛT … », « … CO2 … »).

The seeds are there in every test database (migration 0002): three types
and the UBA format. A test that needs the app empty calls `no_defaults()`;
one that makes a second format with UBA's motifs and asks which format
reads a document must call it too, or both formats recognise it.
"""

import hashlib
import importlib
import io
import itertools
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.core.files.base import ContentFile
from PIL import Image

from invoices.models import Supplier
from invoices.tests.pdf_files import write_pdf
from returnables.models import (
    Pickup,
    PickupCount,
    PickupPhoto,
    ReturnableType,
    Slip,
    SlipFormat,
    SlipLine,
)

#: The seed migration itself, read for its data: the factories default to
#: exactly what every database is given (test_models pins it apart, with
#: literals, so a slip in the migration is caught rather than copied).
SEEDS = importlib.import_module("returnables.migrations.0002_seed_defaults")
SEEDED_TYPE_NAMES = tuple(name for name, _position, _motifs in SEEDS.TYPES)
SEEDED_FORMAT_NAME = SEEDS.FORMAT_NAME
#: Every motif of the seeded UBA format, by field name.
UBA_MOTIFS = dict(SEEDS.FORMAT)

#: An invented delivery day, in the past (a reading refuses a date after
#: today + 7 days).
DELIVERY_DAY = date(2026, 2, 10)

_serial = itertools.count(1)
_DEFAULT = object()


def no_defaults() -> None:
    """Delete what migration 0002 seeded - the UBA format, then the three
    types - so a test starts from an empty app. Raises ProtectedError when
    the test already hung bons or counts on them: call it first."""
    SlipFormat.objects.filter(name=SEEDED_FORMAT_NAME).delete()
    ReturnableType.objects.filter(name__in=SEEDED_TYPE_NAMES).delete()


def uba() -> Supplier:
    """The UBA supplier invoices/0002 seeds in every database."""
    supplier, _created = Supplier.objects.get_or_create(
        code="UBA", defaults={"name": "UBA", "parser_key": "UBA", "is_scrapable": True}
    )
    return supplier


def make_supplier(name=None, code=None) -> Supplier:
    """A supplier with no reader of its own - another seller of drinks."""
    number = next(_serial)
    return Supplier.objects.create(code=code or f"FOURNEX{number}", name=name or f"Fournisseur Exemple {number}")


def seeded_type(name: str) -> ReturnableType:
    return ReturnableType.objects.get(name=name)


def seeded_format() -> SlipFormat:
    return SlipFormat.objects.get(name=SEEDED_FORMAT_NAME)


# -- Types and formats ------------------------------------------------------------------------------------------


def make_type(name=None, position=None, slip_patterns="", is_active=True) -> ReturnableType:
    """A type of consigne. Unnamed, it gets a name of its own; unplaced, it
    sorts after the seeds and after every type made before it."""
    number = next(_serial)
    return ReturnableType.objects.create(
        name=name or f"Type exemple {number}",
        position=100 + number if position is None else position,
        slip_patterns=slip_patterns,
        is_active=is_active,
    )


def make_format(name=None, supplier=None, **fields) -> SlipFormat:
    """A format of bon reading with the seeded UBA motifs unless `fields`
    say otherwise (`section_start=""`, `line_pattern=…`), for UBA unless
    another `supplier` is given."""
    values = {**UBA_MOTIFS, "is_active": True, **fields}
    return SlipFormat.objects.create(
        name=name or f"Format exemple {next(_serial)}", supplier=supplier or uba(), **values
    )


# -- Bons ---------------------------------------------------------------------------------------------------------

#: One invented line per seeded type: (designation, quantity, unit price,
#: amount), as a reading stores them.
KEG_LINE = ("FÛT INOX 30 L", 3, Decimal("30.0000"), Decimal("90.00"))
CRATE_LINE = ("CAISSE 24X25CL", 2, Decimal("6.5000"), Decimal("13.00"))
CO2_LINE = ("BOUTEILLE CO2 10 KG", 1, Decimal("85.0000"), Decimal("85.00"))


def _money(value) -> str:
    return "" if value is None else f"{value + 0:.2f}"  # + 0: never « -0.00 »


def _refund(lines) -> Decimal:
    """What a bon of `lines` prints as its « Deconsigne »: minus the sum."""
    return -sum((amount or Decimal("0") for _designation, _quantity, _unit, amount in lines), Decimal("0")) + 0


def slip_text(number="", delivery_date=DELIVERY_DAY, lines=(KEG_LINE,), references=(), printed_at=None, replaces=False):
    """The text of an invented bon laid out as UBA's driver prints it (the
    STRUCTURE of the real ones, every value made up): the seeded motifs read
    back `number`, `delivery_date`, `references`, `lines`, `replaces` and the
    total from it."""
    printed = printed_at or datetime.combine(delivery_date or DELIVERY_DAY, datetime.min.time()).replace(hour=8)
    text = [
        "U.B.A.",
        "1 RUE IMAGINAIRE",
        "00000 VILLE EXEMPLE",
        f"Le {printed:%d/%m/%Y %H:%M:%S}",
        "------------------------",
        "Tournee : TOURNEE EXEMPLE",
        "Livreur : 00000 EXEMPLE",
    ]
    if number:
        text.append(f"Ticket No : {number:0>10}")
    if replaces:
        text.append("***ANNULE ET REMPLACE***")
    text += ["Compte : 00000", "BAR EXEMPLE", "Tel : 00.00.00.00.00", "REPRISE VIDE", "------------------------"]
    for designation, quantity, unit, amount in lines:
        text.append(f"{designation} {quantity} x {_money(unit)} = {_money(amount)}")
    text += ["FACTURE(S)/BL DU JOUR", "------------------------"]
    for reference in references:
        day = f" du {delivery_date:%d/%m/%Y}" if delivery_date else ""
        text.append(f"BL No: {reference}{day}")
    text += [
        f"Deconsigne : {_money(_refund(lines))}",
        "ENCAISSEMENT",
        "------------------------",
        "Tot. Encaissement : 0.00EUR",
        "Merci de Votre Commande",
        "Signature Client",
    ]
    return "\n".join(text)


def tiny_pdf(lines) -> bytes:
    """A one-page PDF whose text layer prints `lines` (a list of str, or a
    text split on its line ends): a few hundred bytes, Helvetica WinAnsi, so
    « Û » survives (invoices/tests/pdf_files.py)."""
    if isinstance(lines, str):
        lines = lines.split("\n")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "bon.pdf"
        write_pdf(str(path), list(lines))
        return path.read_bytes()


def make_slip(
    fmt=None,
    *,
    lines=(KEG_LINE,),
    number=None,
    delivery_date=DELIVERY_DAY,
    references=None,
    printed_at=None,
    replaces=False,
    origin=Slip.Origin.UPLOAD,
    content=None,
    text=None,
    **fields,
) -> Slip:
    """A bon as returnables.slips would store it: its PDF saved (a real tiny
    PDF printing `text`), its reading on the row and its `lines` as
    SlipLines (tuples of designation, quantity, unit price, amount). Every
    call gets its own number, reference and bytes - so its own sha256 -
    unless told otherwise. `fmt` defaults to the seeded UBA format."""
    serial = next(_serial)
    number = str(1000 + serial) if number is None else number
    references = [f"9{serial:05d}"] if references is None else list(references)
    if text is None:
        text = slip_text(number, delivery_date, lines, references, printed_at, replaces)
    if content is None:
        content = tiny_pdf(text.split("\n") + [f"exemplaire {serial}"])
    slip = Slip(
        format=fmt or seeded_format(),
        origin=origin,
        sha256=hashlib.sha256(content).hexdigest(),
        original_name=f"T{serial:06d}.pdf",
        text=text,
        delivery_date=delivery_date,
        printed_at=printed_at,
        number=number,
        references=references,
        replaces=replaces,
        printed_total=_refund(lines),
        **fields,
    )
    slip.file.save(f"bon-{number or 'sans-numero'}.pdf", ContentFile(content), save=False)
    slip.save()
    SlipLine.objects.bulk_create(
        SlipLine(
            slip=slip, position=position, designation=designation, quantity=quantity, unit_amount=unit, amount=amount
        )
        for position, (designation, quantity, unit, amount) in enumerate(lines, start=1)
    )
    return slip


# -- Reprises and photos ----------------------------------------------------------------------------------------


def tiny_jpeg(size=(4, 3), exif_orientation=None, taken_at=None) -> bytes:
    """A JPEG of a few pixels - never a large image in a test. With
    `exif_orientation`, the EXIF Orientation tag (6 = turn it upright
    clockwise); with `taken_at`, the EXIF DateTimeOriginal: a datetime (an
    aware one also writes its OffsetTimeOriginal) or a raw string, to write
    what a broken phone writes."""
    image = Image.new("RGB", size, (200, 120, 40))
    exif = Image.Exif()
    if exif_orientation is not None:
        exif[0x0112] = exif_orientation
    if taken_at is not None:
        taken = exif.get_ifd(0x8769)
        if isinstance(taken_at, datetime):
            taken[0x9003] = taken_at.strftime("%Y:%m:%d %H:%M:%S")
            offset = taken_at.utcoffset()
            if offset is not None:
                minutes = int(offset.total_seconds()) // 60
                sign = "-" if minutes < 0 else "+"
                taken[0x9011] = f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
        else:
            taken[0x9003] = taken_at
    output = io.BytesIO()
    image.save(output, "JPEG", exif=exif)
    return output.getvalue()


def _type_of(key) -> ReturnableType:
    return key if isinstance(key, ReturnableType) else ReturnableType.objects.get(name=key)


def make_photo(pickup, *, taken_at=None, size=(4, 3)) -> PickupPhoto:
    """A photo of `pickup` as the page stores one: a JPEG and its thumbnail,
    named after the reprise's day."""
    number = pickup.photos.count() + 1
    stem = f"reprise-{pickup.date:%Y%m%d}-{number}"
    photo = PickupPhoto(pickup=pickup, taken_at=taken_at, width=size[0], height=size[1])
    photo.image.save(f"{stem}.jpg", ContentFile(tiny_jpeg(size)), save=False)
    photo.thumb.save(f"{stem}-vignette.jpg", ContentFile(tiny_jpeg((2, 2))), save=False)
    photo.save()
    return photo


def make_pickup(date=DELIVERY_DAY, supplier=_DEFAULT, counts=None, photos=0, note="", photo_taken_at=None) -> Pickup:
    """A reprise of `date`, repris par `supplier` (UBA unless given; None
    for « fournisseur non précisé »), with `counts` - {type or type name:
    quantity}, a zero left out as the page leaves it - and `photos` tiny
    photos. Without `counts` it is 15 of the first seeded type (« Fûts »):
    after `no_defaults()`, give the counts."""
    pickup = Pickup.objects.create(date=date, supplier=uba() if supplier is _DEFAULT else supplier, note=note)
    if counts is None:
        counts = {SEEDED_TYPE_NAMES[0]: 15}
    PickupCount.objects.bulk_create(
        PickupCount(pickup=pickup, returnable_type=_type_of(key), quantity=quantity)
        for key, quantity in counts.items()
        if quantity
    )
    for _ in range(photos):
        make_photo(pickup, taken_at=photo_taken_at)
    return pickup
