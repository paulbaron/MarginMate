"""Reading a bank statement exported as OFX (« Relevé OFX »: Microsoft Money,
Quicken's QFX) - one of `bank.statements`' readers, for a format whose kind
of file is `StatementFormat.FileType.OFX`.

    <STMTTRN>
    <TRNTYPE>POS
    <DTPOSTED>20260803120000.000[+2:CEST]
    <TRNAMT>-4.10
    <FITID>0000000001
    <NAME>CB EPICERIE EXEMPLE
    <MEMO>FACTURE CARTE DU 010826

Both versions read alike, by one tokenizer and never by an XML parser: OFX
1.x is SGML whose leaf elements are left open (above), OFX 2.x is XML whose
leaves are closed (`<TRNAMT>-4.10</TRNAMT>`) behind an `<?xml ?>` and an
`<?OFX ?>` declaration. The file is decoded with the format's encoding
(`statements.decode`), read from its first `<OFX>`, its comments set aside;
a DOCTYPE or an ENTITY declaration, a NUL, or anything that is no tag
followed by its text refuses it. The five XML entities and the numeric
character references are decoded - a reference only to a character a label
may hold (no control but the tab, no surrogate, nothing past U+10FFFF, seven
decimal digits or six hexadecimal ones at most), anything else refused; any
other `&name;` is left as printed.

What is read, the file's own structure deciding (`AGGREGATES`, a leaf's
nearest known aggregate is its parent, an unknown element in between is
transparent):

* a statement is a `STMTRS` (a bank account) or a `CCSTMTRS` (a card): its
  currency `CURDEF`, which must be the euro - a silent other currency is
  wrong money -, and its account, `BANKACCTFROM/ACCTID` or
  `CCACCTFROM/ACCTID` (never the `BANKACCTTO` of a transfer); two accounts
  in one file refuse it;
* an operation is a `STMTTRN` of its `BANKTRANLIST` - never one of the
  pending list `BANKTRANLISTP`: its day is `DTPOSTED`'s first eight digits
  (YYYYMMDD; the time and the zone after them are not read), its value date
  `DTAVAIL`'s, both between 2000 and 2099; its amount `TRNAMT`, read digit
  for digit (« -12.50 », « -12,5 »; one decimal mark, never a thousands
  one, no exponent), bounded by `statements.MAX_AMOUNT`; a `CURRENCY` other
  than the euro refuses it (an `ORIGCURRENCY` only says what it was before
  the bank converted it); its label `NAME` (else `PAYEE/NAME`) then `MEMO`,
  each with its spaces collapsed; its type `TRNTYPE` (POS, DIRECTDEBIT,
  XFER…), cut to its column - what the recognition rules read in « Type
  d'opération ».

What each operation IS is the recognition rules' (`statements.
parse_statement` asks them), as for a CSV; `DTUSER` (the day a card was
used) is not read: a card date comes from a rule, and what is stored must be
what « Relire » reads again. `FITID` is not read either: an operation's
fingerprint is the CSV's (account, days, label, amount, occurrence) -
`statements.finish`.

Every refusal is a French sentence (`REFUSALS`), never a library's: a
ValueError the page shows, nothing written.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal

from . import statements
from .statements import BANK_TYPE_MAX, FIRST_DAY, LAST_DAY, MAX_AMOUNT, RawLine, echoed

#: Why a file is refused - each a sentence the page shows.
NOT_OFX = "Ce fichier ne se lit pas comme un relevé OFX : exportez-le à nouveau au format OFX (Money)."
UNSAFE = "Ce relevé OFX déclare un DOCTYPE ou une entité : il est refusé sans être lu."
BAD_REFERENCE = "Caractère illisible dans le relevé OFX"
NO_CURRENCY = "Ce relevé OFX ne dit pas sa devise (CURDEF) : exportez-le à nouveau au format OFX (Money)."
INCOMPLETE = "Opération incomplète dans le relevé OFX"
NO_OPERATION = "Aucune opération trouvée dans ce relevé OFX"
#: What every refusal of this reader begins with (statements' shared ones -
#: dates, amounts, accounts, currency, bounds - apart): a test runs mutated
#: files through it and finds nothing else.
REFUSALS = (NOT_OFX, UNSAFE, BAD_REFERENCE, NO_CURRENCY, INCOMPLETE, NO_OPERATION)

#: The aggregates whose structure is read - an element with text after its
#: tag is a leaf, one without is an aggregate (or an empty leaf, `LEAVES`).
AGGREGATES = frozenset(
    {
        "OFX",
        "SIGNONMSGSRSV1",
        "SONRS",
        "STATUS",
        "BANKMSGSRSV1",
        "STMTTRNRS",
        "STMTRS",
        "CREDITCARDMSGSRSV1",
        "CCSTMTTRNRS",
        "CCSTMTRS",
        "BANKACCTFROM",
        "CCACCTFROM",
        "BANKACCTTO",
        "CCACCTTO",
        "BANKTRANLIST",
        "BANKTRANLISTP",
        "STMTTRN",
        "STMTTRNP",
        "PAYEE",
        "CURRENCY",
        "ORIGCURRENCY",
        "LEDGERBAL",
        "AVAILBAL",
    }
)
#: The leaves read: one left empty in SGML (`<MEMO>` and the next tag) is
#: an empty leaf, never an aggregate holding the elements after it.
LEAVES = frozenset(
    {"TRNTYPE", "DTPOSTED", "DTAVAIL", "DTUSER", "TRNAMT", "FITID", "NAME", "MEMO", "CURDEF", "CURSYM", "ACCTID"}
)
#: The deepest the elements of a file nest: the standard's statements are
#: a dozen deep, and every leaf looks its parents up - a file nesting a
#: million is refused, never read in quadratic time.
MAX_DEPTH = 64
STATEMENTS = frozenset({"STMTRS", "CCSTMTRS"})
ACCOUNTS_FROM = frozenset({"BANKACCTFROM", "CCACCTFROM"})
EURO = "EUR"

_OFX_START = re.compile(r"<OFX>", re.IGNORECASE)
_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
#: A tag and the text up to the next one: the whole file after <OFX> is
#: made of these, or it is no OFX.
_TOKEN = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9.]*)\s*>([^<]*)")
_REFERENCE = re.compile(r"&(?:(amp|lt|gt|quot|apos)|#([0-9]+)|#[xX]([0-9A-Fa-f]+));")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
#: The widest reference read: past U+10FFFF either way is no character.
_DECIMAL_DIGITS, _HEX_DIGITS = 7, 6
_DAY = re.compile(r"[0-9]{8}")
_AMOUNT = re.compile(r"[+-]?(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)")
#: An amount longer than this is none (`MAX_AMOUNT` has ten digits before
#: its decimals): refused before `Decimal` reads a whole file of digits.
_AMOUNT_MAX_LENGTH = 40


class OfxReading:
    """One OFX file read for `statements.parse_statement`: `lines()` yields a
    `RawLine` per operation as its `STMTTRN` ends; `account` is final once
    they are all read; `no_operation` is the sentence a file of none is
    refused with."""

    def __init__(self, content: bytes, layout: statements.Layout):
        self.layout = layout
        self.account = ""
        said = f" (format « {layout.name} »)" if layout.name else ""
        self.no_operation = f"{NO_OPERATION}{said}."
        if len(content) > statements.STRUCTURED_MAX_BYTES:
            raise ValueError(statements.too_big())
        text = statements.decode(content, layout.encoding)
        if "\N{NULL}" in text:
            raise ValueError(NOT_OFX)
        if _DECLARATION.search(text):
            raise ValueError(UNSAFE)
        start = _OFX_START.search(text)
        if start is None:
            raise ValueError(NOT_OFX)
        self._text = _COMMENT.sub("", text[start.start() :])

    def lines(self):
        accounts: set[str] = set()
        statement = None  # [currency, account] of the STMTRS being read
        operation = None  # the leaves of the STMTTRN being read
        stack: list[str] = []
        last_leaf = None
        for closing, name, text in _tokens(self._text):
            if closing:
                if name == last_leaf:
                    # 2.x closes its leaves: nothing after the close but space.
                    last_leaf = None
                    if text.strip():
                        raise ValueError(NOT_OFX)
                    continue
                if name not in stack:
                    raise ValueError(NOT_OFX)
                if text.strip():
                    raise ValueError(NOT_OFX)
                last_leaf = None
                while stack:
                    ended = stack.pop()
                    if ended == "STMTTRN" and operation is not None:
                        yield _line(operation, statement)
                        operation = None
                    elif ended in STATEMENTS:
                        statement = None
                    if ended == name:
                        break
                continue
            value = _unescape(text)
            if name in LEAVES or value.strip():
                last_leaf = name
                parent, grandparent = _known(stack, 2)
                if operation is not None and parent == "STMTTRN":
                    operation.setdefault(name, value.strip())
                elif operation is not None and parent == "PAYEE" and name == "NAME":
                    operation.setdefault("PAYEE/NAME", value.strip())
                elif operation is not None and parent == "CURRENCY" and name == "CURSYM":
                    operation.setdefault("CURRENCY/CURSYM", value.strip())
                elif statement is not None and parent in STATEMENTS and name == "CURDEF":
                    statement[0] = value.strip()
                elif (
                    statement is not None and parent in ACCOUNTS_FROM and grandparent in STATEMENTS and name == "ACCTID"
                ):
                    statement[1] = value.strip()
                    accounts.add(statement[1])
                    if len(accounts) > 1:
                        raise ValueError(statements.several_accounts(len(accounts)))
                    self.account = statement[1]
                continue
            last_leaf = None
            if len(stack) >= MAX_DEPTH:
                raise ValueError(NOT_OFX)
            if name in STATEMENTS:
                statement = [None, ""]
            elif name == "STMTTRN" and statement is not None and _known(stack, 1)[0] == "BANKTRANLIST":
                operation = {}
            stack.append(name)
        if stack:
            # An aggregate left open at the end: the file was cut short, and
            # its last operation, never ended, would be lost in silence.
            raise ValueError(NOT_OFX)


def _tokens(text: str):
    """(closing, NAME, text) for each tag of `text` - which must be nothing
    but tags and their text."""
    position, end = 0, len(text)
    while position < end:
        token = _TOKEN.match(text, position)
        if token is None:
            raise ValueError(NOT_OFX)
        yield token.group(1) == "/", token.group(2).upper(), token.group(3)
        position = token.end()


def _known(stack: list[str], count: int) -> list[str | None]:
    """The `count` nearest known aggregates open, innermost first."""
    found: list[str | None] = [name for name in reversed(stack) if name in AGGREGATES][:count]
    return found + [None] * (count - len(found))


def _unescape(text: str) -> str:
    """The text with its XML entities and numeric references decoded - a
    reference only to a character a label may hold."""
    if "&" not in text:
        return text
    return _REFERENCE.sub(_character, text)


def _character(reference) -> str:
    named, decimal, hexadecimal = reference.groups()
    if named:
        return _NAMED[named]
    refused = ValueError(f"{BAD_REFERENCE} : « {echoed(reference.group())} ».")
    if (decimal and len(decimal) > _DECIMAL_DIGITS) or (hexadecimal and len(hexadecimal) > _HEX_DIGITS):
        raise refused
    code = int(decimal, 10) if decimal else int(hexadecimal, 16)
    if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
        raise refused
    character = chr(code)
    if character != "\t" and unicodedata.category(character) == "Cc":
        raise refused
    return character


def _line(operation: dict, statement) -> RawLine:
    """The operation of one `STMTTRN`, as `statements.finish` takes it."""
    currency = statement[0] if statement is not None else None
    if not currency:
        raise ValueError(NO_CURRENCY)
    if currency.upper() != EURO:
        raise ValueError(statements.not_euros(currency))
    own = operation.get("CURRENCY/CURSYM")
    if own and own.upper() != EURO:
        raise ValueError(statements.not_euros(own))
    for leaf in ("DTPOSTED", "TRNAMT"):
        if not operation.get(leaf):
            raise ValueError(f"{INCOMPLETE} (sans {leaf}) : exportez-le à nouveau au format OFX (Money).")
    value_date = operation.get("DTAVAIL")
    name = operation.get("NAME") or operation.get("PAYEE/NAME") or ""
    label = " ".join(part for part in (" ".join(name.split()), " ".join(operation.get("MEMO", "").split())) if part)
    return RawLine(
        operation_date=_day(operation["DTPOSTED"]),
        value_date=_day(value_date) if value_date else None,
        bank_type=operation.get("TRNTYPE", "").strip()[:BANK_TYPE_MAX].rstrip(),
        label=label,
        amount=_amount(operation["TRNAMT"]),
    )


def _day(printed: str) -> date:
    """The day of an OFX date: its first eight digits, YYYYMMDD - the time
    and the zone after them are not read. Refused when it is no day, or no
    day a statement holds."""
    refused = ValueError(f"Date illisible dans le relevé : {echoed(printed)!r}")
    if _DAY.match(printed) is None:
        raise refused
    try:
        day = datetime.strptime(printed[:8], "%Y%m%d").date()  # noqa: DTZ007 - the statement's printed day
    except ValueError:
        raise refused from None
    if not FIRST_DAY <= day <= LAST_DAY:
        raise refused
    return day


def _amount(printed: str) -> Decimal:
    """`TRNAMT` digit for digit: a sign, digits, one decimal mark (« . » as
    the standard says, « , » as some banks print it) - never a thousands
    mark, an exponent or anything past `MAX_AMOUNT`."""
    refused = ValueError(f"Montant illisible dans le relevé : {echoed(printed)!r}")
    if len(printed) > _AMOUNT_MAX_LENGTH or _AMOUNT.fullmatch(printed) is None:
        raise refused
    sign = "-" if printed.startswith("-") else ""
    digits = printed.lstrip("+-").replace(",", ".")
    amount = Decimal(f"{sign}0{digits}" if digits.startswith(".") else f"{sign}{digits}")
    if abs(amount) > MAX_AMOUNT:
        raise refused
    return amount
