"""« Ventes » (§7.8): the till's quantities per product and per day - the raw
data -, the sales typed in by hand, and the sale documents.

What is derived from them is rebuilt, never copied: each till product's
total and first/last day (`recipes.sales.recount_pos_products`, proven equal
to what the till import writes), and each recipe's « laddition » sales,
summed through the links this database has when the import ends
(`resync_recipe_from_daily_quantities`). Without « Liens recettes ↔ ventes »
in the same run, the till products this section creates arrive « à lier »
and sell no recipe until they are linked - the page recommends the links
for that reason.

The per-day table is the big one (15 850 rows on 19/09), so it travels as
compact rows (`daily_columns`) and is written in bulk.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from decimal import Decimal

from django.db.models import Prefetch

from recipes.forms import MANUAL_SALE_SOURCE
from recipes.models import (
    PosProduct,
    PosProductDailyQuantity,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
)
from transfer import codec, registry
from transfer.archive import ArchiveError
from transfer.keys import fold
from transfer.sections.base import Section
from transfer.sections.recipes import Skip
from transfer.sections.till_links import (
    DESCRIPTIVE,
    TillProducts,
    laddition_rows,
    merge_descriptive,
    till_names,
)

DAILY_COLUMNS = ["till_product", "sold_on", "quantity"]
LISTS = ("till_products", "daily", "manual_sales", "documents")
TILL_KEYS = ("name", *DESCRIPTIVE)
MANUAL_KEYS = ("recipe", "sold_on", "quantity", "recorded_at")
DOCUMENT_FIELDS = ("reference", "sold_on", "note")
DOCUMENT_KEYS = (*DOCUMENT_FIELDS, "created_at", "lines")
LINE_KEYS = ("recipe", "article", "quantity", "unit_price_ttc")
#: A per-day row is one till product on one day, so its label says so, and
#: the days are counted apart: called « jours de vente (caisse) », the 15 850
#: rows of 19/09 (211 products over 662 days) read as 43 years of sales, or
#: as a clear wiping 15 850 days (UX review, 19/09).
QUANTITIES, TILL_DAYS = "quantités par produit et par jour (caisse)", "jours de caisse"
PRODUCTS, MANUAL, DOCUMENTS = "produits caisse", "ventes saisies", "bons de vente"
LINES, RECIPE_SALES = "lignes de bons de vente", "ventes par recette"
#: Rows written or deleted per query: SQLite caps a statement's parameters.
BATCH = 500


def _day(value) -> str:
    return f"{value:%d/%m/%Y}"


def _text(value, places: int) -> str | None:
    """A Decimal as the fingerprint writes it: at the field's places, so the
    archive's « 2.0000 » and the database's Decimal("2") are one quantity."""
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal(1).scaleb(-places)))


def fingerprint(reference: str, sold_on, note: str, lines) -> str:
    """A sale document's key: it has no number of its own (a reference is
    optional), so it is known by its whole content - its reference, day,
    note and its lines in order, each (« recipe » or « article », folded
    name, quantity, unit price)."""
    canonical = {
        "reference": reference or "",
        "sold_on": sold_on.isoformat(),
        "note": note or "",
        "lines": [[kind, fold(name), _text(quantity, 4), _text(price, 2)] for kind, name, quantity, price in lines],
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _documents():
    """This database's documents in (created_at, id) order - the order the
    export writes them, so two identical documents are occurrence 0 and 1
    on both sides."""
    return SaleDocument.objects.prefetch_related(
        Prefetch("lines", queryset=SaleDocumentLine.objects.select_related("recipe", "stock_type").order_by("id"))
    ).order_by("created_at", "id")


def _line_record(line: SaleDocumentLine) -> dict:
    return {
        "recipe": line.recipe.name if line.recipe_id else None,
        "article": line.stock_type.name if line.stock_type_id else None,
        "quantity": codec.dump(line, "quantity"),
        "unit_price_ttc": codec.dump(line, "unit_price_ttc"),
    }


def _line_shape(line: SaleDocumentLine) -> tuple:
    if line.recipe_id:
        return ("recipe", line.recipe.name, line.quantity, line.unit_price_ttc)
    return ("article", line.stock_type.name, line.quantity, line.unit_price_ttc)


def documents_here() -> dict[tuple[str, int], int]:
    """(fingerprint, occurrence) → document pk."""
    keys: dict[tuple[str, int], int] = {}
    seen: dict[str, int] = defaultdict(int)
    for document in _documents():
        key = fingerprint(document.reference, document.sold_on, document.note, [_line_shape(line) for line in document.lines.all()])
        keys[(key, seen[key])] = document.pk
        seen[key] += 1
    return keys


def _batches(items: list):
    for start in range(0, len(items), BATCH):
        yield items[start:start + BATCH]


def _delete(model, pks: list[int]) -> int:
    deleted = 0
    for batch in _batches(pks):
        _total, per_model = model.objects.filter(pk__in=batch).delete()
        deleted += per_model.get(model._meta.label, 0)
    return deleted


@registry.register
class SalesSection(Section):
    key = "ventes"

    # -- what this database holds ---------------------------------------------------
    def count(self) -> dict[str, int]:
        return {
            QUANTITIES: PosProductDailyQuantity.objects.count(),
            TILL_DAYS: PosProductDailyQuantity.objects.values("sold_on").distinct().count(),
            PRODUCTS: PosProductDailyQuantity.objects.values("product_id").distinct().count(),
            MANUAL: RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).count(),
            DOCUMENTS: SaleDocument.objects.count(),
        }

    def snapshot(self):
        payload = self._payload()
        return {
            "till_products": sorted(
                (product.name, product.category, product.typology, product.total_quantity,
                 codec.dump(product, "first_seen"), codec.dump(product, "last_seen"))
                for product in PosProduct.objects.filter(daily_quantities__isnull=False).distinct()
            ),
            "daily": sorted(tuple(row) for row in payload["daily"]),
            "manual_sales": sorted(
                (sale["recipe"], sale["sold_on"], sale["quantity"], sale["recorded_at"]) for sale in payload["manual_sales"]
            ),
            "documents": sorted(json.dumps(document, sort_keys=True, ensure_ascii=False) for document in payload["documents"]),
            # Derived: the till's sales per recipe, rebuilt through the links.
            "laddition": laddition_rows(),
        }

    # -- export ------------------------------------------------------------------------
    def _payload(self) -> dict:
        till_products = [
            {"name": name, "category": category, "typology": typology}
            for name, category, typology in PosProduct.objects.filter(
                pk__in=PosProductDailyQuantity.objects.values("product_id")
            ).order_by("name").values_list("name", "category", "typology")
        ]
        daily = [
            [name, sold_on.isoformat(), quantity]
            for name, sold_on, quantity in PosProductDailyQuantity.objects.order_by("product__name", "sold_on").values_list(
                "product__name", "sold_on", "quantity"
            )
        ]
        manual_sales = [
            {"recipe": sale.recipe.name, **codec.record(sale, ("sold_on", "quantity", "recorded_at"))}
            for sale in RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).select_related("recipe").order_by(
                "recipe__name", "sold_on"
            )
        ]
        documents = [
            {
                **codec.record(document, (*DOCUMENT_FIELDS, "created_at")),
                "lines": [_line_record(line) for line in document.lines.all()],
            }
            for document in _documents()
        ]
        return {
            "till_products": till_products,
            "daily_columns": list(DAILY_COLUMNS),
            "daily": daily,
            "manual_sales": manual_sales,
            "documents": documents,
        }

    def export(self, out) -> None:
        payload = self._payload()
        out.write(
            payload,
            {
                # The same rules as count(): the import tab prints both side by side.
                QUANTITIES: len(payload["daily"]),
                TILL_DAYS: len({sold_on for _name, sold_on, _quantity in payload["daily"]}),
                PRODUCTS: len(payload["till_products"]),
                MANUAL: len(payload["manual_sales"]),
                DOCUMENTS: len(payload["documents"]),
            },
        )

    # -- import ------------------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        if payload.get("daily_columns") != DAILY_COLUMNS:
            raise ArchiveError(
                "Archive refusée : dans ventes.json, « daily_columns » doit être "
                f"{json.dumps(DAILY_COLUMNS, ensure_ascii=False)}."
            )
        for name in LISTS:
            if not isinstance(payload.get(name, []), list):
                raise ArchiveError(f"Archive refusée : dans ventes.json, « {name} » n'est pas une liste.")
        self.payload = payload
        self.till_products, self.daily, self.manual_sales, self.documents = (payload.get(name, []) for name in LISTS)

    def apply(self, ctx, report) -> None:
        codec.note_unknown(report, self.payload, (*LISTS, "daily_columns", "supplier_names"))
        self.replacing = ctx.replacing(self.key)
        names = till_names(self.till_products) | {
            row[0] for row in self.daily if isinstance(row, list) and row and isinstance(row[0], str)
        }
        self.tills = TillProducts(names)
        #: Till products whose days changed: their totals are recounted, and
        #: the recipes they are linked to rebuilt.
        self.touched: set[int] = set()
        self._till_products(report)
        self._daily(report)
        self._manual_sales(ctx, report)
        self._documents(ctx, report)
        self._settle(ctx)

    def _new_product(self, name: str, **values) -> PosProduct:
        """A till product this database has never seen: « à lier », as the
        till import would leave it, with no total until its days are counted."""
        product = PosProduct.objects.create(name=name, **values)
        self.tills.add(product)
        return product

    def _till_products(self, report) -> None:
        seen: set[int] = set()
        for position, record in enumerate(self.till_products, start=1):
            if not isinstance(record, dict) or not isinstance(record.get("name"), str) or not record["name"].strip():
                report.skip(f"produit caisse n° {position} de l'archive : illisible")
                continue
            name = record["name"]
            codec.note_unknown(report, record, TILL_KEYS, "produits caisse : ")
            try:
                codec.load(PosProduct, "name", name)
                values = {field: codec.load(PosProduct, field, record[field]) for field in DESCRIPTIVE if field in record}
            except codec.FieldValueError as exc:
                report.skip(f"Produit caisse « {name} » : {exc}")
                continue
            product = self.tills.resolve(name)
            if product is None:
                seen.add(self._new_product(name, **values).pk)
                report.created(PRODUCTS)
                continue
            if product.pk in seen:
                report.skip(f"Produit caisse « {name} » : deux fois dans l'archive, la seconde est ignorée")
                continue
            seen.add(product.pk)
            changed, differing = merge_descriptive(product, values, self.replacing)
            if changed:
                product.save(update_fields=changed)
                report.updated(PRODUCTS)
            if differing:
                report.conflict(
                    f"Produit caisse « {product.name} » : différent dans l'archive ({', '.join(differing)}) "
                    "— gardé tel quel"
                )
            elif not changed:
                report.unchanged(PRODUCTS)

    def _daily(self, report) -> None:
        existing = {
            (product_id, sold_on): (pk, quantity)
            for pk, product_id, sold_on, quantity in PosProductDailyQuantity.objects.values_list(
                "pk", "product_id", "sold_on", "quantity"
            )
        }
        #: Every (till product, day) the file holds, skipped or not: prune
        #: leaves those rows as they are.
        self.daily_keys: set[tuple[int, object]] = set()
        creates: list[PosProductDailyQuantity] = []
        updates: list[PosProductDailyQuantity] = []
        for position, row in enumerate(self.daily, start=1):
            if not isinstance(row, list) or len(row) != len(DAILY_COLUMNS) or not isinstance(row[0], str) or not row[0].strip():
                report.skip(f"vente par jour n° {position} de l'archive : illisible")
                continue
            name, sold_on, quantity = row
            try:
                sold_on = codec.load(PosProductDailyQuantity, "sold_on", sold_on)
            except codec.FieldValueError as exc:
                report.skip(f"Produit caisse « {name} » : {exc}")
                continue
            product = self.tills.resolve(name)
            if product is not None:
                if (product.pk, sold_on) in self.daily_keys:
                    report.skip(f"Produit caisse « {name} » le {_day(sold_on)} : deux fois dans l'archive")
                    continue
                self.daily_keys.add((product.pk, sold_on))
            try:
                quantity = codec.load(PosProductDailyQuantity, "quantity", quantity)
                if product is None:
                    codec.load(PosProduct, "name", name)
            except codec.FieldValueError as exc:
                report.skip(f"Produit caisse « {name} » le {_day(sold_on)} : {exc}")
                continue
            if product is None:
                # Named by a day but not in the file's list of till products.
                product = self._new_product(name)
                report.created(PRODUCTS)
                self.daily_keys.add((product.pk, sold_on))
            found = existing.get((product.pk, sold_on))
            if found is None:
                creates.append(PosProductDailyQuantity(product=product, sold_on=sold_on, quantity=quantity))
                self.touched.add(product.pk)
            elif found[1] == quantity:
                report.unchanged(QUANTITIES)
            elif self.replacing:
                updates.append(PosProductDailyQuantity(pk=found[0], quantity=quantity))
                self.touched.add(product.pk)
            else:
                report.conflict(
                    f"Produit caisse « {product.name} » le {_day(sold_on)} : {found[1]} ici, {quantity} dans "
                    "l'archive — gardé tel quel"
                )
        if creates:
            PosProductDailyQuantity.objects.bulk_create(creates, batch_size=BATCH)
            report.created(QUANTITIES, len(creates))
        if updates:
            PosProductDailyQuantity.objects.bulk_update(updates, ["quantity"], batch_size=BATCH)
            report.updated(QUANTITIES, len(updates))

    def _manual_sales(self, ctx, report) -> None:
        recipes = ctx.recipes()
        existing = {
            (recipe_id, sold_on): (pk, quantity)
            for pk, recipe_id, sold_on, quantity in RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).values_list(
                "pk", "recipe_id", "sold_on", "quantity"
            )
        }
        self.manual_keys: set[tuple[int, object]] = set()
        for position, record in enumerate(self.manual_sales, start=1):
            if not isinstance(record, dict) or not isinstance(record.get("recipe"), str):
                report.skip(f"vente saisie n° {position} de l'archive : illisible")
                continue
            codec.note_unknown(report, record, MANUAL_KEYS, "ventes saisies : ")
            recipe_name = record["recipe"]
            try:
                sold_on = codec.load(RecipeSale, "sold_on", record.get("sold_on"))
                quantity = codec.load(RecipeSale, "quantity", record.get("quantity"))
                stamp = codec.load(RecipeSale, "recorded_at", record["recorded_at"]) if record.get("recorded_at") is not None else None
            except codec.FieldValueError as exc:
                report.skip(f"Vente saisie de « {recipe_name} » : {exc}")
                continue
            recipe = recipes.resolve(recipe_name)
            if recipe is None:
                report.skip(f"Vente saisie du {_day(sold_on)} : recette inconnue « {recipe_name} »")
                continue
            key = (recipe.pk, sold_on)
            if key in self.manual_keys:
                report.skip(f"Vente saisie de « {recipe.name} » le {_day(sold_on)} : deux fois dans l'archive")
                continue
            self.manual_keys.add(key)
            found = existing.get(key)
            if found is None:
                sale = RecipeSale.objects.create(recipe=recipe, sold_on=sold_on, quantity=quantity, source=MANUAL_SALE_SOURCE)
                if stamp is not None:
                    # auto_now_add stamped it "now"; the archive's is the real one.
                    RecipeSale.objects.filter(pk=sale.pk).update(recorded_at=stamp)
                report.created(MANUAL)
            elif found[1] == quantity:
                report.unchanged(MANUAL)
            elif self.replacing:
                RecipeSale.objects.filter(pk=found[0]).update(quantity=quantity)
                report.updated(MANUAL)
            else:
                report.conflict(
                    f"Vente saisie de « {recipe.name} » le {_day(sold_on)} : {found[1]} ici, {quantity} dans "
                    "l'archive — gardée telle quelle"
                )

    def _documents(self, ctx, report) -> None:
        recipes, articles = ctx.recipes(), ctx.articles()
        here = documents_here()
        self.document_keys: set[tuple[str, int]] = set()
        seen: dict[str, int] = defaultdict(int)
        created = 0
        for position, record in enumerate(self.documents, start=1):
            if not isinstance(record, dict):
                report.skip(f"bon de vente n° {position} de l'archive : illisible")
                continue
            codec.note_unknown(report, record, DOCUMENT_KEYS, "bons de vente : ")
            try:
                fields, stamp, lines = self._document(record, report)
            except Skip as exc:
                report.skip(f"bon de vente n° {position} de l'archive : {exc}")
                continue
            key = fingerprint(fields["reference"], fields["sold_on"], fields["note"], lines)
            key = (key, seen[key])
            seen[key[0]] += 1
            self.document_keys.add(key)
            if key in here:
                report.unchanged(DOCUMENTS)
                continue
            title = f"Bon de vente du {_day(fields['sold_on'])}" + (f" ({fields['reference']})" if fields["reference"] else "")
            try:
                resolved = [self._resolve_line(kind, name, recipes, articles) for kind, name, _q, _p in lines]
            except Skip as exc:
                report.skip(f"{title} : {exc}")
                continue
            document = SaleDocument.objects.create(**fields)
            if stamp is not None:
                SaleDocument.objects.filter(pk=document.pk).update(created_at=stamp)
            SaleDocumentLine.objects.bulk_create(
                [
                    SaleDocumentLine(document=document, quantity=quantity, unit_price_ttc=price, **target)
                    for target, (_kind, _name, quantity, price) in zip(resolved, lines)
                ]
            )
            report.created(DOCUMENTS)
            if lines:
                report.created(LINES, len(lines))
            created += 1
        if created and here:
            report.note(
                "Les bons de vente sont rapprochés sur tout leur contenu : un bon modifié d'un côté arrive comme "
                "un nouveau bon."
            )

    def _document(self, record: dict, report) -> tuple[dict, object, list[tuple]]:
        """The document's fields, its stamp and its lines as (kind, name,
        quantity, price), checked - but not resolved: its key is its content,
        so a document whose recipe is missing here still has one, and prune
        leaves this database's copy of it alone."""
        try:
            fields = {
                "reference": codec.load(SaleDocument, "reference", record.get("reference", "")),
                "sold_on": codec.load(SaleDocument, "sold_on", record.get("sold_on")),
                "note": codec.load(SaleDocument, "note", record.get("note", "")),
            }
            stamp = codec.load(SaleDocument, "created_at", record["created_at"]) if record.get("created_at") is not None else None
        except codec.FieldValueError as exc:
            raise Skip(str(exc)) from None
        items = record.get("lines", [])
        if not isinstance(items, list):
            raise Skip("lignes illisibles")
        lines = []
        for number, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise Skip(f"ligne n° {number} illisible")
            codec.note_unknown(report, item, LINE_KEYS, "lignes de bons de vente : ")
            recipe, article = item.get("recipe"), item.get("article")
            name = recipe if recipe is not None else article
            if (recipe is None) == (article is None) or not isinstance(name, str):
                raise Skip(f"ligne n° {number} : une recette ou un article, jamais les deux ni aucun")
            try:
                quantity = codec.load(SaleDocumentLine, "quantity", item.get("quantity"))
                price = codec.load(SaleDocumentLine, "unit_price_ttc", item.get("unit_price_ttc"))
            except codec.FieldValueError as exc:
                raise Skip(f"ligne n° {number} : {exc}") from None
            lines.append(("recipe" if recipe is not None else "article", name, quantity, price))
        return fields, stamp, lines

    @staticmethod
    def _resolve_line(kind: str, name: str, recipes, articles) -> dict:
        if kind == "recipe":
            recipe = recipes.resolve(name)
            if recipe is None:
                raise Skip(f"recette inconnue « {name} »")
            return {"recipe": recipe}
        stock_type = articles.resolve(name)
        if stock_type is None:
            raise Skip(f"article inconnu « {name} »")
        return {"stock_type": stock_type}

    def _settle(self, ctx) -> None:
        """What changed days make stale: the till products' totals, and the
        sales of the recipes they are linked to - through the links this
        database has now (« Liens » applied before, in the same run)."""
        touched = sorted(self.touched)
        ctx.dirty.pos_products.update(touched)
        for batch in _batches(touched):
            ctx.dirty.recipes.update(
                PosProduct.objects.filter(pk__in=batch, recipe__isnull=False).values_list("recipe_id", flat=True)
            )

    def prune(self, ctx, report) -> None:
        """What the file does not have goes: days, hand-typed sales,
        documents, and the till products left with no day and no link - pure
        data. A linked or ignored one stays: that is the links'."""
        stale = [
            (pk, product_id)
            for pk, product_id, sold_on in PosProductDailyQuantity.objects.values_list("pk", "product_id", "sold_on")
            if (product_id, sold_on) not in self.daily_keys
        ]
        days = _delete(PosProductDailyQuantity, [pk for pk, _product in stale])
        if days:
            report.deleted(QUANTITIES, days)
            self.touched.update(product_id for _pk, product_id in stale)

        manual = _delete(
            RecipeSale,
            [
                pk
                for pk, recipe_id, sold_on in RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).values_list(
                    "pk", "recipe_id", "sold_on"
                )
                if (recipe_id, sold_on) not in self.manual_keys
            ],
        )
        if manual:
            report.deleted(MANUAL, manual)

        documents = _delete(
            SaleDocument, [pk for key, pk in documents_here().items() if key not in self.document_keys]
        )
        if documents:
            report.deleted(DOCUMENTS, documents)

        orphans = list(
            PosProduct.objects.filter(recipe__isnull=True, ignored=False, daily_quantities__isnull=True).values_list(
                "pk", flat=True
            )
        )
        products = _delete(PosProduct, orphans)
        if products:
            report.deleted(PRODUCTS, products)
        self.touched.difference_update(orphans)
        self._settle(ctx)

    # -- clear ---------------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        """Every day, every sale per recipe (typed in or the till's), every
        document. The till products with no link go; a linked or ignored one
        is the links' and stays, at zero and with no day."""
        days, _ = PosProductDailyQuantity.objects.all().delete()
        manual = RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).count()
        _total, per_model = RecipeSale.objects.all().delete()
        till_sales = per_model.get(RecipeSale._meta.label, 0) - manual
        _total, per_model = SaleDocument.objects.all().delete()
        documents = per_model.get(SaleDocument._meta.label, 0)
        lines = per_model.get(SaleDocumentLine._meta.label, 0)
        _total, per_model = PosProduct.objects.filter(recipe__isnull=True, ignored=False).delete()
        products = per_model.get(PosProduct._meta.label, 0)
        PosProduct.objects.exclude(total_quantity=0, first_seen=None, last_seen=None).update(
            total_quantity=0, first_seen=None, last_seen=None
        )
        for what, n in (
            (QUANTITIES, days), (PRODUCTS, products), (MANUAL, manual), (RECIPE_SALES, till_sales),
            (DOCUMENTS, documents), (LINES, lines),
        ):
            if n:
                report.deleted(what, n)
