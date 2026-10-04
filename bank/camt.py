"""Reading a bank statement exported as CAMT.053 (« Relevé CAMT.053 », XML ISO
20022, the end-of-day statement `BkToCstmrStmt`) - one of `bank.statements`'
readers, for a format whose kind of file is `StatementFormat.FileType.CAMT053`.

    <Ntry>
      <Amt Ccy="EUR">4.10</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <Sts>BOOK</Sts>                      (<Sts><Cd>BOOK</Cd></Sts> from .08)
      <BookgDt><Dt>2026-08-03</Dt></BookgDt>
      <ValDt><Dt>2026-08-03</Dt></ValDt>
      <BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>CCRD</Cd><SubFmlyCd>POSD</SubFmlyCd></Fmly></Domn></BkTxCd>
      <AddtlNtryInf>CB EPICERIE EXEMPLE 01/08</AddtlNtryInf>
    </Ntry>

**The XML comes from outside**, so it is guarded as `invoices.einvoice`
guards an e-invoice, with a bank's sentences and before any parser sees it
(the guards are copied, not imported: that module's sentences are about
invoices): at most `statements.STRUCTURED_MAX_BYTES`; a UTF-16 or UTF-32
byte order mark, or a NUL in the first four bytes, refused (`WIDE_XML`); a
declared encoding outside `ENCODINGS` refused - expat raises an English
ValueError on a multi-byte one and Python a LookupError on an unknown one,
both of which would reach the page or make a 500; a DOCTYPE or an ENTITY
declaration anywhere refused (`UNSAFE_XML`: `ElementTree` expands internal
entities - the billion laughs). Then `ElementTree.XMLPullParser`, fed in
chunks, every element counted (`MAX_ELEMENTS`) and its depth bounded
(`MAX_DEPTH`), every finished element cleared and let go of - so neither a
file of a million empty elements nor one of a single huge entry holds the
memory of its whole tree; and any ParseError, ValueError or LookupError the
parser raises is `BROKEN_XML`, in French.

What is read, by local names - so every version of the message, .02 to the
current one, reads alike:

* the root must be `Document` in a `urn:iso:std:iso:20022:tech:xsd:camt.053.*`
  namespace - another camt message (052, the day's report; 054, the
  notifications) is said as such (`OTHER_CAMT`), any other XML is no
  statement (`NOT_CAMT`);
* per `Stmt`: its account `Acct/Id/IBAN`, else `Acct/Id/Othr/Id` (several
  `Stmt` of one account - a statement a day - are one file; two accounts
  refuse it) and, when said, `Acct/Ccy`, which must be the euro;
* per `Ntry`, one line, a batch included (several `TxDtls` are one
  operation, as the account shows it): its status (`Sts`, or `Sts/Cd` from
  .08) must be BOOK - a pending entry may never be booked, or be booked
  otherwise; its amount `Amt`, digit for digit, its `Ccy` the euro, negative
  for a `DBIT`; its day `BookgDt/Dt` (or a `DtTm`'s day) and its value date
  `ValDt` the same way, both between 2000 and 2099; its type the ISO bank
  transaction code `Domn/Cd/Fmly/Cd/SubFmlyCd` (« PMNT/CCRD/POSD »), then
  the bank's own `Prtry/Cd` after a space, cut to its column - what the
  recognition rules read in « Type d'opération »; its label `AddtlNtryInf`,
  or, where the bank printed none, each detail's counterparty (the creditor
  of a debit, the debtor of a credit, `Nm` or `Pty/Nm`) and its
  unstructured remittance (`RmtInf/Ustrd`), each with its spaces collapsed.

What each operation IS is the recognition rules' (`statements.
parse_statement` asks them); the balances (`Bal`) are not read, nor the
entries' references: an operation's fingerprint is the CSV's
(`statements.finish`). Every refusal is a French sentence (`REFUSALS`).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from xml.etree import ElementTree

from . import statements
from .statements import BANK_TYPE_MAX, FIRST_DAY, LAST_DAY, MAX_AMOUNT, RawLine, echoed

#: Why a file is refused - each a sentence the page shows.
WIDE_XML = "Ce relevé XML n'est pas encodé en UTF-8 : il est refusé sans être lu."
REFUSED_ENCODING = "Ce relevé XML est encodé en"
UNSAFE_XML = "Ce relevé XML déclare un DOCTYPE ou une entité : il est refusé sans être lu."
BROKEN_XML = "Ce fichier XML ne se lit pas : exportez à nouveau le relevé CAMT.053."
NOT_CAMT = "Ce fichier XML n'est pas un relevé CAMT.053 (ISO 20022)."
OTHER_CAMT = "Ce fichier est un message"
TOO_MANY_ELEMENTS = "Ce relevé XML compte trop d'éléments : exportez une période plus courte."
NOT_BOOKED = "Ce relevé contient des opérations non comptabilisées"
DIRECTION = "Sens de l'opération illisible dans le relevé"
NO_CURRENCY = "Une opération du relevé CAMT.053 ne dit pas sa devise : exportez-le à nouveau."
INCOMPLETE = "Opération incomplète dans le relevé CAMT.053"
NO_OPERATION = "Aucune opération trouvée dans ce relevé CAMT.053"
#: What every refusal of this reader begins with (statements' shared ones -
#: dates, amounts, accounts, currency, bounds - apart): a test runs mutated
#: files through it and finds nothing else.
REFUSALS = (
    WIDE_XML,
    REFUSED_ENCODING,
    UNSAFE_XML,
    BROKEN_XML,
    NOT_CAMT,
    OTHER_CAMT,
    TOO_MANY_ELEMENTS,
    NOT_BOOKED,
    DIRECTION,
    NO_CURRENCY,
    INCOMPLETE,
    NO_OPERATION,
)

#: The encodings a statement may declare - each one byte a character, or
#: UTF-8: expat reads them, and the byte grep for a DOCTYPE sees them.
ENCODINGS = frozenset({"utf-8", "us-ascii", "iso-8859-1", "iso-8859-15", "windows-1252"})
NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:camt."
STATEMENT_MESSAGE = "053."
#: The most elements, and the deepest nesting, a statement is read with: a
#: real entry is a few dozen elements a dozen deep.
MAX_ELEMENTS = 1_000_000
MAX_DEPTH = 64
#: How much of the file the parser is fed at a time.
CHUNK = 64 * 1024
EURO = "EUR"
BOOKED = "BOOK"

_WIDE_MARKS = (b"\xff\xfe", b"\xfe\xff")
_DECLARED_ENCODING = re.compile(rb"""^\s*<\?xml[^>]*?\sencoding\s*=\s*["']([^"']*)["']""")
_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT = re.compile(r"[0-9]{1,18}(?:\.[0-9]{1,5})?")
#: Where each datum of an entry is, by the local names under `Ntry`.
_ENTRY_LEAVES = {
    ("Amt",): "amount",
    ("CdtDbtInd",): "direction",
    ("Sts",): "status",
    ("Sts", "Cd"): "status_code",
    ("BookgDt", "Dt"): "booked",
    ("BookgDt", "DtTm"): "booked_at",
    ("ValDt", "Dt"): "value",
    ("ValDt", "DtTm"): "value_at",
    ("BkTxCd", "Domn", "Cd"): "domain",
    ("BkTxCd", "Domn", "Fmly", "Cd"): "family",
    ("BkTxCd", "Domn", "Fmly", "SubFmlyCd"): "subfamily",
    ("BkTxCd", "Prtry", "Cd"): "proprietary",
    ("AddtlNtryInf",): "information",
}
#: And of each of its transactions' details, under `NtryDtls/TxDtls`.
_DETAIL_LEAVES = {
    ("RltdPties", "Cdtr", "Nm"): "creditor",
    ("RltdPties", "Cdtr", "Pty", "Nm"): "creditor",
    ("RltdPties", "Dbtr", "Nm"): "debtor",
    ("RltdPties", "Dbtr", "Pty", "Nm"): "debtor",
}
_REMITTANCE = ("RmtInf", "Ustrd")


class CamtReading:
    """One CAMT.053 file read for `statements.parse_statement`: `lines()`
    yields a `RawLine` per `Ntry` as it ends; `account` is final once they
    are all read; `no_operation` is the sentence a file of none is refused
    with."""

    def __init__(self, content: bytes, layout: statements.Layout):
        self.layout = layout
        self.account = ""
        said = f" (format « {layout.name} »)" if layout.name else ""
        self.no_operation = f"{NO_OPERATION}{said}."
        _guard(content)
        self._content = content
        #: The document's root once read: every finished element let go of.
        self.root = None

    def lines(self):
        accounts: set[str] = set()
        path: list[str] = []
        elements: list = []
        statement: dict | None = None
        entry: dict | None = None
        detail: dict | None = None
        counted = 0
        for event, element in _events(self._content):
            if event == "start":
                counted += 1
                if counted > MAX_ELEMENTS:
                    raise ValueError(TOO_MANY_ELEMENTS)
                if len(path) >= MAX_DEPTH:
                    raise ValueError(NOT_CAMT)
                name = _local(element.tag)
                if not path:
                    _check_root(element.tag)
                    self.root = element
                path.append(name)
                elements.append(element)
                if name == "Stmt":
                    statement = {"account": "", "said": False}
                elif name == "Ntry" and statement is not None and path[-2] == "Stmt":
                    entry = {"details": []}
                elif name == "TxDtls" and entry is not None and path[-3:-1] == ["Ntry", "NtryDtls"]:
                    detail = {"remittance": []}
                    entry["details"].append(detail)
                continue
            # The end of an element: its text is whole.
            name = path[-1]
            text = (element.text or "").strip()
            if entry is not None and "Ntry" in path:
                below = tuple(path[path.index("Ntry") + 1 :])
                if below in _ENTRY_LEAVES:
                    entry.setdefault(_ENTRY_LEAVES[below], text)
                    if below == ("Amt",):
                        entry["currency"] = element.get("Ccy")
                elif detail is not None and below[:2] == ("NtryDtls", "TxDtls"):
                    within = below[2:]
                    if within in _DETAIL_LEAVES:
                        detail.setdefault(_DETAIL_LEAVES[within], text)
                    elif within == _REMITTANCE and text:
                        detail["remittance"].append(text)
                if name == "Ntry" and len(below) == 0:
                    yield _line(entry)
                    entry = detail = None
                elif name == "TxDtls":
                    detail = None
            elif statement is not None and "Stmt" in path and "Ntry" not in path:
                below = tuple(path[path.index("Stmt") + 1 :])
                if below in (("Acct", "Id", "IBAN"), ("Acct", "Id", "Othr", "Id")) and text:
                    if below[-1] == "IBAN" or not statement["said"]:
                        statement["account"], statement["said"] = text, below[-1] == "IBAN"
                elif below == ("Acct", "Ccy") and text and text.upper() != EURO:
                    raise ValueError(statements.not_euros(text))
                elif below == ("Acct",):
                    accounts.add(statement["account"])
                    if len(accounts) > 1:
                        raise ValueError(statements.several_accounts(len(accounts)))
                    self.account = statement["account"]
                elif name == "Stmt" and len(below) == 0:
                    statement = None
            # Let go of what is read: cleared, and out of its parent.
            path.pop()
            elements.pop()
            element.clear()
            if elements:
                elements[-1].remove(element)


def _guard(content: bytes) -> None:
    """What is refused before any parser sees the file: its size, a wide
    encoding, an encoding outside `ENCODINGS`, a DOCTYPE or an entity."""
    if len(content) > statements.STRUCTURED_MAX_BYTES:
        raise ValueError(statements.too_big())
    if content.startswith((*_WIDE_MARKS, b"\x00\x00\xfe\xff")) or b"\x00" in content[:4]:
        raise ValueError(WIDE_XML)
    head = content[3:512] if content.startswith(b"\xef\xbb\xbf") else content[:512]
    declared = _DECLARED_ENCODING.match(head)
    if declared is not None:
        encoding = declared.group(1).decode("ascii", "replace")
        if encoding.lower() not in ENCODINGS:
            raise ValueError(f"{REFUSED_ENCODING} « {echoed(encoding)} » : il est refusé sans être lu.")
    if _DECLARATION.search(content):
        raise ValueError(UNSAFE_XML)


def _events(content: bytes):
    """("start" | "end", element) as the parser reads `content`, fed in
    chunks - whatever it raises said as `BROKEN_XML`."""
    parser = ElementTree.XMLPullParser(events=("start", "end"))
    try:
        for offset in range(0, len(content), CHUNK):
            parser.feed(content[offset : offset + CHUNK])
            yield from parser.read_events()
        parser.close()
        yield from parser.read_events()
    except (ElementTree.ParseError, ValueError, LookupError):
        raise ValueError(BROKEN_XML) from None


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _check_root(tag: str) -> None:
    """A camt.053 `Document`, said apart from another camt message and from
    any other XML."""
    namespace, _, local = tag.rpartition("}")
    namespace = namespace.lstrip("{")
    if not namespace.startswith(NAMESPACE) or local != "Document":
        raise ValueError(NOT_CAMT)
    message = namespace[len(NAMESPACE) :]
    if not message.startswith(STATEMENT_MESSAGE):
        said = echoed(f"camt.{message.split('.', 1)[0]}")
        raise ValueError(
            f"{OTHER_CAMT} {said} : seul le relevé de fin de journée (camt.053) s'importe ; "
            "exportez le relevé de compte."
        )


def _line(entry: dict) -> RawLine:
    """The operation of one `Ntry`, as `statements.finish` takes it."""
    status = entry.get("status_code") or entry.get("status") or ""
    if status.upper() != BOOKED:
        said = echoed(status) if status else "sans statut"
        raise ValueError(f"{NOT_BOOKED} (« {said} ») : exportez le relevé de fin de journée.")
    currency = entry.get("currency")
    if not currency:
        raise ValueError(NO_CURRENCY)
    if currency.upper() != EURO:
        raise ValueError(statements.not_euros(currency))
    printed = entry.get("amount", "")
    if _AMOUNT.fullmatch(printed) is None:
        raise ValueError(f"Montant illisible dans le relevé : {echoed(printed)!r}")
    amount = Decimal(printed)
    if amount > MAX_AMOUNT:
        raise ValueError(f"Montant illisible dans le relevé : {echoed(printed)!r}")
    direction = entry.get("direction", "")
    if direction == "DBIT":
        amount = -amount
    elif direction != "CRDT":
        raise ValueError(f"{DIRECTION} : {echoed(direction)!r}")
    booked = entry.get("booked") or entry.get("booked_at")
    if not booked:
        raise ValueError(f"{INCOMPLETE} (sans date de comptabilisation) : exportez-le à nouveau.")
    value = entry.get("value") or entry.get("value_at")
    return RawLine(
        operation_date=_day(booked),
        value_date=_day(value) if value else None,
        bank_type=_bank_type(entry),
        label=_label(entry, debit=amount < 0),
        amount=amount,
    )


def _day(printed: str) -> date:
    """A `Dt` (YYYY-MM-DD), or a `DtTm`'s day: refused when it is no day,
    or no day a statement holds."""
    refused = ValueError(f"Date illisible dans le relevé : {echoed(printed)!r}")
    if _DAY.match(printed) is None:
        raise refused
    try:
        day = date.fromisoformat(printed[:10])
    except ValueError:
        raise refused from None
    if not FIRST_DAY <= day <= LAST_DAY:
        raise refused
    return day


def _bank_type(entry: dict) -> str:
    """« PMNT/CCRD/POSD », then the bank's own code after a space - cut to
    its column before the rules read it."""
    code = "/".join(part for part in (entry.get(key, "") for key in ("domain", "family", "subfamily")) if part)
    said = " ".join(part for part in (code, entry.get("proprietary", "")) if part)
    return said[:BANK_TYPE_MAX].rstrip()


def _label(entry: dict, *, debit: bool) -> str:
    """`AddtlNtryInf`, else each detail's counterparty and remittance."""
    information = " ".join(entry.get("information", "").split())
    if information:
        return information
    parts = []
    for detail in entry["details"]:
        parts.append(detail.get("creditor" if debit else "debtor", ""))
        parts.extend(detail["remittance"])
    return " ".join(" ".join(part.split()) for part in parts if part.strip())
