"""Signing in to L'Addition Reporting and getting to a page.

Credentials come from the environment (LADDITION_EMAIL / LADDITION_PASSWORD
in .env) and are typed by the browser at run time - the same arrangement the
Metro invoice scraper uses. They are never stored in the database, never
logged, and never committed.

This module deliberately stops at "you are logged in and looking at the page
you asked for". What to click once you're there belongs in whatever module
knows about that particular report.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from contextlib import contextmanager

from django.conf import settings
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

REPORTING_ROOT = "https://reporting.laddition.com"
AUTH_URL = "https://auth.laddition.com/"
PAGE_WAIT_SECONDS = 40

# The login form, read off the live page. Both fields are plain ids.
IDENTIFIER_FIELD = (By.ID, "identifier")
PASSWORD_FIELD = (By.ID, "password")
# Matched on its exact text, and deliberately NOT on @type.
#
# Two traps here, both of which cost a debugging session. First, the form has
# a second button right after this one - "Mot de passe oublié ?" - which also
# submits, so a loose match can fire a password reset instead of a login;
# hence normalize-space equality rather than contains(). Second, neither
# button carries a `type` ATTRIBUTE at all: a <button> defaults to
# type=submit as a DOM *property*, so JavaScript and Selenium's
# get_attribute("type") both report "submit" while the XPath `@type` matches
# nothing whatsoever. `//button[@type='submit']` finds zero elements on this
# page.
SUBMIT_BUTTON = (By.XPATH, "//button[normalize-space(.)='Valider']")


class LadditionAuthError(RuntimeError):
    pass


# Chrome network errors that are worth another go rather than a failed run.
# ERR_NAME_NOT_RESOLVED is the one actually seen here: the very first
# navigation of a fresh browser intermittently fails to resolve the host,
# and the identical request succeeds immediately afterwards. A bar's own
# connection is not going to be more reliable than this machine's, and this
# runs unattended, so a transient blip must not cost a whole import.
TRANSIENT_NETWORK_ERRORS = (
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_CHANGED",
    "ERR_EMPTY_RESPONSE",
    "ERR_TIMED_OUT",
)
NAVIGATION_ATTEMPTS = 4


def _is_transient(exc: Exception) -> bool:
    message = str(exc)
    return any(code in message for code in TRANSIENT_NETWORK_ERRORS)


def navigate(driver, url: str, log=print, attempts: int = NAVIGATION_ATTEMPTS, sleep=time.sleep) -> None:
    """driver.get with a retry on transient network failures.

    A permanent failure (a bad URL, a crashed browser) is re-raised on the
    first attempt rather than retried four times over eight seconds.
    """
    for attempt in range(1, attempts + 1):
        try:
            driver.get(url)
            return
        except WebDriverException as exc:
            if not _is_transient(exc) or attempt == attempts:
                raise
            delay = 2 ** (attempt - 1)
            log(f"Network hiccup reaching {url} (attempt {attempt}/{attempts}), retrying in {delay}s.")
            sleep(delay)


def build_driver(download_dir: str) -> webdriver.Chrome:
    os.makedirs(download_dir, exist_ok=True)
    options = webdriver.ChromeOptions()
    if settings.SCRAPER_HEADLESS:
        options.add_argument("--headless=new")
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": os.path.abspath(download_dir),
            "download.prompt_for_download": False,
            # Otherwise Chrome opens the PDF in its own viewer instead of
            # saving it, and nothing ever lands in the download directory.
            "plugins.always_open_pdf_externally": True,
        },
    )
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    # Headless Chrome has refused downloads by default since ~96; without
    # this every download silently no-ops and the wait loop just times out.
    driver.execute_cdp_cmd(
        "Page.setDownloadBehavior",
        {"behavior": "allow", "downloadPath": os.path.abspath(download_dir)},
    )
    return driver


def log_in(driver, log=print) -> None:
    """Sign in, unless the session is already authenticated."""
    if not settings.LADDITION_EMAIL or not settings.LADDITION_PASSWORD:
        raise LadditionAuthError("LADDITION_EMAIL / LADDITION_PASSWORD are not configured in .env")

    wait = WebDriverWait(driver, PAGE_WAIT_SECONDS)
    navigate(driver, AUTH_URL, log=log)
    try:
        wait.until(EC.presence_of_element_located(IDENTIFIER_FIELD))
    except TimeoutException:
        # Already signed in: auth bounces straight through to the app rather
        # than rendering a form. Not an error.
        if "auth.laddition.com" not in driver.current_url:
            log("Already signed in to L'Addition.")
            return
        raise LadditionAuthError(
            f"The L'Addition login form never appeared (still at {driver.current_url})."
        ) from None

    driver.find_element(*IDENTIFIER_FIELD).send_keys(settings.LADDITION_EMAIL)
    driver.find_element(*PASSWORD_FIELD).send_keys(settings.LADDITION_PASSWORD)
    # "Valider" starts disabled and only enables once the form considers
    # itself complete. Clicking it before then is silently a no-op, and the
    # run then fails much later with a confusing "still on the auth page".
    try:
        wait.until(lambda d: d.find_element(*SUBMIT_BUTTON).is_enabled())
    except TimeoutException:
        raise LadditionAuthError(
            "The L'Addition sign-in button never became clickable - the login form may have changed."
        ) from None
    driver.find_element(*SUBMIT_BUTTON).click()

    try:
        wait.until(lambda d: "auth.laddition.com" not in d.current_url)
    except TimeoutException:
        # Still on the auth host after submitting - almost always wrong
        # credentials. Deliberately says nothing about what was sent.
        raise LadditionAuthError(
            "L'Addition rejected the sign-in - check LADDITION_EMAIL / LADDITION_PASSWORD in .env."
        ) from None
    log("Signed in to L'Addition.")


def normalise_path(path: str) -> str:
    """Turn whatever was passed on the command line into "/v2/whatever".

    Git Bash (MSYS) rewrites any argument that looks like a Unix path into a
    Windows one, so `--path /v2/shift-details` arrives as
    `C:/Program Files/Git/v2/shift-details`. Pasted straight onto the host
    that produces `https://reporting.laddition.comC:/Program Files/...`,
    whose only symptom is ERR_NAME_NOT_RESOLVED - which reads exactly like a
    network problem and sends you looking in entirely the wrong place.
    """
    path = (path or "").strip().replace("\\", "/")
    if path.startswith(REPORTING_ROOT):
        path = path[len(REPORTING_ROOT):]
    # A drive letter or a leading protocol means MSYS (or a copy-paste) got
    # to it; keep only the part from the version segment onwards.
    match = re.search(r"/v\d+/.*$", path)
    if match and (":" in path or path.startswith("//")):
        path = match.group(0)
    if not path.startswith("/"):
        path = "/" + path
    return path


def open_report(driver, path: str, log=print) -> None:
    """Go to a reporting page, signing in on the way if needed.

    Reporting is a single-page app that bounces to auth when the session
    isn't valid, so this navigates, checks where it actually ended up, and
    only then decides whether a login is required.
    """
    url = f"{REPORTING_ROOT}{normalise_path(path)}"
    navigate(driver, url, log=log)
    WebDriverWait(driver, PAGE_WAIT_SECONDS).until(
        lambda d: "auth.laddition.com" in d.current_url or d.current_url.startswith(REPORTING_ROOT)
    )
    if "auth.laddition.com" in driver.current_url:
        log_in(driver, log=log)
        navigate(driver, url, log=log)

    WebDriverWait(driver, PAGE_WAIT_SECONDS).until(
        lambda d: d.current_url.startswith(REPORTING_ROOT)
    )
    # The app renders after the shell loads, so "the URL is right" isn't the
    # same as "the report is on screen". Wait for the shell's own navigation
    # to appear - NOT for a lot of body text, because the report itself is
    # rendered inside an iframe and the top-level document only ever holds
    # the sidebar (about 120 characters of it).
    WebDriverWait(driver, PAGE_WAIT_SECONDS).until(
        lambda d: len((d.find_element(By.TAG_NAME, "body").text or "").strip()) > 40
    )
    log(f"Opened {url}")


REPORT_FRAME = (By.TAG_NAME, "iframe")


@contextmanager
def report_frame(driver, timeout: int = PAGE_WAIT_SECONDS):
    """Work inside the report iframe, then come back out.

    Every v2 report is a legacy page embedded in an iframe, so a Selenium
    call made against the top-level document sees only the sidebar. Nesting
    is easy to lose track of by hand, hence the context manager - it always
    switches back, even if the body raises.
    """
    WebDriverWait(driver, timeout).until(
        lambda d: d.find_elements(*REPORT_FRAME) and d.find_elements(*REPORT_FRAME)[0].is_displayed()
    )
    driver.switch_to.frame(driver.find_elements(*REPORT_FRAME)[0])
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: len((d.find_element(By.TAG_NAME, "body").text or "").strip()) > 50
        )
        yield driver
    finally:
        driver.switch_to.default_content()


@contextmanager
def laddition_session(download_dir: str, path: str = "/v2/shift-details", log=print):
    """A logged-in browser sitting on `path`, closed again afterwards.

        with laddition_session(dir) as driver:
            ...  # driver is on /v2/shift-details, signed in
    """
    driver = build_driver(download_dir)
    try:
        open_report(driver, path, log=log)
        yield driver
    finally:
        # Never let a teardown failure mask the real error.
        with contextlib.suppress(Exception):
            driver.quit()
