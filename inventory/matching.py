"""Resolves a raw invoice line product name to a Product record.

Matching order:
1. Exact (case-insensitive) match on an existing Product for that supplier.
2. Exact match on the supplier product code / EAN, when we have one.
3. Fuzzy match against that supplier's existing product names - catches minor
   formatting drift ("SOBIESKI VODKA 70CL" vs "SOBIESKI VODKA 70 CL") without
   creating a duplicate Product. Below the confidence threshold we don't
   guess: a brand-new Product is created and it lands in the review queue.

Step 3 is the dangerous one, because a match there is applied SILENTLY - it
never reaches the review queue, so a wrong one is invisible and quietly
merges another product's costs into this one's price history. It's therefore
gated twice: a numeric signature that must match exactly (below), and only
then the similarity score.

4. Names read by OCR off a photographed receipt only (`ocr_tolerant=True`):
   the same shop's existing names - each product's own, and every name its
   receipts have been read as - forgiving the mistakes a recogniser makes
   and nothing else; see `ocr_match` and `resolve_products`. Every receipt
   goes through a person on the review screen, which shows both the name as
   read and the product it was attached to, so this step is never silent.
"""

import math
import re
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.conf import settings
from rapidfuzz import fuzz, process

from .models import Product

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def numeric_signature(name: str) -> Counter:
    """Every number in a product name, as a multiset.

    A different number means a different product - a different bottle size
    ("RICARD 45D 1L" vs "1.5L"), strength ("VCE RGE 11D" vs "12D"), ageing
    ("COMTE AOP 12M" vs "18M"), pack count or dimension. String similarity
    can't see that: those names differ by one or two characters out of thirty
    and score 93-97, comfortably above any threshold loose enough to still
    absorb real formatting drift.

    Comparing the numbers directly separates the two concerns cleanly. Values
    are normalised so "1" == "1.0" and "37,5" == "37.5" - a decimal comma is
    formatting drift, a different digit is not.
    """
    signature = Counter()
    for match in NUMBER_RE.finditer(name):
        try:
            signature[Decimal(match.group().replace(",", ".")).normalize()] += 1
        except InvalidOperation:  # pragma: no cover - the regex can't produce one
            continue
    return signature


def find_product(
    supplier, raw_name: str, ean: str = "", ocr_tolerant: bool = False, readings=None
) -> Product | None:
    """The existing product `raw_name` names (steps 1-4 above), or None.
    `readings` is handed to `ocr_match`."""
    raw_name = raw_name.strip()

    existing = Product.objects.filter(supplier=supplier, raw_name__iexact=raw_name).first()
    if existing:
        return existing

    if ean:
        existing = Product.objects.filter(supplier=supplier, ean=ean).exclude(ean="").first()
        if existing:
            return existing

    # Only names with the same numbers are even eligible - see
    # numeric_signature. Filtering before scoring (rather than rejecting the
    # winner afterwards) means a genuine formatting-drift match isn't lost
    # just because some unrelated product happened to score higher.
    signature = numeric_signature(raw_name)
    candidates = {
        product_id: name
        for product_id, name in Product.objects.filter(supplier=supplier).values_list("id", "raw_name")
        if numeric_signature(name) == signature
    }
    if candidates:
        best = process.extractOne(raw_name, candidates, scorer=fuzz.token_sort_ratio)
        if best is not None:
            _match_name, score, product_id = best
            if score >= settings.PRODUCT_FUZZY_MATCH_THRESHOLD:
                return Product.objects.get(pk=product_id)

    if ocr_tolerant:
        return ocr_match(supplier, raw_name, readings)
    return None


def resolve_product(
    supplier, raw_name: str, ean: str = "", ocr_tolerant: bool = False
) -> tuple[Product, bool]:
    """`ocr_tolerant` is for names read off a photo by a recogniser (see
    `ocr_match`). Off by default: a digital PDF has no OCR mistakes to
    forgive, so forgiving them there would only add ways to merge two real
    products."""
    product = find_product(supplier, raw_name, ean, ocr_tolerant)
    if product is not None:
        return product, False
    return Product.objects.create(supplier=supplier, raw_name=raw_name.strip(), ean=ean), True


def resolve_products(supplier, names, ocr_tolerant: bool = False) -> list[tuple[Product, bool]]:
    """`resolve_product` for every line of one document, given in order as
    (name, ean) pairs.

    Names read by OCR are matched together rather than one after another.
    One at a time, the order of the lines decided the outcome: a ticket
    reading "AGUETTE BLANC" then "AGUETTE BLAND" made the first a new product
    before the second could show that both were the shop's "BAGUETTE BLAND"
    - one label, read twice on one photo. So whatever matches is matched
    first, each match making its reading one more name the product is known
    by, and only what still matches nothing once there is nothing left to
    find creates a product - which its own misreadings further down share.
    """
    names = list(names)
    if not ocr_tolerant:
        return [resolve_product(supplier, name, ean) for name, ean in names]

    readings = known_readings(supplier)
    found: list[Product | None] = [None] * len(names)
    progress = True
    while progress:
        progress = False
        for index, (name, ean) in enumerate(names):
            if found[index] is None:
                found[index] = find_product(supplier, name, ean, ocr_tolerant=True, readings=readings)
                if found[index] is not None:
                    _remember_reading(readings, found[index].pk, name)
                    progress = True

    resolved = []
    for product, (name, ean) in zip(found, names):
        if product is None:
            # Nothing known matches it - but a product an earlier line of
            # this same document has just created may.
            product = find_product(supplier, name, ean, ocr_tolerant=True, readings=readings)
            if product is None:
                resolved.append((Product.objects.create(supplier=supplier, raw_name=name.strip(), ean=ean), True))
                continue
            _remember_reading(readings, product.pk, name)
        resolved.append((product, False))
    return resolved


def known_readings(supplier) -> dict[int, set[str]]:
    """Every name each of `supplier`'s products has been read as, folded,
    by product id - from the invoice lines attached to it
    (InvoiceLine.read_as, which the review screen keeps when it corrects a
    line, so a misreading attached by hand to the right product is
    recognised on the next ticket)."""
    # Looked up rather than imported: the invoices app imports this module.
    InvoiceLine = apps.get_model("invoices", "InvoiceLine")
    readings: dict[int, set[str]] = {}
    lines = InvoiceLine.objects.filter(product__supplier=supplier).exclude(read_as="").order_by()
    for product_id, read_as in lines.values_list("product_id", "read_as").distinct():
        _remember_reading(readings, product_id, read_as)
    return readings


def _remember_reading(readings: dict[int, set[str]], product_id: int, name: str) -> None:
    folded = fold_for_ocr(name)
    if folded:
        readings.setdefault(product_id, set()).add(folded)


# ---------------------------------------------------------------------------
# Names read by OCR off a photographed receipt
#
# A recogniser makes mistakes a supplier's own PDF never does: "5OOG" for
# "500G", "BAGUETIE" for "BAGUETTE", "POMME JULIETX4" for "POMME JULIET X4".
# Left alone, each creates a brand-new product - the same lemons become three
# products with three price histories, all waiting in the review queue.
#
# The rule is deliberately generic: compare with the SAME shop's existing
# names, forgiving the kinds of mistake a recogniser makes and nothing else.
# Two guarantees carry over from the strict matcher above:
#
# * A different number is a different product. A letter the recogniser
#   confuses with a digit (O/0, I/1, S/5, B/8...) may stand in for it, but a
#   digit never becomes a different digit and is never added or dropped:
#   "CITRON 5OOG" finds "CITRON 500G", "CITRON 250G" never does.
# * No guess between two candidates. When two existing names are about as
#   close as each other, neither is picked and a new product goes to review.
# ---------------------------------------------------------------------------

# Characters a recogniser swaps for one another because they share a printed
# shape. A swap inside a group costs a fraction of a real typo.
OCR_LOOKALIKE_GROUPS = ("O0QD", "I1L", "S5", "B8", "Z2", "G6")
_LOOKALIKE_GROUP_OF = {char: index for index, group in enumerate(OCR_LOOKALIKE_GROUPS) for char in group}
OCR_LOOKALIKE_COST = 0.3
OCR_TYPO_COST = 1.0
# Below this many letters and digits there is no room for a genuine typo:
# "THE" and "TEE" are both real words. Short names forgive look-alikes only.
OCR_MIN_LENGTH_FOR_TYPOS = 6
# One more typo allowed per this many characters, on top of the first.
OCR_CHARACTERS_PER_EXTRA_TYPO = 15
# How much closer the best candidate must be than the runner-up to count as
# a match rather than a toss-up.
OCR_AMBIGUITY_MARGIN = 0.5


def fold_for_ocr(name: str) -> str:
    """Upper-case ASCII letters and digits only.

    Spacing and punctuation are the least reliable thing a recogniser
    produces ("JULIETX4", "AROM.BASILIC", "75011Paris") and never what tells
    two products apart, so they are dropped before comparing. Accents go the
    same way - thermal tills print few of them and OCR invents others.
    """
    decomposed = unicodedata.normalize("NFKD", name.upper())
    return "".join(char for char in decomposed if char.isascii() and char.isalnum())


def _substitution_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    group = _LOOKALIKE_GROUP_OF.get(a)
    if group is not None and group == _LOOKALIKE_GROUP_OF.get(b):
        return OCR_LOOKALIKE_COST
    if a.isdigit() or b.isdigit():
        # A digit turning into a different digit (or into a letter that
        # doesn't share its shape) changes a size, a count or a strength.
        return math.inf
    return OCR_TYPO_COST


def _insertion_cost(char: str) -> float:
    # "1L" -> "1.5L" and "X4" -> "X46" are different products, however small
    # the edit.
    return math.inf if char.isdigit() else OCR_TYPO_COST


def ocr_edit_cost(a: str, b: str, limit: float = math.inf) -> float:
    """Weighted edit distance between two names already through
    `fold_for_ocr`. Stops early (returning inf) once every alignment costs
    more than `limit`."""
    previous = [0.0]
    for char in b:
        previous.append(previous[-1] + _insertion_cost(char))
    for char_a in a:
        current = [previous[0] + _insertion_cost(char_a)]
        for index, char_b in enumerate(b, start=1):
            current.append(
                min(
                    previous[index] + _insertion_cost(char_a),
                    current[index - 1] + _insertion_cost(char_b),
                    previous[index - 1] + _substitution_cost(char_a, char_b),
                )
            )
        if min(current) > limit:
            return math.inf
        previous = current
    return previous[-1]


def ocr_budget(folded: str) -> float:
    """The total edit cost a name of this length may carry and still be the
    same product."""
    if len(folded) < OCR_MIN_LENGTH_FOR_TYPOS:
        # Look-alikes only: three of them cost 0.9, one real typo costs 1.
        return 3 * OCR_LOOKALIKE_COST
    return OCR_TYPO_COST * (1 + len(folded) // OCR_CHARACTERS_PER_EXTRA_TYPO)


def ocr_match(supplier, raw_name: str, readings: dict[int, set[str]] | None = None) -> Product | None:
    """The one existing product of `supplier` that `raw_name` is a misreading
    of, or None when there isn't exactly one.

    Each product is measured by the closest of its names: its own, and every
    name it has been read as (`readings`, from `known_readings` unless
    given). Its own name is only whichever reading came first, and two
    readings of one label can be three mistakes apart - BAGUETTE BLAND,
    AGUETTE BLANC, BAGUETTE BLAVD. The budget stays that of ONE misreading:
    widened so that two readings could meet, it joined real, different
    products on the invoices already in the database ("1/16CANTAL AOP ED"
    and "... JNE", "GINGERBEER 1L" and "... 1L BIO"). Collected readings
    reach the far spellings one mistake at a time instead.
    """
    folded = fold_for_ocr(raw_name)
    if not folded:
        return None
    if readings is None:
        readings = known_readings(supplier)
    # A hair of float slack so three look-alikes (3 * 0.3) still fit.
    budget = ocr_budget(folded) + 1e-9

    scored = []
    for product_id, name in Product.objects.filter(supplier=supplier).values_list("id", "raw_name"):
        names = readings.get(product_id, set()) | {fold_for_ocr(name)}
        cost = min(_cost_within(folded, known, budget) for known in names)
        if cost <= budget:
            scored.append((cost, product_id))
    if not scored:
        return None
    scored.sort()
    if len(scored) > 1 and scored[1][0] - scored[0][0] < OCR_AMBIGUITY_MARGIN:
        return None
    return Product.objects.get(pk=scored[0][1])


def _cost_within(folded: str, known: str, budget: float) -> float:
    if not known or abs(len(known) - len(folded)) > budget:
        # Every character added or dropped is a whole typo.
        return math.inf
    return ocr_edit_cost(folded, known, limit=budget)
