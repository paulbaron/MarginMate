"""Reading an electronic invoice: Factur-X, UBL, CII.

Since 1 September 2026 every VAT-liable business in France must be able to
RECEIVE its invoices electronically, in one of the EN 16931 formats:
**Factur-X** (a PDF/A-3 with an XML attached), **UBL** (OASIS XML) or **CII**
(UN/CEFACT XML, which is what Factur-X embeds). From 1 September 2027 small
businesses must issue them that way too. So these files are about to become
most of what this application reads.

**Why this module is worth more than it looks.** Everywhere else here, a
supplier's figures are INFERRED. A photographed ticket goes through OCR; a
digital PDF goes through regexes that find an amount in a column. Neither
knows it is right, which is why most of CLAUDE.md is about `parse_checks` -
the apparatus that catches the readings that are wrong, because this
codebase's bugs have all been silently wrong money rather than crashes. An
EN 16931 invoice states the number, the date, the seller's SIREN, every line,
the VAT breakdown and the totals **as data**. A Factur-X PDF read by the
ticket reader is a document whose exact figures were sitting inside it,
thrown away and replaced by a guess.

**This is not an InvoiceParser.** `tests/test_parser_contract.py` forbids a
parser from overriding `parse()`, for a good reason: that is what keeps every
layout testable from hand-written pages. This reader does not work from a
layout at all - it works from bytes - so it is its own module, the way
`invoices/ocr.py` is. Pure: files and bytes in, a `ParsedInvoice` out. No
database, no model, no request, and nothing on the network.

**What it does not do.** It does not fetch anything from a « plateforme
agréée ». The owner's accountant holds the platform and its name is not
known; there is no API here, no source kind, nothing contacted. What this
does is read the documents exactly however they arrive - by e-mail, from a
portal, dropped in the folder import, or downloaded by hand from the
platform and put in a folder.

Four conventions this reader has to honour, each of them somebody's money:

- **A rate is a percentage in the file and a fraction in this database.**
  The XML says 20.00; `InvoiceLine.vat_rate` holds 0.2000.
- **A credit note (type 381, or a UBL CreditNote root) states its amounts
  POSITIVE** and means money going back. This codebase writes that as a
  negative count AND a negative amount. Read at face value, a credit note
  doubles a purchase instead of cancelling it.
- **Document-level allowances and charges** (BG-20/BG-21) are where duty,
  eco-participation and discounts live. They go into
  `Invoice.reconciliation_adjustment`, which CLAUDE.md is emphatic is part
  of the VAT base. Dropped, the invoice is filed at less than it charges.
- **Two profiles carry no lines at all.** MINIMUM and BASIC WL state the
  totals and the VAT breakdown only. That is a valid invoice, and it must be
  said rather than made to look like a reading that failed.

**The XML comes from outside**, so: a DOCTYPE or an ENTITY declaration is
refused before any parser sees it (ElementTree expands internal entities -
that is the billion-laughs door, and an EN 16931 instance never carries
one), what is read out of a PDF attachment is capped and decompressed under
that cap, and every refusal is an `EInvoiceError` carrying a French sentence.
`EInvoiceError` is a `ValueError` so the folder import's existing "broken
file" branch reports it per file instead of failing the whole folder.

No new dependency: pdfminer.six (through pdfplumber, both already here) for
the PDF attachment, `xml.etree.ElementTree` and `zlib` for the rest.
"""

from __future__ import annotations

import re
import zlib
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException, InvalidOperation
from xml.etree import ElementTree

from .parsers.base import EInvoiceFacts, ParseCheck, ParsedInvoice, ParsedLine

ZERO = Decimal("0")
CENTS = Decimal("0.01")
# InvoiceLine.unit_cost_ht is four decimals, .quantity three, .vat_rate four.
UNIT = Decimal("0.0001")
QUANTITIES = Decimal("0.001")
RATES = Decimal("0.0001")
HUNDRED = Decimal("100")

# **What the columns behind these figures actually hold.** A stated amount
# wider than its column is written by SQLite without complaint and then
# raises decimal.InvalidOperation on every READ, which puts the document -
# and the pages that list it - beyond the reach of the application: it can no
# longer be opened, corrected, re-read or even deleted, and only raw SQL gets
# it out. So a figure that does not fit is refused here, at the door, in the
# same breath as a foreign currency. Repairing it is not an option either:
# ten billion euros on a bar's invoice is the supplier's arithmetic, which
# this module reports and never rewrites.
MAX_AMOUNT = Decimal("9999999999.99")  # InvoiceLine.total_ht, Invoice.printed_total_ttc (12,2)
MAX_ADJUSTMENT = Decimal("99999999.99")  # Invoice.reconciliation_adjustment (10,2)
MAX_UNIT = Decimal("999999.9999")  # InvoiceLine.unit_cost_ht (10,4)
MAX_QUANTITY = Decimal("999999999.999")  # InvoiceLine.quantity (12,3)
MAX_RATE = Decimal("9.9999")  # InvoiceLine.vat_rate (5,4) - 999,99 %
# InvoiceLine.raw_name and Product.raw_name. A name is not money: it is cut
# to what the column holds rather than refused, because the figures beside it
# are worth keeping and a 7 MB "product name" is not the invoice's substance.
MAX_NAME = 255

# The two EN 16931 syntaxes.
CII = "CII"
UBL = "UBL"

# A Factur-X XML is tens of kilobytes; a very long one with thousands of
# lines might reach a few megabytes. Past this it is not an invoice, or it is
# a deflate bomb - and either way nothing is read past the cap, so a
# malicious attachment is a message on the import rather than a machine that
# stops answering.
MAX_XML_BYTES = 8 * 1024 * 1024
# The compressed stream itself, before anything is decompressed.
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

# UNTDID 1001 document types that mean money going back. 380 is a commercial
# invoice, 381 a credit note, 261 a self-billed one, 396 a factored one.
CREDIT_NOTE_TYPE_CODES = frozenset({"381", "261", "396"})

# Only EUR. Anything else is refused rather than converted: a conversion rate
# is a decision no part of this application is entitled to take, and a
# purchase filed at the wrong one is exactly the silently-wrong money the
# testing contract exists for.
ONLY_CURRENCY = "EUR"

# The check labels, as module constants because the importer and the review
# screen name them too - a label typed twice is a check that stops matching
# its own list the day one of them is reworded.
TOTAL_CHECK = "Total de la facture électronique"
LINES_CHECK = "Somme des lignes"
VAT_CHECK = "Table de TVA de la facture"
NO_LINES_CHECK = "Lignes détaillées"
LINES_READ_CHECK = "Lignes reprises"
NUMBER_CHECK = "Numéro de la facture"
LINE_SIGN_CHECK = "Sens des lignes"

# What a sender's rounding may leave between two figures the document states
# itself. EN 16931 requires these to balance exactly; a cent of slack absorbs
# a producer that rounded a total it also prints, and anything wider is the
# supplier's arithmetic and is reported rather than repaired.
TOLERANCE = CENTS


class EInvoiceError(ValueError):
    """Refused, with a reason in French for the import to show.

    A ValueError on purpose: `receipt_batches` already reports a plain
    ValueError as one file's error and carries on with the folder, which is
    exactly what a broken or hostile attachment deserves.
    """


# --------------------------------------------------------------------------
# The XML attached to a PDF/A-3
# --------------------------------------------------------------------------


def document_xml(path: str) -> bytes | None:
    """The EN 16931 XML a file carries, whichever way it arrived - or None.

    Two shapes reach the import: a **Factur-X PDF**, whose XML is an
    attachment, and the **XML on its own**, which is what a platform or an
    e-mail may simply forward. Both are the legal invoice.

    An .xml file's bytes come back whether or not they turn out to be an
    invoice, and `read` is what refuses the rest: there is no page to
    photograph and no text layer to fall back on, so the only alternative to
    a sentence saying what is wrong would be a traceback out of a PDF
    renderer handed an XML file.
    """
    if path.lower().endswith(".xml"):
        return _file_bytes(path)
    return embedded_xml(path)


def _file_bytes(path: str) -> bytes | None:
    """An XML file read under the cap - None when it is not there.

    Read with one byte to spare so an oversized file is refused rather than
    truncated into something that parses as a smaller invoice.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_XML_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_XML_BYTES:
        raise EInvoiceError(
            "Ce fichier XML est trop volumineux pour être lu comme une facture électronique."
        )
    return data


def embedded_xml(path: str) -> bytes | None:
    """The EN 16931 XML attached to a PDF, or None.

    None for an ordinary PDF, for a PDF whose attachments are a logo and a
    delivery note, for a file that is not a PDF, for one that is not there
    and for one damaged or truncated - this runs on every file of a folder
    import, so one broken document must never stop the folder.

    It raises only when something that could have BEEN the invoice was too
    big to read and nothing else in the file turned out to be one: a
    refusal a person can see beats silently reading the page instead.

    Which attachment is the invoice is decided by its root element, never by
    its name: ZUGFeRD calls it zugferd-invoice.xml, XRechnung
    xrechnung.xml, Factur-X factur-x.xml, and a sender is free to call it
    anything at all.
    """
    if not path.lower().endswith(".pdf"):
        return None
    too_big: list[str] = []
    try:
        import pdfplumber

        with pdfplumber.open(path) as document:
            for name, stream in _attachments(document.doc):
                try:
                    data = _stream_bytes(stream)
                except _TooBig:
                    too_big.append(name)
                    continue
                except _Undecodable:
                    # Behind a predictor or an exotic filter: not a shape any
                    # Factur-X producer emits, and decoding it to find out
                    # costs more than the whole document is worth.
                    continue
                except Exception:  # noqa: BLE001 - a damaged stream is not an invoice
                    continue
                if data is not None and looks_like_an_invoice(data):
                    return data
    except EInvoiceError:
        raise
    except Exception:  # noqa: BLE001 - pdfminer raises its own zoo for a broken file
        return None
    if too_big:
        raise EInvoiceError(
            f"La pièce jointe « {too_big[0]} » de ce PDF est trop volumineuse pour être lue "
            "comme une facture électronique."
        )
    return None


def _attachments(document):
    """Every embedded file of a PDF, as (name, stream).

    The entries live in the catalog's `/Names /EmbeddedFiles` name tree,
    which is a tree: `/Names` at the leaves and `/Kids` above them, and
    producers split it as soon as a file carries a few attachments. Walked
    one level deep, the invoice of a multi-attachment PDF is simply not
    found. `/AF` is read too - PDF/A-3 requires both and files in the wild
    carry one or the other.
    """
    from pdfminer.pdftypes import resolve1

    seen = set()
    specs = []
    catalog = document.catalog or {}
    try:
        names = resolve1(catalog.get("Names")) or {}
        specs.extend(_name_tree(resolve1(names.get("EmbeddedFiles")), resolve1))
    except Exception:  # noqa: BLE001 - a malformed tree is no attachment
        pass
    try:
        for spec in resolve1(catalog.get("AF")) or []:
            specs.append(("", resolve1(spec)))
    except Exception:  # noqa: BLE001
        pass
    for name, spec in specs:
        if not isinstance(spec, dict):
            continue
        try:
            embedded = resolve1(spec.get("EF")) or {}
            # /F is the usual key; /UF is the Unicode one some producers
            # fill in instead.
            stream = resolve1(embedded.get("F")) or resolve1(embedded.get("UF"))
        except Exception:  # noqa: BLE001
            continue
        if stream is None or id(stream) in seen:
            continue
        seen.add(id(stream))
        yield _filename(spec, name, resolve1), stream


def _name_tree(node, resolve1, depth: int = 0):
    """The (name, filespec) pairs of a PDF name tree, however deep."""
    # A tree this deep is malformed or hostile; stopping is not a loss.
    if depth > 16 or not isinstance(node, dict):
        return
    flat = resolve1(node.get("Names")) or []
    for index in range(0, len(flat) - 1, 2):
        yield _as_text(flat[index]), resolve1(flat[index + 1])
    for kid in resolve1(node.get("Kids")) or []:
        yield from _name_tree(resolve1(kid), resolve1, depth + 1)


def _filename(spec: dict, fallback: str, resolve1) -> str:
    for key in ("UF", "F"):
        value = _as_text(resolve1(spec.get(key)))
        if value:
            return value
    return fallback


def _as_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


class _TooBig(Exception):
    """An attachment that would not fit under the cap. Never raised out."""


# The filters a Factur-X producer puts an XML attachment behind: none, or
# Flate. Everything else - a predictor, LZW, RunLength, a chain of them -
# has to be decoded by pdfminer, which builds the WHOLE result before
# anything can measure it. Measured: 8 MB of double-deflated zeros costs
# 148 MB of memory, and 8 MB behind a PNG predictor costs minutes of CPU,
# single-threaded, for a file of a few kilobytes. Neither is an invoice, and
# the cap that was supposed to stop them ran after the damage was done.
_FLATE = ("FlateDecode", "Fl")


class _Undecodable(Exception):
    """An attachment behind a filter no invoice arrives in. Not raised out."""


def _stream_bytes(stream) -> bytes | None:
    """An embedded file's content, decoded under the cap - always.

    `rawdata` is read before anything else because pdfminer clears it once
    it has decoded the stream itself, and because a few kilobytes of deflate
    can decompress to gigabytes: every inflate here is bounded, including
    each stage of a chain, so the cap is on what comes OUT and not only on
    what went in. A filter outside `_FLATE` is not decoded at all.
    """
    raw = stream.rawdata
    if raw is None:
        # Already decoded by something else: take what it has, capped.
        data = stream.get_data()
        if len(data) > MAX_XML_BYTES:
            raise _TooBig
        return data
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise _TooBig
    filters = stream.attrs.get("Filter")
    names = [getattr(item, "name", item) for item in (filters if isinstance(filters, list) else [filters])]
    names = [name for name in names if name]
    if not names:
        return raw
    # A predictor rearranges the bytes after inflating them, so raw zlib is
    # not the whole decoding - and pdfminer's predictor is what costs the
    # minutes. An XML attachment never needs one.
    if stream.attrs.get("DecodeParms") or stream.attrs.get("DP"):
        raise _Undecodable
    if any(name not in _FLATE for name in names):
        raise _Undecodable
    data = raw
    for _name in names:
        data = _inflate(data)
    return data


def _inflate(raw: bytes) -> bytes:
    """One deflate stage, stopped at the cap rather than built in memory.

    `decompress(data, max_length)` is what makes a deflate bomb a refusal
    instead of a machine that stops answering: it returns the first
    MAX_XML_BYTES + 1 bytes and leaves the rest undone.
    """
    data = zlib.decompressobj().decompress(raw, MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise _TooBig
    return data


# --------------------------------------------------------------------------
# Is this an invoice at all
# --------------------------------------------------------------------------


def looks_like_an_invoice(data: bytes) -> bool:
    """Whether `data` is an EN 16931 invoice, by its ROOT ELEMENT.

    Asked of every attachment of a PDF, a logo included, so it never raises
    and never reads the whole document: the root element's start tag is
    enough, and it is reached without expanding anything - a DOCTYPE is
    refused here too rather than parsed.
    """
    try:
        return _syntax(_root_tag(data)) is not None
    except EInvoiceError:
        return False


def _root_tag(data: bytes) -> str:
    """The root element's fully qualified tag, `{namespace}LocalName`."""
    if not isinstance(data, (bytes, bytearray)):
        raise EInvoiceError("Ce document n'est pas un fichier XML.")
    if len(data) > MAX_XML_BYTES:
        raise EInvoiceError(
            "Ce fichier XML est trop volumineux pour être lu comme une facture électronique."
        )
    _refuse_a_doctype(data)
    # A pull parser hands back the root element as soon as its start tag has
    # been seen, so a 4 MB attachment is not parsed to answer "is this an
    # invoice".
    parser = ElementTree.XMLPullParser(["start"])
    try:
        parser.feed(bytes(data))
        for _event, element in parser.read_events():
            return element.tag
    except ElementTree.ParseError:
        raise EInvoiceError("Ce document n'est pas un fichier XML lisible.") from None
    raise EInvoiceError("Ce document n'est pas un fichier XML lisible.")


_WIDE_BOMS = (b"\xff\xfe", b"\xfe\xff")
_DECLARED_ENCODING = re.compile(rb"""encoding\s*=\s*["']([\w.:+-]+)["']""")
_WIDE_ENCODINGS = ("utf-16", "utf16", "utf-32", "utf32", "ucs-2", "ucs-4", "unicodefffe")


def _refuse_a_doctype(data: bytes) -> None:
    """A DOCTYPE or an ENTITY declaration, refused before anything parses it.

    `xml.etree.ElementTree` expands internal entities, so a document
    carrying its own DTD is the billion-laughs and the external-entity door
    at once. An EN 16931 instance never carries one - every producer writes
    a bare `<?xml ... ?>` prolog - so this costs nothing and closes both.

    **The encoding is decided first, because this check is a byte grep.**
    In UTF-16 `<!DOCTYPE` is `b"<\\x00!\\x00D\\x00..."`, which the grep below
    misses entirely while expat reads the BOM, decodes the document happily
    and expands every entity in it. Measured: a 2 KB UTF-16 file came back
    with a one-million-character seller name. Nothing but libexpat's own
    amplification limit stood in the way, which is a guarantee one dependency
    bump wide. A Factur-X instance is UTF-8, so a wide encoding is refused
    rather than decoded - there is nothing legitimate on the other side of it.
    """
    head = bytes(data[:512])
    declared = _DECLARED_ENCODING.search(head)
    wide = head.startswith(_WIDE_BOMS) or b"\x00" in head[:4]
    if not wide and declared is not None:
        wide = declared.group(1).decode("ascii", "replace").lower().startswith(_WIDE_ENCODINGS)
    if wide:
        raise EInvoiceError(
            "Cette facture électronique n'est pas encodée en UTF-8 : elle est refusée "
            "sans être lue (une facture EN 16931 est toujours en UTF-8)."
        )
    if b"<!DOCTYPE" in bytes(data) or b"<!ENTITY" in bytes(data):
        raise EInvoiceError(
            "Cette facture électronique déclare un DOCTYPE ou une entité XML : "
            "elle est refusée sans être lue."
        )


def _syntax(tag: str) -> str | None:
    """CII or UBL, from the root element - never from the file's name."""
    namespace, _, local = tag.rpartition("}")
    namespace = namespace.lstrip("{").lower()
    if local == "CrossIndustryInvoice":
        return CII
    # A file of one's own called <Invoice> is not UBL. The namespace is what
    # makes the root element more than its name.
    if local in ("Invoice", "CreditNote") and "urn:oasis:names:specification:ubl:" in namespace:
        return UBL
    return None


# --------------------------------------------------------------------------
# Reading it
# --------------------------------------------------------------------------


def read(data: bytes, supplier_code: str = "") -> ParsedInvoice:
    """One EN 16931 invoice, CII or UBL, as a ParsedInvoice.

    `supplier_code` is the caller's; this module resolves no supplier. The
    seller's SIREN and VAT number are written into `source_text` instead,
    where `invoices/identifiers.py` - the one matcher for those - finds
    them. The BUYER's are deliberately left out: that is the bar's own
    company number, printed on every supplier's invoice, and the one figure
    that must never name a supplier.
    """
    tag = _root_tag(data)
    syntax = _syntax(tag)
    if syntax is None:
        raise EInvoiceError(
            "Ce fichier XML n'est pas une facture électronique au format EN 16931 "
            "(ni CII ni UBL)."
        )
    _refuse_a_doctype(data)
    try:
        root = ElementTree.fromstring(bytes(data))
    except ElementTree.ParseError as error:
        raise EInvoiceError(f"Cette facture électronique n'est pas un XML lisible ({error}).") from None
    document = _Cii(root) if syntax == CII else _Ubl(root)
    try:
        return _assemble(document, syntax, supplier_code)
    except EInvoiceError:
        raise
    except DecimalException as error:
        # `1E+500` and `1E+999999999` both parse as Decimals and then raise
        # InvalidOperation or Overflow on the first arithmetic - and those
        # are ArithmeticErrors, not ValueErrors, so they went past every
        # handler the import has and reached the owner as a traceback.
        # Every refusal out of this module is an EInvoiceError with a
        # sentence in French; this is the backstop that keeps that true.
        raise EInvoiceError(
            "Cette facture électronique déclare un nombre que MarginMate ne sait pas "
            f"calculer ({type(error).__name__}) : elle n'est pas importée."
        ) from None


# --- the two syntaxes, each answering the same questions -------------------


def _local(element) -> str:
    return element.tag.rpartition("}")[2] if isinstance(element.tag, str) else ""


def _kids(node, name: str) -> list:
    return [child for child in node if _local(child) == name] if node is not None else []


def _kid(node, *names: str):
    """The first child at `names`, walked down one name at a time."""
    for name in names:
        found = _kids(node, name)
        if not found:
            return None
        node = found[0]
    return node


def _text(node, *names: str) -> str:
    found = _kid(node, *names) if names else node
    return (found.text or "").strip() if found is not None else ""


def _amount(node, *names: str) -> Decimal | None:
    """An amount exactly as the XML states it - never rounded here.

    Rounding before the checks would blame the supplier for this
    application's own storage: an invoice stating 12.345 has to be checked
    against 12.345 and stored as 12.35.
    """
    return _decimal(_text(node, *names))


def _decimal(raw: str) -> Decimal | None:
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    # Decimal("NaN") and Decimal("Infinity") both parse, and either one
    # poisons every sum it reaches.
    return value if value.is_finite() else None


def _rate(percent: Decimal | None) -> Decimal:
    """A percentage (20.00) as the fraction this database stores (0.2000).

    Absent is 0: an exempt line (category E) carries no RateApplicablePercent
    at all, and there is no rate to guess. 5,5 % is what the ticket reader
    falls back on when nothing proves a rate, and guessed here it would
    invent tax on a postage stamp.
    """
    # Quantized even when it is zero: `Decimal("0")` and `Decimal("0.0000")`
    # compare and hash equal, but vat_breakdown is stored as JSON strings and
    # the same rate would come back written two ways in one table.
    percent = percent if percent is not None else ZERO
    return _fits((percent / HUNDRED).quantize(RATES, rounding=ROUND_HALF_UP), MAX_RATE, "un taux de TVA")


class _Cii:
    """UN/CEFACT Cross Industry Invoice - what Factur-X embeds."""

    def __init__(self, root):
        self.root = root
        self.header = _kid(root, "ExchangedDocument")
        transaction = _kid(root, "SupplyChainTradeTransaction")
        self.transaction = transaction
        self.agreement = _kid(transaction, "ApplicableHeaderTradeAgreement")
        self.settlement = _kid(transaction, "ApplicableHeaderTradeSettlement")
        self.summation = _kid(self.settlement, "SpecifiedTradeSettlementHeaderMonetarySummation")
        self.seller = _kid(self.agreement, "SellerTradeParty")

    def profile(self) -> str:
        return _text(self.root, "ExchangedDocumentContext",
                     "GuidelineSpecifiedDocumentContextParameter", "ID")

    def number(self) -> str:
        return _text(self.header, "ID")

    def type_code(self) -> str:
        return _text(self.header, "TypeCode")

    def issued(self) -> date | None:
        moment = _kid(self.header, "IssueDateTime")
        stamp = _kid(moment, "DateTimeString")
        if stamp is None:
            return _iso_date(_text(moment, "DateString") or _text(moment))
        return _coded_date((stamp.text or "").strip(), stamp.get("format", ""))

    def currency(self) -> str:
        return _text(self.settlement, "InvoiceCurrencyCode")

    def seller_name(self) -> str:
        return _text(self.seller, "Name")

    def seller_siren(self) -> str:
        """BT-30, the seller's legal registration. `schemeID="0002"` is the
        SIREN; "0009" is the SIRET, whose first nine digits are it."""
        legal = _kid(self.seller, "SpecifiedLegalOrganization")
        return _text(legal, "ID")

    def seller_vat(self) -> str:
        """BT-31, among the tax registrations - the one with schemeID="VA"."""
        for registration in _kids(self.seller, "SpecifiedTaxRegistration"):
            for identifier in _kids(registration, "ID"):
                if identifier.get("schemeID", "").upper() == "VA":
                    return (identifier.text or "").strip()
        return ""

    def lines(self):
        for item in _kids(self.transaction, "IncludedSupplyChainTradeLineItem"):
            product = _kid(item, "SpecifiedTradeProduct")
            settlement = _kid(item, "SpecifiedLineTradeSettlement")
            price = _kid(item, "SpecifiedLineTradeAgreement", "NetPriceProductTradePrice")
            yield _RawLine(
                name=_text(product, "Name"),
                ean=_scheme_id(product, "GlobalID", "0160"),
                quantity=_amount(item, "SpecifiedLineTradeDelivery", "BilledQuantity"),
                net_price=_amount(price, "ChargeAmount"),
                price_basis=_amount(price, "BasisQuantity"),
                total=_amount(settlement, "SpecifiedTradeSettlementLineMonetarySummation",
                              "LineTotalAmount"),
                rate=_amount(_kid(settlement, "ApplicableTradeTax"), "RateApplicablePercent"),
            )

    def vat_rows(self):
        for tax in _kids(self.settlement, "ApplicableTradeTax"):
            yield (
                _amount(tax, "RateApplicablePercent"),
                _amount(tax, "BasisAmount"),
                _amount(tax, "CalculatedAmount"),
            )

    def allowances_and_charges(self):
        """BG-20/BG-21, each **with its own VAT rate** (BT-96 / BT-103).

        The rate matters as much as the amount. Duty on alcohol is charged
        at 20 % on an invoice whose food and soft drinks are at 5,5 %, and
        guessed from the lines it files the invoice at a total the bank will
        never show. It is stated here; there is nothing to guess.
        """
        for item in _kids(self.settlement, "SpecifiedTradeAllowanceCharge"):
            indicator = _text(item, "ChargeIndicator", "Indicator").lower()
            yield _Adjustment(
                reason=_text(item, "Reason"),
                is_charge=indicator == "true",
                amount=_amount(item, "ActualAmount"),
                rate=_amount(_kid(item, "CategoryTradeTax"), "RateApplicablePercent"),
            )

    def totals(self):
        return _Totals(
            lines=_amount(self.summation, "LineTotalAmount"),
            charges=_amount(self.summation, "ChargeTotalAmount"),
            allowances=_amount(self.summation, "AllowanceTotalAmount"),
            taxable=_amount(self.summation, "TaxBasisTotalAmount"),
            # BT-114: the sender's own rounding of its total. EN 16931 has
            # BT-112 = BT-109 + BT-110 + BT-114, so left out it turns an
            # invoice that balances into an accusation against a supplier
            # who did nothing wrong - and one nobody can act on.
            rounding=_amount(self.summation, "RoundingAmount"),
            # BT-113: already paid. The purchase is still the whole invoice,
            # but the bank will only ever show BT-115.
            prepaid=_amount(self.summation, "TotalPrepaidAmount"),
            # BT-110 and BT-111: a document may state its VAT twice, once in
            # the invoice's currency and once in the currency it accounts
            # for tax in, as two TaxTotalAmount elements in either order.
            # Taking the first would put a foreign figure beside French
            # ones, and the total check would fail an invoice that adds up.
            tax=_in_currency(_kids(self.summation, "TaxTotalAmount"), self.currency()),
            grand=_amount(self.summation, "GrandTotalAmount"),
            payable=_amount(self.summation, "DuePayableAmount"),
        )


class _Ubl:
    """OASIS UBL 2.x - the same invoice, different element names."""

    def __init__(self, root):
        self.root = root
        self.is_credit_root = _local(root) == "CreditNote"
        self.seller = _kid(root, "AccountingSupplierParty", "Party")
        self.monetary = _kid(root, "LegalMonetaryTotal")
        # The document's TaxTotal is the one whose TaxSubtotals are the
        # breakdown; a line's TaxTotal is a child of the line and is never
        # reached from here. A document may state two of them - one in its
        # own currency, one in the currency it accounts for tax in - and
        # only the first kind carries the breakdown this app files on.
        totals = _kids(root, "TaxTotal")
        wanted = (_text(root, "DocumentCurrencyCode") or ONLY_CURRENCY).upper()
        matching = [
            total for total in totals
            if any((amount.get("currencyID") or wanted).upper() == wanted
                   for amount in _kids(total, "TaxAmount"))
        ]
        self.tax_total = (matching or totals or [None])[0]

    def profile(self) -> str:
        return _text(self.root, "CustomizationID")

    def number(self) -> str:
        return _text(self.root, "ID")

    def type_code(self) -> str:
        return _text(self.root, "InvoiceTypeCode") or _text(self.root, "CreditNoteTypeCode")

    def issued(self) -> date | None:
        return _iso_date(_text(self.root, "IssueDate"))

    def currency(self) -> str:
        return _text(self.root, "DocumentCurrencyCode")

    def seller_name(self) -> str:
        return (
            _text(self.seller, "PartyLegalEntity", "RegistrationName")
            or _text(self.seller, "PartyName", "Name")
        )

    def seller_siren(self) -> str:
        return _text(self.seller, "PartyLegalEntity", "CompanyID")

    def seller_vat(self) -> str:
        for scheme in _kids(self.seller, "PartyTaxScheme"):
            value = _text(scheme, "CompanyID")
            if value:
                return value
        return ""

    def lines(self):
        for item in _kids(self.root, "InvoiceLine") + _kids(self.root, "CreditNoteLine"):
            product = _kid(item, "Item")
            price = _kid(item, "Price")
            quantity = _amount(item, "InvoicedQuantity")
            if quantity is None:
                quantity = _amount(item, "CreditedQuantity")
            yield _RawLine(
                name=_text(product, "Name"),
                ean=_scheme_id(_kid(product, "StandardItemIdentification"), "ID", "0160"),
                quantity=quantity,
                net_price=_amount(price, "PriceAmount"),
                price_basis=_amount(price, "BaseQuantity"),
                total=_amount(item, "LineExtensionAmount"),
                rate=_amount(product, "ClassifiedTaxCategory", "Percent"),
            )

    def vat_rows(self):
        for subtotal in _kids(self.tax_total, "TaxSubtotal"):
            yield (
                _amount(subtotal, "TaxCategory", "Percent"),
                _amount(subtotal, "TaxableAmount"),
                _amount(subtotal, "TaxAmount"),
            )

    def allowances_and_charges(self):
        for item in _kids(self.root, "AllowanceCharge"):
            indicator = _text(item, "ChargeIndicator").lower()
            yield _Adjustment(
                reason=_text(item, "AllowanceChargeReason"),
                is_charge=indicator == "true",
                amount=_amount(item, "Amount"),
                # BT-96 / BT-103, where UBL states them.
                rate=_amount(_kid(item, "TaxCategory"), "Percent"),
            )

    def totals(self):
        taxable = _amount(self.monetary, "TaxExclusiveAmount")
        grand = _amount(self.monetary, "TaxInclusiveAmount")
        return _Totals(
            lines=_amount(self.monetary, "LineExtensionAmount"),
            charges=_amount(self.monetary, "ChargeTotalAmount"),
            allowances=_amount(self.monetary, "AllowanceTotalAmount"),
            taxable=taxable,
            rounding=_amount(self.monetary, "PayableRoundingAmount"),
            prepaid=_amount(self.monetary, "PrepaidAmount"),
            tax=_amount(self.tax_total, "TaxAmount"),
            grand=grand,
            payable=_amount(self.monetary, "PayableAmount"),
        )


def _in_currency(elements, currency: str) -> Decimal | None:
    """The one of several amounts that is stated in `currency`.

    EN 16931 lets a document repeat its VAT total in a tax-accounting
    currency (BT-111 beside BT-110), in either order. `currencyID` is what
    tells them apart; without one, the first is the invoice's own.
    """
    wanted = (currency or ONLY_CURRENCY).upper()
    for element in elements:
        if (element.get("currencyID") or "").upper() == wanted:
            return _decimal((element.text or "").strip())
    for element in elements:
        if not element.get("currencyID"):
            return _decimal((element.text or "").strip())
    return _decimal((elements[0].text or "").strip()) if elements else None


def _scheme_id(node, name: str, scheme: str) -> str:
    """A coded identifier, taken only when it is in the scheme asked for -
    0160 is GTIN, which is what `InvoiceLine.ean` means. An identifier in
    the supplier's own scheme is not an EAN, and stored as one it would
    match another supplier's product."""
    for child in _kids(node, name):
        if child.get("schemeID", "") == scheme:
            return (child.text or "").strip()
    return ""


def _coded_date(raw: str, code: str) -> date | None:
    """A CII date: `format="102"` is YYYYMMDD, 203 and 204 add a time.

    Never a locale-dependent string - the one place this codebase has read
    dates wrongly is where it had to guess a language.
    """
    digits = "".join(character for character in raw if character.isdigit())
    if code in ("", "102", "203", "204") and len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return None


def _iso_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


class _RawLine:
    """One line exactly as the file states it, before any sign or rounding."""

    def __init__(self, name, ean, quantity, net_price, price_basis, total, rate):
        self.name = name
        self.ean = ean
        self.quantity = quantity
        self.net_price = net_price
        self.price_basis = price_basis
        self.total = total
        self.rate = rate


class _Adjustment:
    """One document-level allowance or charge (BG-20/BG-21), with its rate."""

    def __init__(self, reason, is_charge, amount, rate):
        self.reason = reason
        self.is_charge = is_charge
        self.amount = amount
        self.rate = rate

    @property
    def signed(self) -> Decimal:
        """Its amount the way the invoice counts it: a charge adds, an
        allowance takes away."""
        return self.amount if self.is_charge else -self.amount


class _Totals:
    def __init__(self, lines, charges, allowances, taxable, tax, grand, payable,
                 rounding=None, prepaid=None):
        self.lines = lines
        self.charges = charges
        self.allowances = allowances
        self.taxable = taxable
        self.tax = tax
        self.grand = grand
        self.payable = payable
        # BT-114 and BT-113. Both are stated data and both were being
        # thrown away; see _Cii.totals.
        self.rounding = rounding
        self.prepaid = prepaid


# --- putting it together ---------------------------------------------------


def _assemble(document, syntax: str, supplier_code: str) -> ParsedInvoice:
    currency = document.currency().upper()
    if currency and currency != ONLY_CURRENCY:
        raise EInvoiceError(
            f"Cette facture électronique est libellée en {currency} et non en euros : "
            "elle n'est pas importée (aucune conversion n'est faite ici)."
        )

    type_code = document.type_code()
    credit = type_code in CREDIT_NOTE_TYPE_CODES or getattr(document, "is_credit_root", False)
    # A credit note states its amounts positive and means the other
    # direction. Everything below is signed once, here, so no reading
    # further down has to remember which kind of document it is on.
    sign = Decimal("-1") if credit else Decimal("1")

    totals = document.totals()
    raw_lines = list(document.lines())
    adjustments = [item for item in document.allowances_and_charges() if item.amount is not None]
    adjustment = sum((item.signed for item in adjustments), start=ZERO)
    if not adjustments:
        # A sender may state only the totals BT-107/BT-108 without listing
        # the allowances behind them; the money still belongs in the base.
        adjustment = (totals.charges or ZERO) - (totals.allowances or ZERO)

    lines = [_line(raw, sign) for raw in raw_lines if raw.total is not None or raw.quantity is not None]
    profile = document.profile()
    facts = EInvoiceFacts(
        syntax=syntax,
        profile=profile,
        seller_name=_short(document.seller_name()),
        is_credit_note=credit,
        document_type_code=type_code,
        carries_no_lines=not raw_lines,
        currency=currency or ONLY_CURRENCY,
        adjustment_reasons=[_short(item.reason) for item in adjustments if item.reason],
        adjustment_vat_rate=_adjustment_rate(adjustments, adjustment),
    )

    breakdown = [
        (_rate(percent), _money(base * sign, what="une base de TVA"),
         _money(tax * sign, what="un montant de TVA"))
        for percent, base, tax in document.vat_rows()
        if base is not None and tax is not None
    ]
    checks = _checks(document, totals, raw_lines, adjustment, sign, facts)
    return ParsedInvoice(
        supplier_code=supplier_code,
        invoice_number=document.number(),
        invoice_date=document.issued(),
        lines=lines,
        source_text=_as_text_document(document, facts, totals, lines, sign),
        checks=checks,
        # The figures are the document's own data, not a reading of a page:
        # `from_ocr` would loosen the product matcher and `confidence` would
        # claim a recogniser had a doubt. Neither applies.
        from_ocr=False,
        reconciliation_adjustment=_money(adjustment * sign, MAX_ADJUSTMENT, "les frais ou remises"),
        warnings=[f"{item.label} : {item.detail}" for item in checks if not item.passed],
        printed_total_ttc=(
            _money(totals.grand * sign, what="le total de la facture")
            if totals.grand is not None else None
        ),
        vat_breakdown=breakdown,
        einvoice=facts,
    )


def _line(raw: _RawLine, sign: Decimal) -> ParsedLine:
    total = (raw.total or ZERO) * sign
    quantity = _quantity((raw.quantity if raw.quantity is not None else Decimal("1")) * sign)
    # BT-146 is the net price of BT-149 units (a price "per 100"), so it is
    # only a unit price once divided by that basis. The unit price stays
    # POSITIVE on a credit note: it is what one of them costs, and made
    # negative beside a negative count it would price the return twice.
    unit = raw.net_price
    basis = raw.price_basis
    if unit is not None and basis is not None and basis != ZERO:
        unit = unit / basis
    if unit is None:
        count = abs(Decimal(quantity))
        unit = (abs(total) / count) if count else abs(total)
    return ParsedLine(
        raw_name=_short(raw.name),
        quantity=quantity,
        total_volume=ZERO,
        unit_cost_ht=_fits(unit.quantize(UNIT, rounding=ROUND_HALF_UP), MAX_UNIT, "un prix unitaire"),
        total_ht=_money(total, what="un montant de ligne"),
        vat_rate=_rate(raw.rate),
        ean=raw.ean,
    )


def _short(text: str) -> str:
    """A name cut to the column that stores it (InvoiceLine.raw_name and the
    Product it creates are both 255).

    Cut and not refused: a name is not money, and an invoice whose figures
    are exact is worth keeping even when a sender put a paragraph where the
    article should be. Stored whole it goes onto the line, onto a new
    Product, into `source_text` and onto the review page; past about 50 000
    characters SQLite's own LIKE limit turns the import into an English
    OperationalError the owner can do nothing with.
    """
    text = (text or "").strip()
    return text if len(text) <= MAX_NAME else text[:MAX_NAME - 1].rstrip() + "…"


def _fits(value: Decimal, limit: Decimal, what: str) -> Decimal:
    """`value`, or a refusal naming the figure that does not fit its column.

    Refused rather than truncated: the figure is the supplier's, which this
    module reports and never rewrites. See MAX_AMOUNT for what a figure too
    wide does to the database it is written to.
    """
    if not -limit <= value <= limit:
        raise EInvoiceError(
            f"Cette facture électronique déclare {what} que MarginMate ne peut pas "
            f"enregistrer ({value}) : elle n'est pas importée."
        )
    return value


def _quantity(value: Decimal):
    """A count as an int when the document states a whole one, a Decimal to
    the thousandth when it sells by a measure (0,82 m² of plywood).
    `InvoiceLine.quantity` holds three decimals."""
    quantized = _fits(value.quantize(QUANTITIES, rounding=ROUND_HALF_UP), MAX_QUANTITY, "une quantité")
    return int(quantized) if quantized == quantized.to_integral_value() else quantized


def _money(value: Decimal, limit: Decimal = MAX_AMOUNT, what: str = "un montant") -> Decimal:
    """To the cent, half away from zero - the rule the rest of this codebase
    converts money with. `InvoiceLine.total_ht` holds two decimals, and an
    invoice stating 12.345 has to land somewhere."""
    return _fits(value.quantize(CENTS, rounding=ROUND_HALF_UP), limit, what)


def _adjustment_rate(adjustments, adjustment: Decimal) -> Decimal | None:
    """The VAT rate the document-level allowances and charges carry - None
    when the document does not say.

    None matters: `Invoice.adjustment_ttc` then keeps guessing, which is what
    a supplier's PDF (UBA's duty) and a till receipt need. Where an invoice
    STATES the rate, guessing it is what files 100,00 € of duty at 5,5 %
    beside soft drinks and leaves the invoice 14,50 € below what the bank
    debits - with every check green, because every check works on the stated
    figures and those all balance.

    Several of them at different rates blend into one rate, since that is the
    shape `Invoice.reconciliation_adjustment` has: 50,00 at 20 % beside 50,00
    at 5,5 % is 12,75 € of VAT on 100,00 €, i.e. 12,75 %. Exact to the
    hundredth of a cent, which is finer than anything downstream shows.
    """
    if not adjustments or any(item.rate is None for item in adjustments):
        return None
    if not adjustment:
        return None
    tax = sum((item.signed * _rate(item.rate) for item in adjustments), start=ZERO)
    return _fits((tax / adjustment).quantize(RATES, rounding=ROUND_HALF_UP), MAX_RATE, "un taux de TVA")


def _checks(document, totals: _Totals, raw_lines, adjustment: Decimal, sign, facts) -> list[ParseCheck]:
    """What the document says about itself, checked against itself.

    These are NOT the receipt parsers' checks, which exist because OCR
    guesses. Everything here is stated as data, so a check failing is the
    SUPPLIER's arithmetic, not a misreading - and it is reported with both
    figures and never repaired. Repairing one silently is how this codebase
    has shipped wrong money before.

    All of it is done on the figures exactly as the file states them: round
    12.345 to 12.35 first and the check blames the supplier for this
    application's own storage.
    """
    checks: list[ParseCheck] = []

    if not document.number():
        checks.append(
            ParseCheck(
                label=NUMBER_CHECK,
                passed=False,
                detail="La facture électronique ne porte aucun numéro (BT-1).",
            )
        )

    if totals.taxable is not None and totals.tax is not None and totals.grand is not None:
        # BT-112 = BT-109 + BT-110 + BT-114. The rounding is rare in France
        # and it is stated data: ignored, an invoice that balances to the
        # centime is reported as its supplier's arithmetic failing, and parked
        # in « Documents à corriger » where nobody can do anything about it.
        rounding = totals.rounding or ZERO
        expected = totals.taxable + totals.tax + rounding
        said = f" + arrondi {rounding:+.2f} €" if rounding else ""
        checks.append(
            ParseCheck(
                label=TOTAL_CHECK,
                passed=abs(expected - totals.grand) <= TOLERANCE,
                detail=(
                    f"HT {totals.taxable:.2f} € + TVA {totals.tax:.2f} €{said} = {expected:.2f} € "
                    f"/ facture {totals.grand:.2f} € (écart {totals.grand - expected:+.2f} €)"
                ),
            )
        )

    unreadable = [raw for raw in raw_lines if raw.total is None and raw.quantity is None]
    if unreadable:
        # The document DID carry lines and one of them has neither an amount
        # nor a quantity. That is not the MINIMUM profile - it is a line
        # lost - and the two must never read the same on the page.
        checks.append(
            ParseCheck(
                label=LINES_READ_CHECK,
                passed=False,
                detail=(
                    f"{len(unreadable)} ligne(s) sur {len(raw_lines)} ne portent ni montant "
                    "ni quantité et n'ont pas pu être reprises."
                ),
            )
        )

    # A count below zero at an amount above it. CLAUDE.md's convention is
    # that a return is a negative count AND a negative amount, and a credit
    # on a charge a count of 1 at a negative amount; this is neither, and
    # unlike the second it cannot be repaired - the importer turns a count
    # of 1 at -60,00 into -1 at -60,00 (importing.credit_as_return), but
    # -500 units at +100,00 says nothing anybody can act on. Filed as
    # stated it takes 500 units out of the ledger while 100 € is charged,
    # and the header totals agree with it, so every other check passes.
    crooked = [
        raw for raw in raw_lines
        if raw.total is not None and raw.quantity is not None
        and raw.quantity < ZERO < raw.total
    ]
    if crooked:
        said = ", ".join(
            f"{raw.name or 'ligne sans nom'} ({raw.quantity} × pour {raw.total:+.2f} €)"
            for raw in crooked[:3]
        )
        checks.append(
            ParseCheck(
                label=LINE_SIGN_CHECK,
                passed=False,
                detail=(
                    f"{len(crooked)} ligne(s) portent une quantité négative et un montant positif, "
                    f"ce qui n'est ni un achat ni un retour : {said}."
                ),
            )
        )

    stated = [raw.total for raw in raw_lines if raw.total is not None]
    if stated and totals.taxable is not None:
        summed = sum(stated, start=ZERO) + adjustment
        checks.append(
            ParseCheck(
                label=LINES_CHECK,
                passed=abs(summed - totals.taxable) <= TOLERANCE,
                detail=(
                    f"lignes {sum(stated, start=ZERO):.2f} € + frais et remises {adjustment:+.2f} € "
                    f"= {summed:.2f} € / base HT {totals.taxable:.2f} € "
                    f"(écart {totals.taxable - summed:+.2f} €)"
                ),
            )
        )

    rows = [(percent, base, tax) for percent, base, tax in document.vat_rows()
            if base is not None and tax is not None]
    if rows and totals.taxable is not None and totals.tax is not None:
        bases = sum((base for _percent, base, _tax in rows), start=ZERO)
        taxes = sum((tax for _percent, _base, tax in rows), start=ZERO)
        checks.append(
            ParseCheck(
                label=VAT_CHECK,
                passed=(
                    abs(bases - totals.taxable) <= TOLERANCE and abs(taxes - totals.tax) <= TOLERANCE
                ),
                detail=(
                    f"table {bases:.2f} € HT / {taxes:.2f} € de TVA "
                    f"- facture {totals.taxable:.2f} € HT / {totals.tax:.2f} € de TVA"
                ),
            )
        )

    if facts.carries_no_lines:
        # Passing, deliberately. MINIMUM and BASIC WL are valid profiles and
        # the reading of one is complete and exact: a failed check would
        # park every such invoice in « À vérifier » for ever with nothing
        # anybody could do about it, which is the noise the review screen
        # exists to avoid. What matters is that the page SAYS it, so that a
        # document with no lines never reads as one whose lines were lost.
        profile = facts.profile or "non précisé"
        checks.append(
            ParseCheck(
                label=NO_LINES_CHECK,
                passed=True,
                detail=(
                    "Cette facture électronique ne contient aucune ligne : son profil "
                    f"({profile}) n'en transporte pas. Seuls les totaux et la TVA sont facturés."
                ),
            )
        )
    return checks


def _seller_registration(value: str) -> str:
    """BT-30 written the way `invoices/identifiers.py` reads it.

    The word SIREN or SIRET beside the digits is not decoration: that module
    takes a bare run of nine digits for a company number only on a line that
    says so, and for a phone number otherwise. But the label has to be TRUE -
    BT-30 can be a GLN or a foreign register number, and « SIREN » in front
    of one is a lie on the review screen and an invitation to match it as a
    French company. So the label follows the length, and anything that is
    neither gets none.
    """
    if not value:
        return ""
    digits = "".join(character for character in value if character.isdigit())
    label = {9: "SIREN", 14: "SIRET"}.get(len(digits), "")
    if not label:
        return f"Identifiant légal du fournisseur : {value}"
    grouped = " ".join(digits[index:index + 3] for index in range(0, len(digits), 3))
    return f"{label} {grouped}"


def _as_text_document(document, facts: EInvoiceFacts, totals: _Totals, lines, sign: Decimal) -> str:
    """The invoice as readable text, for `Invoice.source_text`.

    Two jobs. It is what the review screen shows beside the document, and it
    is what `invoices/identifiers.py` reads to name the supplier - the
    seller's SIREN and VAT number are written here in the shape that module
    already recognises, rather than matched a second time in this file.

    **Only what the invoice itself states goes in.** Two things are
    deliberately left out, each of them a supplier this text would otherwise
    name wrongly:

    - the BUYER's company number. It is the bar's own, printed on every
      supplier's invoice, and CLAUDE.md's hardest-won identifier rule is
      that it must name nobody;
    - the profile URN (BT-24) and the file's name. "urn:cen.eu:en16931:2017"
      is read by `identifiers.BARE_DOMAIN_RE` as the web site « cen.eu » -
      a figure EVERY electronic invoice carries, which would be offered as
      an identifier on whichever supplier filed enough of them first. It
      lives on `EInvoiceFacts.profile`, where nothing matches it.
    """
    said = [
        f"Facture électronique ({facts.syntax})",
        f"Fournisseur : {facts.seller_name}" if facts.seller_name else "",
    ]
    said.append(_seller_registration(document.seller_siren()))
    vat = document.seller_vat()
    if vat:
        said.append(f"TVA intracommunautaire {vat}")
    kind = "Avoir" if facts.is_credit_note else "Facture"
    number = document.number() or "(sans numéro)"
    issued = document.issued()
    said.append(f"{kind} n° {number}" + (f" du {issued:%d/%m/%Y}" if issued else ""))
    for line in lines:
        said.append(
            f"{line.raw_name} - {line.quantity} x {line.unit_cost_ht} "
            f"= {line.total_ht} € HT ({line.vat_rate * HUNDRED:.2f} %)"
        )
    for reason in facts.adjustment_reasons:
        said.append(f"Frais ou remise sur la facture : {reason}")
    # **Signed, like everything else.** A credit note states its amounts
    # positive; the lines above are already negative, and totals left as the
    # file writes them made this panel - the only evidence the owner has to
    # check the lines against - the one thing on the page disagreeing with
    # the rest of it.
    if totals.taxable is not None:
        said.append(f"Total HT {totals.taxable * sign:.2f} €")
    if totals.tax is not None:
        said.append(f"TVA {totals.tax * sign:.2f} €")
    if totals.rounding:
        said.append(f"Arrondi {totals.rounding * sign:+.2f} €")
    if totals.grand is not None:
        said.append(f"Total TTC {totals.grand * sign:.2f} €")
    # BT-113/BT-115. The purchase is the whole invoice; the bank will only
    # ever show what is left, so a document the bank match cannot find has
    # its reason written on it rather than nowhere.
    if totals.prepaid:
        said.append(f"Déjà réglé {totals.prepaid * sign:.2f} €")
        if totals.payable is not None:
            said.append(f"Reste à payer {totals.payable * sign:.2f} €")
    return "\n".join(part for part in said if part)
