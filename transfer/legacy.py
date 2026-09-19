"""The old « Exporter les associations » file, turned into a v1 archive
holding only `associations.json` (§4.6), so it goes through the same
preview, report and rules as any other import.

The old file named a supplier by its *name* - a code never left the
database then - so each distinct name gets a placeholder code "~1", "~2"…
in `supplier_names`, and the resolver falls back to the name (a real code
never starts with "~"). What the old file says is carried over as it is,
never repaired here: a factor of 0 or a blank article reaches the
associations section, which skips it with its reason on the preview. The
old import turned a bad factor into 1 and dropped a blank article without a
word - the owner could not see either.
"""

from __future__ import annotations

from pathlib import Path

from transfer.archive import ArchiveWriter
from transfer.keys import fold

REASON = "ancien export d'associations"


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _number(value):
    """A factor as the old file wrote it: a string ("0.7000"). A number a
    hand edit left there is carried as its own digits - "0.7", never the
    binary float it was parsed into - and anything else as it is, for the
    section to refuse with a reason."""
    if isinstance(value, float):
        return repr(value)
    return value


def convert(payload: dict) -> dict:
    """The legacy payload as an `associations.json` payload."""
    codes: dict[str, str] = {}      # fold(supplier name) -> placeholder code
    names: dict[str, str] = {}      # placeholder code -> supplier name
    articles: dict[str, dict] = {}  # fold(article name) -> article record, first spelling wins
    products = []
    for entry in payload.get("products", []):
        if not isinstance(entry, dict):
            products.append(entry)  # skipped by the section, « illisible »
            continue
        supplier = _text(entry.get("supplier"))
        code = None
        if supplier.strip():
            folded = fold(supplier)
            if folded not in codes:
                codes[folded] = f"~{len(codes) + 1}"
                names[codes[folded]] = supplier
            code = codes[folded]
        article_name = _text(entry.get("stock_type_name"))
        if article_name.strip() and fold(article_name) not in articles:
            # loss_percent was never in the old file: left out, "not said".
            article = {"name": article_name, "unit": entry.get("stock_type_unit")}
            category = entry.get("stock_type_category")
            if category is not None:
                article["category"] = category
            articles[fold(article_name)] = article
        product = {
            "supplier": code,
            "raw_name": entry.get("raw_name"),
            "article": article_name,
        }
        if "product_unit" in entry:
            product["unit"] = entry["product_unit"]
        if "stock_equivalent" in entry:
            product["stock_equivalent"] = _number(entry["stock_equivalent"])
        products.append(product)
    return {"supplier_names": names, "articles": list(articles.values()), "products": products}


def to_archive(payload: dict, dest_path: Path) -> dict:
    """Write a v1 archive at `dest_path` holding only associations.json;
    returns its manifest."""
    converted = convert(payload)
    classified = sum(1 for product in converted["products"] if isinstance(product, dict))
    with ArchiveWriter(Path(dest_path), reason=REASON) as writer:
        writer.section("associations").write(
            converted, {"articles": len(converted["articles"]), "produits classés": classified}
        )
        return writer.close()
