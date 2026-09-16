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

### Facturettes: the input is a photograph

The small-shop receipts (Franprix, Monoprix, Sabbh Oriental, Wing Seng) are
phone photos with **no text layer at all** — `pdfplumber` extracts an empty
string from every one. `invoices/ocr.py` stands in for `extract_text()`, and
`parsers/receipt_base.py::ReceiptParser` is the only class allowed to
override `parse()` besides the LLM fallback. Shop parsers still implement
`parse_pages` **only**, so every layout is still testable from hand-written
text with no photo and no OCR engine — `test_parser_contract.py` enforces
that the override lives in the base and nowhere else.

The engine is **PP-OCRv6 medium through `rapidocr` 3.x** (ONNX on CPU, about
5 s a receipt; models download into the package on first use, ~30 s once).
It was chosen by a bake-off on the 42 receipts: eight engines through the
same parsers, plus a parser-free score of how many printed amounts and names
(typed out by hand from the photos) turn up in each engine's output.
PP-OCRv4, the previous engine, read 73% of amounts; PP-OCRv6 medium 93%, and
100% of names. Tesseract read 22%. Local vision LLMs (Qwen2.5-VL 3B/7B,
Qwen3-VL) add tax codes that aren't printed, drop the price column, and take
minutes a receipt on a 6 GB GPU. Lines are grouped from each detected box's
own angle and position (`ocr.group_boxes_into_lines`), not by a y tolerance.

**OCR is a guessing machine, so nothing here is trusted.** A misread digit
produces a perfectly well-formed wrong price — precisely the failure this
codebase keeps getting bitten by. The lever is that a till receipt is
redundant: it prints the item lines *and* their sum *and* a VAT table, and
those three have to agree. Each parser checks its own arithmetic against the
ticket's own totals and hands the verdict up as `ParseCheck`s, stored on
`Invoice.parse_checks` and shown beside the photo. **Nothing silently
repairs a receipt that does not add up.** On the 42-receipt corpus, through
the real import path: **42/42 right, 38 passing every check, 0 wrong without
saying so** — which is the number that matters. The four flagged are real:
two VAT amounts cut off or faded, a promotion not tied to a product, and a
Franprix ticket whose free baguette was misread, which only the printed
pre-discount total gave away.

**Anchors are arithmetic, never words.** Where the items stop, what the total
is and whether a promotion applies are found by the numbers:
`printed_total` (the largest amount printed at least twice that the VAT table
confirms), `ends_items` (the first line repeating the amount paid, or the
running sum once it has reached it), `amount_printed` (a promotion counts
only if the ticket prints the items' sum) and `printed_promotion` (a
pre-discount total the items fail to reach means one is missing). The parsers
used to look for "TOTAL", "HORS AVANTAGES", "DUPLICATA" and to repair one
engine's misreadings ("T1Q.49", "1.G0", "1,33t"); on a better engine they
scored *worse* than on the one they were tuned to, because the new one spelled
the words differently. **Do not add a pattern for an engine's misspelling.**
Genuine layout logic is fine; a spelling workaround is not.

Six things are load-bearing:

- **Two sums are checked, not one.** The TTC sum proves the amounts were
  *read* right; the HT sum proves they survive the `/ 1.055` each line gets
  on the way in. Six baguettes at 0.49 TTC are 0.4645 HT each, stored as
  0.46: the ticket says 2.79 and the lines say 2.76. The TTC arithmetic
  balances perfectly, so only the HT check catches it. That gap goes into
  `reconciliation_adjustment`; a gap bigger than rounding fails instead.
- **Never assume 5.5%.** Two of the 42 are at 20% (cleaning vinegar at
  Franprix, a discounted line at Monoprix). Reading those at the food rate
  understates the cost by 14% with nothing downstream able to tell.
  `read_rate` rejects any percentage France does not have — OCR reads the
  VAT *amount* "0,26" as a rate of 26% given the chance.
- **Group lines by geometry.** A price column a row off its name column
  (curl, tilt) gave each item its neighbour's price; two tightly printed
  rows merged into one product at the second one's price. The deskew plus
  box-angle grouping fixed both; boxes that overlap horizontally are never
  put on one line.
- **Franprix items sum to the PRE-discount total.** Promotions print in
  their own block rather than reducing the item lines, so the lines add up
  to "TOTAL SANS AVANTAGES" while only "TOTAL A PAYER" reflects the "3 pour
  2". Eleven of nineteen carry one. The discount is what the items add up to
  minus what was paid — **only once the ticket prints that sum**; taken on
  trust, a misread price would pass as a promotion. It is spread over the
  products the block names — pro-rata across *matching* lines, so a bread
  promotion makes bread cheaper rather than shaving centimes off the lemons.
- **A Franprix weight belongs to the item BELOW it.** "BRUTWEIGHT 0.920 KG
  @ 3.49 / KG" is the orange's (0.920 x 3.49 = 3.21, the orange's price). The
  parser used to put the kilos on the item above, silently.
- **The tills disagree about their own VAT tables.** Four shops, five
  layouts, and Sabbh's "Base TVA" column is tax-INCLUSIVE where everyone
  else's is exclusive. `parse_vat_line` settles it by arithmetic — it
  searches the row's plausible readings for the pair that satisfies a VAT
  identity *and* reproduces the printed grand total. Franprix draws its
  table with rules the recogniser reads as digits ("2.261" for "| 2,26 |"),
  and only the grand total separates that from Monoprix's genuinely
  4-decimal HT ("3.0237").

**"Article divers" is a price, not a name.** Sabbh's till prints no product
names at all. `ShopItemPrice` maps a **unit price** (not a line total —
0,70 appears as 3pcs/2,10, 6pcs/4,20, 7pcs/4,90 and 11pcs/7,70) to a
product, and the mapping is applied in `receipts.label_placeholder_lines` at
import time, never in the parser, which must stay free of database access.
An unmapped line is named by its own price ("Article divers (0.70 EUR/u)")
so the review screen is actionable without opening the photo. Recording a
price on the review screen names that line on **every ticket of the shop still
waiting to be checked** (`receipts.apply_known_prices`) - one Pita price left
25 tickets of the queue unnamed when it named only the ticket it was typed
on. Every known price is applied, each as of its own ticket's date, and a
checked ticket is never rewritten (except the one the price was typed on).

**The review screen is the deliverable, not the parser.** `/invoices/tickets/`
takes a batch and detects each shop from its own header (a Franprix ticket
run through the Monoprix parser *would* produce lines, and they would be
wrong — an unrecognised file is reported, never guessed). `.../verification/`
is the queue, oldest first; `.../<pk>/verifier/` puts the photo beside the
checks and the editable lines and moves to the next receipt on save. Saving
goes through `replace_invoice_lines`, the same path as a hand-typed invoice.
The form is in **TTC**, as the ticket prints it: checking an HT price against a
photo meant converting every one in one's head. `ReceiptLineForm.cleaned_total_ht`
stores HT with the line's own rate, and a total saved untouched keeps its HT to
the cent. A receipt's "Corriger les lignes" opens this screen, not the HT form
(which would also drop the lines' OCR readings).

**A receipt line keeps its printed TTC** (`InvoiceLine.printed_ttc`, set by
the receipt parsers and by this form). HT to the cent does not convert back:
7,00 at 5.5% is 6,64 HT, which is 7,01 - ten pitas at 0,70 read 7,01 on the
screen meant to check them. `InvoiceLine.total_ttc` and everything shown in
TTC use it. A line a promotion was spread onto has none - its cost is worked
out - and the form posts back what it showed for such a line
(`computed_ttc`), so saving it untouched does not pass a worked-out figure off
as printed: one Franprix ticket validated before that came to 7,91 for 7,92
paid. `Invoice.total_ttc`, when every line has a printed amount, is their sum
(never plus the adjustment, which in HT puts back cents the printed amounts
never lost: added on top, six 0,49 baguettes came to 2,97) - or the ticket's
own printed total (`Invoice.printed_total_ttc`, what was paid) when the lines
are within the parser's tolerance of it, so a cent the OCR misread never costs
the bank match. Otherwise the whole invoice stays on the HT arithmetic.
Receipts imported before these fields got them back from their stored reading
(`manage.py restore_printed_ttc`, matched by count, rate and HT, never by
name; `--dry-run` first after a parser change): 34 totals a cent or two off
became the printed one, none went the other way.

**A folder is a background job** (`invoices/receipt_batches.py`). The upload
page takes files or a whole folder (`webkitdirectory`; both inputs post as
`files`), and anything that is neither a PDF nor a photo is listed as ignored
rather than refusing the selection. Files are staged under
`media/receipt_batches/<id>/` and a thread imports them one at a time into a
`ReceiptBatch`, whose page polls with htmx and gives every file exactly one
outcome - `ok`, `duplicate`, `unrecognised`, `error`, `ignored`, `cancelled` -
because a receipt that failed silently shows up weeks later as stock never
bought. Three traps: Django refuses more than **100 files** a request by
default, with a 400 before any view runs (`DATA_UPLOAD_MAX_NUMBER_FILES`); a
phone photo's rotation lives in its EXIF tag, so `ocr.page_images` applies it
or the receipt is read sideways; and scanning the same folder again has to be
cheap, so `import_receipt` checks the file's SHA-256
(`Invoice.source_sha256`, backfilled by migration 0014) before any OCR.

**An unrecognised ticket is not a dead end.** A torn or faded header is enough
for `detect_parser` to find nothing (a real Sabbah ticket, 13/08/2024). The
batch raises `UnrecognisedShopError` for that - a plain `ValueError` is a broken
file, reported as an error - keeps the file (`"kept"` on the entry) and, once
the batch has finished, offers a list of shops on that row
(`receipt_batches.import_with_shop`, never while the thread runs: it owns
`results` and would write its copy over the change). The OCR runs in the
request, so shop choices take turns on one lock, and a resume waits for none -
two tabs used to import the same file twice. The chosen shop's reader
runs regardless of the header; a supplier with no ticket reader, or a reader
that fails or reads nothing, still files the ticket, empty, with a failed
"Lecture automatique" check - that check is also what puts it in the review
queue, which lists only receipts with checks. Either way the operator lands on
the review screen, which also takes the ticket's **date** (a blank field keeps
the date read: it is a field nobody filled in, not a date removed).

**The autoreloader kills an import outright** on any code change: a
137-ticket batch died one second in, while code was being edited, and showed
"En cours" for half an hour. So a running batch beats every 15 s from a
thread of its own (`receipt_batches._Heartbeat`), counts as dead after 90 s of
silence (`ReceiptBatch.STALE_AFTER`; the page's live part reaps too, or it
never changes), and can be resumed ("Reprendre l'import"): staged files stay
until every one has been read. Not resumable while it may still run - a beat
within STALE_AFTER could come from a machine that just woke up, and the beat
puts a batch reaped that way back to running. **Check no import or gather is
running before editing code**, or run the server with `--noreload`.

One trap found by opening the page rather than by a test: `vat_rate` is
stored to four decimals, so 20% renders as "20.0000" and the line form
(`decimal_places=2`) refuses the value it just rendered — the save dies with
an error under a field nobody touched. `_vat_percent_for_form` quantizes it.
This affected `edit_invoice_lines` too, and had done all along.

**Product names read by OCR match tolerantly — receipts only.**
`resolve_products(..., ocr_tolerant=True)` (set from `ParsedInvoice.from_ocr`
and `Invoice.is_receipt`) attaches "CITRON SHT 5OOG" to the shop's existing
"CITRON SHT 500G": spacing, punctuation and accents are ignored, letters that
share a shape with a digit (O/0, I/1, S/5, B/8...) are cheap, a real typo
costs one. A digit never becomes another digit and is never added or dropped
— "250G" never finds "500G". Two equally close candidates: no match. Digital
invoices keep the strict matcher. The review screen shows "lu sur le ticket"
under any line matched this way, which is what makes the forgiveness safe.

A product is compared by **every name it has been read as**, not only its own
(`InvoiceLine.read_as`, carried through the review screen in a hidden field).
Its own name is just whichever reading came first, and one Franprix label came
back as BAGUETTE BLANC, BLAND, BLAVC, BLAVD, BAGLETTE, BAGUETRE and AGUETTE
BLANC across fourteen tickets — readings up to three mistakes apart. **Do not
widen the budget instead**: measured against the products already in the
database, a budget wide enough for two readings to meet joined
"1/16CANTAL AOP ED" with "... JNE" and "GINGERBEER 1L" with "... 1L BIO" (at
L/6, "PORTO CRUZ BLC" with "RGE"). Readings reach the far spellings one
mistake at a time. It follows that a receipt's lines are resolved together
(`resolve_products`), so a line reaches a product through its neighbour on the
same ticket whatever their order, and that a misreading attached by hand on
the review screen is recognised on the next ticket. `read_as` is never set on
"Article divers": a price is not a name, and as a reading it would bypass the
price list's dates. The product a corrected line stops using is deleted if
nobody classified it (`deletion.remove_orphan_products`), or it would wait in
the review queue for ever.

**A product is renamed under its line, not in it.** A receipt's product is
named after its first reading ("BAGUETTE BLAND"), which fills every ticket's
form. Typing the right spelling in a line relabels that line only - the typed
name resolves straight back to the same product, since it is one of its
readings - so the review screen offers "Renommer … sur tous les tickets" under
the first line of each product (`receipts.rename_product`), filled in with what
that row says (a typed "ORANGE" on the product "RANGE"), and says so after a
save that kept a typed name on a product of another spelling. A rename keeps
every OCR reading as read (still recognised), relabels the lines named after
the product and the shop's price list, and refuses a name another product of
the shop has: that is a merge, done by typing the name in the line. **Only the
products of shops with a ticket reader**: a paper ticket filed by hand under
Metro shows Metro's catalogue, which its PDFs find by exact name with no
reading to fall back on - and its typed lines match strictly for the same
reason. Renaming and "Retenir ce prix" reload the page, so the page asks
before dropping line corrections not yet validated. A price already known is
refused by the form (the shop is not a form field, so Django never checked
the uniqueness it is part of, and the database answered with a 500); wrong
prices are deleted in Admin → Prix connus des tickets.

### Deleting an invoice (`invoices/deletion.py`)

Deleting the row alone is wrong twice over. `StockMovement.invoice_line` is
SET_NULL, so the purchases would stay in the ledger with no invoice behind
them — deleted explicitly first. `StockTakeLineSource.invoice_line` is
PROTECT: an invoice that priced a past stock take is **refused**, naming the
count, since deleting it would rewrite what that count was worth. Unclassified
products that only it created go with it (a misread receipt's garbled names
would otherwise sit in the review queue for ever); files go on commit.

### Correcting an invoice's lines

The line forms (the HT editor, "Corriger les lignes", and the ticket review
screen) show a name, a count, a total and VAT; a stored line carries more -
Metro's measured volume, duty, discount, pack size, category, a receipt's
reading and printed TTC. Each row posts back its line's id, and
`importing.corrected_line` keeps what the form doesn't show (the volume
scaled when the count changes: an item's size didn't). Rebuilt from the four
visible fields, a Metro invoice saved untouched turned 4.2 L of vodka into
6 L of stock, and a weighed Wing Seng lemon lost its kilos.
`replace_invoice_lines` then **updates those lines in place** - a stock take
priced from one keeps its trail, where deleting and recreating it hit the
PROTECT with a 500 (66 real invoices) - and removes the others, refusing
(`InvoiceLinesInUseError`, nothing saved) to remove one a count was priced
from. A refund is a negative count **and** a negative amount (Metro's "1-" /
"15,00-", on 41 real invoices); a positive count at a negative price is
refused - stock worth less than nothing is what the FIFO guard exists for.

### Gathering invoices

**Metro bans accounts that hammer docs.metro.fr.** One login per run, one
90-day window at a time, never more than one download click every
`CLICK_INTERVAL_SECONDS`, never more than `MAX_IN_FLIGHT` downloads
outstanding. Within those limits the scraper no longer waits for each file
before the next click (`scrapers/metro._download_window`); stragglers are
waited for at the end of each window. Test changes against the fake browser
in `test_scraper_metro.py`, not against the live site.

A download is complete when **the PDF its own row produces** appears
(`134_52_14645_<timestamp>_invoice_cus_copy_main.pdf`, named after the row's
checkbox id `FRA_134_52_14645_…`) - never when the folder's file count goes
up. A click sometimes also starts a stray "downloads.htm" that appears and
vanishes; counted along, it hid PDFs that had landed in a second, and one
gather spent eleven of its twelve minutes timing out downloads long finished.
Deposit credit notes ("Consignes") don't print their store, so the parser
stores them as "052-014645"; the list page is matched on till and number too
(`_is_known`), or every credit note is downloaded again on every run. The
levy and discount lines belong to the product above them **across page
breaks** too.

The mailbox search asks for BEFORE the day **after** the end date: IMAP's
BEFORE is exclusive (RFC 3501), and the form's end date is today - this
morning's invoice email was left for a later run.

The gather form starts from **the newest invoice the gathered sources have
already brought in** (`tasks.default_gather_start`, receipts and future dates
left out). It used to take the earliest of each source's latest - one
supplier billing twice a year sent every gather ten months back.

**Plou & Fils changes its layout**: a "Taux" column on product rows (2026),
a VAT summary one column shorter, "Référence interne" instead of "N°
document" (January 2025). `parsers/ploufils.py` finds the product table and
its columns by header, and checks the lines against the printed "Montant
total HT". A parser that reads nothing, or warns (`ParsedInvoice.warnings`),
leaves the message on the invoice (`error_message`) and holds it in "À
vérifier" - an empty invoice used to say "ce fournisseur n'a pas de parseur".

### Bank statements (`bank/`)

`/banque/` imports the account's CSV export (BNP Paribas) and links each
spending line to the invoice or receipt it paid. `bank/statements.py` reads
the file and `bank/matching.py` decides - both on plain values, no database;
`bank/reconcile.py` does the rest.

A link is made automatically only when **the amount is exact to the cent**,
**the date fits** (a receipt dated three days before to one day after the card
date the label prints; a supplier invoice dated up to 180 days *before* a
debit or transfer, never after) and **the payee the bank prints names the
supplier** ("SABBAH" names "Sabbh Oriental", "U.B.A." names "UBA"; a word of
five letters may be one letter off; legal forms, "FILS", "PARIS" never
count). Several invoices of that supplier adding up exactly to one debit count
too, when exactly one combination does. Anything short of that is a
suggestion, with its reason on screen: the same amount at a payee the bank
doesn't name, or the named shop with a different amount - a misread receipt,
since the bank's figure is the right one. An amount alone never links, and a
debit never gets unnamed suggestions: months of invoices are in reach of it,
and something always has the right amount. A receipt whose date the OCR
missed fits no window; for a card payment, the named shop with that amount -
within a cent, since a receipt's total is rebuilt from its HT lines (a printed
7,76 totals 7,75) - is still suggested.

Card payments are matched first, so a receipt goes to its own card payment
before a debit's wider search sees it. An invoice is paid once
(`InvoicePayment.invoice` is one-to-one); a line can pay several. Anything a
person does to a line - link, unlink, "pas de facture" - sets
`settled_by_hand`, and the automatic pass never touches it again; "Rapprocher
automatiquement" hands it back. Linking a payee the supplier's own name
doesn't contain records a `CounterpartyAlias`, so next month's payment to it
links on its own. Re-importing an overlapping export is safe: an operation's
fingerprint includes its position among identical rows, so two identical
baguette purchases on one morning stay two lines.

**Payments that never have an invoice** (a loan, URSSAF, salaries) are
excluded by `IgnoreRule`: a regular expression searched in the whole label,
case ignored. Rules are applied when the page is drawn (`views.classify`),
never stored on the lines, so pausing or deleting one puts its payments
straight back - and the automatic pass skips what an active rule matches. A
line's invoice beats any rule: a rule never hides a payment that has one. A
pattern that matches the empty string (".*", "URSSAF|") is refused - one
stray "|" would hide every missing invoice. The rules page has a "Tester"
button showing what a pattern catches before it is saved, including how many
of those already have an invoice (the sign of a pattern too broad). The page
filters by month (`?mois=2026-07`) and groups what is still missing an
invoice by payee, each with a pre-filled "Ignorer…".

**Found through the statement: `Invoice.total_ttc` left VAT off the
reconciliation adjustment.** The adjustment is duty the lines don't carry
(UBA's "VIG. SECU" and the like), and duty is part of the VAT base. Added
flat, all four UBA debits of July 2026 were five or six cents above the
invoice's total; taxed at the rate of the goods carrying duty, all four match
to the cent. The adjustment still never goes into any product's cost.

### A big POST is rejected before any view runs

Django caps a request at `DATA_UPLOAD_MAX_NUMBER_FIELDS` — **1000 by
default**, which a stock take reaches at **332 rows** (three fields a row plus
the management form; 249 when editing, since each saved row also posts its
id). Over the line the request dies with `400 Bad Request` in core handling,
*before* the view exists: there is no form to re-render, no message, and every
count the user typed is gone.

That is not hypothetical — it is what "j'ai perdu mon inventaire après avoir
cliqué sur enregistrer" was, on a real evening's counting. `config/settings.py`
now sets it to 25,000. Any new formset that can grow with the data inherits
the same ceiling, so check it before assuming a save is "just slow".

### Formsets: no spare row on a saved record

`extra=1` renders a blank line under the real ones. On a *new* record that's
a convenience; on a *saved* one it reads as a bug — take an item out of an
inventory, reopen it, and the blank row sitting where something used to be
looks exactly like a removal that half-worked. It was reported as one.

`StockTakeLineFormSet` uses `extra=0` and the page adds the first row itself
(`stock_take_form.html`), so what is on screen is only ever what is really in
the count.

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

### Counting an inventory: priced live, by the same code that saves it

The stock-take form prices every row as it is typed, and totals them. Two
rules keep that honest:

- **The preview is not a second implementation.**
  `views.value_stock_take_line` goes through `value_counted_quantity` /
  `value_counted_stock_type_quantity` as of the same date the save will use,
  so the figure on screen and the figure stored cannot drift. A "close
  enough" sum computed in JS is precisely how this codebase has shipped
  silently wrong money before. A row that *can't* be priced says so and drops
  out of the total rather than showing a stale number.
- **A saved line already knows what it is worth.** Its `value_ht` is frozen
  (see `StockTake`), so the running total is right the instant the page opens
  — no request, nothing re-priced. Only rows the user edits ask the server.

The datalist offers only what had actually been delivered by the date being
counted (`services.first_purchase_dates`), and follows the date field live.
`StockTakeLineForm` refuses it too, against the date **submitted** rather than
the one on the saved row — changing the date and adding a line happen in the
same POST. Something with no dated invoice is never "too new": we can't prove
when it arrived, and hiding it would drop real stock out of a count.

A row being **deleted** is never re-judged. Validating a row on its way out
traps the user in an inventory they can no longer fix.

**An invoice line with no printed volume stores `total_volume` 0, not NULL.**
`product_counting_ratios` skips those lines (`total_volume__gt=0`); tested
with isnull, the 0 ratio went through and `stock_units_per_item` turned
eleven 1 L bottles counted on a shelf into 0 litres - 62 real count lines on
210 products read as entirely missing in the écarts and stock pages. An
unmeasured product's items convert with `stock_equivalent`, exactly as its
purchases were booked.

**A stock item in use is merged, not deleted.** Recipes, stock-take lines and
sale lines hold their stock item with PROTECT. `merge_stock_types` moves them
all, in one transaction (a count that measured both items becomes one line
adding both up) - it used to fail on them after the products had already
moved. Deleting an item still in use, alone or through "supprimer les types
vides", is refused with where it is used; "vide" means no product and no
movement, so a loss written down against an item survives.

### N+1s hide in per-object properties

Three of these together were most of the app's runtime, and none of them
looks like a query at the call site:

| Looks innocent | Actually |
|---|---|
| `stock_type.current_unit_cost_ht` | one query per stock type |
| `typical_item_size(stock_type)` | three queries per stock type |
| `stock_units_per_item(product)` | one query per product |

The écarts page called all three per pool — 1,766 queries, 2.4 s. The batched
forms (`_movement_totals`, `typical_item_sizes`, and `ratios` passed into
`stock_units_per_item`) took it to 125 queries and 0.64 s. Same for
`EntryResolver`, which resolves a whole formset's typed names in three
queries instead of one per row.

The tell: SQL time near zero while wall time is seconds. That is hundreds of
tiny queries, not a slow one — profile with `connection.queries`, not EXPLAIN.

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

#### Three things eat the headline before shrinkage does

On the real database the report read €27,318 missing. Almost none of it was
theft, and the order these come off matters more than the arithmetic:

1. **One stock take means "since the beginning".** With no earlier count the
   report compares *everything ever bought* against today's shelf. A second
   count windowed it to twelve months: €27,318 → €12,311.
2. **`StockType.loss_percent` is deducted** (`PoolVariance.loss_allowance`,
   accumulated at each member's own rate, never averaged across a pool):
   €12,311 → €10,500. It comes off `unexplained_min` to give `shortfall`,
   which is what `value_missing_min` and `is_missing` use. `unexplained_*`
   stay raw on purpose, and `is_impossible` still judges on them — an
   estimate must never be the thing that declares the data wrong.
3. **An item no recipe uses can only ever read as 100% missing.** The till
   sells a glass of prosecco or a saucisson board; nothing says what's in
   one, so every drop that leaves is unexplained. That was €8,165 of the
   remaining €10,500 across 54 pools. `PoolVariance.in_recipes` flags them
   and `VarianceReport.only_in_recipes()` sets them aside — `?recettes=1` on
   the écarts page, which shows **both** totals side by side so the filter
   can't hide what it costs. Actual candidate shrinkage: €2,335.

The fix for (3) is writing recipes, not tuning the report — see the "Produits
caisse" backlog, 187 of 211 till products unmapped at the time of writing.

Sales come in through `recipes/sales.py::record_sales` and nowhere else, so a
new source (API, CSV, whatever the till turns out to be) is just a function
that produces `(recipe name, date, count)`. Unmatched names are **returned,
never dropped** — silently discarding one understates every later report.

### "Vendu" does guess — but only where the shelf lets it

The variance report pools alternatives and refuses to say which bottle a
drink came out of. The stock page's **Vendu** column can't do that: it has one
row per item and has to put a number on each. So it guesses — priciest first,
because nobody reaches for the well brand while the good stuff is open — but
what was actually bought rules most splits out, and
`variance.allocate_choices` walks the rest:

1. Fill the priciest option until it reaches **what was bought less that
   item's `loss_percent`** (10% by default, editable per stock item: the
   over-pour, the last centilitres, the keg's foam). Then the next-priciest.
2. Once **every** alternative has had that turn, release the allowance and go
   round again up to 100% of what was bought.
3. Anything still unexplained goes on the priciest option, pushing it past
   what was bought, and the row turns red.

Three things that are load-bearing:

- **Servings are whole, so round one rounds UP** against the allowance (never
  past what was bought). 22 half-litre pints out of a 6 L keg with a 10%
  allowance is 11 pints — 5.5 L — not 10 and a stranded 0.4 L that no sale
  could have produced.
- **Round one is global, not per recipe.** Releasing the allowance per demand
  would let one busy cocktail drain a bottle to the last drop while a full
  alternative stood untouched beside it.
- **A fixed ingredient is never clamped.** If the recipe says every Caipirinha
  takes 50 g of lime, 100 of them took 5 kg whether or not 5 kg was ever
  bought. Saying so is the whole point of the column: red means a missing
  invoice, a wrong recipe, or a till product linked to the wrong drink — *not*
  shrinkage, which is stock that left without being sold.

Greedy, not optimal: demands are served in a stable order (recipe, then
group) and an early one can take capacity a later one wanted. A global
optimum would be a different program and no more defensible to a supplier.

#### The ceiling is different between two counts

`?inventaire=<pk>` on the stock page scopes all of it to one stock-take
window (`variance.stock_between`, sharing `counts_by_stock_type` and
`movements_between` with the écarts page so the two can't disagree about what
a period contains). The closing count is the only thing chosen; the opening
one is whichever came before it, exactly as in `compute_variance`.

All time, the ceiling has to be "everything ever bought" — nothing deducts
sales from the ledger, so that's all there is. Between two counts both ends
were physically measured, which gives a far tighter one:

```
sellable = (opening + purchases − closing) − known_losses
```

Stock still standing there at the closing count obviously wasn't poured, and
a bottle already written down as broken can't have been either. The part of
`sellable` no sale explains (`SoldQuantity.unexplained`) is the shrinkage
figure; sales *above* it are the data error, same red as before.

Two things carried over rather than rediscovered:

- **An item missing from either count gets no verdict.** Its opening and
  closing read as zero, so everything it bought looks evaporated — the row
  says "non compté" and is excluded from every total. Same rule, same reason
  as `VarianceReport.incomplete`.
- **The per-item view is the actionable one, the pooled one is the
  defensible one.** Both stay: the écarts page still values the gap at the
  *cheapest* pool member (a floor you can put to someone), while the stock
  page names bottles by attributing the pool (a guess you can act on). They
  cross-link.

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

Taking a variant off its recipe (ignore, back to the worklist, linked
elsewhere) clears that `happy_hour_name`: the import counts sales by name, so
its sales kept landing on the recipe, and a product sent back to the worklist
was relinked by the next import. An ignored till product sells no recipe
whatever its name (`record_sales` skips it, and does not list it as
unmatched).

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

### UI conventions

`static/css/marginmate.css` holds the design tokens - colours, a 4px spacing
scale (`--s1`..`--s6`), radii. Use the tokens, not literals, and prefer an
existing class to an inline `style=`:

| Want | Use |
|---|---|
| Buttons beside a page title | `<div class="actions">` inside `.page-header` |
| A line saying what a page is for | `<p class="page-subtitle">` |
| Headline figures | `.stat-row` > `.stat` > `.stat-label` / `.stat-value` / `.stat-note` |
| A table | `<div class="table-wrap"><table data-table data-table-label="factures">` |

**Every table gets search and sorting for free** via `static/js/datatable.js`
— add `data-table` and it grows a search box, a live "12 / 261" count and a
sort button in every header. Three things to remember:

- `data-sort="2026-03-31"` on a `<td>` when the displayed text doesn't sort
  correctly. A date shown as `31/03/2026` sorts as *text* without it, so
  every March lands together regardless of year.
- `data-child-row` on a row that explains the row above it (an expanded
  panel, a "valorisé en X" note). Without it, sorting separates the two.
- `data-table-sort-only` when the page already has its own search — the
  stock list's is server-backed and fuzzy, and a second box filtering the
  same rows by a different rule is worse than none.

`tests/test_ui.py` checks these hold, because all three fail *silently*: the
page still renders, it just quietly stops working the way every other page
does.

**Dates are always `|date:"d/m/Y"`.** `LANGUAGE_CODE` is `en-us`, so an
unformatted date renders "March 31, 2026" in an otherwise French interface.

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
