# MarginMate

Django webapp to gather bar purchase invoices, match products to stock types,
and keep a live view of stock quantity/value.

## Architecture

- **inventory** app: `StockType` (a "type" like Vodka or Gin), `Product` (a
  specific supplier product like "SOBIESKI VODKA 70CL"), and `StockMovement`
  (an append-only ledger of stock received). Current stock quantity/value is
  always computed from the ledger, never stored directly, so it can't drift.
  A `Product` with no `StockType` yet sits in the review queue - "Appliquer
  les règles" pre-fills it from `product_matching_rules.py` (a hardcoded,
  regex-based brand/product → stock item table) and `quantity_extraction.py`
  (a regex-based pack-size/count parser), covering known products instantly;
  anything neither recognises still needs a manual "Rattacher".
- **invoices** app: `Supplier`, `Invoice`, `InvoiceLine`, `ScrapeJob`, plus:
  - `parsers/` - one parser class per supplier PDF layout (`metro.py`,
    `uba.py`), a registry (`registry.py`), and a last-resort generic parser
    that asks the Claude API to extract line items (`llm_fallback.py`) for
    suppliers with no dedicated parser yet.
  - `scrapers/` - Selenium (Metro) and IMAP (UBA emails) scrapers, ported
    from the original `ScrapBarInvoices` project.
  - `tasks.py` - runs both scrapers and imports whatever they find, in a
    plain background thread (see `invoices/views.py:trigger_gather`) so
    "Gather new invoices" returns immediately and the page polls a status
    card for live progress instead of blocking the request.

Recipes, margin tracking, and fixed costs (mentioned as future goals) aren't
built yet, but the `StockMovement` ledger is designed so that "deduct stock
when a recipe is sold" can be added later as just another kind of movement,
without changing anything that exists today.

## First-time setup

The tools are pinned in `mise.toml` (Python 3.11, uv, prek); the Python
packages in `pyproject.toml`, every version locked in `uv.lock`.

1. Install [mise](https://mise.jdx.dev) (`winget install jdx.mise`) and put
   its shims on your PATH, so that `uv` and `prek` answer in any terminal and
   in the git hooks (PowerShell, then open a new terminal):
   ```powershell
   [Environment]::SetEnvironmentVariable("Path", "$env:LOCALAPPDATA\mise\shims;" + [Environment]::GetEnvironmentVariable("Path", "User"), "User")
   ```
2. In the project's folder:
   ```bash
   mise install     # Python, uv and prek, at the versions of mise.toml
   uv sync          # .venv with every package at its locked version, the dev tools included
   prek install     # the git hooks of prek.toml
   copy .env.example .env
   ```
   Until `uv sync` has made `.venv`, mise warns « no venv found » and offers
   `python -m venv`: ignore it, `uv sync` makes it. From then on, `python` in
   this folder is `.venv`'s (`mise.toml`, `_.python.venv`).

Edit `.env`:
- `DJANGO_SECRET_KEY` - required unless `DJANGO_DEBUG=True`: at least 50
  random characters, from
  `.venv\Scripts\python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`.
  With DEBUG off, a missing or weak key stops the app from starting.
- `DJANGO_DEBUG` - `False` by default; `True` on your own machine only (the
  app refuses it beside a public host in `DJANGO_ALLOWED_HOSTS`).
- `METRO_EMAIL` / `METRO_PASSWORD` - your docs.metro.fr login, needed for the
  "gather new invoices" button to fetch Metro invoices.
- `UBA_EMAIL_ADDRESS` / `UBA_EMAIL_APP_PASSWORD` - the Gmail account UBA
  sends invoices to. Use a Gmail **app password**, not your real password
  (Google account settings → App passwords, requires 2FA enabled).
- `ANTHROPIC_API_KEY` - optional, only needed to use "Autre (analyse IA)"
  when uploading an invoice from a supplier with no dedicated parser.

**Security note:** never put real credentials directly in Python files. The
original `ScrapBarInvoices/src/Server/ScrapInvoices/ScrapInvoices.py` had a
real Metro password and a Gmail app password hardcoded - it was never
committed to git, but you may still want to rotate that Gmail app password
since it sat in plaintext on disk. `.env` is gitignored here specifically to
avoid repeating that.

Every bar is an « espace » - its own database and folders under
`MARGINMATE_TENANTS_ROOT` - and every page wants a login (the logins live in
`MARGINMATE_ACCOUNTS_DB`). Put both outside the code folder in `.env`. Then:

```bash
.venv\Scripts\python manage.py migrate_tenants
.venv\Scripts\python manage.py create_invitation
.venv\Scripts\python manage.py runserver
```

Open http://127.0.0.1:8000/inscription/ and sign up with the code the second
command printed: that makes your espace and your login. `migrate_tenants`
(never plain `migrate`) is also what every update of the code needs, after a
backup. The Django admin, at `/admin/`, is for superusers only
(`manage.py createsuperuser --database accounts`; a login needs an espace to
open any page). There is no mode without a login: the old « single » mode
was removed on 29/09/2026, and `MARGINMATE_TENANCY` set to anything but
`multi` stops the app from starting.

`runserver` is for development. Online, the app runs under
`manage.py serve` (Waitress on 127.0.0.1, behind a Cloudflare Tunnel, started
by `start_production.cmd`): [DEPLOY.md](DEPLOY.md), in French, lists the steps
and the `.env` lines. The site runs from a production copy of this repository
(`C:\MarginMate\app`, its data in `C:\MarginMate\data`); changes are made in a
development copy, committed, pushed to GitHub's `main`, and put online with
`deploy.cmd` (DEPLOY.md, section 10), which takes GitHub's `main` and backs the
data up first (`manage.py backup_data`).

## Development

- **Dependencies**: `uv add <package>` (or `uv add --dev <tool>`) writes
  `pyproject.toml` and `uv.lock` together; `uv lock --upgrade-package <package>`
  moves one version. Never `pip install` into `.venv`: `uv sync` puts it back
  as the lock says. Production installs with `uv sync --locked --no-dev`
  (`deploy.cmd`).
- **The git hooks** (`prek.toml`, run by prek). Before a commit, on the files
  committed: ruff (lint, with its safe fixes, and format), ty (type checks;
  its warnings are shown, never blocking - `[tool.ty.rules]` in
  `pyproject.toml` says why), `uv.lock` kept in step with `pyproject.toml`,
  and a few file checks (merge markers, big files, private keys). Before a
  push, the continuous integration: ruff and ty over the whole repository,
  Django's system checks, no model change without its migration, and the
  fast test suite. Run it by hand where the hooks are not installed - a deploy
  takes GitHub's `main`, so the push before it is the CI's moment:
  ```bash
  prek run --hook-stage pre-push --all-files
  ```
- **By hand**: `uv run ruff check --fix`, `uv run ruff format`,
  `uv run ty check`, and the tests:
  ```bash
  uv run python manage.py test --settings=config.settings_test --exclude-tag=browser --parallel 6
  ```
  The browser tests (tag `browser`) drive Chrome: CLAUDE.md, « Running the
  tests », says how to run them.
- **Language**: the code and its comments are in English; only what the app
  displays (and the documents written for the owner, like DEPLOY.md) is in
  French. CLAUDE.md, « Toolchain », lists the French names kept on purpose.

## Stock item matching

"📋 Appliquer les règles" in the review queue (`/review/`) pre-fills the
stock-matching form for each pending product without a suggestion yet -
which stock item it likely belongs to, its unit, and the conversion factor.
Both are entirely hardcoded/regex-based, not model-generated:

- `inventory/product_matching_rules.py` - a table of (regex pattern → stock
  item name, category, unit), e.g. any raw name containing "RHUM" maps to
  "Rhum" regardless of brand. Add a new tuple here whenever a recurring
  product keeps landing in the review queue unmatched.
- `inventory/quantity_extraction.py` - parses a pack size or count out of
  the raw name (70CL, 1KG, "MPRO 100 GANT LATEX" → 100, ...), cross-checked
  against the invoice line's own colisage/quantity/volume so a pack size
  the supplier already counted isn't applied a second time.

An earlier version used a local LLM (Ollama) for the naming step instead -
removed after real usage showed it was both too slow (tens of seconds per
batch) and not reliable enough (the same product named differently between
runs, occasional cross-product mixups within a batch). See git history if
that's ever worth revisiting with a faster/more capable model - the
regex-based quantity extraction was already proven more reliable than the
LLM at that specific job even before the naming side was replaced too.

Either way, this only pre-fills the form; you still confirm/edit and click
"Rattacher" yourself. Anything neither the rules nor the quantity extractor
recognise falls through to a normal manual review, same as before.

## Selenium / Chrome

The Metro scraper drives real Chrome via Selenium (`webdriver-manager`
downloads a matching ChromeDriver automatically). Chrome itself must be
installed on the machine running the scraper. `SCRAPER_HEADLESS=True`
(default) runs it without a visible window - set it to `False` temporarily
if you need to debug what the scraper sees.

## Adding a new supplier

1. Add a `Supplier` row (via `/admin/`) with a `code` and a `parser_key`.
2. If you can write a regex parser for it: add `invoices/parsers/<name>.py`
   following `metro.py` / `uba.py`, register it with `@register`, add it to
   `invoices/parsers/__init__.py`, and set `parser_key` to its
   `supplier_code`.
3. If not yet: leave `parser_key` blank or set it to `LLM` - manual uploads
   for that supplier will use the AI-assisted parser instead.
