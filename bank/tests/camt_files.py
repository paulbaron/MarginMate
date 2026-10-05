"""CAMT.053 statements for the tests, written by hand: the structure is the
published ISO 20022 message's - `Document` > `BkToCstmrStmt` > `GrpHdr`,
then `Stmt` (`Id`, `CreDtTm`, `FrToDt`, `Acct` with its `IBAN` and `Ccy`,
`Bal`, `Ntry`…), each `Ntry` with its `Amt Ccy=`, `CdtDbtInd`, `Sts`,
`BookgDt`, `ValDt`, `AcctSvcrRef`, `BkTxCd` (`Domn/Cd`, `Fmly/Cd`,
`SubFmlyCd`, `Prtry`), `NtryDtls/TxDtls` and `AddtlNtryInf`, in the
schema's order - in its two shapes the reader tells apart: version .02
(camt.053.001.02: `<Sts>BOOK</Sts>`, a party's `<Nm>` directly under
`Cdtr`/`Dbtr`) and .08 (camt.053.001.08: `<Sts><Cd>BOOK</Cd></Sts>`, the
party under `Pty`). Every IBAN, BIC, name, reference and amount is
invented.

No real export has been seen yet: whether a French bank fills
AddtlNtryInf with its usual label, and which family and sub-family codes it
really uses for a card payment or a direct debit, must be checked against
one before the « Relevé CAMT.053 » preset's rules are trusted (CLAUDE.md,
« The statement's layout »).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from xml.sax.saxutils import escape

#: The account of the statements below - invented, never a real IBAN.
IBAN = "FR7699999000010001234567890"
V02 = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"
V08 = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"


@dataclass(frozen=True)
class Detail:
    """One `TxDtls`: a counterparty and what it wrote."""

    creditor: str | None = None
    debtor: str | None = None
    remittance: tuple = ()


@dataclass(frozen=True)
class Entry:
    """One `Ntry`."""

    amount: str = "4.10"
    currency: str | None = "EUR"
    direction: str = "DBIT"
    status: str = "BOOK"
    booked: str = "2026-08-03"
    booked_at: str | None = None
    value: str | None = "2026-08-03"
    domain: str | None = "PMNT"
    family: str | None = "CCRD"
    subfamily: str | None = "POSD"
    proprietary: str | None = None
    information: str | None = "CB EPICERIE EXEMPLE 01/08"
    details: tuple = field(
        default_factory=lambda: (Detail(creditor="EPICERIE EXEMPLE", remittance=("FACTURE CARTE DU 010826",)),)
    )
    reference: str = "REF0001"


#: A month of a bar's account, invented: a card payment and its twin, a
#: direct debit, a transfer received with a value date, a batch of two
#: transfers sent (one line), and a fee booked at a moment.
ENTRIES = (
    Entry(),
    Entry(reference="REF0002"),
    Entry(
        amount="120.35",
        booked="2026-08-09",
        value="2026-08-09",
        family="RDDT",
        subfamily="ESDD",
        information="PRLV SEPA FOURNISSEUR EXEMPLE ECH/090826",
        details=(Detail(creditor="FOURNISSEUR EXEMPLE", remittance=("ECH/090826 REF/000123",)),),
        reference="REF0003",
    ),
    Entry(
        amount="250.00",
        direction="CRDT",
        booked="2026-08-05",
        value="2026-08-06",
        family="RCDT",
        subfamily="ESCT",
        information="VIR SEPA RECU CLIENT EXEMPLE",
        details=(Detail(debtor="CLIENT EXEMPLE", remittance=("FACTURE 2026-071",)),),
        reference="REF0004",
    ),
    Entry(
        amount="1500.00",
        booked="2026-08-20",
        value="2026-08-20",
        family="ICDT",
        subfamily="ESCT",
        information=None,
        details=(
            Detail(creditor="SALARIE UN EXEMPLE", remittance=("SALAIRE AOUT",)),
            Detail(creditor="SALARIE DEUX EXEMPLE", remittance=("SALAIRE AOUT",)),
        ),
        reference="REF0005",
    ),
    Entry(
        amount="8.50",
        booked=None,
        booked_at="2026-08-31T23:59:00",
        value=None,
        domain="ACMT",
        family="MDOP",
        subfamily="CHRG",
        proprietary="FRAIS",
        information="FRAIS TENUE DE COMPTE CAFÉ & FILS",
        details=(),
        reference="REF0006",
    ),
)


def changed(entry: Entry = ENTRIES[0], **changes) -> Entry:
    return replace(entry, **changes)


def _party(role: str, name: str, version: str) -> list[str]:
    inner = f"<Nm>{escape(name)}</Nm>"
    return [f"<{role}>", f"<Pty>{inner}</Pty>" if version == V08 else inner, f"</{role}>"]


def _entry(entry: Entry, version: str) -> list[str]:
    lines = [
        "<Ntry>",
        f"<NtryRef>{entry.reference}</NtryRef>",
        f'<Amt Ccy="{entry.currency}">{entry.amount}</Amt>' if entry.currency else f"<Amt>{entry.amount}</Amt>",
        f"<CdtDbtInd>{entry.direction}</CdtDbtInd>",
        f"<Sts><Cd>{entry.status}</Cd></Sts>" if version == V08 else f"<Sts>{entry.status}</Sts>",
    ]
    if entry.booked:
        lines.append(f"<BookgDt><Dt>{entry.booked}</Dt></BookgDt>")
    elif entry.booked_at:
        lines.append(f"<BookgDt><DtTm>{entry.booked_at}</DtTm></BookgDt>")
    if entry.value:
        lines.append(f"<ValDt><Dt>{entry.value}</Dt></ValDt>")
    lines.append(f"<AcctSvcrRef>{entry.reference}</AcctSvcrRef>")
    if entry.domain or entry.proprietary:
        lines.append("<BkTxCd>")
        if entry.domain:
            lines += [
                f"<Domn><Cd>{entry.domain}</Cd><Fmly><Cd>{entry.family}</Cd>",
                f"<SubFmlyCd>{entry.subfamily}</SubFmlyCd></Fmly></Domn>",
            ]
        if entry.proprietary:
            lines.append(f"<Prtry><Cd>{entry.proprietary}</Cd><Issr>BANQUE EXEMPLE</Issr></Prtry>")
        lines.append("</BkTxCd>")
    if entry.details:
        lines.append("<NtryDtls>")
        if len(entry.details) > 1:
            lines.append(f"<Btch><NbOfTxs>{len(entry.details)}</NbOfTxs></Btch>")
        for detail in entry.details:
            lines += ["<TxDtls>", "<Refs><EndToEndId>NOTPROVIDED</EndToEndId></Refs>"]
            if detail.creditor or detail.debtor:
                lines.append("<RltdPties>")
                if detail.debtor:
                    lines += _party("Dbtr", detail.debtor, version)
                if detail.creditor:
                    lines += _party("Cdtr", detail.creditor, version)
                lines.append("</RltdPties>")
            if detail.remittance:
                lines += ["<RmtInf>", *(f"<Ustrd>{escape(text)}</Ustrd>" for text in detail.remittance), "</RmtInf>"]
            lines.append("</TxDtls>")
        lines.append("</NtryDtls>")
    if entry.information is not None:
        lines.append(f"<AddtlNtryInf>{escape(entry.information)}</AddtlNtryInf>")
    lines.append("</Ntry>")
    return lines


def statement_block(entries=ENTRIES, *, version: str = V02, iban: str = IBAN, currency: str | None = "EUR", number=1):
    """One `Stmt`: its account, its two balances, its entries."""
    account = [
        "<Acct>",
        f"<Id><IBAN>{iban}</IBAN></Id>",
        *([f"<Ccy>{currency}</Ccy>"] if currency else []),
        "<Svcr><FinInstnId><BIC>EXMPFRPPXXX</BIC></FinInstnId></Svcr>",
        "</Acct>",
    ]
    balances = [
        "<Bal>",
        "<Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>",
        '<Amt Ccy="EUR">1000.00</Amt>',
        "<CdtDbtInd>CRDT</CdtDbtInd>",
        "<Dt><Dt>2026-08-01</Dt></Dt>",
        "</Bal>",
        "<Bal>",
        "<Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>",
        '<Amt Ccy="EUR">1234.56</Amt>',
        "<CdtDbtInd>CRDT</CdtDbtInd>",
        "<Dt><Dt>2026-08-31</Dt></Dt>",
        "</Bal>",
    ]
    lines = [
        "<Stmt>",
        f"<Id>STMT-2026-08-{number:04d}</Id>",
        f"<ElctrncSeqNb>{number}</ElctrncSeqNb>",
        "<CreDtTm>2026-08-31T18:00:00</CreDtTm>",
        "<FrToDt><FrDtTm>2026-08-01T00:00:00</FrDtTm><ToDtTm>2026-08-31T23:59:59</ToDtTm></FrToDt>",
        *account,
        *balances,
    ]
    for entry in entries:
        lines += _entry(entry, version)
    lines.append("</Stmt>")
    return lines


def camt(*blocks, version: str = V02, encoding: str = "UTF-8", declaration: bool = True) -> bytes:
    """A whole camt.053 document around statement `blocks`."""
    if not blocks:
        blocks = (statement_block(version=version),)
    lines = [
        f'<Document xmlns="{version}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
        "<BkToCstmrStmt>",
        "<GrpHdr>",
        "<MsgId>STMT-20260831-0001</MsgId>",
        "<CreDtTm>2026-08-31T18:00:00</CreDtTm>",
        "</GrpHdr>",
        *(line for block in blocks for line in block),
        "</BkToCstmrStmt>",
        "</Document>",
    ]
    head = f'<?xml version="1.0" encoding="{encoding}"?>\n' if declaration else ""
    codec = {"UTF-8": "utf-8", "windows-1252": "cp1252", "ISO-8859-15": "iso-8859-15"}.get(encoding, "utf-8")
    return (head + "\n".join(lines) + "\n").encode(codec)


def v02(entries=ENTRIES, **options) -> bytes:
    return camt(statement_block(entries, version=V02, **options), version=V02)


def v08(entries=ENTRIES, **options) -> bytes:
    return camt(statement_block(entries, version=V08, **options), version=V08)
