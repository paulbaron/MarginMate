# MarginMate

Django webapp to gather bar purchase invoices, match products to stock types,
and keep a live view of stock quantity/value.

## Architecture

- **inventory** app: `StockType` (a "type" like Vodka or Gin), `Product` (a
  specific supplier product like "SOBIESKI VODKA 70CL"), and `StockMovement`
  (an append-only ledger of stock received). Current stock quantity/value is
  always computed from the ledger, never stored directly, so it can't drift.
  A `Product` with no `StockType` yet sits in the review queue.
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

```bash
cd MarginMate
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:
- `METRO_EMAIL` / `METRO_PASSWORD` - your docs.metro.fr login, needed for the
  "gather new invoices" button to fetch Metro invoices.
- `UBA_EMAIL_ADDRESS` / `UBA_EMAIL_APP_PASSWORD` - the Gmail account UBA
  sends invoices to. Use a Gmail **app password**, not your real password
  (Google account settings → App passwords, requires 2FA enabled).
- `ANTHROPIC_API_KEY` - optional, only needed to use "Autre (analyse IA)"
  when uploading an invoice from a supplier with no dedicated parser.
- `OLLAMA_HOST` / `OLLAMA_MODEL` - only needed for "🤖 Suggérer avec l'IA" in
  the review queue - see "Local LLM (stock suggestions)" below. Free, no API
  key, runs on this machine.

**Security note:** never put real credentials directly in Python files. The
original `ScrapBarInvoices/src/Server/ScrapInvoices/ScrapInvoices.py` had a
real Metro password and a Gmail app password hardcoded - it was never
committed to git, but you may still want to rotate that Gmail app password
since it sat in plaintext on disk. `.env` is gitignored here specifically to
avoid repeating that.

Then:

```bash
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py createsuperuser
.venv\Scripts\python manage.py runserver
```

Open http://127.0.0.1:8000/. The Django admin is at `/admin/`.

## Local LLM (stock suggestions)

"🤖 Suggérer avec l'IA" in the review queue (`/review/`) pre-fills the
stock-matching form for each pending product - which stock item it likely
belongs to, its unit, and the conversion factor - using a local model via
[Ollama](https://ollama.com), so it's free and runs entirely on this
machine. It only pre-fills the form; you still confirm/edit and click
"Rattacher" yourself (see `inventory/ai_suggestions.py` for why: unit
conversion has enough real edge cases that blind auto-apply isn't safe).

Setup:

```bash
winget install --id Ollama.Ollama -e
ollama pull qwen3:8b
```

Qwen3 8B was chosen for its combination of reliable structured-JSON output
and strong French-language support among models that fit comfortably in
6-8GB of VRAM (research done August 2026 - re-check current recommendations
if you revisit this, the local-LLM landscape moves fast). Swap `OLLAMA_MODEL`
in `.env` to try another one; no code changes needed as long as it supports
Ollama's structured-output `format` parameter.

Ollama runs as a background service once installed (check with
`ollama list`) - no extra process to start alongside `runserver`.

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
