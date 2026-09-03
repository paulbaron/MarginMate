# MarginMate — working notes

Django 5.2 / Python 3.11 / SQLite. Bar inventory and costing: invoices in,
real per-unit costs out, recipe margins on top.

## Running the tests

```bash
.venv/Scripts/python.exe manage.py test --settings=config.settings_test
```

`config/settings_test.py` uses an in-memory database, a temp `MEDIA_ROOT`,
and **blanks every credential** so no test can reach the real mailbox, the
real Metro site or the Anthropic API. `tests/support.py::NoNetworkTestCase`
additionally makes an accidental outbound connection fail loudly.

Add `--exclude-tag=browser` once browser tests exist, for the fast loop.

## The testing contract

This codebase has a specific history: nearly every bug found in it has been
**silently wrong money**, not a crash. Three parsers dropped real charges
(a social-security levy, excise duties, packaging deposits), a FIFO
valuation priced a €40 count at −€140, a fuzzy matcher merged
"RICARD 45D 1.5L" into "RICARD 45D 1L". None of that raised an exception.
None of it was visible on screen. That is what the tests are for.

**Every change ships with tests.** Concretely:

| You wrote | You owe |
|---|---|
| A new parser | A `PdfPage` fixture per layout quirk, in `invoices/tests/` |
| A new route | A smoke-GET in `tests/test_views_smoke.py` |
| A new formset | A payload-shape test with **non-contiguous indices** |
| New pure logic | Unit tests, including the zero/None/negative edges |
| A bug fix | A test that **fails before** the fix and passes after |

Write the failing test first. A fix without a test that demonstrated the bug
is a fix you can't prove, and this project has already re-broken the same
thing three times.

### Parsers: never open a PDF in a parser

`InvoiceParser.parse()` does all the pdfplumber I/O and hands
`parse_pages(pages, date_hint, source_name)` a list of `PdfPage(text,
tables)`. Subclasses implement `parse_pages` **only** — that's what lets
every parser be tested from hand-written pages with no PDF file at all,
which matters because real invoices carry IBANs and delivery addresses and
must stay out of git. `invoices/tests/test_parser_contract.py` enforces it.

Fixtures are **structurally faithful, data invented**: copy the real column
positions, separators and quirks exactly; invent every name and amount. Get
the structure from a real PDF first (`pdfplumber` in a scratch script) — a
guessed fixture tests a layout that doesn't exist. Metro's "②" footnote
marker is a real example: substituting a plain "(2)" silently changes which
store number the regex picks up.

### Formsets: test what the browser actually posts

Removing a row client-side leaves a **gap** in the posted indices (0, 1, 3
with `TOTAL_FORMS=4`); index 2 is absent from the POST entirely. Django only
skips an empty extra row when `has_changed()` is False, and any field with
an `initial` makes that True — so the invisible row gets validated and fails
"required" where nobody can see or fix it. This has now hit three separate
forms.

Use `common.BlankRowTolerantFormMixin` and list the fields that carry
defaults or bookkeeping in `bookkeeping_fields`. Then test it: post
non-contiguous indices, and post a row left at its pre-filled default.

### Recipe variations scale multiplicatively — never enumerate them

A recipe's variations are the cartesian product of its choice groups, so 20
either/or ingredients is 1,048,576 variations. That is not an exotic recipe.

Anything the app renders must be linear in the number of **ingredients**:

- `Recipe.summary(ingredients)` — count and all four ranges, O(ingredients).
  It works because every displayed quantity is monotonic in the total cost,
  so the extremes come from the per-group extremes.
- `Recipe.variation_at(i)` / `variation_for(selection)` — one variation
  without building the others.
- `Recipe.variations()` — the real cartesian product. **Only safe on a
  recipe you know is small.** Nothing rendered uses it; tests do, as the
  reference the O(n) version is checked against.

The detail page shows one variation, chosen by `?v=0.2.1` (one option index
per group). Under `MAX_LISTED_VARIATIONS` it lists them all; above it,
one dropdown per choice group.

### Shrinkage: pool the alternatives, never guess the split

`inventory/variance.py` answers "where did the alcohol go" between two stock
takes:

```
unexplained = (opening + purchases - closing) - known_losses - sold
```

Recipes are fuzzy ("vodka OR gin"), so which bottle a drink came from is
unknowable. Rather than guess, stock items that appear as alternatives are
**pooled** (union-find over the choice groups, transitively), and the pool is
accounted for as one thing. Within a pool substitution is invisible; between
pools the accounting is exact.

The headline is deliberately a **floor**: the gap is valued at the *cheapest*
member of the pool, and stated in bottles of the format that item is usually
bought in (`typical_item_size` — the most-purchased format, not the largest).
The real loss is never smaller than what's reported.

Two rules learned from real data:

- **Never report an uncounted item as missing.** If a stock item wasn't in
  both counts, opening and closing are 0 and everything bought looks
  evaporated — 360 L of beer once topped the report, worth more than the
  genuine finding. Those go in `report.incomplete` ("count this next time").
- **Negative variance is a data error, not shrinkage.** You cannot pour stock
  you never had, so `is_impossible` means a miscount, a missing invoice, or a
  wrong recipe.

Sales come in through `recipes/sales.py::record_sales` and nowhere else, so a
new source (API, CSV, whatever the till turns out to be) is just a function
that produces `(recipe name, date, count)`. Unmatched names are **returned,
never dropped** — silently discarding one understates every later report.

### L'Addition (the till)

`manage.py laddition_import --from 2026-06-01 --to 2026-06-30` downloads and
records sales. Add `--dry-run` first: it reports which till products match a
recipe and which don't, without writing. `--file x.xlsx` skips the download.

Credentials live in `.env` (`LADDITION_EMAIL` / `LADDITION_PASSWORD`) and are
typed by the browser at run time, same as the Metro scraper.

Four things that cost real debugging time:

- **The export is a signed URL, and the signature does NOT cover the dates.**
  So the browser is only needed once, to capture it: press "Exporter en XLS"
  with `window.open` stubbed out, keep the URL, then swap
  `date_start`/`date_end` for any range. The alternative — driving the
  react-day-picker calendar in a popover in an iframe — was flaky in the
  worst way, failing by selecting the *wrong range* rather than by raising.
- **openpyxl cannot read these files.** The export declares its "Total" row's
  cells as numeric and writes `-` into them, which is invalid SpreadsheetML;
  read-only mode raises `invalid literal for int()`, normal mode rejects the
  whole workbook. Hence `recipes/pos/xlsx_reader.py`, which reads every cell
  as text. Don't "fix" this by adding openpyxl back.
- **Every reporting page is an iframe.** The top-level document holds only
  the sidebar (~120 chars), so a readiness check on body text times out on a
  page that loaded fine. Use `report_frame()`.
- **The sign-in button has no `type` attribute.** `<button>` defaults to
  submit as a DOM *property*, so JS and `get_attribute("type")` both say
  "submit" while XPath `@type` matches nothing. It's matched on exact text —
  which also avoids the "Mot de passe oublié ?" button right next to it.

The UI is two pages under **Ventes**: `/recipes/caisse/import/` runs the
import (background thread + htmx polling, same shape as the invoice gather)
and `/recipes/caisse/` is the backlog of till products with no recipe -
biggest sellers first, since that's where the unexplained stock is. Four
actions per row: link to a recipe, mark as a happy-hour variant, create a
recipe (name prefilled), or ignore (coffee, food, anything untracked).

`PosProduct` is that backlog, and an explicit mapping on it beats a
coinciding recipe name in `recipe_lookup()` - a mapping made by hand is a
deliberate statement about that exact till product.

Use `SalesDocumentLines`, not `ProductAnalytics`: it carries a **date per
line**, so sales can be sliced by stock-take window afterwards and a download
needn't be aligned to an inventory period.

Comped drinks (`TAG_Offered`) are **included** in the sales quantities — a
free drink is poured from the same bottle. Don't also record them as known
losses or they're subtracted twice.

A happy-hour variant is a separate till product ("Pinte Blonde" vs "Pinte
Blonde HH"). Put its till name in the base recipe's `happy_hour_name` and
both fold into one recipe. `record_sales` therefore aggregates by RESOLVED
RECIPE, not by raw name — summing by name would write one and overwrite it
with the other, silently losing every happy-hour sale.

### One inventory is enough (if the invoices go back far enough)

`compute_variance` has two modes and picks automatically:

- **Two counts** — the previous count is the opening stock, and only what
  happened between them is in scope. Needs no invoice history before the
  opening count.
- **Since the beginning** — used when there is no earlier count. Opening
  stock is zero, and everything ever bought, sold and lost is in scope. One
  inventory really does reconcile: you know what you bought, what you sold,
  and what is on the shelf.

The second assumes the invoices reach back to the day the bar opened; the
page says so out loud (`report.since_beginning`), because otherwise every
bottle bought before the records start reads as missing.

### Sales are unique per (recipe, day, SOURCE)

Not per (recipe, day). A sale typed in by hand exists precisely because the
till never saw it, so an import must never overwrite it — and keyed without
the source, re-importing a period would silently delete the manual entry for
every day it touched. `sales_between` sums across sources.

### "OU" nests

An option in a choice group can be a recipe with choices of its own. So a
group offers not `len(options)` ways to satisfy it but the **sum** of what
each option offers (`Recipe.group_size`), and each option contributes a cost
**range**, not a number (`RecipeIngredient.cost_bounds`). Counting stays
linear in ingredients — a sum inside a product, never an enumeration.

Options are addressed by a flat index per group (`resolve_option` walks the
options accumulating sizes), so a selection is still one number per group
however deep the nesting and the detail page's `?v=` links are unchanged.

For the variance engine this means a nested choice must be pooled too:
`reachable_stock_types` is deliberately separate from the amounts and is
**never capped**, because a pool missing a member reports that member's whole
consumption as unexplained. Amounts are capped (`MAX_SUB_VARIATIONS`).

### Django's `{# … #}` comment is SINGLE-LINE ONLY

A multi-line one prints itself onto the page and executes any tag inside it.
It still returns 200, so only looking at the output catches it. Use
`{% comment %}…{% endcomment %}`;
`tests/test_views_smoke.py::assertNoUnrenderedTemplateSyntax` guards it.

### `has_changed()` answers two different questions

Formsets use it both for "may I skip validating this blank row?" (extra rows
only) and for "should I write this saved row back?"
(`save_existing_objects`). Suppressing a field from the first silently
discards real edits in the second — that's how regrouping an ingredient via
"OU" stopped saving. `BlankRowTolerantFormMixin` gates on `empty_permitted`
for exactly this reason.

### Money is always `Decimal`

Never a float, anywhere in the invoice → cost → margin path. SQLite's own
arithmetic isn't exact decimal either — see the comments on
`StockType.current_value_ht` for why sums are done in Python.

### Test data

`tests/factories.py` — plain functions, no factory_boy. Note that
`invoices/migrations/0002_seed_suppliers` seeds METRO and UBA into every
database, the test one included.

## Known data issues (not code bugs)

136 invoice lines (€3,732.66) are attached to a product with a different
name, from the fuzzy matcher's old behaviour — e.g. `GENEPI 40D 50CL`
absorbed into `GENEPI 40D 70CL`. The matcher no longer does this, but the
existing links were not rewritten: splitting them changes historical stock
values and needs a human decision per pair.

## Privacy

Real invoice PDFs stay out of git (IBANs, addresses, prices), by explicit
choice. `.env` is never committed; `db.sqlite3*` is gitignored.
