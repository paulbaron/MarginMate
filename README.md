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
