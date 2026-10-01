"""Reading a bank statement exported as CSV.

    "Compte de chèques";"Compte de chèques";****0042;14/09/2026;;1 234,56
    03/08/2026;PAIEMENT CB;FACTURE CARTE;FACTURE CARTE DU 010826 FRANPRIX 5333   PARIS   CARTE   4974XXXXXXXX1111;03/08/2026;-4,10

The layout is the BNP Paribas export's: a header line (account, export date,
balance), then one line per operation: date, type, short type, label, value
date, amount - French decimals, a space between thousands, negative when
money went out. What each operation IS - its kind, its payee, the day a card
was used - is not read here but by the rules a person edits
(`bank.recognition.describe`, handed in as `rules`): no bank's words are
written in this module. Nothing here touches the database, so every layout
quirk is testable from a string.
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

from . import recognition

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
ACCOUNT_RE = re.compile(r"\*{2,}\d+")


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


def parse_statement(content: bytes, rules: recognition.Rules) -> Statement:
    """Every operation of the file, each described by `rules` (one
    `recognition.load()`, read by the caller). Refused - ValueError, a
    French sentence - for a file that is no statement, a row cut short, an
    amount that cannot be read, and, once every row was read, any rule that
    could not be applied (`Rules.refusal`): a kind stored wrong is never
    read again."""
    rows = [
        row for row in csv.reader(io.StringIO(_decode(content)), delimiter=";") if any(cell.strip() for cell in row)
    ]
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
        bank_type = row[1].strip()
        operation_date = _date(row[0])
        described = recognition.describe(rules, label, bank_type, operation_date)
        lines.append(
            StatementLine(
                operation_date=operation_date,
                value_date=_date(row[4]) if DATE_RE.match(row[4].strip()) else None,
                bank_type=bank_type,
                label=label,
                amount=parse_amount(row[5]),
                kind=described.kind,
                counterparty=described.counterparty,
                card_date=described.card_date,
            )
        )
    if not lines:
        raise ValueError("Aucune opération trouvée : ce fichier ne ressemble pas à un relevé bancaire exporté en CSV.")
    # After every row: a rule found too slow on the last one counts too.
    if rules.refusal:
        raise ValueError(rules.refusal)
    _fingerprint(account, lines)
    return Statement(account=account, lines=lines)


def parse_amount(text: str) -> Decimal:
    cleaned = (
        text.strip()
        .replace("\N{NO-BREAK SPACE}", "")
        .replace("\N{NARROW NO-BREAK SPACE}", "")
        .replace(" ", "")
        .replace(",", ".")
    )
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
    return datetime.strptime(text.strip(), "%d/%m/%Y").date()  # noqa: DTZ007 - the statement's printed day, read into .date()


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
