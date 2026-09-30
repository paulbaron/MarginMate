"""The month's grid in a real (headless) Chrome: what static/js/timesheet.js
does, which the test client cannot see.

* **The figures follow the typing** - each week's total, the month's, the
  three cards, the orange mark - and agree with what the server draws once
  the same grid is saved. Drawn by the server alone, a week corrected read
  « 36 h » beside the rows just changed (review, 28/09).
* **Nothing typed is lost without a word.** The three forms beside the grid
  reload the month from the database, and the PDF prints the month as
  SAVED: the review typed two corrections, pressed « Appliquer » and lost
  both in silence - and could have handed the employee a sheet to sign
  without them.

Tagged "browser": `--exclude-tag=browser` for the fast loop. Skipped where
Chrome or its driver is missing. Names and figures INVENTED (the typical
week of staff/tests/support.py).
"""

import tempfile
from datetime import date

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from django.urls import reverse

from invoices.scrapers import website
from staff.models import Employee, Timesheet, TimesheetDay
from staff.tests.support import TYPICAL_WEEK
from staff.views import LEAVE_WARNING, PDF_WARNING
from tests.runner import log_in_the_browser

JUNE = date(2026, 6, 1)   # Monday 1 to Tuesday 30: 151,5 h of typical week

FIGURES = """
    var text = function (element) { return element.textContent.replace(/\\s+/g, " ").trim(); };
    var all = function (selector) { return Array.prototype.map.call(document.querySelectorAll(selector), text); };
    return {
        weeks: all('[data-live="week"]'),
        worked: all('[data-live="worked"]'),
        daysWorked: all('[data-live="days-worked"]'),
        difference: all('[data-live="difference"]'),
        monthNote: all('[data-live="month-note"]'),
        absences: all('[data-live="absences"] li, [data-live="absences"] .stat-value'),
        changed: Array.prototype.map.call(
            document.querySelectorAll("tr.day-row.is-changed select[data-kind]"),
            function (select) { return select.name; }
        ),
        unsaved: Array.prototype.map.call(document.querySelectorAll("[data-unsaved]"), function (element) {
            return !element.hidden;
        }),
    };
"""


@tag("browser")
class MonthGridInBrowserTests(StaticLiveServerTestCase):
    # Its flush then fires no post_migrate: recreated content types broke
    # every later class restoring its snapshot (tests/test_transaction_cases.py).
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.driver = website.build_chrome(tempfile.mkdtemp(), True)
        except Exception as exc:  # noqa: BLE001
            cls.tearDownClass()
            raise cls.skipTest(cls, f"Chrome indisponible : {exc}")

    @classmethod
    def tearDownClass(cls):
        driver = getattr(cls, "driver", None)
        if driver is not None:
            driver.quit()
        super().tearDownClass()

    def setUp(self):
        self.person = Employee.objects.create(last_name="Dupont", first_name="Jeanne", **TYPICAL_WEEK)
        self.path = reverse("staff:month", args=[self.person.pk, JUNE])
        # The owner's page wants a login: the test espace's owner.
        log_in_the_browser(self.driver, self.live_server_url)
        self.open(self.path)

    def tearDown(self):
        # A grid left changed would ask before the next test's page opens -
        # a dialog nobody answers. A submit event (not a submission) is what
        # tells timesheet.js the page may go.
        self.script(
            "var form = document.querySelector('form[data-timesheet-form]');"
            "if (form) form.dispatchEvent(new Event('submit'));"
        )

    # -- helpers --------------------------------------------------------------

    def open(self, path):
        self.driver.get(self.live_server_url + path)
        self.wait_for(lambda: self.script("return document.readyState") == "complete")

    def script(self, source, *args):
        return self.driver.execute_script(source, *args)

    def wait_for(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self.driver, 10).until(lambda driver: condition())

    def element(self, css):
        return self.driver.find_element("css selector", css)

    def click(self, css):
        """A real click, the element first brought to the middle of the
        window: the sticky topbar covers what sits right under it."""
        element = self.element(css)
        self.script("arguments[0].scrollIntoView({block: 'center'})", element)
        element.click()

    def type_hours(self, day, text):
        field = self.element(f'[name="heures-2026-06-{day:02d}"]')
        field.clear()
        field.send_keys(text)

    def choose(self, name, value):
        from selenium.webdriver.support.ui import Select

        Select(self.element(f'[name="{name}"]')).select_by_value(value)

    def figures(self):
        return self.script(FIGURES)

    def answer_confirm(self, answer):
        """Every confirm() answered `answer`, and what it asked, kept."""
        self.script(
            "window.asked = [];"
            "var answer = arguments[0];"
            "window.confirm = function (question) { window.asked.push(question); return answer; };",
            answer,
        )

    def asked(self):
        return self.script("return window.asked")

    def unload_is_held(self):
        return self.script(
            "var event = new Event('beforeunload', {cancelable: true});"
            "window.dispatchEvent(event); return event.defaultPrevented;"
        )

    # -- the figures ------------------------------------------------------------

    def test_the_figures_follow_the_typing_and_match_the_server_s(self):
        before = self.figures()
        self.assertEqual(before["weeks"], ["36 h"] * 4 + ["7,5 h"])
        self.assertEqual(before["unsaved"], [False, False])

        # Tuesday 2 on leave, Wednesday 3 at 10 h instead of 6.
        self.choose("motif-2026-06-02", "conges")
        self.type_hours(3, "10")
        typed = self.figures()
        # Week 1: 36 - 7,5 - 6 + 10. The month: 151,5 - 13,5 + 10 = 148 h,
        # the leave's 7,5 h set aside: 148 - 144 = +4 h.
        self.assertEqual(typed["weeks"], ["32,5 h"] + ["36 h"] * 3 + ["7,5 h"])
        self.assertEqual(typed["worked"], ["148 h", "148 h"])
        self.assertEqual(typed["daysWorked"], ["20 jours travaillés"])
        self.assertEqual(typed["difference"], ["dont absences : 7,5 h · Écart hors absences : +4 h"])
        self.assertEqual(typed["monthNote"], ["semaine type : 151,5 h, dont absences 7,5 h (écart hors absences +4 h)"])
        self.assertEqual(typed["absences"], ["Congés payés : 1 jour"])
        self.assertEqual(typed["changed"], ["motif-2026-06-02", "motif-2026-06-03"])
        self.assertEqual(typed["unsaved"], [True, True])

        # A figure the server would refuse makes no total - « 1 5 » included.
        self.type_hours(4, "1 5")
        refused = self.figures()
        self.assertEqual(refused["weeks"][0], "— h")
        self.assertEqual(refused["worked"], ["— h", "— h"])
        self.type_hours(4, "7,5")

        # Saved: the server draws the same figures.
        self.click(".timesheet-save button[type=submit]")
        self.wait_for(lambda: "Fiche de juin 2026 enregistrée" in self.script("return document.body.textContent"))
        self.assertTrue(Timesheet.objects.filter(employee=self.person).exists())
        saved = self.figures()
        for key in ("weeks", "worked", "daysWorked", "difference", "monthNote", "absences", "changed"):
            with self.subTest(key=key):
                self.assertEqual(saved[key], typed[key])
        self.assertEqual(saved["unsaved"], [False, False])

    def test_typed_and_typed_back_is_no_change(self):
        self.type_hours(3, "10")
        self.assertEqual(self.figures()["unsaved"], [True, True])
        self.type_hours(3, "6")
        after = self.figures()
        self.assertEqual(after["unsaved"], [False, False])
        self.assertEqual(after["changed"], [])
        self.assertFalse(self.unload_is_held())

    # -- leaving a grid changed -----------------------------------------------

    def test_leaving_a_changed_grid_asks_first_and_stays_when_told_to(self):
        self.type_hours(3, "10")
        self.assertTrue(self.unload_is_held())
        self.answer_confirm(False)

        # « juillet → », the PDF, « Du … au … »: each asks, and stays.
        self.click('a[href$="/2026-07/"]')
        self.click("a[data-download]")
        self.choose("debut", "2026-06-16")
        self.choose("fin", "2026-06-17")
        self.click(".range-form button[type=submit]")
        self.assertEqual(self.asked(), [LEAVE_WARNING, PDF_WARNING, LEAVE_WARNING])
        self.assertTrue(self.driver.current_url.endswith(self.path))
        self.assertEqual(self.element('[name="heures-2026-06-03"]').get_attribute("value"), "10")
        self.assertFalse(Timesheet.objects.exists())

        # Told to go on: the range is applied, the typed 10 h dropped - as said.
        self.answer_confirm(True)
        self.click(".range-form button[type=submit]")
        self.wait_for(lambda: Timesheet.objects.filter(employee=self.person).exists())
        self.wait_for(lambda: "Congés payés du 16 au 17 juin 2026" in self.script("return document.body.textContent"))
        self.assertEqual(
            list(TimesheetDay.objects.filter(timesheet__employee=self.person, date__day__in=(3, 16, 17))
                 .order_by("date").values_list("kind", flat=True)),
            ["travail", "conges", "conges"],
        )
        self.assertEqual(self.element('[name="heures-2026-06-03"]').get_attribute("value"), "6")
        self.assertEqual(self.figures()["unsaved"], [False, False])

    def test_an_untouched_grid_leaves_without_a_word(self):
        self.answer_confirm(False)
        self.assertFalse(self.unload_is_held())
        self.click('a[href$="/2026-07/"]')
        self.wait_for(lambda: self.driver.current_url.endswith("/2026-07/"))

    def test_a_page_drawn_back_after_a_refused_save_holds_nothing_saved(self):
        """Its figures are as typed, none of them stored: leaving asks."""
        self.type_hours(3, "abc")
        self.click(".timesheet-save button[type=submit]")
        self.wait_for(lambda: "Rien n'a été enregistré." in self.script("return document.body.textContent"))
        self.assertFalse(Timesheet.objects.exists())
        self.assertEqual(self.figures()["unsaved"], [True, True])
        self.assertTrue(self.unload_is_held())
