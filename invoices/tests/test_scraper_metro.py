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

    def execute_script(self, script, *args):
        if script == metro.ROWS_JS:
            return [(FakeElement(row), row.checkbox_id) for row in self.rows]
        element = args[0]
        self.clicks.append((element.row.checkbox_id, self.clock.now))
        element.row.on_click()

    def save_screenshot(self, path):
        return False


class BlankOnceDriver(FakeDriver):
    """The list read blank once, at its `blank_call`-th reading - as a list
    caught between two drawings."""

    def __init__(self, rows, clock, blank_call):
        super().__init__(rows, clock)
        self.blank_call = blank_call
        self.reads = 0

    def execute_script(self, script, *args):
        if script == metro.ROWS_JS:
            self.reads += 1
            if self.reads == self.blank_call:
                return []
        return super().execute_script(script, *args)


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

    def run_window(self, rows, known=(), run=None, driver=None):
        driver = driver or FakeDriver(rows, self.clock)
        started = metro._download_window(
            driver, self.dir, len(rows), set(known), self.log.append, lambda: None,
            sleep=self.clock.sleep, clock=self.clock, run=run,
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

    def test_one_download_at_a_time(self):
        """Three overlapped once, to spare waits a counting bug made long;
        with the bug gone, a file lands in a second, and one at a time is
        what a person does (Metro's firewall blocks what hammers it)."""
        slow, fast = "134_53_40586", "134_52_45126"
        driver, _started = self.run_window([Row(slow, self.lands(slow, after=10)), Row(fast, self.lands(fast, after=0.5))])
        self.assertGreaterEqual(driver.clicks[1][1], 10, "the second download was started with the first outstanding")
        self.assertEqual(self.timeouts(), [])

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

    def test_downloads_that_keep_failing_stop_the_run(self):
        """Not only the window: the next one was searched and clicked on."""
        with self.assertRaises(metro.MetroError) as stopped:
            self.run_window([Row(f"134_52_{number}") for number in range(1, 9)])
        self.assertEqual(len(self.timeouts()), metro.MAX_CONSECUTIVE_TIMEOUTS)
        # One verb for a gather, on the page: « récupérer ».
        self.assertIn("Le reste viendra à une prochaine récupération.", str(stopped.exception))

    def test_the_count_of_failures_runs_across_windows(self):
        run = metro._Run()
        self.run_window([Row("134_52_1"), Row("134_52_2")], run=run)
        with self.assertRaises(metro.MetroError) as stopped:
            self.run_window([Row("134_52_3")], run=run)
        self.assertIn("Le reste viendra à une prochaine récupération.", str(stopped.exception))

    def test_rows_are_followed_by_their_number_when_the_list_redraws(self):
        """A row arrived on top under a click: walked by place, the next
        place held the row just downloaded - clicked twice - and the last
        row was never reached."""
        rows = []

        def first_click():
            rows.insert(0, Row("134_52_99", self.lands("134_52_99", after=0.3)))
            self.lands("134_52_1", after=0.3)()

        rows.extend([Row("134_52_1", first_click), Row("134_52_2", self.lands("134_52_2", after=0.3))])
        driver = FakeDriver(rows, self.clock)
        self.run_window(list(rows), driver=driver)
        clicked = [checkbox for checkbox, _at in driver.clicks]
        self.assertEqual(clicked.count(f"FRA_134_52_1_{STAMP}"), 1)
        self.assertIn(f"FRA_134_52_2_{STAMP}", clicked)
        self.assertIn(f"FRA_134_52_99_{STAMP}", clicked)

    def test_a_row_is_counted_as_fetched_once_its_file_landed(self):
        known = set()
        rows = [Row("134_52_1", self.lands("134_52_1", after=0.3)), Row("134_52_2")]
        driver = FakeDriver(rows, self.clock)
        started = metro._download_window(
            driver, self.dir, len(rows), known, self.log.append, lambda: None, sleep=self.clock.sleep, clock=self.clock
        )
        self.assertEqual(started, 2)
        self.assertEqual(known, {"134-052-000001"}, "a download that never came was counted as fetched")

    def test_a_row_without_a_readable_number_is_kept_for_the_end_of_the_run(self):
        """Said once every window has been searched (scrape_metro_invoices),
        not by stopping the run at the first."""
        row = Row("134_52_1")
        row.checkbox_id = "autre"
        run = metro._Run()
        self.run_window([row], run=run)
        self.assertEqual(run.unreadable, ["cette période"])

    def test_a_list_read_blank_between_two_drawings_is_read_again(self):
        """One read caught the list empty after a click: the window ended
        there, the rest of its invoices never clicked, and nothing said."""
        rows = [Row(f"134_52_{n}", self.lands(f"134_52_{n}", after=0.3)) for n in (1, 2, 3)]
        driver = BlankOnceDriver(rows, self.clock, blank_call=3)
        self.run_window(list(rows), driver=driver)
        self.assertEqual(len(driver.clicks), 3)

    def test_a_row_missing_when_its_click_comes_is_tried_again(self):
        rows = [Row(f"134_52_{n}", self.lands(f"134_52_{n}", after=0.3)) for n in (1, 2)]
        driver = BlankOnceDriver(rows, self.clock, blank_call=4)
        self.run_window(list(rows), driver=driver)
        self.assertIn(f"FRA_134_52_2_{STAMP}", [checkbox for checkbox, _at in driver.clicks])

    def test_fewer_rows_read_than_announced_is_said(self):
        with self.assertRaises(metro.MetroError):
            metro._download_window(
                FakeDriver([Row("134_52_1", self.lands("134_52_1", after=0.3))], self.clock), self.dir, 3, set(),
                self.log.append, lambda: None, sleep=self.clock.sleep, clock=self.clock,
            )

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
