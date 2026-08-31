"""Resolves a raw invoice line product name to a Product record.

Matching order:
1. Exact (case-insensitive) match on an existing Product for that supplier.
2. Exact match on the supplier product code / EAN, when we have one.
3. Fuzzy match against that supplier's existing product names - catches minor
   formatting drift ("SOBIESKI VODKA 70CL" vs "SOBIESKI VODKA 70 CL") without
   creating a duplicate Product. Below the confidence threshold we don't
   guess: a brand-new Product is created and it lands in the review queue.
"""

from django.conf import settings
from rapidfuzz import fuzz, process

from .models import Product


def resolve_product(supplier, raw_name: str, ean: str = "") -> tuple[Product, bool]:
    raw_name = raw_name.strip()

    existing = Product.objects.filter(supplier=supplier, raw_name__iexact=raw_name).first()
    if existing:
        return existing, False

    if ean:
        existing = Product.objects.filter(supplier=supplier, ean=ean).exclude(ean="").first()
        if existing:
            return existing, False

    candidates = dict(Product.objects.filter(supplier=supplier).values_list("id", "raw_name"))
    if candidates:
        best = process.extractOne(raw_name, candidates, scorer=fuzz.token_sort_ratio)
        if best is not None:
            _match_name, score, product_id = best
            if score >= settings.PRODUCT_FUZZY_MATCH_THRESHOLD:
                return Product.objects.get(pk=product_id), False

    product = Product.objects.create(supplier=supplier, raw_name=raw_name, ean=ean)
    return product, True
