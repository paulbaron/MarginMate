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

Add `--exclude-tag=browser` for the fast loop: the browser tests drive a
real headless Chrome (`invoices/tests/test_website_scraper_browser.py`,
against a customer portal served from the machine) and take a minute and a
half. They skip themselves where Chrome or its driver is missing.

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
string from every one. A PDF that does carry text (a web shop's invoice) is
read from it and never OCR'd (`ocr.text_layer_pages`): two Nisbets invoices
read as seven characters because the OCR was given their logo, the only
image in the file, for the page - an embedded image is the page only when it
covers it (`ocr.covers_page`). `invoices/ocr.py` stands in for `extract_text()`, and
`parsers/receipt_base.py::ReceiptParser` is the only class allowed to
override `parse()` besides the LLM fallback. The ticket reader still
implements `parse_pages` **only**, so every layout is still testable from
hand-written text with no photo and no OCR engine — `test_parser_contract.py`
enforces that the override lives in the base and nowhere else.

**One reader for every till** (`parsers/generic_receipt.py`). The four shop
parsers were replaced by one that reads a line for what its numbers do; a
shop is data (`TicketShop`: header patterns, the placeholder name its till
prints, whether its items carry a VAT code), registered once per supplier.
Measured against the 368 tickets a person had checked (`eval` against their
stored lines, on a scratch copy of the database), it disagreed on 5 where the
shop parsers disagreed on 21 - each of the 5 a person's shortcut (a quantity
left at 1, a refund typed as a price) or something the photo lost, and said -
and passed every check on 361 tickets against 336.
What it knows, all arithmetic:

- a **count** is the integer that multiplies a unit price into the amount
  ("8 X 1,89 5,67" is 3: the money wins, and "Quantités recalculées" says so);
  a **detail line** ("2x 0.50EUR", "BRUTWEIGHT 0.920 KG / @3.49 / KG")
  belongs to the neighbouring item whose amount it explains, and makes the
  amount of a name standing above it when that faded;
- a **cancelled item** is a negative amount under the same name ("NUL
  LIGNE"): the pair goes. A negative amount under another name is a promotion
  on the item above; one that cancels more than the item cost leaves a refund
  line (quantity -1);
- **the items are the longest run that adds up** - to what was paid, or to a
  pre-discount total printed after them. When the ticket *proves* a promotion
  (its amount printed twice between that total and the amount paid, see
  `printed_promotion`; change printed after the amount paid never counts),
  only the pre-discount total does: two loaves making exactly what was paid on
  a "3 pour 2" ticket are a coincidence;
- a **repair** is tried only when no run adds up, towards a proven total, and
  kept only when exactly one amount changed makes it: an item priced like its
  namesakes (0,45 among loaves at 0,49), or an amount whose leading digit was
  a VAT code ("120.30" for "T2 0.30"). Said under "Montants recalculés";
- a **row printing its own tax** (Monoprix's invoice layout: unit HT, count,
  HT, rate, VAT, TTC) carries its rate and HT, and its TTC is worked out from
  the printed rate when unreadable ("0 63e");
- **rates**: a row's own, else the bucket its code's items add up to, else -
  uncoded items on a two-rate ticket - the one split of the items that makes
  both buckets. Nothing proven: 5.5% (the shops sell food) and "Taux par
  article" fails;
- lines read but left out of the run (a header, a total misread) are listed
  under "Lignes écartées", in case one was an item; a promotion printed under
  an item never goes beyond its price; a phone number is 0 and nine digits
  (the pairs pattern it replaced took "10.49 31.47" for one, and dropped the
  line); a percentage stays in a name ("FROMAGE BLANC 20% MG");
- a **table row** - a DIY store's invoice, a web shop's - is a code, the
  name, an EAN (told by its check digit: kept on the line, out of the name), a
  count, prices and, last, the row's own rate as a plain number ("20,00").
  **Patterns, never names or column positions**: a column missing changes
  nothing. A count can be a **fraction** (0,35 m² of plywood;
  `InvoiceLine.quantity` is a decimal, shown through the `quantity` filter or
  `common.plain_number` - never raw, "4.200"), except beside "kg", where it is
  a weight, and a fraction is never "recounted";
- a **document priced in HT**: the rows making the VAT table's HT base *to
  the cent* are the purchase when their TTC, worked out, is what was paid and
  the other reading is no better (`_prefer_ht` - including when the run making
  the amount paid is the VAT table's own base and tax, read as two lines).
  A VAT row may print its rate as a bare "20,00" (`unmarked_vat_row`: base x
  rate is the tax, and base + tax is printed elsewhere), or no rate at all - a
  web shop's order page printing the goods' value and the tax
  (`untabled_vat`): that stands for a one-rate table only for HT rows printed
  above both lines, and says so ("Taux déduit"). Two items of 10,00 and 2,00
  on a ticket fit the same arithmetic, and must stay TTC at no proven rate;
- **read past a total** none of the items makes (`_segment`): an invoice and
  the till's ticket on one photo, the ticket's half misread. What is found
  further down has to be a table (`_structured`) - after a total, "CB 0,98"
  alone also makes what was paid. Further down, only rows saying what makes
  their amount count, so a charge included in one of them ("Dont éco-part
  DEEE 0,02") no longer breaks the run;
- a line opening on **« dont »** ("of which") prints part of the item above
  and is **never an item** (`INCLUDED_RE`): "Dt Ecopart. unit. EcoMob 0.72"
  under a tool box - Leroy Merlin's till abbreviates it -, "- Dont DDS 0.20"
  at Mr.Bricolage's. Read as items, those eco-participations put four of
  Leroy Merlin's seven tickets wrong: the lines 0,92 € over what was paid, an
  included cent passing within the tolerance (6,00 € for 5,99 €), a shelf
  whose sub-total no longer restated it and came out as the item instead. A
  **sub-total** is stepped over when it restates every item read or **those
  since the last one** (that till prints a sale in blocks, each ending on its
  own) - never a row printing what no sub-total prints (a VAT code, an EAN,
  a count and its unit price), nor a row with no name of its own that
  completes the name printed on the line just above it: a pack of four
  after two packs of two costs what they do, and so can a worktop's cut on
  the store's own reference (no EAN); stepped over, either left the block's
  sub-total to be filed as the item, and every check passed. That row
  prints its reference beside the amount: an amount **alone** under a name
  is still a sub-total, printed on two lines ("SOUS TOTAL" / "9.00") - kept
  as the item « SOUS TOTAL », it made what was paid with the next sale's
  items, silently too. **A size in
  one word** ("76X46", "21X35", "16*25") is no count, unless its two numbers
  multiply into an amount the line prints ("TASSE 2x4 8,00" is two at 4,00:
  `_is_size`, the money decides): read as 76, a rug's name became a detail
  line and its price went with it. Measured on the 627 documents the reader
  reads (scratch copy, 19/09): the seven tickets match their checked lines
  (three before), 470 documents match against 468, and the three no longer
  matching are checked lines keeping an old misreading - a slate board filed
  as "Total" (its ticket prints 2 x 7,95), 16 clamps and 21 battens that are
  a size. The sub-total's row guards and the amount guards change none of
  those readings;
- a **row printing only a product code** takes the name, and the count, of
  the line that named that code above it (`_take_linked_name`): an
  electronics till prints "1  5550001-ENCEINTE PORTABLE XL", then
  "5550001  120,00 €  A  100,00 €  120,00 €" three lines further down;
- a **run that is the document's own base and tax** read as two lines is not
  the purchase (`_is_vat_table`); a **discount printed again in HT** under
  the one already taken is the same discount, and a row whose discount line
  says what it was taken off ("sur 11,76 soit -1,76" under a row of 10,00)
  is already net of it;
- an **amount in brackets that is the one before it excluding tax**
  ("Abonnement 19.99 (16.66)") is that amount printed twice, not two; a
  **date spelled out** ("19 mai 2026") is the document's own, read before the
  day it says it will be debited; and past a total, a run has to say
  something the total does not - a table, or a price and the discount under
  it making what was paid (`_priced_detail`, a phone bill printing its
  totals first and its lines below them). A line whose amount is the
  document's HT base, printed alone or twice, is that total restated, not a
  row: a row has more figures than its amount (`_looks_like_a_row`);
- a **percentage is never an amount**, whatever sign is in front of it;
  "100X35X2.5" is a size, not a count of 100; a **quantity column of one**
  between a price and the amount it makes is a count; and a number in front
  of a name is a count when the ticket says how many articles it sold
  ("3 ARTICLE(S)", `_count_from_articles`). A count in front of the name
  stays **out of the name** (`_without_leading_count`), or the same
  champagne bought by six and by twelve is two products that never meet -
  as a whole number only: cut as a prefix, a count of 2 took the first digit
  off an article number ("2000123"), on 19 rows of Metro's invoices when the
  ticket reader reads them;
- a **row printing its price and its amount both ways**, HT and TTC, with no
  tax column ("18  4,50  5,40  81,00  97,20") is read by `_ht_ttc_row`:
  nothing on it adds up, so what proves it is one French rate turning both
  prices into both amounts and a whole number of them making the amount -
  in HT only, since a unit price is rounded before it is multiplied (24
  bottles at 5,80 TTC print 139,26, not 139,20). Without it those eighteen
  bottles came out as one at 81,00, and the unit cost is what every value
  downstream is divided by;
- a **bucket of VAT covers the whole document**, so between two readings of
  one rate the larger base is the table's (`_better_bucket`): a water bill
  whose every row prints its own tax states its real table fifty lines
  below, and the subscription's row - read first - made its 27,42 € the
  260,63 € the bill charges;
- a **tax printed beside its rate** ("TVA [20.00%]  2.16") with the value it
  taxes and their sum printed too is a total, each printed once
  (`_taxed_total`): the rate is what makes one printing enough, where
  `_ht_and_tax` searches without one and asks for each figure twice;
- **thousands are grouped**: by a space ("1 011,00"), by a point
  ("1.162,80") or, in English, by a comma ("1,162.80") - one amount, not
  eleven euros and not none at all (`UNITS`: groups of exactly three digits,
  and a point or comma only when the *other* one is the decimal separator,
  or "2.261" and two amounts in neighbouring columns would read as one);
- a **date may be written month first** ("août 03, 2026"), abbreviated
  ("déc. 02, 2025") or in English ("Dec 02, 2024"): a platform billing in
  French dates its invoices in whichever language its template was written
  in, and 26 documents were filed with no date at all - counting in no stock
  valuation and matching no payment. The first date the document prints is
  still its own, so the next billing date below it names nothing;
- a **line holding a date is an item all the same** when it prints amounts:
  an invoice's rows carry the period they cover ("Abonnement 01/08/2026au
  31/08/2026 1,00 64,44 20,00% 64,44"), and thrown away for its date, one
  left the subscription unread with the bill's own "Total hors TVA" standing
  in for it. A line stamped with an **hour** is never an item: a till stamps
  the hour and nothing it sells carries one;
- a **document's number** is the one it prints for itself ("N° document",
  "Commande N°", "Référence interne", "Facture # FR-F0001", "Nº" with an
  ordinal indicator: `DOCUMENT_NUMBER_RES`) and **never an IBAN**, which is a
  long digit run once its spaces are gone and the same one on every invoice a
  supplier sends - read as the number, the second invoice of the year was
  refused as a duplicate of the first. A dash a PDF's own rules leave inside
  a number or a month ("FR-F033—763", "avr—. 26, 2024") is taken back out.
  A number **printed with its date** ("N° 2026100000001 DU 10 AVRIL 2026",
  "n°1400000001 du 19 Janvier 2024") is the document's own, whatever lines
  stand between it and the word « facture »: looked for beside that word
  only, Eau de Paris' and the Freebox bills were filed under a payment
  reference or a number made up from the date and the total - and the
  portal's list, printing the real number, never recognised an invoice
  already imported. `manage.py refresh_document_numbers --dry-run` puts the
  printed number in place of such a stand-in (a date-total or a long digit
  run, never a real number, never one another document of the supplier
  holds, never a supplier with its own reader): 139 documents on 19/09.

Those rules replaced two parsers: measured on every De Poivre and Plou &
Fils invoice filed, the one reader reproduces them line for line - names,
counts, unit prices, rates, dates and numbers - so `parsers/depoivre.py` and
`parsers/ploufils.py` are gone (migration `invoices/0024`, which forgets
their keys) and their layouts are fixtures in `test_generic_documents.py`.
Metro's and UBA's stay: theirs are not one table.

Evaluate a change the same way before trusting it: parse every stored
`ocr_text` and compare with the checked lines, per shop, counting separately
the tickets whose only difference is how a promotion was spread.

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
- **5.5% is a fallback, never an answer.** Two of the 42 are at 20%
  (cleaning vinegar at Franprix, a discounted line at Monoprix). Reading those
  at the food rate understates the cost by 14% with nothing downstream able
  to tell - so a line priced at 5.5% because nothing proved a rate always
  fails "Taux par article". `read_rate` rejects any percentage France does
  not have — OCR reads the VAT *amount* "0,26" as a rate of 26% given the
  chance.
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
  promotion makes bread cheaper rather than shaving centimes off the lemons -
  and over every reading of that product ("BAGUETTE BLAND", "BLAVC"): matched
  to the one spelled like the block, a 0,50 "3 pour 2" made one 0,49 loaf
  cost -0,01. Never more than those products cost; cents go to the largest
  remainders. The line keeps its printed price, the share sits beside it
  (`discount_ttc`).
- **A Franprix weight belongs to the item BELOW it.** "BRUTWEIGHT 0.920 KG
  @ 3.49 / KG" is the orange's (0.920 x 3.49 = 3.21, the orange's price). The
  parser used to put the kilos on the item above, silently. Arithmetic
  decides; with no legible price per kilo, the item below.
- **The tills disagree about their own VAT tables.** Four shops, five
  layouts, and Sabbh's "Base TVA" column is tax-INCLUSIVE where everyone
  else's is exclusive. `parse_vat_line` settles it by arithmetic — it
  searches the row's plausible readings for the pair that satisfies a VAT
  identity *and* reproduces the printed grand total. Franprix draws its
  table with rules the recogniser reads as digits ("2.261" for "| 2,26 |"),
  and only the grand total separates that from Monoprix's genuinely
  4-decimal HT ("3.0237"). Both identities are tried on every pair: at 2,80
  and 0,15, both hold, and trying the tax-exclusive one alone left five
  one-line Sabbh tickets without a total.

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

### A supplier of charges has no products

A subscription, a rent, a water bill: there is no product behind them and
nothing to classify, so `Supplier.expenses_only` (switched from the
supplier's page, « changer… », behind a confirmation saying what it redoes -
`importing.redo_as_expenses`, reading each document again from the text it
kept; a box on the suppliers' list, then on the Sources tab, saved on a
click, and a POST from a page still showing it now gets the confirmation)
files them by
`importing.charge_reading` instead of resolving products. Its lines land on
`Product.is_expense` products, which reach no stock page, no review queue
and no stock movement; **Produits & charges** shows what they cost in a fold of its
own ("Charges et abonnements", `inventory.views.charge_suppliers`, over the
stock-take window or the last twelve months; outside a period the headline
also gives their twelve months as « Charges (TTC) », beside « Total
acheté ») - charges are not stock, but they are spending.

**`Product.is_expense` follows its supplier**, both ways
(`replace_invoice_lines`, `importing.stop_expenses` when the box is unticked,
`redo_as_expenses` when it is ticked again - not a product a stock item has
claimed, which is stock after all). Set one way only, a box ticked by mistake
was irreversible: unticked, the supplier's products stayed flagged for ever -
out of the review queue, out of every stock page, out of every stock movement -
and correcting the document by hand, which is what the message after unticking
asks for, resolved the very same flagged product.

Three readings, in this order: **the VAT table** when it accounts for the
total to the cent (one line per rate); **the postes the document names**
(`invoices/charges.py`); **the total alone**, on one line named after the
supplier. What was paid is never the sum of whatever was read as lines - a
rent statement lists the previous balance, the direct debit, the tax and the
rent, and adding those up gives a figure nobody ever paid. A charge whose
total was not read at all is filed with what was read and held in "À
vérifier" (`charge_needs_a_look`): it must not pass for settled.

**Every path that reads a document again goes through
`importing.refile_as_charge`** (`receipts.reread_receipt`, "Relire le
document" for a ticket and for an invoice): read as a ticket, a rent
statement's lines are the previous balance and the direct debit beside the
rent, and a document relu that way went back to being worth what it was
before the charge reading settled it. A charge also **never enters the
review queue** (`receipts.pending_receipts`): there is nothing to type on a
rent, and forty-two of them behind the tickets is a queue nobody works
through - a total that could not be read holds the document in "À vérifier"
with what is wrong written on it instead.

**A charge keeps its own checks** ("Total de la charge", "Date du ticket":
`importing.charge_state`). A charge fetched by a portal or the mailbox goes
through the ticket reader first, and `import_receipt` stored that reader's
checks over the ones the charge reading had just set - about lines the
charge reading replaced: Eau de Paris' bills, filed at exactly what they
charge, waited in « À vérifier » under « 5.5 % supposé » and « lignes
310,15 € HT / ticket 303,28 € ». `manage.py refresh_charge_checks
--dry-run` gives the documents filed before that their own checks back
(never one a person validated): 36 on 19/09.

**A charge keeps the amount it charges, tax included** (`InvoiceLine.printed_ttc`,
set by `_expense_line` from the figures the document prints): 33,33 € HT at
20% works back out to 40,00 € where the bill says 39,99 €, and 39,99 € is
what leaves the bank. Everything that shows what a charge cost adds those
up, `inventory.views.charge_suppliers` included.

**A credit on a charge is a line like any other**: a count of 1 and a
negative amount (`LineCorrectionForm`, `charge=`). An electricity bill takes
the month's subscription back at 5,5 % and bills it again at 20 %, printing
-2,76 € HT at 5,5 %; the correction page refused it ("Un retour a une
quantité et un montant négatifs" - the guard against stock worth less than
nothing, and a charge has no stock, bar the one below), so that bill could
not be entered at all - nor two rent statements saved untouched, whose deposit given back the
charge reading files the same way (-3,00 at a count of 1). The count stays
the one typed: nothing reads a charge's count, and at -1 its unit price would
read as a charge. A count of -1 at a positive amount is still refused (the
count says credited, the money charged), and goods keep the guard. Every
total adds the credit with its sign - `Invoice.total_ttc`, the bank match,
the charges fold and its rows - and HT and TTC convert as for a positive
amount, half away from zero. Validated on the page, a charge takes its state
and its own checks from its total, the one just typed included
(`charge_state`, in `views._save_corrections`): kept from the import, "Total
de la charge" went on saying a total typed there was never read, and a
charge saved with none came out settled. The ticket reader still drops the
minus of a VAT row (`amount_candidates` is unsigned): that bill was filed at
+2,76 € on its 5,5 % row, its total unread, and waits for the credit to be
typed.

**Where there is stock, the guard stands** - on a charge too. A product a
stock item claimed stays one when its supplier turns to charges
(`redo_as_expenses`: it is stock after all), and a line on it books a
movement like any line of goods; relaxed on every row of a charge, a credit
typed there was stock at -30 € the unit. So a charge takes a credit at a
count of 1 only while none of its supplier's products is stock
(`views._correction_page`) - the supplier's products rather than the stored
line's, since a row lands on a product by its name once saved, and a row
added or renamed onto the stock item is that item (no such supplier on
19/09). A supplier **leaving charges** turns such credits into returns
(`importing.stop_expenses`, counted on the confirmation page by
`charge_credits`), and so does a document moved from charges to a
supplier of goods (`receipts.move_documents`; both through
`credit_as_return`): the count negative, the amount as it was. Kept at 1, the
goods guard refused the row on a document saved untouched, and classifying
its poste booked stock at a negative unit cost - on 19/09, the three
deposits given back on two rent statements.

**Every charge opens like a stock item.** The charges fold lives inside
`#catalogue`, so the page's own toggle script reaches it: a row opens on the
documents behind it (each linking to the document it came from, with its
"Corriger") and its 📈 shows what it has cost over time (the same chart a
stock item's price history draws, one point per document, against the
document's own date). The rent is followed month after month like anything
else bought.

**The supplier's own row is one of them.** Only the postes opened at first,
and a poste is listed only where a document names several - so the water,
the phone, the alarm, the venue (one poste each, the charge itself under
another name) had nothing at all to click: five suppliers out of six. The
supplier's row opens on all its documents
(`inventory.charge_supplier_documents` / `charge_supplier_history`), a poste's
on that poste's (`charge_documents` / `charge_history`), and both go through
`_charge_document_rows`, which is **one row per document**: a bill printing
two rates is read as two lines, and 29 Total Energie bills showed up as 46.
It prints the tax as an **amount**, never as a rate - on a document with two
of them, naming one would be a lie about the other, and "—" says nothing.

Two things the row and the panel do not share, said on the page rather than
left to be discovered: what a row **opens** is the whole history, where its
Documents and Total are the window being looked at; and **"Dernier" is the
last document ever**, window or not. A supplier is listed as soon as it has
a document at all - windowed, a water bill arriving twice a year dropped off
the page between two of them, taking its history with it, and the date is
exactly what a row with nothing over the window has left to say.

Three traps a review found on the charges fold, each a test that failed
first: the first stock take's window has no start (a None in the filter was
a 500), a curve adds up what one day charged, and a document filed with
nothing read is listed on its supplier's row.

**A poste is a label and the amount printed after it**, and a document's
postes are the run of them adding up to an amount printed below them. That
run is what proves the reading *and* settles the total: a statement puts two
columns on one line (the account's history left, this month's postes right),
so only each line's **last** amount is this month's; a subtotal printed
among them is the run restated and is stepped over; a minus on its own in
front of an amount is its sign; and the tax poste - the one that is a French
rate of exactly one other - is folded into the poste it taxes rather than
kept as one. Measured on the 31 rent statements filed, that separates the
rent from the building and water provisions and corrects six totals the
reader had taken from the left column (last month's échéance, printed twice,
is bigger than this month's). **A document is worth what it charges**: where
the debit also settles arrears, those were charged on the avis they come
from, and counting them again would book them twice.

A breakdown is a **block**, and its total is printed at the foot of it
(`MAX_LINES_BETWEEN`): two rows of a consumption table pages apart that add
up to something printed elsewhere are not one, and an electricity bill read
that way turned its 298,05 € into the 67,94 € of its network charges. The
document's **VAT table comes first** all the same (`charge_reading`): where
it accounts for the total to the cent, that is the reading, and the postes
are only asked when it does not.

**One import for every document.** Tickets and PDF invoices went in through
two cards, and the person importing had to know which; the Achats page has
one now (files or a whole folder, of anything), and the file decides
(`receipts.import_document`): a photo or a scan is read as a ticket, a
digital document goes through its supplier's own reader when that supplier
has one (`has_own_reader`) and the document says whose it is - otherwise the
ticket reader, which reads an invoice's table too. Nothing is guessed from
the file's name or extension: `ocr.text_layer_pages` says whether it carries
text, and `detect_shop` who printed it. The import reports each file as what
it became, and links a ticket to its review screen, an invoice to its lines.
`/invoices/upload/` still takes one PDF with its supplier named by hand -
folded under the import card, for a document that says nothing about its
sender, or for the AI pseudo-supplier.

**The review screen is the deliverable, not the parser.** The import card
takes a batch (`/invoices/tickets/`) and detects each shop from its own
header (a Franprix ticket run through the Monoprix parser *would* produce
lines, and they would be wrong — an unrecognised file is reported, never
guessed). `.../verification/` is the queue tab, oldest first;
`.../<pk>/verifier/` puts the photo beside the checks and the editable lines
and moves to the next receipt on save (a ticket already checked, reopened
from its page, goes back to its page). With `?lot=<batch>` it goes through
that import's tickets only and ends on the import; the whole queue ends on the
list of tickets checked recently. Saving goes through `replace_invoice_lines`,
the same path as a hand-typed invoice.

**One correction page for tickets and invoices** (`views._correction_page`,
`document_review.html`): `<pk>/lignes/` is the same page for a supplier
invoice, its PDF framed beside the lines (media may be framed by this site
only, `config/urls.py`); a ticket's `lignes/` redirects to its review URL. Each
row shows the amount **both ways, HT and TTC**, each following the other as it
is typed through the line's rate; the one typed last (`amount_source`) is kept
and the other worked out - checking an HT price against a photo meant
converting every one in one's head. A ticket starts from its printed TTC, an
invoice from its HT. A ticket's **promotion sits beside its price**
(`discount_ttc`), and the live check says "articles X € moins Y € de
remises", which is what the ticket prints as its pre-discount total. The
weight is a field (`total_volume`), and a row of several says what one costs
(`LineCorrectionForm.unit_price_hint`, redone by the page as typed): five
baguettes at 2,45 read like one at 2,45 until divided. A row left as drawn keeps every stored
figure to the cent (`LineCorrectionForm.untouched`) - so a line kept from
before promotions were kept apart stays worked out from HT - and a TTC typed
converts back to its HT to the cent. New ticket lines start at 5.5%, invoice
lines at 20%. **A line taken out stays where it was**, struck through, with a
button to put it back and a count of the lines kept: removed at once, the rows
below moved up under the pointer, and a repeated click silently took out the
first line typed below - a ticket re-typed in full then "did not add up" by
exactly that line. Build test posts from the page (`invoices/tests/page_posts.py`):
a hand-written subset tests a request no browser sends, and several such tests
passed without ever saving.

**A check compares two things, and both have to be on the page.** The lines
and the printed total were; the VAT table the document prints was not, so
"TVA 5,5% cohérente", "Table TVA lue" and "Somme HT des lignes = base HT du
ticket" were warnings nobody could answer - 21 of them still standing on
tickets checked long ago. The table is stored (`Invoice.vat_breakdown`, from
`ParsedInvoice.vat_breakdown` at import) and **typed on the review screen**
beside the total, a row per rate; validating rebuilds every check from what
the page holds (`receipts.recheck_after_review`, `vat_table_checks`). An
empty table asks nothing - a document that prints none is not wrong, and a
failure nobody asked for is the noise this page exists to avoid - and a row
typed half way is refused rather than half read. On the real data that took
21 unanswerable failures to 6, every one of them pointing at a field: three
tickets whose lines and printed table genuinely disagree (one by 1,25 €),
two whose lines miss the total, one charge whose total was never read.

**The HT check follows the lines as they stand, like the sum**
(`views._checks_context`, `ht_check`). Validating did rebuild it from the
lines as saved, but under the label the reading writes too, and the page
marked any check carrying a `READING_CHECKS` label "(à la lecture du
ticket)": all 445 documents holding it on 19/09 - three tickets whose
reading failed it passing under that mark once corrected (two Leroy Merlin,
read with each item's eco-participation as an item), and the three still
wrong blamed on the reading. Nor did it move while lines were typed. The
page now works it out from its lines and its VAT table, and the script
follows both as they are typed (each line after its promotion, a cent of
slack a line, as `vat_table_checks`). With no table it is hidden, and a stored one can only
be the reading's - validating without a table drops it - so that one stays
marked as read until a table is typed. What stays "as read" after
validating is about the reading, not the lines: "Confiance OCR", the shop
checks, an unread total nobody typed. It is a **ticket's** (a charge's
too, opened as one): a supplier's invoice never showed nor stored it, and
its lines leave out what the reconciliation adds
(`Invoice.reconciliation_adjustment`, a duty) where the printed base counts
it - a table typed there exactly as printed failed it, on 68 of the 84
invoices carrying one.

The page also **reads the document again** ("Relire le document",
`receipts.reread_document`): the photo through OCR, or the PDF through its
supplier's parser, replacing date, total and lines - corrections included, the
page asks first - and a ticket goes back to the queue. Nothing changes when
there is no file, no reader, or nothing read, and a line a stock take was
priced from stops it. A checked ticket can be put back in the queue ("Remettre
à vérifier"), and a known price forgotten ("Oublier"; the lines it named keep
their name).

**Every document is dated between 2000 and today** (`forms.check_document_date`,
on this page and on a hand-typed invoice). An import still files an undated
document - there is no one to ask - but a ticket gets a failed "Date du ticket"
check (so it waits in the queue) and a PDF an `error_message`; the invoice list
counts them and lists them (`?sans_date=1`).

**A receipt line keeps its printed TTC** (`InvoiceLine.printed_ttc`, set by
the receipt parsers and by this form). HT to the cent does not convert back:
7,00 at 5.5% is 6,64 HT, which is 7,01 - ten pitas at 0,70 read 7,01 on the
screen meant to check them. `InvoiceLine.total_ttc` and everything shown in
TTC use it, less the line's promotion (`discount_ttc`). A receipt line with no
printed amount - imported before promotions were kept apart - is worked out
from HT, and saved untouched it stays so: passed off as printed, one Franprix
ticket validated that way came to 7,91 for 7,92 paid. `Invoice.total_ttc`, when
every line has a printed amount, is their sum
(never plus the adjustment, which in HT puts back cents the printed amounts
never lost: added on top, six 0,49 baguettes came to 2,97) - or the ticket's
own printed total (`Invoice.printed_total_ttc`, what was paid) when the lines
are within the parser's tolerance of it, so a cent the OCR misread never costs
the bank match. Otherwise the whole invoice stays on the HT arithmetic.
Receipts imported before these fields got them back from their stored reading
(`manage.py restore_printed_ttc`, matched by count, rate and HT, never by
name, and never from a promoted reading, whose printed amount is before the
promotion; `--dry-run` first after a parser change): 34 totals a cent or two
off became the printed one, none went the other way.

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

**An unrecognised ticket is not a dead end - the reader works for any shop.**
A torn or faded header, or a shop nothing was set up for, is enough for
`detect_parser` to find nothing. The batch raises `UnrecognisedShopError` for
that - a plain `ValueError` is a broken file, reported as an error - keeps the
file (`"kept"` on the entry), shows what the ticket reads as (the line that
looks like its name, date, total: `receipts.first_reading`) and, once the
batch has finished, offers the suppliers on that row - or a **new shop**
(`receipts.create_shop`), named and given the text its tickets print at the
top (`Supplier.ticket_header`). The ticket is read whatever the shop:
`receipts.parser_for` gives every supplier but the AI pseudo-supplier a
reader, the configured till's or the same reader without settings (a Metro
paper ticket included). **The header is given from the review screen, not from the import card**: the
card is filled in before anyone has seen the document, so it asks for a name
only, and the review page - the photo beside it - has the box, filled in with
what the document seems to print (`header_guess`) and with its own top lines
offered as chips (`header_choices`, `set_shop_header`, refusals as
`create_shop`'s). Giving it sends every recent import's unrecognised files
through again (`requeue_everywhere`), and
`detect_parser` looks for headers people gave **before** the configured tills,
a header printed inside another giving way to it: "EPICERIE SABAH" before the
"SABAH" Sabbh's till answers to. Headers compare without accents, case or
punctuation, as whole words (`receipts.prints_header`, the one definition),
and one shorter than four characters, one another supplier already has, or
one already printed on the tickets of two other shops or on more than three,
is refused - a few tickets of one shop carrying it are more likely the new
shop's, filed before it existed, and are named so they can be moved, each
from its own page (`views._say_new_shop`, `_set_shop_header`). The chips
offered are only those the save would take (`offerable_headers`, over the
documents read once, `document_corpus`): the customer's own street is on
every supplier's documents, and a line with no space before the length a
header may have is not offered at all - cut, it matched nothing. A document filed under the
wrong supplier is moved from its own page ("Changer d'enseigne" on a ticket,
"Changer de fournisseur" on an invoice, `receipts.move_to_shop`): its lines
stay and find their products among the new supplier's, the orphans go. A
digital invoice gets no check added by the move - a check is what makes a
document a receipt (`Invoice.is_receipt`) - and seven invoices had no way
back at all before that page offered it to them. A file waiting for its shop is counted apart
("À ranger", `ReceiptBatch.awaiting_shop_count`), not as a failure:
`failed_count` is the errors plus the unrecognised files no longer kept.

**A shop is not a supplier with invoices** (`parsers.is_ticket_shop`): a
configured till, or any supplier with no PDF parser of its own. Only a shop's
products match OCR readings tolerantly and can be renamed from a ticket;
Metro's are named by its invoices.

The shop choice is `import_with_shop`, **while the batch still runs** too - a
folder of a hundred tickets used to have to finish before the one without a
shop could be checked. So `results` has three writers (the thread, a shop
choice, a new shop's re-read) and each takes `receipt_batches.RESULTS_LOCK`,
reads the entries fresh and writes back only its own: the thread used to save
the copy it started with after every file, which would have undone a choice
made meanwhile. The thread takes the next pending file each time round, so a
file sent back to "pending" while it runs is read by the same run; the end of
the batch is written under the lock with the check for pending files, so a
re-read a moment later starts it again instead of being lost. A file being
imported by hand is left out of re-reads (`_BY_HAND`, in memory), and
`append_log` appends in the database. The live part is fetched every second:
its shop forms keep what was typed (`hx-preserve`, stable ids) and the polling
waits while one has the focus (`shopChoiceInUse`, ui.js) - a swap takes the
focus away. The OCR runs in the request, so shop choices and "Relire le
document" take turns on one lock (`receipts.OCR_LOCK`) - two tabs used to
import the same file twice.

**A shop is recognised by what its documents print, header or not**
(`invoices/identifiers.py`, `receipts.identified_supplier`): a SIREN (alone
where "SIREN"/"RCS" names it, in a SIRET, in a VAT number whose key matches),
a phone number (same separator between every pair: "01.23 45.67 89.00" is
prices), a web site (never an e-mail's domain - a customer's address is on
invoices too). Checked the way each is built, since a misread one must not
name a shop. Stored on `Supplier.ticket_identifiers` and learned only when a
person said whose a document is - a shop chosen, a ticket moved (the old shop
forgets what it printed), a ticket checked on the review page, a digital
invoice imported through its supplier's own reader (`learn_identifiers`;
`manage.py learn_shop_identifiers --dry-run` for the documents filed before).
A digital document's text is kept for this (`Invoice.source_text`; only
`ocr_text` makes a document a receipt), which is what makes **the customer's
own SIREN** - printed on every supplier's invoice - name nobody: the command
reads the PDFs already filed before learning anything. Measured on the real
tickets, these rules keep it honest (`identifiers_naming`, `still_naming`,
arithmetic with no database):

- a **new** identifier is learned when **a quarter of the supplier's
  documents print it** - or, for a supplier with a header, a quarter of
  those **not** printing the header, two at least (misreadings -
  "mmoprix.fr", "monoprii.fr" - and labels on some goods - "fsc.org" on
  Mr.Bricolage's wood - are on one ticket or two), and **no other
  supplier's documents print it** (the customer's own phone);
- one it **knows** is dropped only for a reason about that identifier: another
  supplier's documents print it, or none of its own do any more. Measured
  again against a quarter of ALL its documents, Free's mobile figures (7 bills
  among 37) went on 18/09 - silently, on a correction validated - once thirty
  box bills had been filed beside them by the header;
- a figure a person set aside (« Retirer », `Supplier.refused_identifiers`)
  is never learned again;
- **a web site alone names no one** (the one branding the goods is printed
  at every shop selling them).

It comes after the headers people gave and the configured tills, and says so
("Enseigne reconnue"). One identifier learned by two suppliers names
neither. Every document's figures are read once and kept by their text
(`identifiers._document_identifiers`, `may_print`): a supplier's page reads
all 880 documents, and reading them again made it half a second.

**Every change of what names a supplier is recorded** (`SupplierChange`,
`supplier_changes.py`): its name, header, identifiers, nature, a
type moved, its first document - with the cause of the act it came from (a
view or a gather says what it is doing around the call, `with
cause("validation de …", invoice=…, by_person=True)`; nothing set is
"automatique") and what an undo needs. `receipts.set_identifiers` is the one
writer of `ticket_identifiers`, and a contract test says so: the 18/09 loss
left no trace anywhere. A figure dropped that nobody asked to drop is
`needs_review` until someone has seen it; `collect()` hands the changes of
an act back, and the message, the batch log or the gather log says them.

Two more rules came from seven Free invoices filed under UBA, on a mobile
number both print - **the customer's own**, learned while UBA was the only
supplier printing it. A document that prints a **company number nobody
knows** is not the supplier whose phone or web site it also prints
(`identified_supplier`): a SIREN is what a company is, a phone is where
someone answers. And a **supplier's own reader** - which turns a whole
document into lines - runs only for a supplier named by its company number
or by the text it prints at the top (`document_supplier`); read as a ticket,
a document is checked against its own totals, so that path can be less
strict. What a document moved away from a supplier printed is forgotten by
it (`move_to_shop`), which is what unlearns a number that named it wrongly.
(The seven themselves were deleted and imported again under Free; UBA lost
the number at the `learn_shop_identifiers` pass that followed, once Free's
documents printed it too. Nothing names the customer's number because two
suppliers' documents print it - no list of "my numbers" is kept anywhere.)

**A header only adds documents.** It says "a document printing this is
this supplier's", never "a document not printing it is someone else's": a
torn or faded top is exactly what the learned identifiers are for. So
giving Free the text of its box subscription ("Abonnement Freebox Pop")
left its seven mobile bills where they were - filed by the company number
and web sites Free had learned from them. Saving a header now says how many
of the supplier's documents do not print it, what filed them there, and
that one not its own is moved from its own page (`views._say_headerless`).
Giving a header never takes a second subscription's documents out of a
supplier: two subscriptions are two suppliers (next paragraph).

**One source, one supplier.** A source (`InvoiceType`) files what it fetches
under its supplier, whatever it prints (below), so two sources of one company -
Free's box and its mobile line - are two suppliers from the start, each
learning what its own documents print. Filed as one, they needed a page to
take one subscription back out: a split, chosen, previewed inside a
rolled-back transaction, confirmed against a fingerprint of everything it
was worked out from, undone from either side's history - all for a case
that only exists when a source is filed under another's supplier. The
owner removed it on 19/09 as unneeded, its history kind (`SPLIT`) with it:
no split had ever been done. The one real case was fixed the same day by a
data operation. The owner had created Free Mobile and moved the mobile
portal's source to it from the source form (`TYPES`) - left behind, a source
files what it fetches back under Free; then Free Mobile's seven bills,
filed under Free by its SIREN and web sites, went over together through
`move_documents`. Free Mobile learned its SIREN and web sites but not the
customer's phone (UBA's documents print it too), Free kept its header
and its support site, every box bill is recognised as Free and every
mobile bill as Free Mobile, and no other document moved. A second
subscription found under one supplier later is the same fix: a supplier of
its own, its source moved to it, then its documents.

**Documents of one source move together** (`receipts.move_documents`, the
one definition of a move; `move_to_shop` is it for one document, and a
page only ever moves one - a group move is a data operation). Every
document first, then each supplier left forgets what they print and checks
what it still knows, then the destination learns **once**, from all of
them. Moved one at a time (`move_to_shop`), the first of the seven mobile
bills taught its new supplier nothing: the siblings left behind printed the
same SIREN and web site, so nothing was the new supplier's alone, and the
next mobile bill came back unrecognised - the new supplier learns them only
once the last has left. All or nothing: two of them with one number, or a
number the destination already has, and none moves. What both sides print
(the customer's own number) names neither; where two contracts of one
company print the same company number too (two meters, two sites), only a
header tells them apart.

What a move keeps, each a test that failed first:

- **Learning corrects the others** (`learn_identifiers` re-checks every
  supplier holding something the documents print, `_recheck`): one that
  had learned the customer's company number while it was the only one
  printing it refused every bill of another, header and all, until someone
  filed one of its own; and a third supplier sharing a number with the one
  the documents left was left its only owner.
- **What the lines do not say** (`move_documents`): a document read as
  goods and moved into a supplier of charges is read again as a charge
  (`refile_as_charge`) - its previous balance, direct debit and rent had
  become three postes, three times what it charges; a charge's state is its
  total's (`charge_state`) - an unread total came out COMPLETE and left
  "À corriger"; and a classified line's product at the new supplier takes
  the same stock item (`link_product_to_stock_type`) - re-resolved, the
  purchase silently left the stock ledger. Between two suppliers of
  charges, a charge line named after the supplier it leaves takes the new
  name; a new supplier made from a charge's page is charges (the move
  form's box starts ticked).

Two guards **ask rather than choose** (`recognise_shop`, the reason travels
to the import's message): the headers of **two suppliers** on one document,
neither inside the other - the longest used to win, and a mobile bill
advertising the box would have gone to the box - and **a header against a
company number** another supplier learned. A header still beats another
supplier's phone or web site. Achats' « Enseignes et fournisseurs » tab
(`supplier_list`, `workspace._suppliers`) lists every supplier with what
names it and the sources fetching for it, those with a reader of their own
too (UBA, Metro), and how many of a shop's documents print its header; a
row opens its page.

**A supplier has a page of its own** (`supplier_views.py`,
`/invoices/fournisseurs/<pk>/`): what files its documents under it (header,
identifiers retained - « Retirer » -, printed but not retained with why -
« Retenir » when the rule would keep it -, set aside - « Ne plus
l'écarter »), its sources (« + Nouvelle source pour X »; Metro has none, and
says its own module fetches it - `workspace.OWN_MODULE`, what the gather
card lists it by), its history with
an undo per change, and « Modifier » and « Supprimer… », the latter disabled
with the reason when it cannot be (a till, a reader of its own, documents
filed, a source fetching for it). It leads back to « ← Achats · Enseignes et
fournisseurs ». Every action answers where it was taken and is recorded; an undo is
recorded too, marked with what it undoes (`data["undoes"]`), and has no
undo of its own - offered one, it redid the change in one click, nothing
shown first; posted by hand, it is refused. A value the page did not offer
is a message, never a 500, and no GET writes. Two suppliers never share a
name whatever its case, accented capitals included (`supplier_named`:
SQLite's case-blind comparison is ASCII only).

- **Created before its first document** (`supplier_create`, « + Nouveau
  fournisseur » on « Enseignes et fournisseurs », or « + Nouveau
  fournisseur… » in the source form, saved with the source in one
  transaction - a source refused leaves no supplier). A new source had to
  name an existing supplier, and one never sent a document did not exist.
  Where its invoices will come from sends the next page: the source form,
  filled in (`?fournisseur=&source=&retour=`).
  Its creation is undone by deleting it, while nothing rests on it.
- **Modified after a look** (`supplier_edit`): « Vérifier les changements »
  says what a rename takes along (a supplier of charges: the lines and the
  poste named after it, `rename_supplier` - never to the name of another of
  its postes; the code never changes, bank matching keeps the payee names it
  learned) and what a header does (printed
  on how many of its documents, on how many of others', refused by
  `check_header`) - nothing saved; the save applies only if the supplier is
  as it was checked. One with no documents saves in one step.
- **Deleted only when empty** (`supplier_delete`): no document, no source;
  its unused products, known prices, payee names and history go with it,
  said before.
- **Its first document** is recorded, to be seen (`FIRST_DOCUMENT`,
  `needs_review`): what it taught, or what it prints that was not retained
  (one recognised by its header teaches nothing). Nothing else vouches for
  a first reading. Suppliers waiting for theirs come first in every import
  choice (`WAITING_GROUP`).
- **A source files what it fetches under its supplier, whatever it prints -
  and a document printing what names another supplier teaches nothing**
  (`type_supplier_doubt`, `by_type`: another supplier's header, till or
  learned figures - or the company numbers of others printed beside this
  one's own, every one of them: `_companies_of_others`).
  The doubt is kept on the document (`Invoice.supplier_doubt`), not as a
  check: reading it again, or as a charge, rewrote the checks and the doubt
  went with them. The document waits (the ticket queue while unchecked,
  "À corriger" otherwise, `DOCUMENT_TO_FIX`), says why on its page and its
  row, is **nobody's** to learning (`_stored_texts` leaves it out - counted
  among everybody else's, it made the other supplier forget its own
  number the next time it learned; `learn_shop_identifiers` too), and is
  answered by a person: validated on its page (it then teaches, a digital
  invoice included) or moved (`move_documents`). It is not its supplier's
  first document either: the next one, which teaches, is. A source moved to another
  supplier is recorded on both and given back from either (`TYPES`, « Rendre
  cette source à X »); the documents it already fetched stay where they were
  filed, and the message says to change their supplier from their own pages
  if they are the new one's. A source's page drawn before it moved (a
  « Rendre » in another tab, or the source saved from another tab) does not
  move it back unseen
  (`supplier_was`). Enter in one of its fields saves: « Tester », the
  form's first button, signed in on a portal.

**A PDF invoice from a supplier with no reader of its own** - or a new one,
named in the import card ("+ Nouveau fournisseur…", `InvoiceUploadForm` is a
`ReceiptShopForm`) - is read the way a ticket is (`import_receipt` with the
supplier) and opens on the correction page beside its PDF; the supplier learns
what it prints. A supplier with its own reader (Metro, UBA...) keeps it, and a
digital invoice dropped among ticket photos and filed by hand under one goes
through it (`receipts.import_invoice_pdf`, when the file has a text layer; a
scan is read as a ticket whatever the supplier). The AI pseudo-supplier is
still offered there, last. A reader that fails or reads nothing (or the AI
pseudo-supplier, which has none) still files the ticket, empty, with a failed
"Lecture automatique" check - that check is also what puts it in the review
queue, which lists only receipts with checks. Either way the operator lands on
the review screen, which also takes the ticket's date and total (a blank
total keeps the one read: it is a field nobody filled in, not a total
removed).

A ticket number of four digits or fewer ("Ticket no 4278") is the till's count
of the day: it comes round, so it is stored with the date, or a later ticket
was refused as a duplicate.

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
an error under a field nobody touched. `forms.line_initial` quantizes it, and
writes a weight as 4.184 or 10, never "10.000" or the "1E+1"
`Decimal.normalize()` makes of it.

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
reason. Renaming, moving, reading again and the price list reload the page, so
the page asks before dropping line corrections not yet saved (and Enter in a
line moves to the next box instead of validating the document). The price
list is folded away where the till prints names and none was started. A price already
known is refused by the form (the shop is not a form field, so Django never
checked the uniqueness it is part of, and the database answered with a 500);
a wrong one is forgotten from the list under it.

### Deleting an invoice (`invoices/deletion.py`)

Deleting the row alone is wrong twice over. `StockMovement.invoice_line` is
SET_NULL, so the purchases would stay in the ledger with no invoice behind
them — deleted explicitly first. `StockTakeLineSource.invoice_line` is
PROTECT: an invoice that priced a past stock take is **refused**, naming the
count, since deleting it would rewrite what that count was worth. Unclassified
products that only it created go with it (a misread receipt's garbled names
would otherwise sit in the review queue for ever); files go on commit.

### Correcting an invoice's lines

The correction page shows a name, a count, a weight, the amounts and VAT (and
a ticket's promotion); a stored line carries more - duty, Metro's discount,
pack size, category, a receipt's reading. Each row posts back its line's id,
and `importing.corrected_line` keeps what the form doesn't show, and the
volume unless one was typed (scaled when the count changes: an item's size
didn't). Rebuilt from the four
visible fields, a Metro invoice saved untouched turned 4.2 L of vodka into
6 L of stock, and a weighed Wing Seng lemon lost its kilos.
`replace_invoice_lines` then **updates those lines in place** - a stock take
priced from one keeps its trail, where deleting and recreating it hit the
PROTECT with a 500 (66 real invoices) - and removes the others, refusing
(`InvoiceLinesInUseError`, nothing saved) to remove one a count was priced
from. A refund is a negative count **and** a negative amount (Metro's "1-" /
"15,00-", on 41 real invoices); a positive count at a negative price is
refused - stock worth less than nothing is what the FIFO guard exists for
(not on a charge whose supplier holds no stock item: "A credit on a charge",
above).

### Gathering invoices

**Never make AdminMate contact Metro, a portal or the mailbox from a
coding session** - no gather submitted in the browser pane, no curl POST,
no `gather_invoices_task` / `scrape_metro_invoices` from a shell or a
script. Coding agents launched 7 of the 18 recorded Metro runs and some
fourteen unrecorded dev-script sessions on 31/08, and both refused sign-ins
of 02/09 were an agent's end-to-end check: Metro's firewall blocked the
owner's access twice. Test against the fakes (`test_scraper_metro.py`,
`test_metro_refusal.py`); the owner clicks "Tester" for a portal.

**Metro's firewall judges each automated sign-in** (an Akamai edge: « Vous
avez été bloqué par notre pare-feu … identifiant :#18.… », at the moment the
credentials are sent, even after two quiet days). So AdminMate signs in
rarely and never argues with a refusal (`scrapers/metro.py`):
- the refusal (`blocked_reference`) is looked for wherever it can show - the
  page loaded, before typing, the credentials sent (`_await_sign_in`: the
  filters, the refusal, or the sign-in page kept = `MetroLoginFailed`), a
  search coming back empty, a download that never came, any page that
  did not come - and raised as `MetroBlocked`, never retried;
- `metro_pause` - kept on the METRO supplier (`scrape_*` fields), checked
  before any browser starts, whoever calls: 7 days after a refusal, 14 if
  it refused again within 30 days, and 24 h between two sign-ins (noted
  before the password goes). A person can ask for one sign-in through it
  ("Réessayer Metro maintenant" = `ignore_pause`); its own box is
  disabled, since the browser re-ticked a remembered one;
- a gather with `source_codes=None` never includes Metro: named only;
- the browser is restarted only when it died (`_session_died`), once,
  after `RESTART_PAUSE_SECONDS`; a page not as expected, or a window closed
  by hand, stops the run - each hiccup used to mean a new sign-in;
- one download at a time, one click every `CLICK_INTERVAL_SECONDS`;
  `MAX_CONSECUTIVE_TIMEOUTS` downloads in a row that never came stop the
  run (counted across windows; one late download alone says nothing - the
  old "timeouts" were a counting bug);
- rows are followed by their number (`ROWS_JS` reads every row's button
  and checkbox id in one call), never their place: a list re-rendering
  under a click had a row skipped and another clicked twice. A row counts
  as fetched once its **file landed** - marked at the click, the download
  a dying browser cut off was skipped after the restart, and lost;
- "Annuler" is heard before the browser starts and before the password
  goes: a cancelled gather still signed in once;
- a stop carries the PDFs already landed (`MetroError.files`): imported,
  not fetched again next time.

Metro is searched from its own newest invoice at the latest
(`min(posted start, suggested_start_date("METRO"))`): while it was paused,
gathers of the other sources moved the offered start past it, and the
days between would never have been searched on Metro.

**One source failing is said on its own line and the others run**
(`tasks._gather_metro`, `_gather_email`, `_gather_website`): on 18/09
Metro's refusal failed the whole gather, and the mailbox and the five
portals were never searched. A run with a source in error ends "Terminé"
with "N source(s) en échec" beside its pill, and the form offers that
run's period again (the default, since the newest invoice brought in,
skipped what it missed) - unless the run before asked the same period and
missed the same sources (`workspace._missed_again`): a portal asking for a
code every time held every gather on 01/01 for good. A mailbox source with
no reader goes through `receipts.import_document` like a portal's -
`parse_and_import` filed it empty, and again at every gather. The gather
beats (`tasks._GatherHeartbeat`, like the receipt batches): silent through
a long step, it was reaped while running, and a second one could start -
but only while it moves (log or progress changed within `STALE_AFTER`): a
thread blocked for good in one call is left to the reaper.

**SQLite takes the write lock when a transaction starts**
(`SQLITE_OPTIONS["transaction_mode"] = "IMMEDIATE"`): in the default mode a
transaction that had read, then wrote after another connection committed -
a heartbeat, every 15 s - failed at once with "database is locked", the
timeout not even tried, and the invoice being imported was lost.

**A job's button follows its status card** (`data-job-control` ↔
`data-job-active`, ui.js): drawn disabled while the job ran, it stayed so
after the job ended - only the card was redrawn - and a failed gather
could not be run again without reloading the page. A dead job is reaped
where it is polled (`gather_status`, `sales_import_status`), and at startup
only by the process serving the pages (`apps.serving_requests`): a
`manage.py shell` used to mark a running gather failed seven seconds in.

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

**A customer portal is data, not code** (`models.WebsiteInvoiceSource`,
`scrapers/website.py`, set up on Achats → Sources → « + Nouvelle source »,
« Canal : Espace client »). The rent's, the water's, the phone's: a login page, the NAMES of
the two .env variables holding the credentials (never the values - the
database is copied and shown on screen, a .env is not; they are read from
the .env file at each run, so a line added counts without a restart), and
nothing else required. The scraper does what a person does: refuses the
cookie banner (only a refusing button is ever clicked, shadow roots
included), finds the login form - the visible password field, the text
field in front of it, the button that submits them, identifier and password
on two pages if the site does that - follows the first link that speaks of
invoices and is not one (`leads_to_invoices`: a home page listing the
latest invoices by month had one clicked as the menu) and holds no other
link (`link_to_follow`: Eau de Paris draws its side menu as one clickable
block around its entries, first in the page; clicked, it only closed
itself - nesting read from the page, never from the words; the page script
clears the marks of its previous read first, or a page drawn in place had
its hidden menu clicked for its first invoice; and buttons whose address is
only "#" are not one file, or every invoice but the first was left), or the links named
under "Liens à suivre", or the page given - and downloads, page after
page, each invoice of the period, one click every
`CLICK_INTERVAL_SECONDS`. CSS selectors exist for a site that defeats that,
folded away under "Réglages avancés".

The deciding is pure Python over what one script reads off the page (every
link with the text of its row): `periods` (a date is that day, a month
named - "mai 2026", "05/2026" - that month; a day's figures are not read
again as a month), `in_window` (a row printing no date is downloaded: nothing
says it is out), `looks_like_an_invoice` (leads to a PDF, says
"Télécharger"/"PDF" - in words or by its icon's name, `icon-download…`
read into `label` - or opens one, "Voir ma facture", **and** sits in a row
printing a date or an amount), `known_number_in` (a row printing the number
of an invoice already imported is not clicked). **One link a row**, the one
that downloads first (`_strength`): Free Mobile's cards offer « Voir ma
facture » and a button holding only a download icon - nothing on them said
« Télécharger », and the run found no invoice. A link's row is its table
row, list item or card - never the block holding the rows: a footer's terms
of sale climbed to the whole page, whose dates were the invoices', and were
downloaded as one. A button with little text of its own climbs to the block
printing a figure (its month, its amount): stopped at the block holding
only its neighbour « Voir ma facture », it had no date. **A gather
recognising no invoice link at all fails** (its own line, the period
offered again, the page kept and its links named): said as "0 found", it
passed for a quiet month and left nothing to set the site up from. Only a
file that *starts* like a PDF counts as the download - Chrome's stray
"downloads.htm" appeared and vanished mid-wait (as on Metro's site).

**How the file is obtained** (`_Visit.download`), each rule a 45-second
wait per invoice on a real site: a link naming its file is **fetched** with
the browser's session - no click to wait on (Free Mobile's « Voir ma
facture » opened a tab a script's click could not); otherwise it is
**clicked as a person clicks** (`_press`; the script's click only when
something covers it), in a profile allowing pop-ups and several downloads
(Chrome held every download of a site after its first, asking a question
nobody saw) with downloads allowed in every tab (`Browser.setDownloadBehavior`
- the page's own setting left a new tab without); a tab the click opened is
**read** for the document it shows, one held in memory too; a click that
has started nothing - no file, no download under way, no tab - after
`DOWNLOAD_START_SECONDS` started nothing, and a file landing later is still
taken (`late_downloads`). A file counts as arrived when it is **new or
changed** (size, time): Eau de Paris names each file after its invoice, and
Chrome downloading as told through DevTools writes over a file of the same
name - a run after the first found every name already there and said
nothing arrived while the browser showed each download done. Each file is
moved at once to a name of its own (`_take`), so neither the next invoice
of the same name nor the next run writes over it. **One invoice once a
run** (`invoice_key`): a list growing under « Voir plus » (Free Mobile's
shows five, then eight) was downloaded again from its first row, and paging
stops when a page brings nothing new. Clicked and none arrived is a failure
("aucune des N factures cliquées n'est arrivée"), not "nothing to
download". What
arrives is imported through `receipts.import_document` with the supplier
named - never `parse_and_import`, which files a supplier without a reader
empty - one OCR at a time.

**A person is asked, never impersonated**: a code sent by SMS or a captcha
(`NeedsAPerson`) waits five minutes for whoever is at the keyboard when the
source's "Navigateur visible" is ticked, and stops the source with that
advice when headless. A check standing where the login form will be - a
slider "faites glisser vers la droite", "non pas à un robot" (TotalEnergies',
18/09) - is looked for where the form is not, and handed over the same way:
the scraper had looked for a form twenty seconds and said there was none.
**A login form on screen is filled, whatever its page says**: Free Mobile's
explains that a first sign-in asks for "un code reçu par SMS", and read as a
check, those words stopped every run before its form was filled - "waiting
for a verification" with nothing to verify. Words count only on a page
without the login form, and after signing in only beside something to
answer (an empty field, a slider: `asks_for_a_person`); a field marked for
a one-time code or a captcha a person can see counts anywhere, never an
invisible one (a badge scoring the visitor, a frame parked off screen). A
window closed by hand during the wait is said as such (`alive`): read as an
empty page, it passed for « vérification faite »; a cancel is heard during
the wait and before the password goes. Also, from a second review: the
password field gone is not yet signed in - a code prompt drawn a moment
later is looked for (`SETTLE_SECONDS`), or the run ended with nothing; a
search box, a chat or a newsletter field is nothing to answer, and the
notice every page reCAPTCHA protects prints is no check; a field named for
a one-time code (`otp`, `sms…`), or a slider over a form, is one; with no
password on the page, a text field is the identifier only if it says so -
on a check page the login went into the captcha's answer; a check at the
identifier step of a two-step login is handed over too; and a page's words
alone count only once its form has had `WORDS_GRACE_SECONDS` to be drawn.
The login form is filled and submitted as it is at that moment (fields
found again for each step): Free Mobile's redraws its button as it is
filled, and the button found a moment before was clicked, gone. A site turning automated browsers away
(`RefusedByTheSite`, "The requested URL was rejected" - TotalEnergies' was,
to a plain browser, at the time of writing) is said as such; nothing tries
to get past it. Either way the gather goes on with the next source and the
failure sits on its own line of the progress table. A failure leaves a
screenshot and the page's HTML under `<scrape dir>/<type>/_debug/`, which is
what a site that changed is fixed from. "Tester" signs in and lists what a
gather would download (`list_website_invoices`), downloading nothing - and
keeps the page it read, found or not, under `<scrape dir>/test-<job>/_debug/`,
naming the page's links when it recognised no invoice: a new site is set
up from that ("Liens à suivre", the selectors), never by signing in on the
owner's behalf.

The source form holds both channels' fields (« Réglages : boîte mail » /
« Réglages : espace client »); the one not chosen is a
`<fieldset>` **disabled** as well as hidden. Hidden only, its empty
required field stopped the browser sending the form, silently: "Tester"
did nothing, and no mailbox source could be saved. The test client posts
whatever it is given, so only `test_invoice_type_form_browser` (a real
Chrome) sees that class of bug. It says first that a source fetches for one
supplier only. Its channel reads « Canal », « E-mail » / « Espace client »
from the form (`forms.CHANNELS`), not from `InvoiceType.SourceKind`, whose
labels stay « Email » / « Site web » (admin only): a label changed on the
model is a migration to apply to the real database, for a word.

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

### An id read from `request.POST` is checked before it reaches a query

`pk="abc"` in a filter is a `ValueError`, so a tampered or stale form is a
500 instead of a message. A view that reads an id by hand (not through a
form field) checks `common.is_id(posted)` first and treats anything else as
not found (`_forget_price`, `merge_stock_type`, `pos_product_assign`, the
type pages). Not `str.isdigit()`: "²" is a digit to it, and no
int - the query raised all the same; nor more than 18 digits, past which
SQLite's integer overflows.

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
moved. Deleting an item still in use, alone or through "supprimer les articles
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

**`prefetch_related` does not reach a method that builds its own queryset.**
`Recipe.choice_groups()` re-reads its ingredients (it wants them ordered,
with their stock items' movements), so prefetching them at the call site
bought nothing: every recipe was asked three times over - its usage terms,
its pools, its allocation - and each ask was a query, with another for the
movements behind it. That is 290 queries to draw **Produits & charges** and 277 for
**Écarts**. `recipes.models.variation_scope()` is the memo made for exactly
this, and wrapping `quantities_sold` and `compute_variance` in one took them
to 114 and 109. It is scoped, not cached on the instance: a grouping changes
every time someone presses "OU".

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

The fix for (3) is writing recipes, not tuning the report — see the "À lier"
backlog, 187 of 211 till products unmapped at the time of writing.

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
sales from the ledger, so that's all there is. It is the ledger (bought, less
the losses written down), not the « Acheté » column beside it, which counts
the purchases alone (`catalogue_context` sums both from one scan; they differ
only once a loss is recorded). So the red « Vendu », its « ? » and the
headline's « Vendu > acheté » say « pertes déclarées déduites »: 11 L sold of
12 L bought, 2 L broken, is red beside an « Acheté » above it, and « vendu
plus qu'il n'en a été acheté » alone was false. Between two counts both ends
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

The UI is two tabs of **Recettes & ventes**: "Ventes" runs the import
(background thread + htmx polling, same shape as the invoice gather) and "À
lier" (`/recipes/caisse/`) is the backlog of till products with no recipe -
biggest sellers first, since that's where the unexplained stock is. Four
actions per row, in place: link to a recipe (the one with a close name is
chosen already, `links.suggest_recipe`; an "HH" name ticks happy hour), mark
as a happy-hour variant, create the recipe (`?caisse=`: named after it,
linked to it on save), or ignore (coffee, food, anything untracked). Linking
and unlinking go through `recipes/links.py` only - from these rows and from
the recipe form's "Vendue en caisse sous" - which rebuilds the sales and
clears a happy-hour name that leaves with its product.

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

### Three workspaces, not eight pages

The navigation is **Produits & charges** (what was bought, by article, the
charges, and the products to classify),
**Achats** (invoices, tickets, their sources and suppliers) and **Recettes & ventes**
(recipes, till products, sales), each with the count of what waits there
(`config/navigation.py` decides which link a page lights up). They were
separate pages, and checking that something added had landed meant going
back and forth between them. The rules that came with merging them:

- **Every old address still works** and draws the merged page on its tab or
  import (`inventory.views.StockListView` + `review_queue`,
  `invoices/workspace.py::render_purchases`, `recipes/menu.py::render_menu`).
  Tabs are links to those addresses; `hx-boost` swaps only `#workspace`, so
  the import card above keeps a running import as it is.
- **An action answers where it was taken.** A product classified in the
  side panel of Produits & charges gets the panel back with a note and an undo (the undo
  deletes a stock item the classification created - signed, `UNDO_SALT`),
  and `HX-Trigger: catalogue-changed` makes the list reload itself opened on
  that item. A till product linked from its row gets the row back. A PDF
  imported comes back highlighted and opened in the list (`?surligner=`);
  the list reloads when an import or a gather ends (`documents-changed`,
  sent by their status partials once they stop polling).
- **A bounded list needs a search the database answers.** The table's own
  box (`datatable.js`) only ever sees the rendered rows, so a page that
  shows its first 250 cannot use it: the Eau de Paris invoices sit at the
  277th row and the Total Energies ones at the 575th, and looking for them
  found nothing. `workspace.documents_matching` searches every document by
  supplier, number, date as it is written (12/07/2026, 07/2026, 2026) or
  amount, and the table is `data-table-sort-only` - two boxes filtering by
  two different rules is worse than one. Same on the sales list
  (`recipes.menu._sales_matching`, `SALES_PAGE_SIZE`): 8 099 rows was 2,6 Mo
  on one page, and it only grows.
- **The list shows the newest documents** (`workspace.PAGE_SIZE`, 250), and
  "tout afficher" renders the rest. Every row is about 1,4 Ko of HTML and a
  slice of a second of template: at 823 documents the page was 1,2 Mo, 15 000
  nodes and 626 ms of server time, and opening a row moved a table 47 000
  pixels tall. Bounded, the same page is 393 Ko, 4 000 nodes and 200 ms. The
  row that holds an opened document's lines is made when it is first opened,
  not printed hidden under all of them. Two things to keep: the document just
  imported (`?surligner=`) is shown whatever its date, since it may be older
  than everything on the page; and the table's search and sort only ever see
  what is rendered, which the page says out loud.
- **A page shows a document as it is, not as it was.** An import's log
  copied each file's shop, date, total and state at the second it was read,
  and its page went on showing those: a ticket corrected afterwards still
  read its first total there. `workspace.batch_rows` hands the template the
  documents themselves, and the state pill is `Invoice.review_state` - the
  same rule as every other list.
- **A badge and the list it stands for share one definition.** The "À
  vérifier" tab counted `TICKET_TO_CHECK` while the page listed
  `receipts.pending_receipts()`, and when charges were left out of one and
  not the other the tab read "102" over an empty page. `pending_receipts`
  is that Q and nothing else; the same went for the "Produits & charges" badge, which
  counted the postes of charge the page does not list (110 over 97). When a
  count is cheap to get from the list, take it from the list.
- **Where invoices come from and who they are filed under are two tabs** of
  Achats (the owner, 19/09): « Sources » lists the sources of invoices
  (`InvoiceType`), « Enseignes et fournisseurs » (`supplier_list`, which
  redirected to `types/#fournisseurs` before) every supplier, with a
  « Sources » column naming the sources fetching for each. Each tab builds
  only its own list (`workspace._sources`, `_suppliers`): the suppliers' list
  reads every document's text, and the tab counts are drawn on every page of
  Achats, so its count is a plain `Supplier` count - amber, the suppliers
with a change to see (`_changes_to_see`, their rows' « À voir »), as
« À vérifier » beside it counts what waits. `id="fournisseurs"` stays
  on the Sources tab as a pointer to the new tab: an old bookmark's fragment
  never reaches the server, so nothing can redirect it.
- **Without JavaScript the same forms post and redirect** to the page; the
  in-place answer is chosen on `HX-Request`.
- **No out-of-band part beside a `<tr>`**: htmx 1.9 parses a row response
  inside a table, and the HTML parser moves a sibling `<span>` out of it.
  Counts next to a row answer travel as an `HX-Trigger` event instead
  (`to-link-count`, handled in `ui.js`).
- A page with a side panel is wider (`container-wide`), and the stock list's
  columns are shares, not pixels, so it fits beside the panel.

**The words on screen, and why** (the owner, 19/09: tell the sources of the
invoices apart from the « Enseignes et fournisseurs », and the Produits page
"is not a stock but just a list of every spending (charges) + products
bought"). An `InvoiceType` is a **source** (« source de factures »: one
mailbox search or one customer portal, always for one fournisseur); a
`Supplier` is a **fournisseur** (« enseigne » only on ticket screens, where
it is what the ticket prints); fetching is **Récupérer**. A `StockType` is an
**article** - never « type de stock » - and a product no article claims yet
is **à classer** (a poste of charge never is: « poste de charge »). The
inventory workspace is **Produits & charges**, and its
all-time figures say what was bought (« Total acheté », « Acheté »), summed
from `PURCHASE` movements only, so a loss written down later never makes them
a lie. « Stock » stays where it is true: between two inventaires, what left
the shelf, what is missing, the losses, the ceiling of « Vendu ». Internal
names did not follow (models, fields, url names, context keys, `data-persist`
and localStorage keys, anchors), nor did texts already stored; these notes
still say "stock item" and "stock page" for the article and that workspace.

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
