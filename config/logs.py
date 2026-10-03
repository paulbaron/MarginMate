"""The server's logs: settings.LOGGING, built by `logging_config`
(security audit DEPLOY-5, and the log half of ANON-6).

Without a LOGGING setting, Django sends django.request and django.security
only to a console handler that works under DEBUG and to e-mail with no
ADMINS: with DEBUG off, a 500's traceback, a CSRF refusal and a forged Host
left no trace anywhere. Here:

* the console (stderr) and, **for the production server alone**
  (`manage.py serve`, `is_the_server`), a rotating file, `marginmate.log` in
  the log folder (MARGINMATE_LOG_DIR; by default `logs/` beside
  TENANTS_ROOT - `../data/logs/` for the owner), five files of 5 MB at most.
  Every other process - runserver, migrate_tenants, any command, a WSGI
  server loading config.wsgi - logs to its console only. One file for every
  process could not rotate on Windows: Python opens it without
  FILE_SHARE_DELETE, so while a second process held it (a debug runserver,
  a long command that had logged one warning) the rename in doRollover
  failed with WinError 32, and every record past the size was DROPPED -
  serve's 500s and CSRF refusals missing from the log DEPLOY.md tells the
  owner to read (review PROD-5). One writer, and the rename always works;
* django.request at ERROR (a 500 and its traceback - not every 404),
  django.security at WARNING (a CSRF refusal, a forged Host, a request too
  big), everything else at WARNING; Django's INFO lines (runserver's
  requests, its reloads) reach the console only;
* the file is opened at its first record and its folder made then
  (`FolderRotatingFileHandler`): loading the settings creates nothing, and a
  server that logs nothing leaves no file anywhere;
* every record, on both, goes through `SigningLinkFilter`: the employee's
  signing link is a secret in the address (/personnel/signer/<token>/…) -
  for 14 days it opens the month and its PDF - and so is an employee's
  invitation (/invitation/<token>/), so a log keeps the first 4
  characters of the token only. Waitress writes no access log (nothing in
  `manage.py serve` turns one on), so no other line of this server holds
  the path; Cloudflare's logs are Cloudflare's.

Nothing secret is logged by the application: no password, no key, no
request body (Django's request logger logs the path).

The test settings keep Django's defaults instead (config/settings_test.py):
a test must never write into the owner's data, and a suite exercising 500s
on purpose would fill the console.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = "marginmate.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5
#: The command whose process writes LOG_FILE: the production server.
SERVER_COMMAND = "serve"


def is_the_server(argv) -> bool:
    """Whether a process started with `argv` (sys.argv) is the production
    server, `manage.py serve` - read the way manage.py reads it, its first
    argument. `serve --verifier` too: it logs nothing while it checks."""
    return list(argv[1:2]) == [SERVER_COMMAND]


#: The signing link's token, after /personnel/signer/ - or its encoded form
#: in a query string - and an employee's invitation's, after /invitation/
#: (accounts/members.py: for 7 days it lets whoever holds it choose the
#: employee's password). A token is URL-safe base64: never a « % ».
SIGNING_LINK = re.compile(r'((?:/|%2[fF])(?:personnel(?:/|%2[fF])signer|invitation)(?:/|%2[fF]))([^/%\s?#&"\'<>]+)')
#: How much of a token a log keeps: enough to tell two links apart when
#: reading a log, nothing to open one with.
KEPT = 4


def redact_signing_links(text: str) -> str:
    """`text` with every signing link's token cut to its first `KEPT`
    characters."""
    return SIGNING_LINK.sub(lambda match: f"{match.group(1)}{match.group(2)[:KEPT]}\N{HORIZONTAL ELLIPSIS}", text)


class SigningLinkFilter(logging.Filter):
    """Cuts the signing links' tokens out of a record: its message (the
    arguments merged in), its traceback and its stack. Keeps every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a filter must never raise
            # A record whose arguments do not fit its message: left for the
            # handler to report as logging always has.
            return True
        redacted = redact_signing_links(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            # Formatted here, once: every formatter uses exc_text when set.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_signing_links(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_signing_links(record.stack_info)
        return True


class FolderRotatingFileHandler(RotatingFileHandler):
    """A RotatingFileHandler that makes its folder when it opens its file -
    at the first record, with ``delay=True``."""

    def _open(self):
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()


def logging_config(log_dir=None, *, server=False) -> dict:
    """settings.LOGGING: the console, and `LOG_FILE` in `log_dir` for the
    production server (`server`, from `is_the_server`) when there is a
    folder - never for any other process (the module's docstring)."""
    handlers = {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "plain",
            "filters": ["signing_links"],
        },
    }
    if log_dir and server:
        handlers["file"] = {
            "class": "config.logs.FolderRotatingFileHandler",
            "level": "WARNING",
            "filename": str(Path(log_dir) / LOG_FILE),
            "maxBytes": MAX_BYTES,
            "backupCount": BACKUP_COUNT,
            "encoding": "utf-8",
            "delay": True,
            "formatter": "plain",
            "filters": ["signing_links"],
        }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"signing_links": {"()": "config.logs.SigningLinkFilter"}},
        "formatters": {"plain": {"format": "{asctime} {levelname} {name} : {message}", "style": "{"}},
        "handlers": handlers,
        "root": {"handlers": list(handlers), "level": "WARNING"},
        "loggers": {
            # Django's own handlers (a console under DEBUG only, e-mail to
            # ADMINS) are replaced by the root's: its records propagate.
            "django": {"handlers": [], "level": "INFO", "propagate": True},
            "django.request": {"level": "ERROR"},
            "django.security": {"level": "WARNING"},
            # runserver's request lines: the console only, filtered too.
            "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
        },
    }
