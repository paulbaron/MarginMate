"""An id read from a request is ASCII digits (common.is_id): "²" is a digit
to str.isdigit() and no int, and the query it reached raised - a 500 where a
tampered form gets a message."""

from django.test import SimpleTestCase

from common import is_id


class IsIdTests(SimpleTestCase):
    def test_ascii_digits_only(self):
        self.assertTrue(is_id("12"))
        self.assertTrue(is_id("9" * 18))
        # Past an SQLite integer, the query raised OverflowError.
        self.assertFalse(is_id("9" * 19))
        for value in ("²", "١٢", "12a", "", " 12", "-1", None, 12):
            with self.subTest(value=value):
                self.assertFalse(is_id(value))
