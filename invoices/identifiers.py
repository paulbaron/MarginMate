"""What a document prints that tells its seller, whatever its layout: a
company number (a SIREN, alone, in a SIRET or in a VAT number), a phone
number, a web site.

A shop named by hand learns the ones its tickets print (receipts.
learn_identifiers), so that its next ticket is recognised even when its header
is torn or was never given. Each is checked the way it is built - a SIREN by
its Luhn digit, a VAT number by its key, a phone number by its pairs - since a
misread one must not name a shop. Plain functions, no database.

Keys are "siren:900000019", "tel:0123456789", "web:brico-exemple.fr".
"""

from __future__ import annotations

import re
from functools import lru_cache

# Where nine digits alone are a company number.
SIREN_LABEL_RE = re.compile(r"(?i)\b(?:siren|siret|rcs)\b")
# "FR 25 900 000 019": the country, a key, the SIREN.
VAT_RE = re.compile(r"(?<![A-Za-z0-9])FR ?([0-9A-Z]{2}) ?(\d{3}) ?(\d{3}) ?(\d{3})(?!\d)")
# Digits in groups separated by single spaces: "900 000 019 10000".
DIGIT_RUN_RE = re.compile(r"(?<![\d,.])\d(?:[  ]?\d){8,}(?!\d|[.,]\d)")
# "01 23 45 67 89", "01.23.45.67.89", "0123456789", "+33 1 23 45 67 89":
# the same separator between every pair - "01.23 45.67 89.00" is prices.
PHONE_RE = re.compile(
    r"(?<!\d)(?<!\d[.,])(?:\+33 ?(?:\(0\) ?)?|0)[1-9]([ .-]?)\d{2}(?:\1\d{2}){3}(?!\d|[.,]\d)"
)
# A web address: after "www." or a scheme, any domain; bare, the usual
# endings only. Never the part of an e-mail address after "@" - a customer's
# address is printed on invoices too, and a mail provider is everybody's.
WEB_RE = re.compile(r"(?i)(?<![\w@.-])(?:https?://)?www\.((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w@-])")
SCHEME_RE = re.compile(r"(?i)(?<![\w@.-])https?://((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w@-])")
BARE_DOMAIN_RE = re.compile(
    r"(?i)(?<![\w@.-])((?:[a-z0-9-]+\.)*[a-z][a-z0-9-]{2,}\.(?:fr|com|net|org|eu|shop|paris|biz|info|io))(?![\w@-]|\.\w)"
)

SIREN_LENGTH = 9
SIRET_LENGTH = 14


def luhn_valid(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def is_siren(digits: str) -> bool:
    return len(digits) == SIREN_LENGTH and digits.isdigit() and luhn_valid(digits)


def vat_key(siren: str) -> str:
    """The two digits a French VAT number puts before `siren`."""
    return f"{(12 + 3 * (int(siren) % 97)) % 97:02d}"


# Every stored document (2,2 Mo of text for 880) is read for its figures by
# a supplier's page and by learning: read again each time, it was most of
# their half a second. A text read once is kept, by its content.
READINGS_KEPT = 4096


def document_identifiers(text: str) -> set[str]:
    """What `text` prints that can name a supplier - a set of its own."""
    return set(_document_identifiers(text))


@lru_cache(maxsize=READINGS_KEPT)
def _document_identifiers(text: str) -> frozenset[str]:
    found: set[str] = set()
    for line in text.splitlines():
        found |= _line_identifiers(line)
    return frozenset(found)


def _line_identifiers(line: str) -> set[str]:
    found = set()
    for match in VAT_RE.finditer(line):
        key, siren = match.group(1), "".join(match.group(2, 3, 4))
        # A numeric key is checked; the letter keys of recent numbers can't be.
        if (key == vat_key(siren) if key.isdigit() else is_siren(siren)):
            found.add(f"siren:{siren}")
    labelled = SIREN_LABEL_RE.search(line) is not None
    for match in DIGIT_RUN_RE.finditer(line):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) == SIRET_LENGTH and luhn_valid(digits) and is_siren(digits[:SIREN_LENGTH]):
            found.add(f"siren:{digits[:SIREN_LENGTH]}")
        elif labelled and is_siren(digits):
            found.add(f"siren:{digits}")
    for match in PHONE_RE.finditer(line):
        digits = re.sub(r"\D", "", match.group(0))
        if digits.startswith("33"):
            digits = "0" + digits[2:].removeprefix("0")
        if len(digits) == 10:
            found.add(f"tel:{digits}")
    for pattern in (WEB_RE, SCHEME_RE, BARE_DOMAIN_RE):
        for match in pattern.finditer(line):
            found.add(f"web:{match.group(1).lower().removeprefix('www.')}")
    return found


def may_print(text: str, identifiers) -> bool:
    """Whether `text` can print one of `identifiers` - a quick look before
    reading it for them: their figures or name in it, separators aside."""
    compact = _compact(text)
    return any(_needle(identifier) in compact for identifier in identifiers)


@lru_cache(maxsize=READINGS_KEPT)
def _compact(text: str) -> str:
    return re.sub(r"[\s.()+-]", "", text.lower())


@lru_cache(maxsize=READINGS_KEPT)
def _needle(identifier: str) -> str:
    return re.sub(r"[.-]", "", identifier.partition(":")[2])[-9:]


class TypedIdentifierError(ValueError):
    """What a person typed is not one of these, and the sentence saying so."""


#: Said where nothing at all could be read out of what was typed.
TYPE_ONE = (
    "Tapez un n° SIREN (neuf chiffres), un n° SIRET, un n° de TVA, "
    "un numéro de téléphone ou un site (exemple.fr)."
)


def read_typed(text: str) -> str:
    """One identifier as a person typed it, as the key this module stores.

    **Read by the very function that reads a printed page**
    (`_line_identifiers`), so a figure typed by hand and the same figure
    found on a document can never come out as two different keys - which
    would file one supplier's documents under two rules, one of them
    invisible. The word SIREN is put in front of it because that is what
    makes a bare run of nine digits a company number rather than a phone
    number to that reader; every other shape says what it is on its own.

    Raises `TypedIdentifierError` - a ValueError carrying a French
    sentence - rather than guessing: a misread identifier names a shop, and
    a person typing one is entitled to be told which digit is wrong.
    """
    typed = " ".join(str(text or "").split())
    if not typed:
        raise TypedIdentifierError("Tapez un identifiant.")
    found = _line_identifiers(f"SIREN {typed}")
    if len(found) == 1:
        return next(iter(found))
    if len(found) > 1:
        raise TypedIdentifierError(
            "Tapez un seul identifiant à la fois : "
            + ", ".join(describe(identifier) for identifier in sorted(found))
            + "."
        )
    raise TypedIdentifierError(_why_not(typed))


def _why_not(typed: str) -> str:
    """Why nothing could be read, named as precisely as the figure allows: a
    refusal that only says « non » is one nobody can answer."""
    digits = re.sub(r"\D", "", typed)
    if not digits and "." in typed:
        return "Un site s'écrit comme une adresse : exemple.fr."
    if not digits:
        return TYPE_ONE
    if len(digits) == SIREN_LENGTH:
        return "Ce n'est pas un n° SIREN : sa clé de contrôle ne tombe pas juste. Vérifiez les neuf chiffres."
    if len(digits) == SIRET_LENGTH:
        return "Ce n'est pas un n° SIRET : sa clé de contrôle ne tombe pas juste. Vérifiez les quatorze chiffres."
    if len(digits) == 10:
        return "Un numéro de téléphone français a dix chiffres et commence par 0 : 01 23 45 67 89."
    return (
        f"{len(digits)} chiffre{'s' if len(digits) > 1 else ''} : un n° SIREN en a neuf, "
        "un n° SIRET quatorze, un téléphone dix."
    )


def describe(identifier: str) -> str:
    """An identifier as the operator reads it."""
    kind, _, value = identifier.partition(":")
    if kind == "siren":
        return f"n° SIREN {value[:3]} {value[3:6]} {value[6:]}"
    if kind == "tel":
        return "téléphone " + " ".join(value[index:index + 2] for index in range(0, len(value), 2))
    return f"site {value}"
