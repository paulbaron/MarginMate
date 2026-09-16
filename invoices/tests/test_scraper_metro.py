"""The Metro download loop, driven against a fake browser, a fake clock and a
real folder - no Selenium session, no network, no Metro account.

What it pins down came from one real gather: ten downloads that had landed
within a second or two were each waited on for the full timeout, because
completion was judged by counting the files in the folder; and every deposit
credit note was downloaded again on every run, because the list page names
it "134-052-014645" while the parser stores "052-014645".
"""

import os
import shutil
import tempfile

from django.test import SimpleTestCase

from invoices.scrapers import metro

STAMP = "20260122072041"


class Clock:
    """Time that only moves when the code under test sleeps - and runs
    whatever the fake site scheduled for that moment."""

    def __init__(self):
        self.now = 0.0
        self.events = []

    def __call__(self):
        return self.now

    def at(self, delay, action):
        self.events.append((self.now + delay, action))

    def sleep(self, seconds):
        self.now += seconds
        due = sorted((event for event in self.events if event[0] <= self.now), key=lambda event: event[0])
        self.events = [event for event in self.events if event[0] > self.now]
        for _when, action in due:
            action()


class Row:
    def __init__(self, key, on_click=None):
        self.checkbox_id = f"FRA_{key}_{STAMP}"
        self.on_click = on_click or (lambda: None)


class FakeElement:
    """Stands for a row's download button, its <tr> and its checkbox alike."""

    def __init__(self, row):
        self.row = row

    def find_element(self, by, value):
        return self

    def get_attribute(self, name):
        return self.row.checkbox_id


class FakeDriver:
    current_url = "https://docs.metro.fr/"
    title = "eInvoice"

    def __init__(self, rows, clock):
        self.rows = rows
        self.clock = clock
        self.clicks = []

    def find_elements(self, by, value):
        return [FakeElement(row) for row in self.rows]

    def find_element(self, by, value):
        raise LookupError("no page body in a fake")

    def execute_script(self, script, element):
        self.clicks.append((element.row.checkbox_id, self.clock.now))
        element.row.on_click()

    def save_screenshot(self, path):
        return False


class DownloadWindowTests(SimpleTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.clock = Clock()
        self.log = []

    def write(self, name):
        with open(os.path.join(self.dir, name), "wb") as handle:
            handle.write(b"%PDF-1.4")

    def pdf_name(self, key, suffix=""):
        return f"{key}_{STAMP}_invoice_cus_copy_main{suffix}.pdf"

    def lands(self, key, after, suffix=""):
        return lambda: self.clock.at(after, lambda: self.write(self.pdf_name(key, suffix)))

    def run_window(self, rows, known=()):
        driver = FakeDriver(rows, self.clock)
        started = metro._download_window(
            driver, self.dir, len(rows), set(known), self.log.append, lambda: None,
            sleep=self.clock.sleep, clock=self.clock,
        )
        return driver, started

    def timeouts(self):
        return [line for line in self.log if "did not finish downloading" in line]

    def test_a_file_that_lands_at_once_is_not_waited_for(self):
        keys = ("134_52_14645", "134_90_1309", "134_52_13683")
        self.run_window([Row(key, self.lands(key, after=0.3)) for key in keys])
        self.assertEqual(self.timeouts(), [])
        self.assertLessEqual(self.clock.now, len(keys) * metro.CLICK_INTERVAL_SECONDS + 1)

    def test_a_stray_download_vanishing_as_the_pdf_lands_does_not_hide_it(self):
        """The folder holds as many files after as before - the old count
        never moved, and the loop waited out its full timeout."""
        stray = os.path.join(self.dir, "downloads.htm.crdownload")

        def click():
            self.write("downloads.htm.crdownload")
            self.clock.at(0.3, lambda: (os.remove(stray), self.write(self.pdf_name("134_52_14645"))))

        self.run_window([Row("134_52_14645", click)])
        self.assertEqual(self.timeouts(), [])
        self.assertLess(self.clock.now, 5)

    def test_the_next_click_does_not_wait_for_the_previous_file(self):
        slow, fast = "134_53_40586", "134_52_45126"
        driver, _started = self.run_window([Row(slow, self.lands(slow, after=10)), Row(fast, self.lands(fast, after=0.5))])
        self.assertLess(driver.clicks[1][1], 10)
        self.assertEqual(self.timeouts(), [])

    def test_never_more_than_three_downloads_outstanding(self):
        keys = [f"134_52_{number}" for number in range(1, 6)]
        driver, _started = self.run_window([Row(key, self.lands(key, after=30)) for key in keys])
        click_times = [clicked_at for _id, clicked_at in driver.clicks]
        self.assertLess(click_times[2], 30)
        self.assertGreaterEqual(click_times[3], 30, "a fourth download was started with three outstanding")

    def test_clicks_are_spaced_like_a_person_would(self):
        keys = [f"134_52_{number}" for number in range(1, 4)]
        driver, _started = self.run_window([Row(key, self.lands(key, after=0.1)) for key in keys])
        times = [clicked_at for _id, clicked_at in driver.clicks]
        self.assertTrue(all(b - a >= metro.CLICK_INTERVAL_SECONDS for a, b in zip(times, times[1:])))

    def test_a_download_that_never_arrives_is_reported_and_the_rest_go_on(self):
        rows = [
            Row("134_52_1", self.lands("134_52_1", after=0.5)),
            Row("134_52_2"),
            Row("134_52_3", self.lands("134_52_3", after=0.5)),
        ]
        driver, started = self.run_window(rows)
        self.assertEqual(started, 3)
        self.assertEqual(len(self.timeouts()), 1)
        self.assertIn("2/3", self.timeouts()[0])

    def test_downloads_that_keep_failing_stop_the_window(self):
        driver, started = self.run_window([Row(f"134_52_{number}") for number in range(1, 9)])
        self.assertLess(started, 8)
        self.assertTrue(any("Stopping this window" in line for line in self.log))

    def test_a_file_left_by_an_earlier_run_is_not_this_download(self):
        self.write(self.pdf_name("134_52_14645"))
        self.run_window([Row("134_52_14645")])
        self.assertEqual(len(self.timeouts()), 1)

    def test_a_download_saved_under_a_new_name_counts(self):
        """Chrome saves "name (1).pdf" when the name is taken."""
        self.write(self.pdf_name("134_52_14645"))
        self.run_window([Row("134_52_14645", self.lands("134_52_14645", after=0.5, suffix=" (1)"))])
        self.assertEqual(self.timeouts(), [])

    def test_an_invoice_already_imported_is_never_clicked(self):
        rows = [Row("134_53_20875"), Row("134_52_14645")]
        driver, started = self.run_window(rows, known={"134-053-020875", "052-014645"})
        self.assertEqual((driver.clicks, started), ([], 0))


class RowNumberTests(SimpleTestCase):
    def test_a_row_names_its_invoice_zero_padded(self):
        self.assertEqual(metro._invoice_number((134, 52, 14645)), "134-052-014645")

    def test_a_credit_note_is_known_by_till_and_number(self):
        """Its PDF doesn't print the store, so it is stored without one."""
        self.assertTrue(metro._is_known((134, 52, 14645), {"052-014645"}))
        self.assertTrue(metro._is_known((134, 53, 20875), {"134-053-020875"}))
        self.assertFalse(metro._is_known((134, 52, 14646), {"052-014645", "134-052-014645"}))

    def test_the_file_a_row_produces(self):
        self.assertEqual(metro._file_prefix((134, 52, 14645)), "134_52_14645_")
        self.assertEqual(metro._file_prefix(None), "")
