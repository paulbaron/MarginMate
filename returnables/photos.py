"""The photos of a pickup: taken on a phone, re-encoded here, the original
never kept.

`prepare_photo(uploaded)` turns one upload into a `PreparedPhoto`: a JPEG of
at most PHOTO_MAX_SIDE pixels a side, upright, WITHOUT its EXIF (a phone's
photo carries the bar's GPS position; Pillow's JPEG writer only writes the
metadata it is handed, and it is handed none), a thumbnail of THUMB_SIDE,
the size, and when it was taken (EXIF DateTimeOriginal) - or `PhotoError`,
a French sentence. Nothing here touches the database or the storage: the
page prepares every photo in memory before its transaction.

Memory is the constraint (critique of 29/09: a photo fully decoded below
Pillow's bomb limit can take 2 GB). So, one photo at a time, every image
closed, and nothing decoded before its size is known:

1. at most PHOTO_MAX_BYTES;
2. `Image.open` reads the header only; only JPEG (and its multi-picture
   MPO), PNG and WebP are tried - HEIC, and formats whose loading would run
   a program (EPS), never;
3. width × height at most PHOTO_MAX_PIXELS (PHOTO_MAX_PIXELS_JPEG for a
   JPEG), before any decoding. Pillow's DecompressionBombWarning is an
   error too (caught locally), except for a JPEG within its own limit:
   `draft()` decodes a JPEG at 1/2 to 1/8 of its size, so the full decode
   the warning is about never happens - without that exception a 108 MP
   phone photo would be refused and PHOTO_MAX_PIXELS_JPEG would mean nothing.
   A PROGRESSIVE JPEG is held to PHOTO_MAX_PIXELS all the same: libjpeg
   keeps its coefficients at full size (3 to 6 bytes a pixel) whatever the
   scale, and no phone writes one;
4. EXIF in its own try: a broken EXIF means no date and no rotation, never
   a refusal;
5. draft at the scale `_draft_scale` chooses - Pillow's own for the kept
   side, min(width // side, height // side), or more: the smallest that
   brings the DECODED pixels within PHOTO_MAX_PIXELS (an elongated JPEG,
   59 600 × 3 000, got min(29, 1) = 1 from Pillow alone and was decoded
   whole: 0.5 to 1.8 GB a photo). Then what draft actually GAVE is checked
   against PHOTO_MAX_PIXELS before anything is decoded (a JPEG of several
   tiles is not reduced at all). Then thumbnail, exif_transpose in place,
   RGB only when needed, JPEG quality 85.

Tests patch PHOTO_MAX_PIXELS / PHOTO_MAX_PIXELS_JPEG / Image.MAX_IMAGE_PIXELS
down and use images of a few pixels: never allocate a large image.
"""

from __future__ import annotations

import io
import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import timezone as fixed_offset

from django.utils import timezone
from PIL import Image, ImageOps

PHOTO_MAX_BYTES = 30 * 1024 * 1024
#: Pixels (width × height) a photo may have, checked before decoding it.
PHOTO_MAX_PIXELS = 40_000_000
PHOTO_MAX_PIXELS_JPEG = 180_000_000
#: The kept photo's longest side, and its thumbnail's.
PHOTO_MAX_SIDE = 2000
THUMB_SIDE = 480
PHOTO_QUALITY = 85
THUMB_QUALITY = 75

READABLE_FORMATS = ("JPEG", "PNG", "WEBP")
JPEG_FORMATS = ("JPEG", "MPO")
#: The scales libjpeg decodes a JPEG at (1/1 to 1/8), finest first.
DRAFT_SCALES = (1, 2, 4, 8)

EXIF_IFD = 0x8769
DATETIME_ORIGINAL = 0x9003
OFFSET_TIME_ORIGINAL = 0x9011
DATETIME = 0x0132
#: The oldest date a photo's EXIF may say.
OLDEST_PHOTO = date(2000, 1, 1)

NOT_READABLE = "Cette photo n'est pas dans un format lisible (JPEG, PNG, WebP)."
TOO_HEAVY = "Cette photo est trop lourde (30 Mo au plus)."
DAMAGED = "Cette photo est illisible : le fichier est abîmé ou incomplet."

_OFFSET = re.compile(r"([+-])([0-9]{2}):?([0-9]{2})")


class PhotoError(ValueError):
    """A photo refused: `message` is the French sentence (the page adds the
    file's name)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class PreparedPhoto:
    jpeg: bytes
    thumb: bytes
    width: int
    height: int
    taken_at: datetime | None


def _too_big(width: int, height: int) -> PhotoError:
    return PhotoError(
        f"Cette photo est trop grande ({width} × {height} pixels) : réglez l'appareil sur une résolution normale."
    )


def _stream(uploaded):
    """A readable, seekable file for `uploaded` (an UploadedFile, a file,
    or bytes), its size checked first."""
    if isinstance(uploaded, (bytes, bytearray, memoryview)):
        if len(uploaded) > PHOTO_MAX_BYTES:
            raise PhotoError(TOO_HEAVY)
        return io.BytesIO(bytes(uploaded))
    size = getattr(uploaded, "size", None)
    if size is None:
        uploaded.seek(0, io.SEEK_END)
        size = uploaded.tell()
    if size > PHOTO_MAX_BYTES:
        raise PhotoError(TOO_HEAVY)
    uploaded.seek(0)
    return uploaded


def _exif_text(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    if not isinstance(value, str):
        return ""
    return value.strip("\N{NULL} ").strip()


def taken_at_from_exif(exif, now: datetime | None = None) -> datetime | None:
    """When the photo was taken: DateTimeOriginal (the Exif IFD), else the
    IFD0 DateTime; `%Y:%m:%d %H:%M:%S`, str or bytes, NUL-padded or not;
    at OffsetTimeOriginal's offset when there is one, else Paris time.
    Zero, blank, unparseable, or outside [2000-01-01, now + 1 day]: None."""
    try:
        sub = exif.get_ifd(EXIF_IFD)
    except Exception:  # noqa: BLE001 - an Exif IFD that will not read leaves the time unknown
        sub = {}
    paris = timezone.get_default_timezone()
    offset = _OFFSET.fullmatch(_exif_text(sub.get(OFFSET_TIME_ORIGINAL)))
    original_zone = paris
    if offset:
        hours, minutes = int(offset.group(2)), int(offset.group(3))
        if hours <= 14 and minutes <= 59:
            delta = timedelta(hours=hours, minutes=minutes)
            original_zone = fixed_offset(-delta if offset.group(1) == "-" else delta)
    now = now or timezone.now()
    oldest = timezone.make_aware(datetime.combine(OLDEST_PHOTO, datetime.min.time()), paris)
    # The original's date first (at its offset), then IFD0's (Paris time):
    # the first that reads, within the bounds.
    for value, zone in ((sub.get(DATETIME_ORIGINAL), original_zone), (exif.get(DATETIME), paris)):
        raw = _exif_text(value)
        if not raw:
            continue
        try:
            taken = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").replace(tzinfo=zone)
        except ValueError:
            continue
        if oldest <= taken <= now + timedelta(days=1):
            return taken
    return None


def _rgb(image: Image.Image) -> Image.Image:
    """The image in RGB: itself when it already is, else a new one - a
    transparent PNG laid on white rather than on black."""
    if image.mode == "RGB":
        return image
    if "A" in image.getbands() or (image.mode == "P" and "transparency" in image.info):
        with image.convert("RGBA") as rgba:
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            return flat
    return image.convert("RGB")


def _draft_scale(width: int, height: int) -> int | None:
    """The scale a photo is decoded at (a JPEG's; the others ignore draft):
    Pillow's own for the kept side - min(width // side, height // side),
    down to 1, 2, 4 or 8 - or a coarser one, the finest whose decoded
    pixels fit PHOTO_MAX_PIXELS. None: not even 1/8 does."""
    side = min(width // PHOTO_MAX_SIDE, height // PHOTO_MAX_SIDE)
    wanted = max((scale for scale in DRAFT_SCALES if scale <= side), default=1)
    for scale in DRAFT_SCALES:
        # libjpeg rounds each side up.
        if scale >= wanted and -(-width // scale) * -(-height // scale) <= PHOTO_MAX_PIXELS:
            return scale
    return None


def _jpeg(image: Image.Image, quality: int) -> bytes:
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue()


def prepare_photo(uploaded, *, now: datetime | None = None) -> PreparedPhoto:
    """One uploaded photo, re-encoded (see the module docstring), or
    PhotoError."""
    stream = _stream(uploaded)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", Image.DecompressionBombWarning)
        try:
            image = Image.open(stream, formats=READABLE_FORMATS)
        except Image.DecompressionBombError:
            raise PhotoError("Cette photo est trop grande : réglez l'appareil sur une résolution normale.") from None
        except Exception:  # noqa: BLE001 - a malformed header is a French message, never a 500
            # OSError (UnidentifiedImageError: HEIC, a PDF, text…),
            # SyntaxError and ValueError from a plugin's header parser - and
            # whatever else a malformed header raises: never a 500.
            raise PhotoError(NOT_READABLE) from None
    warned = any(issubclass(warning.category, Image.DecompressionBombWarning) for warning in caught)

    with image:
        width, height = image.size
        is_jpeg = image.format in JPEG_FORMATS
        limit = PHOTO_MAX_PIXELS_JPEG if is_jpeg else PHOTO_MAX_PIXELS
        if width * height > limit or (warned and not is_jpeg):
            raise _too_big(width, height)
        if is_jpeg and image.info.get("progressive") and width * height > PHOTO_MAX_PIXELS:
            # Its coefficients are kept whole whatever the scale.
            raise _too_big(width, height)
        scale = _draft_scale(width, height)
        if scale is None:
            raise _too_big(width, height)

        taken_at, exif_read = None, False
        try:
            taken_at = taken_at_from_exif(image.getexif(), now)
            exif_read = True
        except Exception:  # noqa: BLE001 - EXIF that will not read leaves the time unknown
            taken_at, exif_read = None, False

        try:
            # Asked (width // scale, height // scale), Pillow's draft takes
            # exactly `scale`: both of its ratios fall in [scale, 2 × scale)
            # (a side shorter than `scale` gives less - checked just below).
            image.draft("RGB", (max(1, width // scale), max(1, height // scale)))
        except Exception:  # noqa: BLE001 - a damaged photo is a French message, never a 500
            raise PhotoError(DAMAGED) from None
        # What draft GAVE, not what it was asked: it reduces nothing for a
        # JPEG of several tiles, and the others decode whole.
        if image.size[0] * image.size[1] > PHOTO_MAX_PIXELS:
            raise _too_big(width, height)
        try:
            # thumbnail() leaves an image already small enough undecoded:
            # load() decodes it HERE, where a cut-off file is « abîmé », not
            # later in save().
            image.thumbnail((PHOTO_MAX_SIDE, PHOTO_MAX_SIDE))
            image.load()
        except Image.DecompressionBombError:
            raise _too_big(width, height) from None
        except Exception:  # noqa: BLE001 - a damaged photo is a French message, never a 500
            raise PhotoError(DAMAGED) from None

        if exif_read:
            try:
                ImageOps.exif_transpose(image, in_place=True)
            except Exception:  # noqa: BLE001, S110 - a photo that cannot be turned upright is kept as taken
                pass

        flat = None
        try:
            flat = _rgb(image)
            # Nothing of the phone's metadata goes out: no EXIF (GPS), no
            # comment, no XMP - Pillow takes its JPEG comment from info.
            flat.info = {}
            jpeg = _jpeg(flat, PHOTO_QUALITY)
            with flat.copy() as small:
                small.thumbnail((THUMB_SIDE, THUMB_SIDE))
                thumb = _jpeg(small, THUMB_QUALITY)
            size = flat.size
        except Exception:  # noqa: BLE001 - a damaged photo is a French message, never a 500
            raise PhotoError(DAMAGED) from None
        finally:
            if flat is not None and flat is not image:
                flat.close()
    return PreparedPhoto(jpeg=jpeg, thumb=thumb, width=size[0], height=size[1], taken_at=taken_at)
