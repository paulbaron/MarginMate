"""OFX statements for the tests, written by hand: the structure is the
published standard's - OFX 1.0.2 (SGML, its leaf elements left open, behind
the plain-text header) and OFX 2.1.1 (XML behind its `<?xml ?>` and `<?OFX ?>`
declarations), section 11 « Banking », the bank statement response
(`STMTTRNRS` > `STMTRS`: `CURDEF`, `BANKACCTFROM`, `BANKTRANLIST` of
`STMTTRN`, `LEDGERBAL`) and the card's (`CCSTMTTRNRS` > `CCSTMTRS`,
`CCACCTFROM`). Every bank code, account number, name, reference and amount
is invented.

No real export has been seen yet: which TRNTYPE codes a French bank really
prints (POS, DIRECTDEBIT, XFER… or DEBIT/CREDIT for everything), and how it
fills NAME and MEMO, must be checked against one before the « Relevé OFX »
preset's rules are trusted (CLAUDE.md, « The statement's layout »).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

#: The account the statements below are of - invented.
ACCOUNT = "00012345678"


@dataclass(frozen=True)
class Operation:
    """One `STMTTRN`: its leaves in the standard's order, None left out."""

    trntype: str = "POS"
    dtposted: str = "20260803"
    dtuser: str | None = "20260801"
    dtavail: str | None = None
    trnamt: str = "-4.10"
    fitid: str = "0000000001"
    name: str | None = "CB EPICERIE EXEMPLE"
    memo: str | None = "FACTURE CARTE DU 010826"
    #: Extra elements written inside it, as they are (an aggregate).
    extra: tuple = field(default_factory=tuple)

    def leaves(self) -> list[tuple[str, str]]:
        pairs = [
            ("TRNTYPE", self.trntype),
            ("DTPOSTED", self.dtposted),
            ("DTUSER", self.dtuser),
            ("DTAVAIL", self.dtavail),
            ("TRNAMT", self.trnamt),
            ("FITID", self.fitid),
            ("NAME", self.name),
            ("MEMO", self.memo),
        ]
        return [(tag, value) for tag, value in pairs if value is not None]


#: A month of a bar's account, invented: a card payment and its twin (two
#: baguettes, one morning), a direct debit, a transfer received with its
#: value date, a fee whose name holds an ampersand and accents.
OPERATIONS = (
    Operation(),
    Operation(fitid="0000000002"),
    Operation(
        trntype="DIRECTDEBIT",
        dtposted="20260809120000.000[+2:CEST]",
        dtuser=None,
        trnamt="-120.35",
        fitid="0000000003",
        name="PRLV SEPA FOURNISSEUR EXEMPLE",
        memo="ECH/090826 REF/000123",
    ),
    Operation(
        trntype="XFER",
        dtposted="20260805",
        dtuser=None,
        dtavail="20260806",
        trnamt="250.00",
        fitid="0000000004",
        name="VIR SEPA RECU CLIENT EXEMPLE",
        memo=None,
    ),
    Operation(
        trntype="SRVCHG",
        dtposted="20260831",
        dtuser=None,
        trnamt="-8.50",
        fitid="0000000005",
        name="FRAIS CAFÉ ÉTOILE &amp; FILS",
        memo="COTISATION",
    ),
)

SGML_HEADER = (
    "OFXHEADER:100\n"
    "DATA:OFXSGML\n"
    "VERSION:102\n"
    "SECURITY:NONE\n"
    "ENCODING:USASCII\n"
    "CHARSET:1252\n"
    "COMPRESSION:NONE\n"
    "OLDFILEUID:NONE\n"
    "NEWFILEUID:NONE\n"
    "\n"
)
XML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    '<?OFX OFXHEADER="200" VERSION="211" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>\n'
)


def _leaf(tag: str, value: str, xml: bool) -> str:
    return f"<{tag}>{value}</{tag}>" if xml else f"<{tag}>{value}"


def _transaction(operation: Operation, xml: bool, tag: str = "STMTTRN") -> list[str]:
    return [
        f"<{tag}>",
        *(_leaf(name, value, xml) for name, value in operation.leaves()),
        *operation.extra,
        f"</{tag}>",
    ]


def statement_block(
    operations=OPERATIONS,
    *,
    xml: bool,
    account: str | None = ACCOUNT,
    currency: str | None = "EUR",
    card: bool = False,
    pending=(),
) -> list[str]:
    """One `STMTTRNRS` (or the card's `CCSTMTTRNRS`): its statement, its
    account, its operations, the pending ones in `BANKTRANLISTP` (OFX 2.1)
    and its balance."""
    rs, trnrs, account_tag = (
        ("CCSTMTRS", "CCSTMTTRNRS", "CCACCTFROM") if card else ("STMTRS", "STMTTRNRS", "BANKACCTFROM")
    )
    account_lines = (
        [f"<{account_tag}>", _leaf("ACCTID", account, xml), f"</{account_tag}>"]
        if card
        else [
            f"<{account_tag}>",
            _leaf("BANKID", "99999", xml),
            _leaf("BRANCHID", "00001", xml),
            _leaf("ACCTID", account, xml),
            _leaf("ACCTTYPE", "CHECKING", xml),
            f"</{account_tag}>",
        ]
    )
    if account is None:
        account_lines = []
    lines = [
        f"<{trnrs}>",
        _leaf("TRNUID", "1001", xml),
        "<STATUS>",
        _leaf("CODE", "0", xml),
        _leaf("SEVERITY", "INFO", xml),
        "</STATUS>",
        f"<{rs}>",
        *([_leaf("CURDEF", currency, xml)] if currency is not None else []),
        *account_lines,
        "<BANKTRANLIST>",
        _leaf("DTSTART", "20260801", xml),
        _leaf("DTEND", "20260831", xml),
    ]
    for operation in operations:
        lines += _transaction(operation, xml)
    lines.append("</BANKTRANLIST>")
    if pending:
        lines += ["<BANKTRANLISTP>", _leaf("DTASOF", "20260831", xml)]
        for operation in pending:
            lines += _transaction(operation, xml, tag="STMTTRNP")
        lines.append("</BANKTRANLISTP>")
    lines += [
        "<LEDGERBAL>",
        _leaf("BALAMT", "1234.56", xml),
        _leaf("DTASOF", "20260831", xml),
        "</LEDGERBAL>",
        f"</{rs}>",
        f"</{trnrs}>",
    ]
    return lines


def ofx(*blocks, xml: bool = False, card: bool = False, encoding: str | None = None) -> bytes:
    """A whole OFX file around statement `blocks` (each `statement_block`),
    in SGML (1.0.2, Windows-1252 as its header says) or XML (2.1.1, UTF-8)."""
    if not blocks:
        blocks = (statement_block(xml=xml, card=card),)
    messages = "CREDITCARDMSGSRSV1" if card else "BANKMSGSRSV1"
    lines = [
        "<OFX>",
        "<SIGNONMSGSRSV1>",
        "<SONRS>",
        "<STATUS>",
        _leaf("CODE", "0", xml),
        _leaf("SEVERITY", "INFO", xml),
        "</STATUS>",
        _leaf("DTSERVER", "20260831120000.000[+2:CEST]", xml),
        _leaf("LANGUAGE", "FRA", xml),
        "</SONRS>",
        "</SIGNONMSGSRSV1>",
        f"<{messages}>",
        *(line for block in blocks for line in block),
        f"</{messages}>",
        "</OFX>",
    ]
    text = (XML_HEADER if xml else SGML_HEADER) + "\n".join(lines) + "\n"
    return text.encode(encoding or ("utf-8" if xml else "cp1252"))


def sgml(operations=OPERATIONS, **options) -> bytes:
    return ofx(statement_block(operations, xml=False, **options), xml=False, card=options.get("card", False))


def xml(operations=OPERATIONS, **options) -> bytes:
    return ofx(statement_block(operations, xml=True, **options), xml=True, card=options.get("card", False))


def changed(operation: Operation = OPERATIONS[0], **changes) -> Operation:
    return replace(operation, **changes)
