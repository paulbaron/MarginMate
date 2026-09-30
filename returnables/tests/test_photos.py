"""returnables/photos.py: a phone photo, re-encoded upright, without its
EXIF, with the moment it was taken - or a French refusal.

MACHINE SAFETY: every image here is a few pixels. « Too big » is tested by
patching the limits (PHOTO_MAX_PIXELS, PHOTO_MAX_PIXELS_JPEG,
Image.MAX_IMAGE_PIXELS, PHOTO_MAX_BYTES) DOWN to the size of these images,
never by building a large one.
"""

import io
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.utils import timezone
from PIL import Image, ImageFile, features

from returnables import photos
from returnables.photos import PhotoError, prepare_photo, taken_at_from_exif

#: An invented moment, in the past (a date after now + 1 day is refused).
TAKEN = "2025:06:14 08:14:00"


def exif_with(orientation=None, taken=None, offset=None, gps=False, ifd0_date=None) -> Image.Exif:
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    if ifd0_date:
        exif[0x0132] = ifd0_date
    if taken or offset:
        sub = exif.get_ifd(0x8769)
        if taken:
            sub[0x9003] = taken
        if offset:
            sub[0x9011] = offset
    if gps:
        location = exif.get_ifd(0x8825)
        location[1] = "N"
        location[2] = (1.0, 2.0, 3.0)
        location[3] = "E"
        location[4] = (4.0, 5.0, 6.0)
    return exif


def jpeg(size=(40, 20), color="white", **kwargs) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG", **kwargs)
    return output.getvalue()


def png(size=(20, 20), mode="RGB", color="white") -> bytes:
    output = io.BytesIO()
    Image.new(mode, size, color).save(output, format="PNG")
    return output.getvalue()


def opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


class FakeExif(dict):
    """What taken_at_from_exif reads: IFD0 as a dict, and get_ifd."""

    def __init__(self, ifd0=None, sub=None):
        super().__init__(ifd0 or {})
        self.sub = sub or {}

    def get_ifd(self, tag):
        return self.sub if tag == 0x8769 else {}


class PreparedPhotoTests(SimpleTestCase):
    def test_a_photo_comes_out_a_jpeg_with_its_thumbnail(self):
        result = prepare_photo(jpeg())
        self.assertTrue(result.jpeg.startswith(b"\xff\xd8"))
        self.assertTrue(result.thumb.startswith(b"\xff\xd8"))
        self.assertEqual((result.width, result.height), (40, 20))
        self.assertEqual(opened(result.jpeg).size, (40, 20))
        self.assertIsNone(result.taken_at)

    def test_it_takes_an_upload_a_file_or_bytes(self):
        for uploaded in (SimpleUploadedFile("image.jpg", jpeg()), io.BytesIO(jpeg()), jpeg(), bytearray(jpeg())):
            with self.subTest(kind=type(uploaded).__name__):
                self.assertEqual(prepare_photo(uploaded).width, 40)

    def test_a_portrait_shot_stored_on_its_side_comes_out_upright(self):
        """Orientation 6: the phone stored it rotated and said so in EXIF."""
        result = prepare_photo(jpeg(exif=exif_with(orientation=6)))
        self.assertEqual((result.width, result.height), (20, 40))
        self.assertEqual(opened(result.jpeg).size, (20, 40))
        self.assertEqual(opened(result.thumb).size, (20, 40))
        self.assertNotIn(0x0112, opened(result.jpeg).getexif())

    def test_the_date_it_was_taken_is_read_in_paris_time(self):
        result = prepare_photo(jpeg(exif=exif_with(taken=TAKEN)))
        self.assertEqual(result.taken_at, datetime(2025, 6, 14, 8, 14, tzinfo=timezone.get_default_timezone()))
        self.assertEqual(result.taken_at.utcoffset(), timedelta(hours=2))

    def test_its_offset_is_used_when_the_phone_wrote_one(self):
        result = prepare_photo(jpeg(exif=exif_with(taken=TAKEN, offset="+05:00")))
        self.assertEqual(result.taken_at.utcoffset(), timedelta(hours=5))
        self.assertEqual(result.taken_at, datetime(2025, 6, 14, 3, 14, tzinfo=UTC))

    def test_the_ifd0_date_is_the_fallback(self):
        result = prepare_photo(jpeg(exif=exif_with(ifd0_date="2025:06:13 19:00:00")))
        self.assertEqual(result.taken_at.date().isoformat(), "2025-06-13")

    def test_nothing_of_the_phone_s_metadata_goes_out(self):
        """GPS, the date, the orientation, a JPEG comment: none survives the
        re-encoding - the photo and its thumbnail carry no EXIF at all."""
        original = jpeg(exif=exif_with(orientation=6, taken=TAKEN, gps=True), comment=b"commentaire-secret")
        self.assertEqual(opened(original).getexif().get_ifd(0x8825)[1], "N")
        result = prepare_photo(original)
        for output in (result.jpeg, result.thumb):
            image = opened(output)
            self.assertEqual(dict(image.getexif()), {})
            self.assertNotIn("exif", image.info)
            self.assertNotIn(b"Exif", output)
            self.assertNotIn(b"commentaire-secret", output)
        self.assertIsNotNone(result.taken_at)

    def test_a_broken_exif_is_no_date_and_no_rotation_never_a_refusal(self):
        with mock.patch.object(Image.Image, "getexif", side_effect=SyntaxError("broken")):
            result = prepare_photo(jpeg(exif=exif_with(orientation=6, taken=TAKEN)))
        self.assertIsNone(result.taken_at)
        self.assertEqual((result.width, result.height), (40, 20))

    def test_a_big_photo_is_brought_down_and_its_thumbnail_further(self):
        with mock.patch.object(photos, "PHOTO_MAX_SIDE", 10), mock.patch.object(photos, "THUMB_SIDE", 4):
            result = prepare_photo(jpeg(size=(40, 20)))
        self.assertEqual((result.width, result.height), (10, 5))
        self.assertEqual(opened(result.jpeg).size, (10, 5))
        self.assertEqual(opened(result.thumb).size, (4, 2))

    def test_a_transparent_png_is_laid_on_white(self):
        result = prepare_photo(png(mode="RGBA", color=(0, 0, 0, 0)))
        image = opened(result.jpeg)
        self.assertEqual(image.mode, "RGB")
        self.assertTrue(all(channel > 240 for channel in cast("tuple[int, ...]", image.getpixel((5, 5)))))

    def test_other_modes_are_converted(self):
        for mode, color in (("L", 128), ("P", 3), ("LA", (10, 255)), ("CMYK", (0, 0, 0, 0))):
            with self.subTest(mode=mode):
                output = io.BytesIO()
                image_format = "JPEG" if mode == "CMYK" else "PNG"
                Image.new(mode, (8, 8), color).save(output, format=image_format)
                self.assertEqual(opened(prepare_photo(output.getvalue()).jpeg).mode, "RGB")

    def test_webp_is_read(self):
        if not features.check("webp"):
            self.skipTest("Pillow without WebP")
        output = io.BytesIO()
        Image.new("RGB", (12, 6), "white").save(output, format="WEBP")
        self.assertEqual(prepare_photo(output.getvalue()).width, 12)


class RefusedPhotoTests(SimpleTestCase):
    def assertRefused(self, uploaded, text: str):
        with self.assertRaises(PhotoError) as caught:
            prepare_photo(uploaded)
        self.assertIn(text, caught.exception.message)
        return caught.exception.message

    def test_too_heavy(self):
        with mock.patch.object(photos, "PHOTO_MAX_BYTES", 10):
            for uploaded in (jpeg(), SimpleUploadedFile("image.jpg", jpeg()), io.BytesIO(jpeg())):
                with self.subTest(kind=type(uploaded).__name__):
                    self.assertEqual(
                        self.assertRefused(uploaded, "trop lourde"), "Cette photo est trop lourde (30 Mo au plus)."
                    )

    def test_too_many_pixels_before_any_decoding(self):
        with (
            mock.patch.object(photos, "PHOTO_MAX_PIXELS", 399),
            mock.patch.object(ImageFile.ImageFile, "load", side_effect=AssertionError("decoded")) as load,
        ):
            message = self.assertRefused(png(size=(20, 20)), "trop grande")
        load.assert_not_called()
        self.assertEqual(
            message, "Cette photo est trop grande (20 × 20 pixels) : réglez l'appareil sur une résolution normale."
        )
        with mock.patch.object(photos, "PHOTO_MAX_PIXELS", 400):
            self.assertEqual(prepare_photo(png(size=(20, 20))).width, 20)

    def test_a_jpeg_has_its_own_limit(self):
        # Over PHOTO_MAX_PIXELS in its header, a JPEG is still taken: draft()
        # decodes it at a scale that fits (here 1/2, 20 × 10 = 200) - never
        # whole, which is what the pixel limit is about.
        with mock.patch.object(photos, "PHOTO_MAX_PIXELS", 200):
            self.assertEqual(prepare_photo(jpeg(size=(40, 20))).width, 20)
        with mock.patch.object(photos, "PHOTO_MAX_PIXELS_JPEG", 799):
            self.assertRefused(jpeg(size=(40, 20)), "trop grande (40 × 20 pixels)")

    def test_a_decompression_bomb_is_refused(self):
        # Above twice Pillow's limit, Image.open itself raises.
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 100):
            self.assertRefused(png(size=(20, 20)), "trop grande")
            self.assertRefused(jpeg(size=(20, 20)), "trop grande")

    def test_pillow_s_warning_is_an_error_except_for_a_jpeg_decoded_small(self):
        # Between once and twice the limit Pillow only warns: refused, except
        # a JPEG within its own limit, which draft() decodes at 1/2 to 1/8.
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 300):
            self.assertRefused(png(size=(20, 20)), "trop grande")
            self.assertEqual(prepare_photo(jpeg(size=(20, 20))).width, 20)

    def test_not_an_image(self):
        heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heicmiaf" + b"\x00" * 64
        gif = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(gif, format="GIF")
        eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 2 2\nshowpage\n"
        for content in (b"bonjour", heic, gif.getvalue(), eps, b"%PDF-1.4\n", b""):
            with self.subTest(content=content[:12]):
                self.assertEqual(
                    self.assertRefused(content, "format lisible"),
                    "Cette photo n'est pas dans un format lisible (JPEG, PNG, WebP).",
                )

    def test_a_cut_off_photo_is_refused_as_damaged(self):
        # Noise, so that the picture's data - not its header - fills the file.
        output = io.BytesIO()
        Image.effect_noise((64, 64), 80).convert("RGB").save(output, format="JPEG", quality=95)
        whole = output.getvalue()
        self.assertEqual(
            self.assertRefused(whole[: len(whole) * 3 // 4], "abîmé ou incomplet"),
            "Cette photo est illisible : le fichier est abîmé ou incomplet.",
        )


class DecodedSizeTests(SimpleTestCase):
    """What is DECODED stays within PHOTO_MAX_PIXELS, whatever the header.
    Pillow's own draft scale is min(width // side, height // side): an
    elongated JPEG (59 600 × 3 000, 178.8 MP, a few MB when flat) got
    min(29, 1) = 1 and was decoded whole - 0.5 to 1.8 GB a photo, ten
    photos a post. The scale is now chosen from the pixels, and what draft
    actually gave is checked before anything is decoded. Tiny images,
    limits patched down (never a large image here)."""

    def prepared(self, data):
        """prepare_photo(data), and the sizes Pillow decoded at."""
        decoded = []
        real_load = ImageFile.ImageFile.load

        def load(image):
            if image.tile:
                decoded.append(image.size)
            return real_load(image)

        with mock.patch.object(ImageFile.ImageFile, "load", load):
            try:
                return prepare_photo(data), decoded
            except PhotoError as error:
                return error, decoded

    def test_an_elongated_jpeg_is_decoded_within_the_pixels_not_whole(self):
        # 820 × 30 = 24 600 in its header; side 10: Pillow's scale alone is
        # min(82, 3) → 1/2, 410 × 15 = 6 150 decoded. The pixels ask 1/4.
        with (
            mock.patch.object(photos, "PHOTO_MAX_SIDE", 10),
            mock.patch.object(photos, "PHOTO_MAX_PIXELS", 2000),
            mock.patch.object(photos, "PHOTO_MAX_PIXELS_JPEG", 30_000),
        ):
            result, decoded = self.prepared(jpeg(size=(820, 30)))
        self.assertNotIsInstance(result, PhotoError)
        self.assertTrue(decoded)
        self.assertLessEqual(max(width * height for width, height in decoded), 2000, decoded)
        self.assertEqual(decoded, [(205, 8)])
        self.assertEqual((result.width, result.height), (10, 1))

    def test_one_that_even_an_eighth_cannot_bring_within_is_refused_undecoded(self):
        with (
            mock.patch.object(photos, "PHOTO_MAX_SIDE", 10),
            mock.patch.object(photos, "PHOTO_MAX_PIXELS", 100),
            mock.patch.object(photos, "PHOTO_MAX_PIXELS_JPEG", 30_000),
        ):
            result, decoded = self.prepared(jpeg(size=(820, 30)))
        self.assertIsInstance(result, PhotoError)
        self.assertEqual(
            result.message,
            "Cette photo est trop grande (820 × 30 pixels) : réglez l'appareil sur une résolution normale.",
        )
        self.assertEqual(decoded, [])

    def test_a_jpeg_draft_cannot_reduce_is_refused_before_it_is_decoded(self):
        # draft() does nothing for a JPEG of several tiles: the size it
        # leaves is the one checked, not the one hoped for.
        with (
            mock.patch.object(photos, "PHOTO_MAX_PIXELS", 200),
            mock.patch("PIL.JpegImagePlugin.JpegImageFile.draft", return_value=None),
        ):
            result, decoded = self.prepared(jpeg(size=(40, 20)))
        self.assertIsInstance(result, PhotoError)
        self.assertIn("trop grande (40 × 20 pixels)", result.message)
        self.assertEqual(decoded, [])

    def test_a_progressive_jpeg_is_held_to_the_pixels_before_any_scale(self):
        # libjpeg keeps a progressive JPEG's coefficients at FULL size (about
        # 3 to 6 bytes a pixel) whatever draft's scale: a phone never writes
        # one, and one over PHOTO_MAX_PIXELS is refused from its header.
        progressive = jpeg(size=(40, 20), progressive=True)
        self.assertTrue(Image.open(io.BytesIO(progressive)).info.get("progressive"))
        with mock.patch.object(photos, "PHOTO_MAX_PIXELS", 799):
            result, decoded = self.prepared(progressive)
        self.assertIsInstance(result, PhotoError)
        self.assertIn("trop grande (40 × 20 pixels)", result.message)
        self.assertEqual(decoded, [])
        with mock.patch.object(photos, "PHOTO_MAX_PIXELS", 800):
            self.assertEqual(prepare_photo(progressive).width, 40)
        # A baseline JPEG of the same size is still taken, decoded at 1/2.
        with mock.patch.object(photos, "PHOTO_MAX_PIXELS", 799):
            self.assertEqual(prepare_photo(jpeg(size=(40, 20))).width, 20)

    def test_the_scale_for_real_sizes_arithmetic_only(self):
        # No image here: the figures of the finding, through the arithmetic.
        for (width, height), scale in (
            ((59_600, 3_000), 4),  # Pillow alone: 1, i.e. 178.8 MP decoded
            ((12_000, 9_000), 4),  # a 108 MP phone photo: Pillow's own 1/4
            ((8_000, 6_000), 2),
            ((4_000, 3_000), 1),  # 12 MP, decoded whole as before
            ((1_000_000, 100), 2),  # a side under 2 000: Pillow alone, 1
            ((60_000, 60_000), None),  # 1/8 still 7 500 × 7 500 = 56.25 MP
        ):
            with self.subTest(size=(width, height)):
                found = photos._draft_scale(width, height)
                self.assertEqual(found, scale)
                if found is not None:
                    self.assertLessEqual(-(-width // found) * -(-height // found), photos.PHOTO_MAX_PIXELS)

    def test_the_usual_phone_photo_keeps_pillow_s_scale(self):
        # 40 × 20 with a side of 10 (a 4000 × 2000 photo, side 1000): Pillow
        # would take 1/2 for the side alone; the pixels allow it whole - the
        # finer of the two is never taken over the side's.
        with mock.patch.object(photos, "PHOTO_MAX_SIDE", 10):
            result, decoded = self.prepared(jpeg(size=(40, 20)))
        self.assertEqual(decoded, [(20, 10)])
        self.assertEqual((result.width, result.height), (10, 5))


class TakenAtTests(SimpleTestCase):
    NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

    def taken(self, sub=None, ifd0=None):
        return taken_at_from_exif(FakeExif(ifd0=ifd0, sub=sub), now=self.NOW)

    def test_str_or_bytes_nul_padded_or_not(self):
        expected = datetime(2025, 6, 14, 8, 14, tzinfo=timezone.get_default_timezone())
        for value in (TAKEN, TAKEN.encode(), TAKEN + "\N{NULL}", (TAKEN + "\N{NULL}").encode(), f"  {TAKEN} "):
            with self.subTest(value=value):
                self.assertEqual(self.taken({0x9003: value}), expected)

    def test_what_is_not_a_date(self):
        for value in (
            "",
            "   ",
            "\N{NULL}" * 20,
            "0000:00:00 00:00:00",
            "2025:02:30 08:00:00",
            "2025-06-14 08:14:00",
            "hier",
            None,
            20250614,
            b"\xff\xfe",
        ):
            with self.subTest(value=value):
                self.assertIsNone(self.taken({0x9003: value}))

    def test_the_bounds(self):
        self.assertIsNone(self.taken({0x9003: "1999:12:31 23:59:59"}))
        self.assertIsNotNone(self.taken({0x9003: "2000:01:01 00:00:00"}))
        self.assertIsNotNone(self.taken({0x9003: "2026:09:30 11:00:00"}))
        self.assertIsNone(self.taken({0x9003: "2026:09:30 15:00:00"}))

    def test_the_original_date_wins_over_ifd0(self):
        self.assertEqual(self.taken({0x9003: TAKEN}, {0x0132: "2024:01:01 00:00:00"}).year, 2025)
        self.assertEqual(self.taken({}, {0x0132: "2024:01:01 10:00:00"}).year, 2024)
        # A zeroed original date is not read: IFD0's is.
        self.assertEqual(self.taken({0x9003: "0000:00:00 00:00:00"}, {0x0132: "2024:01:01 10:00:00"}).year, 2024)
        self.assertEqual(self.taken({0x9003: "1999:01:01 00:00:00"}, {0x0132: "2024:01:01 10:00:00"}).year, 2024)
        self.assertIsNone(self.taken({0x9003: "0000:00:00 00:00:00"}, {0x0132: "   "}))

    def test_the_offset(self):
        for offset, hours in (("+05:00", 5), ("-03:30", -3.5), ("+0100", 1)):
            with self.subTest(offset=offset):
                result = self.taken({0x9003: TAKEN, 0x9011: offset})
                self.assertEqual(result.utcoffset(), timedelta(hours=hours))
        for offset in ("", "+25:00", "05:00", "abc", b"\x00"):
            with self.subTest(offset=offset):
                self.assertEqual(self.taken({0x9003: TAKEN, 0x9011: offset}).utcoffset(), timedelta(hours=2))

    def test_an_unreadable_ifd_is_no_date(self):
        class Broken(FakeExif):
            def get_ifd(self, tag):
                raise ValueError("broken")

        self.assertIsNone(taken_at_from_exif(Broken(), now=self.NOW))
        self.assertIsNotNone(taken_at_from_exif(Broken({0x0132: TAKEN}), now=self.NOW))
