"""The drawn signature as it is kept (`signing.clean_signature_png`,
`signing.signature_png_from_data_url`): what the page's canvas posts is
decoded by Pillow and encoded again, never stored as received."""

import base64
import io

from django.test import SimpleTestCase
from PIL import Image, PngImagePlugin

from staff import signing
from staff.tests.signing_support import data_url, drawn_signature


def png_of(image, **params) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", **params)
    return buffer.getvalue()


class CleanSignatureTests(SimpleTestCase):
    def test_a_drawing_is_kept_with_its_transparency(self):
        kept = signing.clean_signature_png(drawn_signature())
        image = Image.open(io.BytesIO(kept))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.size, (600, 200))
        self.assertEqual(image.getpixel((0, 0))[3], 0, "the background stays transparent")

    def test_everything_but_the_pixels_is_dropped(self):
        drawing = Image.open(io.BytesIO(drawn_signature()))
        info = PngImagePlugin.PngInfo()
        info.add_text("Author", "quelqu'un")
        info.add_text("Comment", "<script>alert(1)</script>")
        kept = signing.clean_signature_png(png_of(drawing, pnginfo=info))
        self.assertNotIn(b"quelqu'un", kept)
        self.assertNotIn(b"script", kept)
        self.assertEqual(Image.open(io.BytesIO(kept)).info.get("Author"), None)

    def test_an_empty_canvas_is_refused(self):
        blank = png_of(Image.new("RGBA", (600, 200), (255, 255, 255, 0)))
        white = png_of(Image.new("RGBA", (600, 200), (255, 255, 255, 255)))
        dot = Image.new("RGBA", (600, 200), (255, 255, 255, 0))
        dot.putpixel((10, 10), (0, 0, 0, 255))
        for name, data in (("transparent", blank), ("blanc", white), ("un point", png_of(dot))):
            with self.subTest(name), self.assertRaises(signing.SignatureImageError) as caught:
                signing.clean_signature_png(data)
            self.assertIn("vide", str(caught.exception))

    def test_the_refusal_speaks_to_whoever_is_drawing(self):
        """The employee signs, the owner countersigns: a pad left blank - or
        clicked once, a dot of a few pixels - is refused in each one's own
        words (review, 28/09: the owner was told « avant de signer » beside
        « Contresigner le relevé »)."""
        dot = Image.new("RGBA", (600, 200), (255, 255, 255, 0))
        for x in range(10, 13):
            for y in range(10, 13):
                dot.putpixel((x, y), (20, 30, 120, 255))
        self.assertEqual(signing.DRAWING_EMPTY, "La signature est vide : dessinez-la dans le cadre avant de signer.")
        self.assertEqual(
            signing.EMPLOYER_DRAWING_EMPTY, "La signature est vide : dessinez-la dans le cadre avant de contresigner."
        )
        with self.assertRaises(signing.SignatureImageError) as caught:
            signing.clean_signature_png(png_of(dot))
        self.assertEqual(str(caught.exception), signing.DRAWING_EMPTY)
        with self.assertRaises(signing.SignatureImageError) as caught:
            signing.clean_signature_png(png_of(dot), empty=signing.EMPLOYER_DRAWING_EMPTY)
        self.assertEqual(str(caught.exception), signing.EMPLOYER_DRAWING_EMPTY)

    def test_what_is_not_a_png_is_refused(self):
        jpeg = io.BytesIO()
        Image.new("RGB", (60, 20), (0, 0, 0)).save(jpeg, format="JPEG")
        for name, data in (
            ("jpeg", jpeg.getvalue()),
            ("texte", b"bonjour"),
            ("vide", b""),
            ("png tronque", drawn_signature()[:60]),
        ):
            with self.subTest(name), self.assertRaises(signing.SignatureImageError):
                signing.clean_signature_png(data)

    def test_too_many_pixels_is_refused_before_decoding(self):
        big = png_of(Image.new("RGBA", (1201, 200), (0, 0, 0, 255)))
        with self.assertRaises(signing.SignatureImageError) as caught:
            signing.clean_signature_png(big)
        self.assertIn("1201 × 200", str(caught.exception))
        tall = png_of(Image.new("RGBA", (600, 401), (0, 0, 0, 255)))
        with self.assertRaises(signing.SignatureImageError):
            signing.clean_signature_png(tall)
        self.assertTrue(signing.clean_signature_png(drawn_signature(1200, 400)))

    def test_too_heavy_is_refused(self):
        import os

        noise = Image.frombytes("RGBA", (600, 200), os.urandom(600 * 200 * 4))
        heavy = png_of(noise)
        self.assertGreater(len(heavy), 300 * 1024)
        with self.assertRaises(signing.SignatureImageError) as caught:
            signing.clean_signature_png(heavy)
        self.assertIn("300 Ko", str(caught.exception))

    def test_a_decompression_bomb_is_refused(self):
        """A few KB of PNG declaring a huge canvas: refused on its size,
        never decoded."""
        bomb = png_of(Image.new("L", (6000, 6000), 0))
        self.assertLess(len(bomb), 300 * 1024)
        with self.assertRaises(signing.SignatureImageError):
            signing.clean_signature_png(bomb)


class DataUrlTests(SimpleTestCase):
    def test_the_canvas_data_url_is_decoded(self):
        self.assertEqual(signing.signature_png_from_data_url(data_url(drawn_signature())), drawn_signature())

    def test_anything_else_is_a_french_refusal(self):
        png = drawn_signature()
        cases = {
            "absent": None,
            "vide": "",
            "jpeg": "data:image/jpeg;base64," + base64.b64encode(png).decode(),
            "pas de base64": "data:image/png;base64,!!!pas du base64!!!",
            "sans virgule": "data:image/png;base64",
            "trop lourd": "data:image/png;base64," + "A" * (500 * 1024),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(signing.SignatureImageError):
                signing.signature_png_from_data_url(text)
