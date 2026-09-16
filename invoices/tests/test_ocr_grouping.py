"""Grouping recognised text boxes into printed lines.

The recogniser returns detached boxes; which of them form one line of the
receipt has to be inferred, and getting it wrong is how an item ends up with
its neighbour's price. These are hand-built box lists shaped like the two
failures seen on real photos: a price column a row off its name column on a
tilted or curled receipt, and tightly printed rows merging into one.
"""

import math

from django.test import SimpleTestCase

from invoices.ocr import group_boxes_into_lines


def box(text, cx, cy, width, height=30.0, degrees=0.0, confidence=0.95):
    """A RapidOCR-shaped box: four corners clockwise from the top left,
    rotated by `degrees` about its centre."""
    angle = math.radians(degrees)
    along = (math.cos(angle), math.sin(angle))
    across = (-math.sin(angle), math.cos(angle))
    corners = []
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        corners.append(
            [
                cx + sx * width / 2 * along[0] + sy * height / 2 * across[0],
                cy + sx * width / 2 * along[1] + sy * height / 2 * across[1],
            ]
        )
    return [corners, text, confidence]


def texts(lines):
    return [line.text for line in lines]


class GroupBoxesIntoLinesTests(SimpleTestCase):
    def test_a_straight_receipt(self):
        lines = group_boxes_into_lines(
            [
                box("BAGUETTE BLANC", 200, 100, 300),
                box("T1 0.49", 800, 101, 120),
                box("CITRON 500G", 180, 150, 260),
                box("T1 2.29", 800, 149, 120),
            ]
        )
        self.assertEqual(texts(lines), ["BAGUETTE BLANC  T1 0.49", "CITRON 500G  T1 2.29"])

    def test_a_tilted_receipt_keeps_each_price_with_its_own_item(self):
        """At 6 degrees the price sits 63px lower than its name across a
        600px-wide receipt - more than a whole 50px row. Comparing raw
        heights pairs each name with the price of the row above."""
        slope = math.tan(math.radians(6))
        boxes = []
        for row, (name, price) in enumerate((("BAGUETTE BLANC", "T1 0.49"), ("CITRON 500G", "T1 2.29"), ("ORANGE", "T1 3.21"))):
            y = 100 + 50 * row
            boxes.append(box(name, 200, y, 300, degrees=6))
            boxes.append(box(price, 800, y + slope * 600, 120, degrees=6))
        self.assertEqual(
            texts(group_boxes_into_lines(boxes)),
            ["BAGUETTE BLANC  T1 0.49", "CITRON 500G  T1 2.29", "ORANGE  T1 3.21"],
        )

    def test_two_rows_of_the_same_column_are_never_one_line(self):
        """Rows printed this tightly overlap vertically, and used to be glued
        together - two products read as one, at the second one's price."""
        lines = group_boxes_into_lines(
            [
                box("ORANGE DE TABLE MT", 200, 100, 300, height=30),
                box("CITRON JNE SP 500G", 205, 118, 300, height=30),
                box("4,99", 800, 100, 80, height=30),
                box("2,79", 800, 118, 80, height=30),
            ]
        )
        self.assertEqual(texts(lines), ["ORANGE DE TABLE MT  4,99", "CITRON JNE SP 500G  2,79"])

    def test_a_short_box_takes_the_slope_of_its_long_neighbours(self):
        """A lone amount has no angle of its own worth trusting."""
        slope = math.tan(math.radians(5))
        lines = group_boxes_into_lines(
            [
                box("NOMBRE D'ARTICLES", 200, 100, 320, degrees=5),
                box("7", 800, 100 + slope * 600, 20, degrees=0),
                box("RESTE A PAYER", 180, 150, 260, degrees=5),
                box("10,28", 800, 150 + slope * 620, 90, degrees=5),
            ]
        )
        self.assertEqual(texts(lines), ["NOMBRE D'ARTICLES  7", "RESTE A PAYER  10,28"])

    def test_cells_read_left_to_right_and_keep_their_confidence(self):
        (line,) = group_boxes_into_lines([box("13.06", 800, 100, 80, confidence=0.7), box("TOTAL", 200, 100, 120)])
        self.assertEqual(line.text, "TOTAL  13.06")
        self.assertAlmostEqual(line.confidence, 0.7)

    def test_empty_input_and_empty_text(self):
        self.assertEqual(group_boxes_into_lines([]), [])
        self.assertEqual(group_boxes_into_lines([box("", 100, 100, 50)]), [])
