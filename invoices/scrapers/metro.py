"""Selenium scraper for Metro France invoices, ported from ScrapBarInvoices.
Logs into docs.metro.fr, filters invoices to a date range, and downloads
every PDF not already imported into ``download_dir``.

Metro blocks accounts that hammer its site, so this stays gentle: one login
per run, one date window at a time, one download at a time, never more than
one click every CLICK_INTERVAL_SECONDS.

**Metro's firewall is recognised, and never tried again** (MetroBlocked). It
refused the sign-in on 02/09 and 18/09 - at the moment the credentials were
sent - with « Vous avez été bloqué par notre pare-feu … identifiant :#18.… ».
The scraper used to wait 15 s for the date filters and blame a cookie popup
or a new layout, which read like a glitch to retry; every sign-in against a
block is one more refused sign-in. The refusal is looked for wherever it can
show (the page loaded, the credentials sent, a search coming back empty, a
download that never came, any page that did not come), and Metro is left
alone for a while after it - and between two sign-ins (metro_pause, kept on
the supplier and checked before any browser starts, whoever calls).
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta

from django.conf import settings
from django.utils import timezone
from selenium import webdriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

PAGE_WAIT_SECONDS = 15  # for one-off waits: login, filters, page structure appearing
DOWNLOAD_TIMEOUT_SECONDS = 65  # per invoice, from its click - older invoices can be
# noticeably slower for Metro to generate, so this is generous. It is only
# ever spent on a download that really is late: see _PendingDownloads.
CLICK_INTERVAL_SECONDS = 2.0  # never faster than a person clicking down the list
MAX_IN_FLIGHT = 1  # downloads clicked but not on disk yet; the next click waits for room.
# Three overlapped once, to spare the waits a counting bug made long (a file
# landed in a second, and was waited on for 65); with that bug gone, one at
# a time costs a second per invoice and is what a person does.
POLL_SECONDS = 0.5
WINDOW_DAYS = 90  # a wide date range is processed in chunks this big rather than
# in one go: Metro's own results page caps at 100 rows with no pagination we
# drive, so a single request spanning years would silently truncate to the
# newest 100 and never surface the rest. Smaller windows also keep the
# rendered results list light, which the one real crash we saw (a stuck
# session after several identical-looking timeouts on a 100-row/3-year
# window) points at as a contributing factor.
MAX_CONSECUTIVE_TIMEOUTS = 3  # if this many downloads in a row never arrive -
# counted across the whole run, not per window - something is systematically
# wrong (not one slow invoice): the run stops, rather than searching the next
# window and clicking on. One late download alone says nothing (the past
# "timeouts" were a counting bug), so it only gets the refusal checked.
MAX_SESSION_RESTARTS = 1  # a browser that really died (the session gone -
# seen in practice) gets one fresh browser and sign-in, after
# RESTART_PAUSE_SECONDS, to go on from the window it was on. A page that is
# not what was expected is not a dead browser: each such hiccup used to open
# a new browser and sign in again, three sign-ins in seconds.
RESTART_PAUSE_SECONDS = 120
DATE_FROM_SELECTOR = "input[data-testid='DateInputFieldInputDe']"
# Metro's firewall judges each sign-in: it refused one after two quiet days,
# and 31/08 had seen some twenty-five (development testing, mostly). So
# AdminMate signs in rarely, and leaves Metro alone after a refusal - how
# long a block lasts is not known (the 02/09 one was over by 16/09).
BLOCK_PAUSE = timedelta(days=7)
REPEAT_BLOCK_WITHIN = timedelta(days=30)  # refused again this soon: twice as long
MIN_GAP_BETWEEN_LOGINS = timedelta(hours=24)  # Metro bills a few times a month


class MetroError(RuntimeError):
    """Metro's part of a gather stopped - in words for the person, with the
    invoices already downloaded (`files`), which are imported all the same:
    left on disk, they were fetched from Metro again at the next run."""

    def __init__(self, message: str, files=()):
        super().__init__(message)
        self.files = list(files)


class MetroBlocked(MetroError):
    """Metro's firewall refused this browser. Never retried; `reference` is
    what Metro's support asks for."""

    def __init__(self, message: str, reference: str = "", files=()):
        super().__init__(message, files)
        self.reference = reference


class MetroLoginFailed(MetroError):
    """Metro kept the sign-in page up without its firewall's words: the
    identifier or the password. Not retried, and no long pause."""


class _MetroCancelled(Exception):
    """A person cancelled before the credentials went: no sign-in."""


class MetroPaused(MetroError):
    """Metro is not contacted - no browser started: refused lately
    (`after_block`), or signed in to less than MIN_GAP_BETWEEN_LOGINS ago."""

    def __init__(self, message: str, until, after_block: bool):
        super().__init__(message)
        self.until = until
        self.after_block = after_block


def _metro_supplier():
    from invoices.models import Supplier  # local import: models are not a scraper's concern otherwise

    return Supplier.objects.filter(code="METRO").first()


def _said(moment) -> str:
    return f"{timezone.localtime(moment):%d/%m à %H:%M}"


def metro_pause(now=None) -> MetroPaused | None:
    """Why Metro is not to be contacted now, and until when - None when it
    may be. Read from the supplier, by every caller of the scraper: a gather,
    a shell, a script."""
    now = now or timezone.now()
    supplier = _metro_supplier()
    if supplier is None:
        return None
    if supplier.scrape_paused_until and supplier.scrape_paused_until > now:
        return MetroPaused(
            f"{supplier.scrape_pause_reason} Metro n'est pas contacté avant le {_said(supplier.scrape_paused_until)}.",
            supplier.scrape_paused_until,
            after_block=True,
        )
    last = supplier.scrape_last_login_at
    if last and now - last < MIN_GAP_BETWEEN_LOGINS:
        until = last + MIN_GAP_BETWEEN_LOGINS
        return MetroPaused(
            f"Metro a déjà été consulté le {_said(last)} : une connexion par jour au plus (son pare-feu bloque "
            f"les comptes trop sollicités). Prochaine connexion possible le {_said(until)}.",
            until,
            after_block=False,
        )
    return None


def record_login(now=None) -> None:
    """Noted before the password is sent: a run that dies after it counts."""
    supplier = _metro_supplier()
    if supplier is not None:
        supplier.scrape_last_login_at = now or timezone.now()
        supplier.save(update_fields=["scrape_last_login_at"])


def record_block(reference: str, now=None):
    """Metro refused: left alone for BLOCK_PAUSE - twice that when it had
    refused within REPEAT_BLOCK_WITHIN. Returns until when."""
    now = now or timezone.now()
    supplier = _metro_supplier()
    if supplier is None:
        return None
    repeat = supplier.scrape_last_block_at is not None and now - supplier.scrape_last_block_at < REPEAT_BLOCK_WITHIN
    supplier.scrape_last_block_at = now
    supplier.scrape_paused_until = now + BLOCK_PAUSE * (2 if repeat else 1)
    supplier.scrape_pause_reason = (
        f"Metro a bloqué la connexion le {_said(now)} (pare-feu" + (f", référence {reference}" if reference else "") + ")."
    )
    supplier.save(update_fields=["scrape_last_block_at", "scrape_paused_until", "scrape_pause_reason"])
    return supplier.scrape_paused_until


# What a firewall, or a site counting requests, answers instead of the page.
BLOCKED_RE = re.compile(
    r"bloqu[ée]e?s?\s+par\s+(?:notre|le)\s+pare-feu|too many requests|trop de (?:requ[êe]tes|demandes)"
    r"|access denied|acc[eè]s refus[ée]|request (?:was )?(?:rejected|blocked)|requested url was rejected"
    r"|you have been blocked",
    re.I,
)
REFERENCE_RE = re.compile(r"#\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.I)


def blocked_reference(text: str) -> str | None:
    """The reference of the refusal a page shows ("#18.608655f.…"), "" for a
    refusal without one - None when the page refuses nothing."""
    if not BLOCKED_RE.search(text or ""):
        return None
    found = REFERENCE_RE.search(text)
    return found.group(0) if found else ""


def blocked_message(reference: str) -> str:
    return (
        "Metro a bloqué la connexion (pare-feu"
        + (f", référence {reference}" if reference else "")
        + "). Chaque nouvelle tentative prolonge le blocage : Metro est laissé de côté quelque temps. "
        "S'il persiste, contactez l'assistance Metro "
        + ("en lui donnant cette référence." if reference else "en indiquant l'heure du blocage.")
    )


def _page_text(driver) -> str:
    """What the page shows (visible text only: a hidden alert template in
    the HTML is no refusal)."""
    try:
        return driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:  # noqa: BLE001 - a page between two loads, or no page
        return ""


def _raise_if_blocked(driver) -> None:
    reference = blocked_reference(_page_text(driver))
    if reference is not None:
        raise MetroBlocked(blocked_message(reference), reference=reference)


def _capture_diagnostics(driver, download_dir: str, log, context: str, screenshot_suffix: str = ""):
    """Called when a wait times out. Headless mode means nobody can just look
    at the browser, so instead we grab whatever we can (URL, title, visible
    text, a screenshot) and put it in the job log to make the failure
    diagnosable without needing to reproduce it with a visible browser. The
    screenshot is deleted right after logging it - useful for one failed run,
    not worth leaving behind permanently once the log has already captured
    the same information as text.
    """
    current_url = title = "?"
    try:
        current_url = driver.current_url
        title = driver.title
    except Exception:
        pass

    body_text = ""
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.strip()[:800]
    except Exception:
        pass

    log(f"Timed out {context}.")
    log(f"  Current URL: {current_url}")
    log(f"  Page title: {title}")
    if body_text:
        log(f"  Visible page text (first 800 chars):\n{body_text}")

    screenshot_path = os.path.join(os.path.abspath(download_dir), f"metro_debug_screenshot{screenshot_suffix}.png")
    try:
        driver.save_screenshot(screenshot_path)
        with open(screenshot_path, "rb") as f:
            # Removed right after (the text above stands for it): the path
            # was said as if it could still be opened.
            log(f"  (screenshot captured, {len(f.read())} bytes, not kept)")
    except Exception:
        screenshot_path = None

    return current_url, title, body_text, screenshot_path


def _fail_with_diagnostics(driver, download_dir: str, log, context: str):
    """Like _capture_diagnostics, but for waits where there's nothing sensible
    left to do afterwards (e.g. login itself never succeeded) - logs the same
    detail and then aborts the whole scrape.
    """
    current_url, title, _body_text, screenshot_path = _capture_diagnostics(driver, download_dir, log, context)
    if screenshot_path and os.path.exists(screenshot_path):
        os.remove(screenshot_path)
    # The firewall's page is the first thing a page that never came can be.
    _raise_if_blocked(driver)
    raise MetroError(
        f"Metro : la page attendue n'est pas venue ({context}) - page « {title} » sur "
        f"{str(current_url).split('?')[0]}. Le texte de la page est dans le journal."
    )


def _log_page_state(driver, download_dir: str, log, context: str) -> None:
    _url, _title, _body, screenshot_path = _capture_diagnostics(driver, download_dir, log, context)
    if screenshot_path and os.path.exists(screenshot_path):
        os.remove(screenshot_path)


CHECKBOX_ID_REGEX = re.compile(r"^FRA_(\d+)_(\d+)_(\d+)_\d+$")


def _key_of(checkbox_id: str) -> tuple[int, int, int] | None:
    """(store, till, number) of a result row, from its checkbox id
    ("FRA_134_52_45126_<timestamp>"). The same triple names the row's PDF
    ("134_52_45126_<timestamp>_invoice_cus_copy_main.pdf") and, zero-padded,
    the invoice number the PDF parser derives - so a row can be recognised as
    already imported, and its file as landed, without opening anything.
    """
    match = CHECKBOX_ID_REGEX.match(checkbox_id or "")
    if not match:
        return None
    store, till, number = (int(part) for part in match.groups())
    return store, till, number


# Every result row's download button with its checkbox id, read at once: one
# browser round-trip, and no row read half-way through a re-render (read one
# call at a time, a row caught mid-render read as none, and was skipped).
ROWS_JS = """
return Array.from(document.querySelectorAll('tr #downloadPdfButton')).map(function (button) {
  var row = button.closest('tr');
  var box = row ? row.querySelector("input[type='checkbox']") : null;
  return [button, box ? box.id : ''];
});
"""


def _rows(driver) -> list:
    """(button, key) for each result row - key None when unreadable."""
    return [(button, _key_of(box_id)) for button, box_id in (driver.execute_script(ROWS_JS) or [])]


def _invoice_number(key) -> str:
    store, till, number = key
    return f"{store}-{till:03d}-{number:06d}"


def _is_known(key, known_numbers) -> bool:
    """A deposit credit note ("Consignes") doesn't print its store, so the
    parser stores it as "052-014645" - matched on till and number too.
    Compared with the full "134-052-014645" only, every credit note used to
    be downloaded again on every run, then thrown away as a duplicate.
    """
    _store, till, number = key
    return _invoice_number(key) in known_numbers or f"{till:03d}-{number:06d}" in known_numbers


def _file_prefix(key) -> str:
    """How the PDF a row's download produces starts ("" matches any PDF)."""
    if key is None:
        return ""
    store, till, number = key
    return f"{store}_{till}_{number}_"


def _date_windows(start_date: date, end_date: date):
    window_start = start_date
    while window_start <= end_date:
        window_end = min(window_start + timedelta(days=WINDOW_DAYS), end_date)
        yield window_start, window_end
        window_start = window_end + timedelta(days=1)


def _login(driver, wait, download_dir, log, should_cancel=lambda: False):
    driver.get("https://docs.metro.fr/")
    # Refused before anything is typed: the credentials are not sent into it.
    _raise_if_blocked(driver)
    try:
        cookie_banner = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "cms-cookie-disclaimer")))
        shadow_root = driver.execute_script("return arguments[0].shadowRoot", cookie_banner)
        shadow_root.find_element(By.CSS_SELECTOR, "button.accept-btn.btn-primary").click()
    except TimeoutException:
        log("No cookie banner appeared (or it didn't match the expected selector) - continuing.")

    try:
        wait.until(EC.presence_of_element_located((By.ID, "user_id")))
    except TimeoutException:
        _fail_with_diagnostics(driver, download_dir, log, "waiting for the login form to appear")
    _raise_if_blocked(driver)
    driver.find_element(By.ID, "user_id").send_keys(settings.METRO_EMAIL)
    driver.find_element(By.ID, "password").send_keys(settings.METRO_PASSWORD)
    if should_cancel():
        raise _MetroCancelled()
    record_login()
    driver.find_element(By.ID, "submit").click()
    _await_sign_in(driver, download_dir, log)


def _await_sign_in(driver, download_dir, log, sleep=time.sleep, clock=time.monotonic):
    """After the credentials: the invoices' page, or Metro's refusal - said
    at once, never waited out as a missing date filter."""
    deadline = clock() + PAGE_WAIT_SECONDS
    while clock() < deadline:
        try:
            if driver.find_elements(By.CSS_SELECTOR, DATE_FROM_SELECTOR):
                return
        except WebDriverException:
            pass  # between two pages
        _raise_if_blocked(driver)
        sleep(POLL_SECONDS)
    try:
        still_asked = "idam.metro.fr" in (driver.current_url or "") and bool(driver.find_elements(By.ID, "password"))
    except WebDriverException:
        still_asked = False
    if still_asked:
        _log_page_state(driver, download_dir, log, "waiting for the sign-in to finish")
        raise MetroLoginFailed(
            "Metro a gardé la page de connexion : identifiant ou mot de passe refusé (METRO_EMAIL / METRO_PASSWORD "
            "dans le fichier .env). Le message de Metro est dans le journal."
        )
    _fail_with_diagnostics(driver, download_dir, log, "waiting for the sign-in to finish")


def _js_click(driver, element):
    """A native .click() can fail with "element click intercepted" when some
    other element visually overlaps the target (seen in practice: applying a
    second date-range window's filter got blocked by a header/banner
    container that native click's visibility check refused to click through,
    even though the date field itself was the right element). A JS-dispatched
    click bypasses that geometry check entirely - it doesn't care what's
    drawn on top, it just fires the click handler on the exact element.
    """
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'}); arguments[0].click();", element)


def _read_date_field(field) -> date | None:
    try:
        return datetime.strptime(field.get_attribute("value"), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def _set_date_field(driver, field, value: date):
    """Types a date into one of the filter fields. Clears via End+repeated
    Backspace rather than Ctrl+A/Delete - live testing showed this field is
    a masked/controlled (react-datepicker) input that doesn't reliably honor
    a synthetic select-all, but does accept the same keystrokes a real user
    deleting digit-by-digit would produce. Ends on Tab (not Enter) to just
    commit the field's own value without guessing what Enter might trigger.
    """
    _js_click(driver, field)
    field.send_keys(Keys.END)
    for _ in range(15):
        field.send_keys(Keys.BACKSPACE)
    field.send_keys(value.strftime("%d.%m.%Y"))
    field.send_keys(Keys.TAB)


def _apply_date_filter(driver, wait, download_dir, log, start_date: date, end_date: date):
    try:
        date_from = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, DATE_FROM_SELECTOR)))
        date_to = driver.find_element(By.CSS_SELECTOR, "input[data-testid='DateInputFieldInputÀ']")
    except TimeoutException:
        _fail_with_diagnostics(driver, download_dir, log, "waiting for the invoice date filters")

    # Confirmed live (see the investigation that found this bug): the widget
    # silently reverts any edit that would leave it in a transient from > to
    # state - e.g. typing a later "from" while the still-unchanged "to" is
    # earlier than it. Since a scrape only ever moves the window forward or
    # (on the very first call of a session) starts from whatever Metro's own
    # default range happens to be, which field is safe to write first
    # depends on the *current* value of the other one, not a fixed order.
    # This is exactly what silently produced the multi-year gap the fix
    # addresses: every window after the first kept re-showing the first
    # window's results because the "from" field never actually moved.
    for attempt in range(2):
        current_from = _read_date_field(date_from)
        current_to = _read_date_field(date_to)
        to_first = current_to is None or current_from is None or start_date > current_to

        if to_first:
            _set_date_field(driver, date_to, end_date)
            _set_date_field(driver, date_from, start_date)
        else:
            _set_date_field(driver, date_from, start_date)
            _set_date_field(driver, date_to, end_date)

        if _read_date_field(date_from) == start_date and _read_date_field(date_to) == end_date:
            break
        log(f"Le filtre de dates n'a pas pris à l'essai {attempt + 1}, nouvelle tentative dans l'autre ordre.")
    else:
        log(
            f"⚠ Impossible de fixer le filtre de dates à {start_date}-{end_date} après 2 essais "
            f"(champs actuels : {_read_date_field(date_from)}-{_read_date_field(date_to)}) - "
            "les résultats ci-dessous risquent d'être incorrects."
        )

    try:
        dropdown = wait.until(EC.presence_of_element_located((By.ID, "invoiceLimit")))
    except TimeoutException:
        _fail_with_diagnostics(driver, download_dir, log, "waiting for the invoice list page limit dropdown")
    Select(dropdown).select_by_value("100")

    # Typing into the fields - even when the value visibly sticks - doesn't
    # reliably re-trigger the search past the very first filter application
    # of a session (confirmed live: the results silently kept showing the
    # previous window's rows even once both fields held the right dates).
    # Only an explicit click on the real search button does.
    search_btn = driver.find_element(By.CSS_SELECTOR, "#search-btn")
    _js_click(driver, search_btn)


def _wait_for_stable_results(get_buttons, max_wait: float = 20, poll: float = 1.5) -> int:
    """The results table re-renders asynchronously after a filter change - a
    blind fixed sleep here used to guess how long that takes, but real runs
    showed 5s isn't always enough: the very first download right after a new
    window's filter was applied would occasionally time out as if the click
    never truly registered, while later downloads in the same window were
    fine. Watching the actual row count until it stops changing between two
    checks in a row is more honest about "is it actually done" than any
    fixed guess. `get_buttons` returns the current list of button elements -
    it's their count that's tracked here, not the list itself.
    """
    start = time.time()
    last_count = -1
    stable_checks = 0
    while time.time() - start < max_wait:
        count = len(get_buttons())
        if count == last_count:
            stable_checks += 1
            if stable_checks >= 2:
                return count
        else:
            stable_checks = 0
        last_count = count
        time.sleep(poll)
    return last_count


def _visible_download_buttons(driver):
    # The page renders a desktop copy of each row (inside a <tr>) and a
    # hidden mobile copy (inside a <div data-testid="mobileRowTest">), both
    # sharing the same id. Scoping to <tr> excludes the mobile duplicates
    # structurally, in a single query - calling .is_displayed() on every
    # match instead would mean one browser round-trip per element, on every
    # single invoice download, which gets extremely slow once there are more
    # than a handful.
    return driver.find_elements(By.CSS_SELECTOR, "tr #downloadPdfButton")


class _PendingDownloads:
    """Downloads clicked but not on disk yet, each recognised by the file its
    row produces - never by counting what is in the folder.

    A click sometimes also starts a stray non-PDF download ("downloads.htm")
    that appears and vanishes. Counted along with the PDFs, it hid the one
    that had just landed, and the scraper sat out a full timeout on a
    download long finished: ten times in one gather, eleven of its twelve
    minutes. A PDF only counts if it wasn't already there, with that same
    modification time, when its row was clicked.
    """

    def __init__(self, download_dir: str, clock):
        self.download_dir = download_dir
        self.clock = clock
        self.pending: dict[str, tuple[str, float, set, tuple | None]] = {}
        self.arrived: list = []  # keys of the rows whose file landed, not yet taken

    def _pdfs(self) -> set[tuple[str, int]]:
        found = set()
        with os.scandir(self.download_dir) as entries:
            for entry in entries:
                if not entry.name.lower().endswith(".pdf"):
                    continue
                try:
                    found.add((entry.name, entry.stat().st_mtime_ns))
                except OSError:
                    continue
        return found

    def start(self, label: str, prefix: str, key=None) -> None:
        self.pending[label] = (prefix, self.clock(), self._pdfs(), key)

    def landed(self) -> list[str]:
        on_disk = self._pdfs()
        done = [
            label
            for label, (prefix, _clicked, before, _key) in self.pending.items()
            if any(name.startswith(prefix) and (name, mtime) not in before for name, mtime in on_disk)
        ]
        for label in done:
            key = self.pending.pop(label)[3]
            if key is not None:
                self.arrived.append(key)
        return done

    def take_arrived(self) -> list:
        arrived, self.arrived = self.arrived, []
        return arrived

    def expired(self) -> list[str]:
        now = self.clock()
        late = [
            label
            for label, (_prefix, clicked, _before, _key) in self.pending.items()
            if now - clicked > DOWNLOAD_TIMEOUT_SECONDS
        ]
        for label in late:
            del self.pending[label]
        return late


def _settle(driver, downloads: _PendingDownloads, download_dir, log, failures: int, sleep, until_empty: bool) -> int:
    """Wait until a download slot is free - or, with `until_empty`, until
    nothing is outstanding - noting what landed and what never will.
    Returns how many downloads have failed in a row."""
    while True:
        if downloads.landed():
            failures = 0
        for label in downloads.expired():
            failures += 1
            log(f"Invoice {label} did not finish downloading within {DOWNLOAD_TIMEOUT_SECONDS}s - skipped.")
            _raise_if_blocked(driver)
            if failures == 1:
                _log_page_state(driver, download_dir, log, f"waiting for invoice {label} to download")
        if not downloads.pending:
            return failures
        if not until_empty and (len(downloads.pending) < MAX_IN_FLIGHT or failures >= MAX_CONSECUTIVE_TIMEOUTS):
            return failures
        sleep(POLL_SECONDS)


class _Run:
    """What one Metro run keeps from one date window to the next."""

    def __init__(self):
        self.failures = 0  # downloads in a row that never arrived
        self.unreadable: list[str] = []  # windows holding rows whose number could not be read


SHORT_READS_TOLERATED = 2  # a list read shorter than already seen is read again this often
MISSES_TOLERATED = 2  # a row not found again for its click is tried again this often


def _download_window(
    driver,
    download_dir: str,
    total: int,
    known_numbers,
    log,
    on_step,
    sleep=time.sleep,
    clock=time.monotonic,
    run: _Run | None = None,
    should_cancel=lambda: False,
    window: str = "cette période",
) -> int:
    """Download every row of the current results page not already imported:
    one click every CLICK_INTERVAL_SECONDS at most, never more than
    MAX_IN_FLIGHT downloads outstanding, and whatever is still on its way
    when the list is done is waited for before the next date window. Returns
    how many downloads were started. MAX_CONSECUTIVE_TIMEOUTS downloads in a
    row that never came - across windows - stop the whole run (MetroError).

    Rows are followed by their number, never their place: the list can
    re-render under a click (a row moving up, one arriving), and a place
    walked over then was a row skipped, or one clicked twice. A row counts
    as fetched once its file has landed: marked at the click, the download a
    dying browser cut off was skipped after the restart as "already
    imported", and never imported - and a file that landed just before the
    browser died is counted all the same, not fetched again.

    No window ends in silence: a list read shorter than already seen is read
    again, a row missing when its click comes is tried again, fewer rows read
    than announced is an error, and so is a list that never stops changing.
    Rows whose number cannot be read are left to the end of the run
    (`run.unreadable`), which names their windows.
    """
    run = run or _Run()
    downloads = _PendingDownloads(download_dir, clock)
    failures = run.failures
    started = 0
    decided: set = set()  # rows of this window downloaded, skipped, or given up
    seen: set = set()  # every row number read in this window
    misses: dict = {}
    lost: list[str] = []
    unreadable = 0
    short_reads = 0
    steps = 0
    cancelled = False

    def note_arrivals():
        for key in downloads.take_arrived():
            known_numbers.add(_invoice_number(key))

    try:
        while True:
            if should_cancel():
                log("Annulé : plus aucun téléchargement Metro.")
                cancelled = True
                break
            rows = _rows(driver)
            read = {key for _button, key in rows if key is not None}
            seen |= read
            unreadable = max(unreadable, len(rows) - len(read))
            waiting = [(place, key) for place, (_button, key) in enumerate(rows) if key is not None and key not in decided]
            if not waiting:
                if len(read) < len(seen) and short_reads < SHORT_READS_TOLERATED:
                    # Caught between two drawings of the list: read again.
                    short_reads += 1
                    sleep(POLL_SECONDS)
                    continue
                break
            short_reads = 0
            steps += 1
            if steps > 2 * max(total, len(seen)) + 5:
                raise MetroError(
                    f"Metro : la liste de {window} n'a cessé de changer - récupération Metro arrêtée, "
                    f"{len(waiting)} facture(s) non traitée(s)."
                )
            place, key = waiting[0]
            decided.add(key)
            label = f"{place + 1}/{len(rows)} ({_invoice_number(key)})"
            if _is_known(key, known_numbers):
                log(f"Invoice {label} already imported - skipping download.")
                on_step()
                continue

            failures = _settle(driver, downloads, download_dir, log, failures, sleep, until_empty=False)
            note_arrivals()
            if failures >= MAX_CONSECUTIVE_TIMEOUTS:
                raise MetroError(
                    f"Metro : {MAX_CONSECUTIVE_TIMEOUTS} téléchargements de suite ne sont jamais arrivés - récupération "
                    "Metro arrêtée plutôt que de continuer à cliquer. Le reste viendra à une prochaine récupération."
                )
            if should_cancel():
                log("Annulé : plus aucun téléchargement Metro.")
                cancelled = True
                break
            # Found again by its number: the handle can have gone stale in the wait.
            button = next((found for found, found_key in _rows(driver) if found_key == key), None)
            if button is None:
                misses[key] = misses.get(key, 0) + 1
                if misses[key] <= MISSES_TOLERATED:
                    decided.discard(key)  # read again at the next round
                    log(f"Invoice {label} is not in the list just now - tried again.")
                else:
                    lost.append(_invoice_number(key))
                    log(f"Invoice {label} kept leaving the list - not clicked.")
                    on_step()
                continue
            _js_click(driver, button)
            downloads.start(label, _file_prefix(key), key)
            started += 1
            log(f"Downloading Metro invoice {label}")
            on_step()
            sleep(CLICK_INTERVAL_SECONDS)

        run.failures = _settle(driver, downloads, download_dir, log, failures, sleep, until_empty=True)
    finally:
        # Only the folder is looked at: a file that landed before a browser
        # died counts, and is not fetched from Metro again after the restart.
        try:
            downloads.landed()
        except OSError:
            pass
        note_arrivals()
    if run.failures >= MAX_CONSECUTIVE_TIMEOUTS:
        raise MetroError(
            f"Metro : {MAX_CONSECUTIVE_TIMEOUTS} téléchargements de suite ne sont jamais arrivés - récupération "
            "Metro arrêtée. Le reste viendra à une prochaine récupération."
        )
    if cancelled:
        return started
    if lost:
        raise MetroError(
            f"Metro : {len(lost)} facture(s) de {window} n'ont pas pu être cliquées ({', '.join(lost)}) - "
            "récupération Metro incomplète."
        )
    if len(seen) + unreadable < total:
        raise MetroError(
            f"Metro : {total} factures annoncées pour {window}, {len(seen) + unreadable} lues - récupération "
            "Metro incomplète."
        )
    if unreadable:
        run.unreadable.append(window)  # said once every window has been searched
    return started


def _session_died(exc: WebDriverException) -> bool:
    """A browser that is gone - not a page that is not what was expected."""
    if isinstance(exc, InvalidSessionIdException):
        return True
    text = str(exc).lower()
    return any(
        sign in text
        for sign in ("chrome not reachable", "session deleted", "disconnected: not connected", "no such session")
    )


def _build_driver(download_dir: str):
    options = webdriver.ChromeOptions()
    if settings.SCRAPER_HEADLESS:
        options.add_argument("--headless=new")
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": os.path.abspath(download_dir),
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
        },
    )
    service = Service(ChromeDriverManager().install())
    new_driver = webdriver.Chrome(service=service, options=options)
    # Headless Chrome blocks file downloads by default for security reasons
    # since Chrome ~96 - without this, every PDF download silently no-ops
    # and the "wait for the file to appear" loop below just times out.
    new_driver.execute_cdp_cmd(
        "Page.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": os.path.abspath(download_dir)},
    )
    return new_driver


def scrape_metro_invoices(
    download_dir: str,
    start_date: date,
    end_date: date,
    log=print,
    on_progress=None,
    should_cancel=lambda: False,
    ignore_pause: bool = False,
) -> list[str]:
    """The PDFs downloaded for [start_date, end_date], not already imported.
    Raises MetroError (MetroBlocked for the firewall) carrying the files that
    landed before the stop - and MetroPaused, before any browser starts,
    while Metro is to be left alone (metro_pause), unless a person asked for
    one sign-in all the same (`ignore_pause`)."""
    if not settings.METRO_EMAIL or not settings.METRO_PASSWORD:
        raise MetroError("METRO_EMAIL / METRO_PASSWORD manquent dans le fichier .env.")
    paused = None if ignore_pause else metro_pause()
    if paused is not None:
        raise paused

    from invoices.models import Invoice  # local import: scrapers avoid a hard dependency on models otherwise

    known_numbers = set(
        Invoice.objects.filter(supplier__code="METRO").exclude(invoice_number="").values_list(
            "invoice_number", flat=True
        )
    )

    os.makedirs(download_dir, exist_ok=True)
    # A multi-MB "downloads.htm" sometimes appears beside the PDFs: Chrome's
    # own component download (it starts "Cr24", a CRX package - seen with no
    # Metro run at all), not Metro's. It never finishes and is no PDF: junk.
    # Debug screenshots are deleted right after being logged now, but this
    # also mops up any left over from before that change.
    for stale in os.listdir(download_dir):
        if stale.endswith(".crdownload") or stale.startswith("metro_debug_screenshot") or not stale.lower().endswith(".pdf"):
            try:
                os.remove(os.path.join(download_dir, stale))
            except OSError:
                pass
    existing = set(os.listdir(download_dir))

    def landed() -> list[str]:
        new_files = sorted(set(os.listdir(download_dir)) - existing)
        return [os.path.join(download_dir, f) for f in new_files if f.lower().endswith(".pdf")]

    windows = list(_date_windows(start_date, end_date))
    multi_window = len(windows) > 1
    overall_index = 0
    window_idx = 0
    session_restarts = 0
    driver = None
    run = _Run()
    cancelled = False

    def on_step():
        nonlocal overall_index
        overall_index += 1
        if on_progress:
            on_progress(overall_index, None)

    try:
        while window_idx < len(windows) and not cancelled:
            if should_cancel():
                log("Annulé : Metro n'est pas contacté.")
                break
            try:
                driver = _build_driver(download_dir)
                wait = WebDriverWait(driver, PAGE_WAIT_SECONDS)
                _login(driver, wait, download_dir, log, should_cancel)

                while window_idx < len(windows):
                    if should_cancel():
                        log("Annulé : les fenêtres Metro restantes ne sont pas cherchées.")
                        cancelled = True
                        break
                    window_start, window_end = windows[window_idx]
                    if multi_window:
                        log(f"--- Fenêtre {window_start} → {window_end} ---")
                    _apply_date_filter(driver, wait, download_dir, log, window_start, window_end)

                    try:
                        wait.until(lambda d: len(_visible_download_buttons(d)) > 0)
                    except TimeoutException:
                        # A search refused looks like a search that found nothing.
                        _raise_if_blocked(driver)
                        log(f"Aucune facture entre {window_start} et {window_end}.")
                        window_idx += 1
                        continue

                    # Rows appearing isn't the same as the page being done re-rendering
                    # them (event handlers, etc.) - wait for the count to stop
                    # changing before trusting it or starting to click.
                    total = _wait_for_stable_results(lambda: _visible_download_buttons(driver))
                    if total >= 100:
                        log(
                            f"⚠ 100 factures ou plus trouvées entre {window_start} et {window_end} - "
                            "certaines pourraient être ignorées (limite d'affichage de Metro). "
                            "Relancez avec une période plus courte si besoin."
                        )
                    log(f"Found {total} Metro invoice(s) between {window_start} and {window_end}")
                    _download_window(
                        driver,
                        download_dir,
                        total,
                        known_numbers,
                        log,
                        on_step,
                        run=run,
                        should_cancel=should_cancel,
                        window=f"{window_start} → {window_end}",
                    )
                    window_idx += 1
            except _MetroCancelled:
                log("Annulé avant l'envoi de l'identifiant à Metro.")
                cancelled = True
            except MetroError:
                raise
            except NoSuchWindowException as exc:
                # A person closed the window: that is a stop, not a crash.
                raise MetroError("La fenêtre du navigateur a été fermée : récupération Metro arrêtée.") from exc
            except WebDriverException as exc:
                log(f"Le navigateur a rencontré une erreur : {exc.__class__.__name__} - {str(exc).strip()[:200]}")
                if not _session_died(exc):
                    # The page, not the browser: said, never answered with a
                    # new browser and a new sign-in.
                    _raise_if_blocked(driver)
                    _log_page_state(driver, download_dir, log, "after a browser error")
                    raise MetroError(
                        f"Metro : la page n'a pas répondu comme prévu ({exc.__class__.__name__}) - récupération "
                        "Metro arrêtée. Le détail est dans le journal."
                    ) from exc
                # A browser that died (seen in practice) gets one fresh
                # browser, after a pause, to go on from the window it was on
                # - quietly giving up lost the rest of a long range.
                session_restarts += 1
                remaining = ", ".join(f"{w[0]}→{w[1]}" for w in windows[window_idx:])
                if session_restarts > MAX_SESSION_RESTARTS:
                    raise MetroError(
                        f"Le navigateur s'est arrêté {session_restarts} fois : récupération Metro arrêtée. "
                        f"Période(s) non cherchée(s) : {remaining}."
                    ) from exc
                log(
                    f"Le navigateur s'est arrêté : nouvelle session dans {RESTART_PAUSE_SECONDS} s pour les "
                    f"fenêtres restantes ({remaining})."
                )
                time.sleep(RESTART_PAUSE_SECONDS)
                cancelled = should_cancel()
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None
        if run.unreadable and not cancelled:
            raise MetroError(
                "Metro : des factures sans numéro lisible dans la liste n'ont pas été téléchargées ("
                + ", ".join(run.unreadable)
                + ") - récupération Metro incomplète."
            )
    except MetroError as exc:
        exc.files = exc.files or landed()
        if isinstance(exc, MetroBlocked):
            try:
                until = record_block(exc.reference)
            except Exception as problem:  # noqa: BLE001 - the refusal itself must reach the person
                log(f"La pause de Metro n'a pas pu être enregistrée : {problem}")
            else:
                if until is not None:
                    log(f"Metro ne sera plus contacté avant le {_said(until)}.")
        raise
    except Exception as exc:  # noqa: BLE001 - said in words, with what landed
        raise MetroError(
            f"Metro : erreur inattendue ({exc.__class__.__name__} - {str(exc).strip()[:200]}).", files=landed()
        ) from exc
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    return landed()
