"""Uses a local LLM (via Ollama - free, runs on this machine, no API key or
per-call cost) to pre-fill the review queue: for each pending product,
suggest which stock item it belongs to (or a new one to create), what unit
the product itself is counted/measured in, and the conversion factor to the
stock item's own unit.

This never assigns anything by itself - see inventory/views.py:assign_product,
which is still the only code path that actually links a product and creates
stock movements. Getting a unit/factor wrong silently produces a bad stock
value (we've hit several real edge cases doing this by hand - a variable-
weight cut of meat, a "colisage" pack size, a container size embedded only
in the product name), so suggestions are meant to save you the research, not
to replace checking them.

Uses Ollama's structured-output mode (a JSON schema passed as `format`)
rather than tool-calling: small local models are notably less reliable at
emitting proper tool_calls than a JSON body matching a schema.
"""

from __future__ import annotations

import json

from django.conf import settings

from .models import Product, StockType

BATCH_SIZE = 10  # smaller than a hosted-model batch would use - local models
# follow instructions less reliably across a long list of items.

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer"},
                    "stock_type_name": {"type": "string"},
                    "new_stock_type_unit": {"type": "string", "enum": ["L", "UNIT", "KG"]},
                    "new_stock_type_category": {"type": "string"},
                    "product_unit": {"type": "string", "enum": ["L", "UNIT", "KG"]},
                    "stock_equivalent": {"type": "number", "exclusiveMinimum": 0},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reasoning": {"type": "string", "maxLength": 60},
                },
                # Every field is required, even reasoning/confidence and the
                # new_stock_type_* ones that only matter for a brand-new
                # stock item (ignored otherwise): local models reliably skip
                # any field that isn't required rather than filling it in
                # half-heartedly - verified twice now (unit/category were
                # always empty at 0/65 until required; reasoning vanished
                # entirely the moment it was merely encouraged to be short
                # instead of mandatory).
                "required": [
                    "product_id",
                    "stock_type_name",
                    "new_stock_type_unit",
                    "new_stock_type_category",
                    "product_unit",
                    "stock_equivalent",
                    "confidence",
                    "reasoning",
                ],
            },
        }
    },
    "required": ["suggestions"],
}

PROMPT_TEMPLATE = """You help categorize bar purchase invoice line items into stock inventory items.

Unit semantics you must follow exactly:
- product_unit=UNIT means the invoice's quantity is a count of discrete items (bottles, packs, boxes). stock_equivalent is then how much of the stock unit ONE such item contains - e.g. a "VODKA X 70CL" bottle: product_unit=UNIT, stock_equivalent=0.7, matching a Vodka stock item in litres.
- product_unit=L or KG means the meaningful amount for this line is a measured volume/weight - either a genuinely variable-weight item (a cut of meat), or a line where "quantity" already directly represents litres/kg because the invoice encodes it that way (e.g. a line billed as 10 x some unit price with no separate pack-size column, where quantity IS the litre/kg count). stock_equivalent is usually 1 in that case.
- Always prefer matching an EXISTING stock item over creating a new one when it clearly represents the same underlying thing (e.g. "SOBIESKI VODKA 70CL" and "WYBOROWA VODKA 70CL" both belong to a "Vodka" stock item, regardless of brand).
- Parse pack sizes out of the product name whenever present (70CL -> 0.7, 1L -> 1, 1KG -> 1, 5L -> 5, ...).
- Only propose a new stock item when nothing existing fits. Keep new stock item names short and generic - the brand belongs on the product, not the stock item (e.g. "Vodka" not "Vodka Sobieski").
- When unsure, prefer confidence=low over guessing silently.
- stock_equivalent must always be a positive number greater than 0 - NEVER 0. If you genuinely can't determine a size/count from the name or the invoice data, use 1 (a safe "no conversion" default) and set confidence=low, rather than emitting 0 (0 would mean the product contributes nothing to stock at all, which is never correct).
- reasoning: at most 8 words, French, telling the user the key fact that drove your answer - not a full sentence (e.g. "70CL bouteille, correspond à Vodka existant").

Cross-checking `colisage` against the product name (this matters, get it right):
`colisage` is the supplier's own "items per line" multiplier, already folded into `qty` (qty = colisage * how many were ordered). The product name often ALSO states how many sub-items are in one pack - a leading count ("300 ETIQ ...", "20 RLX ..."), or a "SIZExCOUNT" pattern ("10GX100" = 10g each, 100 of them; "75CL X 6" = 6 bottles of 75cl). Compare the two:
- If colisage ALREADY equals the count implied by the name, that pack size is already applied to qty - use product_unit=UNIT, stock_equivalent=1. Example: "20 RLX 57X40X12 THERM SBPA" with colisage=20, qty=20 -> the 20 rolls are already counted; stock_equivalent=1.
- If colisage does NOT reflect the name's count (commonly colisage=1, because the supplier's own colisage field is blank/wrong on that line) AND volume_ou_poids=0 too (so the invoice gave no other signal), the product name is the only source of truth - use product_unit=UNIT, stock_equivalent=<the name's count>. Examples:
  - "300 ETIQ DLC 70X45MM", colisage=1, qty=1 -> 300 étiquettes per pack -> stock_equivalent=300.
  - "ARO 400 SERV 29X29 1P BLANC", colisage=1, qty=2 -> 400 serviettes per pack (qty=2 means 2 packs bought, so 800 total) -> stock_equivalent=400.
  - "BEURRE DS PORT 10GX100 RIOBA", colisage=1, qty=1 -> "10GX100" is 10g x 100 individual portions -> stock_equivalent=100.
- If volume_ou_poids is already non-zero and consistent with the name (e.g. "COCA COLA 33 CL X 24 VC" with volume_ou_poids=7.920, since 24 x 0.33 = 7.92), that's already correct - just use product_unit=L, stock_equivalent=1. Don't re-derive from the name when the measured volume already accounts for it.
- Dimensions that are just a physical size, not a count (e.g. "57X40X12" = width x height x depth of one roll) are NOT the quantity - don't confuse a size pattern for a count pattern. The count is the number that, multiplied by colisage/qty, would NOT already reproduce a sensible total on its own.
- The count or size is often fused to the surrounding text with NO space - don't only look for "<number> <word>" with a space. Examples: "CANADOU PUR SUCRE CANNE PET2L" is a 2L bottle (stock_equivalent=2, product_unit=UNIT or L), "MPRO 100SAC (G) CONS 20X30" is a box of 100 bags (stock_equivalent=100, product_unit=UNIT) - the "20X30" there is the bag's size in cm, not a count.

For new_stock_type_unit and new_stock_type_category, ALWAYS fill in a value, even if you're matching an existing stock item (it'll simply be ignored in that case) - never leave them blank:
- new_stock_type_category: pick the closest EXISTING category by theme (a cheese goes with whatever category other food items use, a spirit with whatever category other spirits use) if one reasonably fits; otherwise propose a short, sensible new category name (e.g. "Spiritueux", "Softs", "Épicerie", "Bières"). Prefer reusing an existing category over inventing a near-duplicate (don't propose "Boissons" if "Softs" already exists and fits).
- new_stock_type_unit: the physical unit the new stock item should be tracked in (usually matches product_unit's spirit: UNIT for discrete goods, L for liquids, KG for solids weighed).

Existing stock items, grouped by category:
{stock_types}

Existing categories you can reuse for a new stock item: {categories}

Respond with ONLY a JSON object (no other text) with one suggestion per product_id, for these products.
Product data shown is: id | supplier | raw name | example invoice line (qty | colisage | measured volume/weight | unit price HT | line total HT):

{products}
"""


def _format_stock_types() -> str:
    # Grouped by category instead of repeating it on every line - shorter,
    # and the grouping itself is a hint (a new product that looks like a
    # spirit should look under the same heading as other spirits).
    types = list(StockType.objects.all().order_by("category", "name"))
    if not types:
        return "(aucun pour le moment)"
    lines = []
    current_category = None
    first = True
    for st in types:
        if first or st.category != current_category:
            current_category = st.category
            lines.append(f"[{current_category or 'Sans catégorie'}]")
            first = False
        lines.append(f"- {st.name} ({st.unit})")
    return "\n".join(lines)


def _format_categories() -> str:
    cats = list(StockType.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category"))
    return ", ".join(cats) if cats else "(aucune pour le moment)"


def _format_products(products: list[Product]) -> str:
    lines = []
    for p in products:
        line = p.invoice_lines.first()
        if line:
            lines.append(
                f"- {p.id} | {p.supplier.code} | {p.raw_name} | qty={line.quantity} | colisage={line.colisage} | "
                f"volume_ou_poids={line.total_volume} | prix_unitaire_ht={line.unit_cost_ht} | total_ht={line.total_ht}"
            )
        else:
            lines.append(f"- {p.id} | {p.supplier.code} | {p.raw_name} | (aucune ligne de facture)")
    return "\n".join(lines)


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _resolve_stock_type_match(suggestion: dict) -> None:
    """Overrides whatever the model claimed with an authoritative lookup, so
    the review template can trust matched_stock_type_id / is_new_stock_type
    without re-doing fuzzy matching itself."""
    name = (suggestion.get("stock_type_name") or "").strip()
    match = StockType.objects.filter(name__iexact=name).first() if name else None
    suggestion["is_new_stock_type"] = match is None
    suggestion["matched_stock_type_id"] = match.id if match else None
    # Pre-format so the review form's editable input doesn't show something
    # like "0.7000000000000001" - it's meant to be edited, not just displayed.
    try:
        suggestion["stock_equivalent"] = f"{float(suggestion.get('stock_equivalent', 1)):g}"
    except (TypeError, ValueError):
        suggestion["stock_equivalent"] = "1"


class SuggestionCancelled(Exception):
    """Raised when a running SuggestionJob is cancelled mid-generation."""


def _call_ollama(prompt: str, should_cancel=None) -> list[dict]:
    import ollama

    client = ollama.Client(host=settings.OLLAMA_HOST)
    try:
        # Streamed rather than a single blocking call: it's the only way to
        # actually stop the model generating when cancelled. A non-streamed
        # call has no way to abandon it early - closing this generator
        # closes the underlying HTTP connection (see ollama.Client._request:
        # the stream path is a generator wrapping `with self._client.stream()
        # as r:`), which Ollama's server detects as a dropped client and
        # aborts generation for - not just "stop waiting for the result".
        stream = client.chat(
            model=settings.OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            format=RESPONSE_SCHEMA,
            think=False,  # this is classification/extraction, not a task that
            # benefits from Qwen3's chain-of-thought mode - which is also the
            # single biggest cost driver for local inference speed here.
            keep_alive="30m",  # Ollama unloads the model after 5 min idle by
            # default - since a run does several batches back-to-back (and
            # the button gets clicked again later in the same session), keep
            # it resident so only the very first batch ever pays the ~10-20s
            # model-load cost instead of every batch.
            options={"temperature": 0.1},
            stream=True,
        )
        parts = []
        try:
            for chunk in stream:
                if should_cancel and should_cancel():
                    raise SuggestionCancelled()
                parts.append(chunk["message"]["content"] or "")
        finally:
            stream.close()
        content = "".join(parts)
    except ConnectionError as exc:
        raise RuntimeError(
            f"Impossible de joindre Ollama sur {settings.OLLAMA_HOST}. "
            "Vérifiez qu'Ollama est lancé (`ollama serve` ou l'application Ollama)."
        ) from exc

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Réponse du modèle local non-JSON : {content[:200]!r}") from exc
    return data.get("suggestions", [])


def _check_ollama_ready() -> None:
    """Fails fast with one clear message instead of letting every batch fail
    identically with the same connection/model error."""
    import ollama

    client = ollama.Client(host=settings.OLLAMA_HOST)
    try:
        available = client.list()
    except Exception as exc:  # noqa: BLE001 - covers connection refused, DNS, etc.
        raise RuntimeError(
            f"Impossible de joindre Ollama sur {settings.OLLAMA_HOST}. "
            "Installez/lancez Ollama (https://ollama.com), puis relancez cette action."
        ) from exc

    names = {getattr(m, "model", None) or (m.get("model") if isinstance(m, dict) else None) for m in available.models}
    wanted = settings.OLLAMA_MODEL
    if wanted not in names and not any((n or "").startswith(wanted.split(":")[0]) for n in names):
        raise RuntimeError(
            f'Le modèle "{wanted}" n\'est pas disponible dans Ollama. '
            f"Téléchargez-le avec : ollama pull {wanted}"
        )


def generate_suggestions_for_pending_products(
    log=lambda msg: None,
    on_batch_done=lambda succeeded, failed: None,
    should_cancel=lambda: False,
) -> tuple[int, int]:
    """Suggests a mapping for every pending product that doesn't already
    have one (re-running the button is cheap: it only covers new arrivals).
    Returns (succeeded_count, failed_count). `on_batch_done` is called after
    each batch with the running totals so a caller can show live progress -
    local inference is slow enough (tens of seconds per ~10 products) that
    silently blocking until the whole thing finishes isn't acceptable.
    `should_cancel` is checked both between batches and between streamed
    chunks of the in-flight Ollama call - raises SuggestionCancelled, which
    the caller is expected to handle (not swallowed here as a batch failure,
    since it's an intentional stop, not an error).
    """
    pending = list(
        Product.objects.filter(stock_type__isnull=True, ai_suggestion__isnull=True)
        .select_related("supplier")
        .prefetch_related("invoice_lines")
    )
    if not pending:
        return 0, 0

    _check_ollama_ready()

    stock_types_text = _format_stock_types()
    categories_text = _format_categories()

    succeeded = 0
    failed = 0

    for batch in _chunk(pending, BATCH_SIZE):
        if should_cancel():
            log("Annulé.")
            raise SuggestionCancelled()

        prompt = PROMPT_TEMPLATE.format(
            stock_types=stock_types_text,
            categories=categories_text,
            products=_format_products(batch),
        )
        try:
            suggestions = _call_ollama(prompt, should_cancel=should_cancel)
            suggestions_by_id = {s["product_id"]: s for s in suggestions if "product_id" in s}

            for product in batch:
                suggestion = suggestions_by_id.get(product.id)
                if not suggestion:
                    failed += 1
                    continue
                _resolve_stock_type_match(suggestion)
                product.ai_suggestion = suggestion
                product.save(update_fields=["ai_suggestion"])
                succeeded += 1
            log(f"{len(suggestions_by_id)}/{len(batch)} produit(s) traité(s) dans ce lot.")
        except SuggestionCancelled:
            log(f"Annulé pendant le traitement d'un lot de {len(batch)} produit(s).")
            on_batch_done(succeeded, failed)
            raise
        except Exception as exc:  # noqa: BLE001 - one bad batch shouldn't stop the rest
            log(f"Échec du lot ({len(batch)} produit(s)) : {exc}")
            failed += len(batch)
        on_batch_done(succeeded, failed)

    return succeeded, failed
