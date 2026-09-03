"""Selenium scraper for Metro France invoices, ported from ScrapBarInvoices.
Logs into docs.metro.fr, filters invoices to a date range, and downloads
every available PDF into ``download_dir``.
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta

from django.conf import settings
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

PAGE_WAIT_SECONDS = 15  # for one-off waits: login, filters, page structure appearing
DOWNLOAD_TIMEOUT_SECONDS = 45  # per invoice - independent of how many invoices there are in total
GRACE_PERIOD_SECONDS = 20  # older invoices can be noticeably slower to generate; a bit of extra patience
# before writing one off as failed avoids false "skipped" reports for downloads that were just about to land.
WINDOW_DAYS = 90  # a wide date range is processed in chunks this big rather than
# in one go: Metro's own results page caps at 100 rows with no pagination we
# drive, so a single request spanning years would silently truncate to the
# newest 100 and never surface the rest. Smaller windows also keep the
# rendered results list light, which the one real crash we saw (a stuck
# session after several identical-looking timeouts on a 100-row/3-year
# window) points at as a contributing factor.
MAX_CONSECUTIVE_TIMEOUTS = 3  # if this many downloads in a row time out with
# no visible change on the page, something is systematically wrong (not just
# one slow invoice) - stop wasting the rest of the per-invoice timeout budget
# on a pattern that isn't going to resolve itself.
MAX_SESSION_RESTARTS = 2  # a crashed browser session (seen in practice, not
# just theoretical - Chrome occasionally dies mid-run) used to silently
# abandon every window that hadn't been processed yet while still reporting
# the whole gather as successful. A fresh driver + re-login can pick up
# right where the dead one left off instead of quietly losing months of
# invoices; this caps how many times we'll do that before giving up.


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
            log(f"  (screenshot captured, {len(f.read())} bytes - inspect at {screenshot_path} before it's removed)")
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
    raise RuntimeError(
        f"Metro scraping timed out {context}. URL was {current_url!r}, title was {title!r} - "
        "see the log above for the page's visible text"
        ". This usually means the login didn't succeed, a cookie/consent popup is still blocking the "
        "page, or Metro changed their site layout."
    )


CHECKBOX_ID_REGEX = re.compile(r"^FRA_(\d+)_(\d+)_(\d+)_\d+$")


def _row_invoice_number(button) -> str | None:
    """The list page's row checkbox id looks like "FRA_134_52_45126_<timestamp>"
    - the same (store, ref1, ref2) triple our PDF-text-based invoice_number
    parsing derives, just not zero-padded. Reconstructing it here lets us
    recognise an already-imported invoice from the list page alone, without
    downloading and parsing its PDF first only to discover it's a duplicate -
    the difference matters a lot when a wide date range mostly overlaps
    invoices already gathered before.
    """
    try:
        row = button.find_element(By.XPATH, "./ancestor::tr[1]")
        checkbox = row.find_element(By.CSS_SELECTOR, "input[type='checkbox']")
        checkbox_id = checkbox.get_attribute("id") or ""
    except Exception:
        return None
    match = CHECKBOX_ID_REGEX.match(checkbox_id)
    if not match:
        return None
    store, ref1, ref2 = match.groups()
    return f"{store}-{int(ref1):03d}-{int(ref2):06d}"


def _date_windows(start_date: date, end_date: date):
    window_start = start_date
    while window_start <= end_date:
        window_end = min(window_start + timedelta(days=WINDOW_DAYS), end_date)
        yield window_start, window_end
        window_start = window_end + timedelta(days=1)


def _login(driver, wait, download_dir, log):
    driver.get("https://docs.metro.fr/")
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
    driver.find_element(By.ID, "user_id").send_keys(settings.METRO_EMAIL)
    driver.find_element(By.ID, "password").send_keys(settings.METRO_PASSWORD)
    driver.find_element(By.ID, "submit").click()


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
        date_from = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[data-testid='DateInputFieldInputDe']"))
        )
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


def scrape_metro_invoices(
    download_dir: str, start_date: date, end_date: date, log=print, on_progress=None
) -> list[str]:
    if not settings.METRO_EMAIL or not settings.METRO_PASSWORD:
        raise RuntimeError("METRO_EMAIL / METRO_PASSWORD are not configured in .env")

    from invoices.models import Invoice  # local import: scrapers avoid a hard dependency on models otherwise

    known_invoice_numbers = set(
        Invoice.objects.filter(supplier__code="METRO").exclude(invoice_number="").values_list(
            "invoice_number", flat=True
        )
    )

    os.makedirs(download_dir, exist_ok=True)
    # Clicking a download button occasionally also triggers an unrelated
    # multi-MB "downloads.htm" partial download (a stray side effect of the
    # page's own JS, not something we asked for) that never finishes and
    # would otherwise pile up across runs - it's not a PDF, so it's junk.
    # Debug screenshots are deleted right after being logged now, but this
    # also mops up any left over from before that change.
    for stale in os.listdir(download_dir):
        if stale.endswith(".crdownload") or stale.startswith("metro_debug_screenshot") or not stale.lower().endswith(".pdf"):
            try:
                os.remove(os.path.join(download_dir, stale))
            except OSError:
                pass
    existing = set(os.listdir(download_dir))

    def build_driver():
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

    windows = list(_date_windows(start_date, end_date))
    multi_window = len(windows) > 1
    overall_index = 0
    window_idx = 0
    session_restarts = 0
    driver = None

    def visible_download_buttons():
        # The page renders a desktop copy of each row (inside a <tr>) and
        # a hidden mobile copy (inside a <div data-testid="mobileRowTest">),
        # both sharing the same id. Scoping to <tr> excludes the mobile
        # duplicates structurally, in a single query - calling
        # .is_displayed() on every match instead would mean one browser
        # round-trip per element, on every single invoice download,
        # which gets extremely slow once there are more than a handful.
        return driver.find_elements(By.CSS_SELECTOR, "tr #downloadPdfButton")

    try:
        while window_idx < len(windows):
            try:
                driver = build_driver()
                wait = WebDriverWait(driver, PAGE_WAIT_SECONDS)
                _login(driver, wait, download_dir, log)

                while window_idx < len(windows):
                    window_start, window_end = windows[window_idx]
                    if multi_window:
                        log(f"--- Fenêtre {window_start} → {window_end} ---")
                    _apply_date_filter(driver, wait, download_dir, log, window_start, window_end)

                    try:
                        wait.until(lambda d: len(visible_download_buttons()) > 0)
                    except TimeoutException:
                        log(f"Aucune facture entre {window_start} et {window_end}.")
                        window_idx += 1
                        continue

                    # Rows appearing isn't the same as the page being done re-rendering
                    # them (event handlers, etc.) - wait for the count to stop
                    # changing before trusting it or starting to click.
                    total = _wait_for_stable_results(visible_download_buttons)
                    if total >= 100:
                        log(
                            f"⚠ 100 factures ou plus trouvées entre {window_start} et {window_end} - "
                            "certaines pourraient être ignorées (limite d'affichage de Metro). "
                            "Relancez avec une période plus courte si besoin."
                        )
                    log(f"Found {total} Metro invoice(s) between {window_start} and {window_end}")

                    consecutive_timeouts = 0
                    for idx in range(total):
                        # Re-fetch fresh each time instead of reusing element handles
                        # captured before any clicks: the page can re-render rows
                        # after a download, which silently invalidates old
                        # references and can make a stale click land on the wrong
                        # element entirely.
                        current_buttons = visible_download_buttons()
                        if idx >= len(current_buttons):
                            log(
                                f"Expected a download button at position {idx + 1}/{total} but the page only "
                                f"has {len(current_buttons)} now - stopping early for this window."
                            )
                            break

                        invoice_number = _row_invoice_number(current_buttons[idx])
                        if invoice_number and invoice_number in known_invoice_numbers:
                            overall_index += 1
                            log(f"Invoice {idx + 1}/{total} ({invoice_number}) already imported - skipping download.")
                            if on_progress:
                                on_progress(overall_index, None)
                            continue

                        _js_click(driver, current_buttons[idx])
                        overall_index += 1
                        log(f"Downloading Metro invoice {idx + 1}/{total}")
                        before = len(os.listdir(download_dir))
                        try:
                            # A generous, per-file timeout - independent of how long
                            # the whole run takes overall (a big date range with
                            # hundreds of invoices is expected to take a while; one
                            # slow or stuck file should never take the rest down
                            # with it).
                            WebDriverWait(driver, DOWNLOAD_TIMEOUT_SECONDS).until(
                                lambda d, before=before: len(os.listdir(download_dir)) > before
                            )
                            consecutive_timeouts = 0
                        except TimeoutException:
                            # Older invoices seem to take noticeably longer for Metro
                            # to generate/serve than recent ones - a download that's
                            # merely slow can land just after our wait gives up. A
                            # short grace check avoids reporting a false "skip" for
                            # something that actually succeeded a moment later.
                            try:
                                WebDriverWait(driver, GRACE_PERIOD_SECONDS).until(
                                    lambda d, before=before: len(os.listdir(download_dir)) > before
                                )
                                log(
                                    f"Invoice {idx + 1}/{total} finished just after the "
                                    f"{DOWNLOAD_TIMEOUT_SECONDS}s timeout - no action needed."
                                )
                                consecutive_timeouts = 0
                            except TimeoutException:
                                consecutive_timeouts += 1
                                _current_url, _title, _body, screenshot_path = _capture_diagnostics(
                                    driver,
                                    download_dir,
                                    log,
                                    f"waiting for invoice {idx + 1}/{total} to finish downloading",
                                    screenshot_suffix=f"_invoice_{idx + 1}",
                                )
                                if screenshot_path and os.path.exists(screenshot_path):
                                    os.remove(screenshot_path)
                                total_wait = DOWNLOAD_TIMEOUT_SECONDS + GRACE_PERIOD_SECONDS
                                log(
                                    f"Skipping invoice {idx + 1}/{total} after a {total_wait}s timeout "
                                    f"({consecutive_timeouts}/{MAX_CONSECUTIVE_TIMEOUTS} consecutive)."
                                )
                                if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                                    log(
                                        f"{MAX_CONSECUTIVE_TIMEOUTS} downloads in a row timed out with no visible "
                                        "change on the page - something is systematically stuck (not just one slow "
                                        "invoice). Stopping this window early instead of retrying the rest at the "
                                        "full timeout each."
                                    )
                                    break
                                continue
                        if on_progress:
                            on_progress(overall_index, None)
                    window_idx += 1
            except WebDriverException as exc:
                # A dead/crashed browser session (seen more than once in
                # practice) used to take the whole function down without
                # returning anything past that point - even the rest of a
                # wide, multi-year date range would simply never be
                # attempted, while the caller still reported the gather as a
                # plain success. A fresh session picking up at the next
                # unprocessed window (rather than giving up, or silently
                # restarting from the very first one) is what actually keeps
                # a long backfill from losing whatever window it happened to
                # be on when Chrome died.
                log(f"Le navigateur a rencontré une erreur et la session s'est arrêtée : {exc}")
                session_restarts += 1
                if session_restarts > MAX_SESSION_RESTARTS:
                    remaining = ", ".join(f"{w[0]}→{w[1]}" for w in windows[window_idx:])
                    log(
                        f"⚠ Abandon après {MAX_SESSION_RESTARTS} nouvelles tentatives - fenêtre(s) jamais "
                        f"traitée(s) : {remaining}. Relancez une recherche sur cette période pour les récupérer."
                    )
                    break
                log(
                    f"Nouvelle tentative avec une session de navigateur fraîche pour les fenêtres restantes "
                    f"(essai {session_restarts}/{MAX_SESSION_RESTARTS})..."
                )
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    new_files = sorted(set(os.listdir(download_dir)) - existing)
    return [os.path.join(download_dir, f) for f in new_files if f.lower().endswith(".pdf")]
