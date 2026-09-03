"""Downloading the "Lignes de ventes" export from L'Addition Reporting.

That report is the one that says what was actually sold: "Détail ligne par
ligne, ticket par ticket de chaque vente sur la période sélectionnée :
produit, quantité, prix et taux de TVA". It arrives as an XLSX of several
sheets - see laddition_xlsx.py for what's in them.

The export turns out to be a plain signed URL:

    https://data.laddition.com/scripts/reporting/v1/SalesDocumentLines.php
        ?db=add-<account>&date_start=YYYY-MM-DD&date_end=YYYY-MM-DD
        &signature=<hex>&price=ttc

and - this is the useful part - **the signature does not cover the dates**.
So the browser is only needed to obtain one signed URL: sign in, press
"Exporter en XLS" once with window.open stubbed out so nothing actually
downloads, keep the URL it tried to open, then fetch any range at all by
swapping date_start/date_end.

The alternative was driving the date picker, which is react-day-picker
inside a popover inside an iframe inside a React app: paging months by
clicking a nav button, telling real day cells from the greyed-out
`day-outside` spill-over that carries the same number, and coping with the
popover closing itself part-way through. That was flaky in the worst
possible way - it fails by selecting the WRONG RANGE rather than by raising
- so this route is both simpler and much harder to get silently wrong.
"""

from __future__ import annotations

import os
import time
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from .laddition import date_windows
from .laddition_session import PAGE_WAIT_SECONDS, laddition_session, report_frame

SALES_LINES_PATH = "/v2/download?report=sales_document_line"
EXPORT_BUTTON = (By.XPATH, "//button[contains(normalize-space(.), 'Exporter en XLS')]")

# Replaces window.open so pressing Export records the URL instead of starting
# a download nobody asked for. Returning null is fine; the app ignores it.
CAPTURE_OPEN = """
window.__ladditionExportUrl = null;
window.open = function (url) { window.__ladditionExportUrl = url; return null; };
return true;
"""

DOWNLOAD_TIMEOUT_SECONDS = 180


class LadditionDownloadError(RuntimeError):
    pass


def with_dates(template: str, start: date, end: date) -> str:
    """The captured export URL, pointed at a different range.

    Rewrites the query properly rather than string-replacing the dates: the
    same digits also appear inside the downloaded filename and could appear
    in other parameters, and a replace that caught one of those would
    produce a URL that still works but covers the wrong period - which is
    exactly the class of failure this whole approach exists to avoid.
    """
    parsed = urlparse(template)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["date_start"] = start.isoformat()
    params["date_end"] = end.isoformat()
    return urlunparse(parsed._replace(query=urlencode(params)))


CAPTURE_ATTEMPTS = 4


def capture_export_template(driver, log=print) -> str:
    """Press Export, with the download suppressed, and keep the URL it opens.

    Clicked more than once if need be: the button is in the DOM well before
    React has attached its handler, so an early click is silently a no-op.
    There's nothing to observe that distinguishes "wired up" from "not yet",
    and the click is harmless to repeat (window.open is stubbed, so nothing
    downloads), which makes retrying the honest way to wait for it.
    """
    template = None
    with report_frame(driver):
        WebDriverWait(driver, PAGE_WAIT_SECONDS).until(
            lambda d: any(b.is_displayed() and b.is_enabled() for b in d.find_elements(*EXPORT_BUTTON))
        )
        driver.execute_script(CAPTURE_OPEN)
        for attempt in range(1, CAPTURE_ATTEMPTS + 1):
            driver.execute_script("arguments[0].click()", driver.find_element(*EXPORT_BUTTON))
            for _ in range(10):
                time.sleep(1)
                template = driver.execute_script("return window.__ladditionExportUrl;")
                if template:
                    break
            if template:
                break
            log(f"Export button not wired up yet (attempt {attempt}/{CAPTURE_ATTEMPTS}).")

    if not template:
        raise LadditionDownloadError(
            "Pressing 'Exporter en XLS' opened no URL - the export button may have changed."
        )
    if "date_start" not in template or "date_end" not in template:
        raise LadditionDownloadError(f"Export URL has no date parameters to rewrite: {template}")
    log("Captured a signed export URL.")
    return template


class DownloadCancelled(RuntimeError):
    """Raised when the caller asks to stop mid-download."""


def _wait_for_new_xlsx(download_dir: str, before: set[str], on_wait=None) -> str:
    """Wait for a completed .xlsx that wasn't there before.

    Only .xlsx counts: the page also kicks off an unrelated "downloads.htm"
    partial that never finishes - the Metro scraper hits the same thing - so
    waiting for "no .crdownload files at all" would wait for ever.
    """
    deadline = time.time() + DOWNLOAD_TIMEOUT_SECONDS
    while time.time() < deadline:
        new = [
            name
            for name in set(os.listdir(download_dir)) - before
            if name.lower().endswith(".xlsx")
        ]
        if new:
            return os.path.join(download_dir, new[0])
        # The caller gets a look in on every tick: this is the longest wait in
        # the whole run - a multi-year export takes the server a while to
        # produce - so a Cancel pressed here has to be noticed here.
        if on_wait is not None:
            on_wait()
        time.sleep(2)
    raise LadditionDownloadError(
        f"No .xlsx appeared in {download_dir} within {DOWNLOAD_TIMEOUT_SECONDS}s."
    )


def download_sales_lines(
    start: date, end: date, download_dir: str, log=print, should_cancel=None
) -> list[str]:
    """Download the sales-lines export covering [start, end].

    One file per date window: the back office caps a range at two years, so
    a longer history comes back as several files. date_windows guarantees
    they neither overlap nor leave a gap, so summing them counts every sale
    exactly once.
    """
    os.makedirs(download_dir, exist_ok=True)
    windows = list(date_windows(start, end))
    if not windows:
        return []

    def check():
        """Stop, and say so, if the caller has asked us to."""
        if should_cancel is not None and should_cancel():
            raise DownloadCancelled()

    paths = []
    check()
    with laddition_session(download_dir, path=SALES_LINES_PATH, log=log) as driver:
        check()
        template = capture_export_template(driver, log=log)
        for index, (window_start, window_end) in enumerate(windows, start=1):
            check()
            label = f"{window_start} to {window_end}"
            if len(windows) > 1:
                label = f"[{index}/{len(windows)}] {label}"
            before = set(os.listdir(download_dir))
            driver.get(with_dates(template, window_start, window_end))
            path = _wait_for_new_xlsx(download_dir, before, on_wait=check)
            log(f"{label}: {os.path.basename(path)}")
            paths.append(path)
    return paths
