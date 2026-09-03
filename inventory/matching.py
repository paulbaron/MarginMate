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
"""

import re
from collections import Counter
from decimal import Decimal, InvalidOperation

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


def resolve_product(supplier, raw_name: str, ean: str = "") -> tuple[Product, bool]:
    raw_name = raw_name.strip()

    existing = Product.objects.filter(supplier=supplier, raw_name__iexact=raw_name).first()
    if existing:
        return existing, False

    if ean:
        existing = Product.objects.filter(supplier=supplier, ean=ean).exclude(ean="").first()
        if existing:
            return existing, False

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
                return Product.objects.get(pk=product_id), False

    product = Product.objects.create(supplier=supplier, raw_name=raw_name, ean=ean)
    return product, True
