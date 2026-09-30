"""The two signature boxes at the foot of the sheet are ONE definition
(`staff.pdf.SIGNATURE_BOXES`): the page draws them from it and the
electronic signature (`staff.signing`) places its fields and stamps by it.
Read back from the drawn page through pdfplumber, so a box moved on the page
moves the stamp with it - or fails here. Invented names only."""

import io
from datetime import date

import pdfplumber
from django.test import SimpleTestCase

from staff.models import Establishment
from staff.pdf import (
    ELECTRONIC_SIGNATURE_BOXES,
    EMPLOYEE_BOX,
    EMPLOYER_BOX,
    MARGIN_BOTTOM,
    PAGE_HEIGHT,
    SIGNATURE_BOXES,
    render_month_pdf,
)
from staff.tests.support import employee
from staff.timesheet import build_sheet

JUNE = date(2026, 6, 1)
BAR = Establishment(name="BAR EXEMPLE", address="12 rue Imaginaire\n75000 PARIS")
#: The paper sheet, and the document frozen for the electronic signature
#: (its boxes' words differ, so where the stamp may go is measured again).
SHEETS = ((False, SIGNATURE_BOXES), (True, ELECTRONIC_SIGNATURE_BOXES))


def _page(electronic=False):
    sheet = build_sheet(employee(save=False), JUNE)
    return pdfplumber.open(io.BytesIO(render_month_pdf(sheet, BAR, electronic=electronic))).pages[0]


def _pdf_rect(rect):
    """pdfplumber measures from the top; the PDF from the foot."""
    return (rect["x0"], PAGE_HEIGHT - rect["bottom"], rect["x1"], PAGE_HEIGHT - rect["top"])


class SignatureBoxesTests(SimpleTestCase):
    def test_the_page_draws_exactly_the_boxes_defined(self):
        for electronic, boxes in SHEETS:
            page = _page(electronic)
            drawn = [_pdf_rect(rect) for rect in page.rects if rect["stroke"]]
            for box in boxes:
                with self.subTest(box=box.label, electronic=electronic):
                    self.assertTrue(
                        any(all(abs(a - b) < 0.05 for a, b in zip(rect, box.rect)) for rect in drawn),
                        f"{box.label}: {box.rect} not among the drawn boxes {drawn}",
                    )

    def test_employee_left_employer_right_at_the_foot(self):
        self.assertEqual(SIGNATURE_BOXES, (EMPLOYEE_BOX, EMPLOYER_BOX))
        self.assertEqual(EMPLOYEE_BOX.label, "Le salarié")
        self.assertEqual(EMPLOYER_BOX.label, "L'employeur")
        self.assertLess(EMPLOYEE_BOX.right, EMPLOYER_BOX.left)
        self.assertEqual(EMPLOYEE_BOX.bottom, MARGIN_BOTTOM)
        self.assertEqual(EMPLOYER_BOX.bottom, MARGIN_BOTTOM)

    def test_the_stamp_area_is_inside_the_box_and_under_what_it_prints(self):
        """The stamp covers neither the box's frame nor its label and
        instructions: every word printed in the box sits above it - on the
        paper sheet and on the document frozen to be signed, whose fields are
        placed by its own boxes (signing.FIELD_BOXES)."""
        for electronic, boxes in SHEETS:
            with self.subTest(electronic=electronic):
                self.assert_stamps_clear(_page(electronic), boxes)

    def assert_stamps_clear(self, page, boxes):
        words = page.extract_words()
        for box in boxes:
            with self.subTest(box=box.label):
                left, bottom, right, top = box.stamp_rect
                self.assertGreater(left, box.left)
                self.assertLess(right, box.right)
                self.assertGreater(bottom, box.bottom)
                self.assertLess(top, box.top)
                inside = [
                    word
                    for word in words
                    if box.left <= word["x0"] <= box.right
                    and PAGE_HEIGHT - word["bottom"] >= box.bottom
                    and PAGE_HEIGHT - word["top"] <= box.top
                ]
                self.assertTrue(inside, "the box prints its label")
                lowest = min(PAGE_HEIGHT - word["bottom"] for word in inside)
                self.assertLess(top, lowest, f"the stamp area reaches the printed words of « {box.label} »")
                # Room for a drawing and three lines of text.
                self.assertGreater(top - bottom, 50)
                self.assertGreater(right - left, 200)
