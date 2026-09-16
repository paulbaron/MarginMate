"""Reading a bank statement exported as CSV (BNP Paribas).

    "Compte de chèques";"Compte de chèques";****0042;14/09/2026;;1 234,56
    03/08/2026;PAIEMENT CB;FACTURE CARTE;FACTURE CARTE DU 010826 FRANPRIX 5333   PARIS   CARTE   4974XXXXXXXX1111;03/08/2026;-4,10

A header line (account, export date, balance), then one line per operation:
date, type, short type, label, value date, amount - French decimals, a space
between thousands, negative when money went out. Nothing here touches the
database, so every layout quirk is testable from a string.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
ACCOUNT_RE = re.compile(r"\*{2,}\d+")
# "FACTURE CARTE DU 150726 FRANPRIX 5333 PARIS CARTE 4974XXXXXXXX1111": the
# day the card was used, then the merchant up to the masked card number.
CARD_RE = re.compile(r"FACTURE CARTE DU (\d{2})(\d{2})(\d{2}) (.*?)\s+CARTE\s+\d{4}X+\d{4}")
DEBIT_RE = re.compile(r"^PRLV SEPA (?:B2B )?(.*?) ECH/")
TRANSFER_OUT_RE = re.compile(r"/BEN (.*?) /REFDO")
TRANSFER_IN_RE = re.compile(r"/FRM (.*?) /")


@dataclass
class StatementLine:
    operation_date: date
    value_date: date | None
    bank_type: str
    label: str
    amount: Decimal
    kind: str
    counterparty: str
    card_date: date | None
    fingerprint: str = ""


@dataclass
class Statement:
    account: str
    lines: list[StatementLine] = field(default_factory=list)


def parse_statement(content: bytes) -> Statement:
    rows = [row for row in csv.reader(io.StringIO(_decode(content)), delimiter=";") if any(cell.strip() for cell in row)]
    account = ""
    lines: list[StatementLine] = []
    for row in rows:
        if not DATE_RE.match(row[0].strip()):
            found = ACCOUNT_RE.search(";".join(row))
            if found and not lines:
                account = found.group()
            continue
        if len(row) < 6:
            raise ValueError(f"Ligne incomplète dans le relevé : {';'.join(row)[:80]}")
        label = " ".join(row[3].split())
        kind, counterparty, card_date = describe(label)
        lines.append(
            StatementLine(
                operation_date=_date(row[0]),
                value_date=_date(row[4]) if DATE_RE.match(row[4].strip()) else None,
                bank_type=row[1].strip(),
                label=label,
                amount=parse_amount(row[5]),
                kind=kind,
                counterparty=counterparty,
                card_date=card_date,
            )
        )
    if not lines:
        raise ValueError("Aucune opération trouvée : ce fichier ne ressemble pas à un relevé bancaire exporté en CSV.")
    _fingerprint(account, lines)
    return Statement(account=account, lines=lines)


def describe(label: str) -> tuple[str, str, date | None]:
    """(kind, counterparty, card date) from an operation's label."""
    card = CARD_RE.search(label)
    if card:
        day, month, year, merchant = card.groups()
        try:
            card_date = date(2000 + int(year), int(month), int(day))
        except ValueError:
            card_date = None
        return "CARD", merchant.strip(), card_date
    debit = DEBIT_RE.search(label)
    if debit:
        return "DEBIT", debit.group(1).strip(), None
    if label.startswith("VIR"):
        found = TRANSFER_OUT_RE.search(label) or TRANSFER_IN_RE.search(label)
        return "TRANSFER", found.group(1).strip() if found else "", None
    return "OTHER", "", None


def parse_amount(text: str) -> Decimal:
    cleaned = text.strip().replace(" ", "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"Montant illisible dans le relevé : {text!r}") from None


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252")


def _date(text: str) -> date:
    return datetime.strptime(text.strip(), "%d/%m/%Y").date()


def _fingerprint(account: str, lines: list[StatementLine]) -> None:
    """The same operation in two exports gets the same fingerprint; two
    identical operations in one export (two baguettes, same morning, same
    card) get different ones - by their order among the identical rows."""
    seen: Counter = Counter()
    for line in lines:
        value_date = line.value_date.isoformat() if line.value_date else ""
        key = f"{account}|{line.operation_date.isoformat()}|{value_date}|{line.label}|{line.amount}"
        occurrence = seen[key]
        seen[key] += 1
        line.fingerprint = hashlib.sha256(f"{key}|{occurrence}".encode()).hexdigest()
