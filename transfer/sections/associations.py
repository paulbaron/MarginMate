"""« Associations produits → articles » (§7.3): the articles, and for each
classified product the article it fills and its conversion.

A product is its supplier's (supplier code, raw name) - the pair the invoice
lines name - and never matched fuzzily: an import that merged "RICARD 45D
1.5L" into "RICARD 45D 1L" would be silently wrong money. What follows from
a classification - the PURCHASE movements, the invoices' « à classer »
status - is not copied: the products whose classification changed go into
`ctx.dirty.products`, and the rebuild books them with the app's own
arithmetic (inventory.services.rebuild_purchase_movements).

Three checks the old import did not make, each skipping the record with its
reason rather than guessing:

* a conversion is a positive number - the old import turned a bad one into
  1, and the stock quantities it then booked were wrong with nothing on
  screen saying so;
* a product and its article agree on the unit, here and in the archive - a
  factor of 0,7 means "litres per bottle" only against an article in
  litres;
* a charge's poste (a product flagged `is_expense` here) is never
  classified - a rent would become bottles, with a stock movement behind it.
  assign_product's own rule, on the product and not its supplier: a supplier
  turned to charges keeps the products a stock item claimed
  (importing.redo_as_expenses, « it is stock after all »), and refused at
  the supplier's level they lost their classification and their purchases in
  a round trip.

Each product is counted once, as what happens to its row: a classification
removed from a product that stays is « modifié », and only a product that
goes is « supprimé ». Counted as deleted classifications too, a full clear
read 1 462 products « à supprimer » for the 794 the database held.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from inventory.models import (
    MovementKind,
    Product,
    StockMovement,
    StockTakeLine,
    StockType,
    UnitChoices,
)
from transfer import codec, registry
from transfer.archive import ArchiveError
from transfer.keys import fold, line_ordinals
from transfer.sections.base import Section

ARTICLE_FIELDS = ("unit", "category", "loss_percent")
ARTICLE_KNOWN = ("name", *ARTICLE_FIELDS, "created_at")
#: What « Fusionner » may fill when this database leaves it blank (§6.1).
ARTICLE_FILLABLE = ("category",)
#: `created_at` is written only on a product this import creates (§4.3,
#: §6.4): never compared, so an archive taken later is still « inchangé ».
PRODUCT_KNOWN = ("supplier", "raw_name", "ean", "article", "unit", "stock_equivalent", "created_at")

#: How the report names a field that differs.
FIELD_LABELS = {
    "unit": "unité",
    "category": "catégorie",
    "loss_percent": "pertes",
    "article": "article",
    "stock_equivalent": "conversion",
    "ean": "EAN",
}
UNIT_WORDS = {UnitChoices.LITRE: "litres", UnitChoices.UNIT: "unités", UnitChoices.KILOGRAM: "kilos"}

ARTICLES = "articles"
CLASSIFIED = "produits classés"
ORPHANS = "produits sans facture"
MOVEMENTS = "mouvements d'achat"


class _Skip(Exception):
    """A record that cannot be imported: the message says why, in French."""


def _fields(names) -> str:
    return ", ".join(FIELD_LABELS.get(name, name) for name in names[:3])


def _unit_word(unit) -> str:
    return UNIT_WORDS.get(unit, str(unit))


def _shown(value) -> str:
    return value if isinstance(value, str) else str(value)


def _supplier_codes(products) -> dict[str, str]:
    return {product.supplier.code: product.supplier.name for product in products}


@registry.register
class AssociationsSection(Section):
    key = "associations"

    # -- what this database holds ----------------------------------------------------
    def count(self) -> dict[str, int]:
        return {
            ARTICLES: StockType.objects.count(),
            CLASSIFIED: Product.objects.filter(stock_type__isnull=False).count(),
        }

    def snapshot(self):
        """Articles and classifications by natural key, and the PURCHASE
        movements they book - the derived data a round trip must rebuild
        equal, line by line."""
        articles = sorted(
            (
                fold(article.name),
                article.name,
                article.unit,
                article.category,
                codec.dump(article, "loss_percent"),
                codec.dump(article, "created_at"),
            )
            for article in StockType.objects.all()
        )
        products = sorted(
            (
                product.supplier.code,
                product.raw_name,
                product.ean,
                product.stock_type.name,
                product.unit,
                codec.dump(product, "stock_equivalent"),
            )
            for product in Product.objects.filter(stock_type__isnull=False).select_related("supplier", "stock_type")
        )
        movements = StockMovement.objects.filter(invoice_line__isnull=False).select_related(
            "stock_type", "invoice_line__invoice__supplier", "invoice_line__product__supplier"
        )
        movements = list(movements)
        ordinals = line_ordinals({movement.invoice_line.invoice_id for movement in movements})
        # An undated invoice dumps None beside a dated one's text: ordered
        # by repr, never by comparing the two.
        purchases = sorted(
            (
                (
                    movement.stock_type.name,
                    movement.kind,
                    movement.invoice_line.product.supplier.code,
                    movement.invoice_line.product.raw_name,
                    movement.invoice_line.invoice.supplier.code,
                    movement.invoice_line.invoice.invoice_number,
                    codec.dump(movement.invoice_line.invoice, "invoice_date"),
                    ordinals[movement.invoice_line_id][1],
                    codec.dump(movement, "quantity"),
                    codec.dump(movement, "unit_cost_ht"),
                )
                for movement in movements
            ),
            key=repr,
        )
        return {"articles": articles, "products": products, "movements": purchases}

    # -- export ----------------------------------------------------------------------
    def export(self, out) -> None:
        articles = [
            {"name": article.name, **codec.record(article, (*ARTICLE_FIELDS, "created_at"))}
            for article in StockType.objects.order_by("name", "id")
        ]
        products = list(
            Product.objects.filter(stock_type__isnull=False)
            .select_related("supplier", "stock_type")
            .order_by("supplier__code", "raw_name", "id")
        )
        out.write(
            {
                "supplier_names": _supplier_codes(products),
                "articles": articles,
                "products": [
                    {
                        "supplier": product.supplier.code,
                        "raw_name": product.raw_name,
                        "ean": product.ean,
                        "article": product.stock_type.name,
                        "unit": product.unit,
                        "stock_equivalent": codec.dump(product, "stock_equivalent"),
                        "created_at": codec.dump(product, "created_at"),
                    }
                    for product in products
                ],
            },
            {ARTICLES: len(articles), CLASSIFIED: len(products)},
        )

    # -- import ----------------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        for name in ("articles", "products"):
            if not isinstance(payload.get(name), list):
                raise ArchiveError(f"Archive refusée : associations.json n'a pas de liste « {name} ».")
        if "supplier_names" in payload and not isinstance(payload["supplier_names"], dict):
            raise ArchiveError("Archive refusée : associations.json a un « supplier_names » illisible.")
        self.articles = payload["articles"]
        self.products = payload["products"]

    def apply(self, ctx, report) -> None:
        replacing = ctx.replacing(self.key)
        #: fold(name) of every article the file names, applied or not: prune keeps them.
        self._named_articles: set[str] = set()
        #: fold(name) -> the unit the file gives that article, for the products' checks.
        self._file_units: dict[str, str] = {}
        #: Articles whose unit this run changed (Remplacer): their conversions no longer mean anything.
        self._unit_changed: set[int] = set()
        #: Products a record of the file set, left as it said, or created.
        self._applied: set[int] = set()
        #: Products here whose record the file has but that could not be applied.
        self._skipped_here: set[int] = set()
        self._apply_articles(ctx, report, replacing)
        self._apply_products(ctx, report, replacing)

    def _apply_articles(self, ctx, report, replacing: bool) -> None:
        articles = ctx.articles()
        created: list[tuple[StockType, object]] = []
        for record in self.articles:
            if not isinstance(record, dict):
                report.skip("Article illisible dans l'archive : ignoré")
                continue
            codec.note_unknown(report, record, ARTICLE_KNOWN, "articles.")
            name = record.get("name")
            if not isinstance(name, str) or not name.strip():
                report.skip("Article sans nom dans l'archive : ignoré")
                continue
            folded = fold(name)
            if folded in self._named_articles:
                report.skip(f"Article « {name} » : en double dans l'archive, seul le premier compte")
                continue
            self._named_articles.add(folded)
            unit = record.get("unit")
            if unit in UnitChoices.values:
                self._file_units[folded] = unit
            try:
                self._check_article(record)
            except (_Skip, codec.FieldValueError) as exc:
                report.skip(f"Article « {name} » : {exc}")
                continue

            existing = articles.resolve(name)
            if existing is None:
                if "unit" not in record:
                    report.skip(f"Article « {name} » : sans unité")
                    continue
                article = StockType(name=name)
                codec.assign(article, record, ARTICLE_FIELDS)
                article.save()
                articles.add(article)
                if record.get("created_at") is not None:
                    created.append((article, codec.load(StockType, "created_at", record["created_at"])))
                report.created(ARTICLES)
                continue

            different = codec.differences(existing, record, ARTICLE_FIELDS)
            if not different:
                report.unchanged(ARTICLES)
                continue
            if replacing:
                old_unit = existing.unit
                changed = codec.assign(existing, record, ARTICLE_FIELDS)
                existing.save(update_fields=changed)
                report.updated(ARTICLES)
                if existing.unit != old_unit:
                    self._unit_changed.add(existing.pk)
                    self._say_unit_change(ctx, report, existing, old_unit)
                continue
            fills = [field for field in different if field in ARTICLE_FILLABLE and codec.is_blank(getattr(existing, field))]
            if fills:
                codec.assign(existing, record, fills)
                existing.save(update_fields=fills)
                report.updated(ARTICLES)
            conflicts = [field for field in different if field not in fills]
            if conflicts:
                report.conflict(
                    f"Article « {existing.name} » : différent dans l'archive ({_fields(conflicts)}) — gardé tel quel"
                )

        # auto_now_add wrote "now" on insert; the archive's date is the true one.
        for article, created_at in created:
            article.created_at = created_at
        StockType.objects.bulk_update([article for article, _ in created], ["created_at"])

    @staticmethod
    def _check_article(record) -> None:
        """Every stated field is loaded before anything is written; a loss
        allowance outside 0-100 means nothing (the form refuses it too)."""
        codec.load(StockType, "name", record["name"])
        for name in (*ARTICLE_FIELDS, "created_at"):
            if name in record:
                value = codec.load(StockType, name, record[name])
                if name == "loss_percent" and value is not None and not Decimal(0) <= value <= Decimal(100):
                    raise _Skip(f"pertes « {record[name]} » : entre 0 et 100 attendu")

    def _say_unit_change(self, ctx, report, article, old_unit) -> None:
        from recipes.models import RecipeIngredient

        if ctx.importing("recettes"):
            return  # the recipes' quantities come from the same archive
        if RecipeIngredient.objects.filter(stock_type=article).exists():
            report.note(
                f"« {article.name} » change d'unité ({_unit_word(old_unit)} → {_unit_word(article.unit)}) : "
                "vérifiez les recettes qui l'utilisent"
            )

    def _apply_products(self, ctx, report, replacing: bool) -> None:
        articles = ctx.articles()
        # Two products the archive holds apart stay two: a record never
        # lands on a product the archive names itself by its folded name
        # (« … FÛT » and « … Fût » of one supplier were merged) - the
        # context's resolver knows what the archive names.
        products = ctx.products()
        # What each record lands on: the product here it resolves to, or
        # the exact name it will create - not its folded name, since the
        # database may hold "Vodka" and "VODKA" as two products.
        seen: set[tuple] = set()
        created: set[tuple[str, str]] = set()  # (file code, raw name)
        for record in self.products:
            if not isinstance(record, dict):
                report.skip("Produit illisible dans l'archive : ignoré")
                continue
            codec.note_unknown(report, record, PRODUCT_KNOWN, "produits.")
            raw_name = record.get("raw_name")
            code = record.get("supplier")
            supplier = ctx.suppliers.resolve(code) if isinstance(code, str) else None
            if not isinstance(raw_name, str) or not raw_name:
                report.skip("Produit sans nom dans l'archive : ignoré")
                continue
            if not isinstance(code, str) or not code:
                report.skip(f"Produit « {raw_name} » : sans fournisseur")
                continue
            if supplier is None:
                report.skip(f"Produit « {raw_name} » : fournisseur inconnu « {ctx.suppliers.names.get(code) or code} »")
                continue
            label = f"Produit « {raw_name} » ({supplier.name})"
            product = products.resolve(supplier, raw_name)
            target = ("here", product.pk) if product is not None else ("new", supplier.pk, raw_name)
            if target in seen:
                report.skip(f"{label} : en double dans l'archive, seul le premier compte")
                continue
            seen.add(target)
            try:
                article, unit, factor, ean = self._check_product(record, supplier, product, articles)
                # Read only for a product it creates: nothing else writes it.
                moment = (
                    codec.load(Product, "created_at", record["created_at"])
                    if product is None and record.get("created_at") is not None
                    else None
                )
            except (_Skip, codec.FieldValueError) as exc:
                report.skip(f"{label} : {exc}")
                if product is not None:
                    self._skipped_here.add(product.pk)
                continue

            if product is None:
                product = Product.objects.create(
                    supplier=supplier,
                    raw_name=raw_name,
                    ean=ean or "",
                    is_expense=False,
                    stock_type=article,
                    unit=unit,
                    stock_equivalent=factor,
                )
                if moment is not None:
                    # auto_now_add wrote "now" over it on insert.
                    product.created_at = moment
                    Product.objects.filter(pk=product.pk).update(created_at=moment)
                products.add(product)
                seen.add(("here", product.pk))  # a later record naming it is the same product
                self._applied.add(product.pk)
                ctx.dirty.products.add(product.pk)
                report.created(CLASSIFIED)
                created.add((code, raw_name))
                continue

            self._applied.add(product.pk)
            fill_ean = bool(ean) and not product.ean
            if product.stock_type_id is None:
                # Unclassified here: classifying it is filling a blank.
                self._classify(ctx, product, article, unit, factor, ean if fill_ean else None)
                report.updated(CLASSIFIED)
                continue
            different = []
            if product.stock_type_id != article.pk:
                different.append("article")
            different += codec.differences(product, {"unit": unit, "stock_equivalent": record["stock_equivalent"]},
                                           ("unit", "stock_equivalent"))
            if ean and product.ean and ean != product.ean:
                different.append("ean")
            if not different:
                if fill_ean:
                    product.ean = ean
                    product.save(update_fields=["ean"])
                    report.updated(CLASSIFIED)
                else:
                    report.unchanged(CLASSIFIED)
                continue
            if replacing:
                self._classify(ctx, product, article, unit, factor, ean or None)
                report.updated(CLASSIFIED)
                continue
            if fill_ean:
                product.ean = ean
                product.save(update_fields=["ean"])
                report.updated(CLASSIFIED)
            report.conflict(f"{label} : différent dans l'archive ({_fields(different)}) — gardé tel quel")
        waiting = len(created - self._bought_in_this_run(ctx))
        if waiting == 1:
            report.note("1 produit créé sans facture : ses achats compteront dès que ses factures arriveront")
        elif waiting:
            report.note(
                f"{waiting} produits créés sans facture : leurs achats compteront dès que leurs factures arriveront"
            )

    @staticmethod
    def _bought_in_this_run(ctx) -> set[tuple[str, str]]:
        """(file code, raw name) of the products the archive's invoices use,
        when they are imported in the same run: those get their lines a
        moment later (factures applies after this section), and the 668
        products of a whole-database import were said to wait for invoices
        that came with them."""
        if not ctx.importing("factures"):
            return set()
        products = ctx.reader.section("factures").payload().get("products")
        return {
            (item["supplier"], item["raw_name"])
            for item in (products if isinstance(products, list) else [])
            if isinstance(item, dict) and isinstance(item.get("supplier"), str) and isinstance(item.get("raw_name"), str)
        }

    def _check_product(self, record, supplier, product, articles):
        """(article here, unit, factor, ean) for a record that can be
        applied; _Skip with the reason otherwise."""
        # The product, not its supplier: a supplier of charges keeps the
        # products a stock item claimed (redo_as_expenses), and one the
        # archive classifies but this database lacks is created as that
        # leaves them - classified, is_expense False.
        if product is not None and product.is_expense:
            raise _Skip("poste de charge : jamais classé")
        codec.load(Product, "raw_name", record["raw_name"])
        article_name = record.get("article")
        if not isinstance(article_name, str) or not article_name.strip():
            raise _Skip("sans article")
        if record.get("stock_equivalent") is None:
            raise _Skip("sans conversion")
        shown = _shown(record["stock_equivalent"])
        if isinstance(record["stock_equivalent"], (bool, float)):
            # 0.7 parsed as a binary float is not the 0.7 anybody typed.
            raise _Skip(f"conversion « {shown} » : un nombre s'écrit entre guillemets")
        try:
            factor = codec.load(Product, "stock_equivalent", record["stock_equivalent"])
        except codec.FieldValueError:
            raise _Skip(f"conversion « {shown} » : un nombre positif d'au plus 4 décimales est attendu") from None
        if factor is None or factor <= 0:
            raise _Skip(f"conversion « {shown} » : un nombre positif est attendu")
        ean = codec.load(Product, "ean", record["ean"]) if record.get("ean") is not None else ""
        article = articles.resolve(article_name)
        if article is None:
            raise _Skip(f"article inconnu « {article_name} »")
        file_unit = self._file_units.get(fold(article_name))
        unit = codec.load(Product, "unit", record["unit"]) if "unit" in record else (file_unit or article.unit)
        if file_unit is not None and unit != file_unit:
            raise _Skip(
                f"en {_unit_word(unit)} alors que son article « {article_name} » est en {_unit_word(file_unit)} "
                "dans l'archive"
            )
        archive_unit = file_unit or unit
        if article.unit != archive_unit:
            raise _Skip(
                f"l'article « {article.name} » est en {_unit_word(article.unit)} ici, en "
                f"{_unit_word(archive_unit)} dans l'archive : sa conversion ne veut plus rien dire"
            )
        return article, unit, factor, ean

    @staticmethod
    def _classify(ctx, product, article, unit, factor, ean) -> None:
        """link_product_to_stock_type's writes; its movements and its
        invoices' statuses come from the rebuild."""
        product.stock_type = article
        product.unit = unit
        product.stock_equivalent = factor
        product.ai_suggestion = None
        fields = ["stock_type", "unit", "stock_equivalent", "ai_suggestion"]
        if ean is not None:
            product.ean = ean
            fields.append("ean")
        product.save(update_fields=fields)
        ctx.dirty.products.add(product.pk)

    def prune(self, ctx, report) -> None:
        from invoices.deletion import remove_orphan_products

        # 1. Classifications the file does not have are removed - the product
        #    goes back to « à classer », as unlink_product does; its invoices
        #    and lines are someone else's data.
        unlinked = []
        for product in Product.objects.filter(stock_type__isnull=False).select_related("supplier"):
            if product.pk in self._applied:
                continue
            if product.pk in self._skipped_here and product.stock_type_id not in self._unit_changed:
                report.keep(
                    f"Produit « {product.raw_name} » ({product.supplier.name}) : gardé tel quel, "
                    "sa ligne de l'archive est ignorée"
                )
                continue
            unlinked.append(product.pk)
        if unlinked:
            Product.objects.filter(pk__in=unlinked).update(stock_type=None)
            ctx.dirty.products.update(unlinked)
            # A product nothing was ever bought as, and nothing counts, was
            # one this section created: it goes (deletion's own rule).
            removed = remove_orphan_products(unlinked)
            # Counted once each: modified if it stays, deleted if it goes.
            if len(unlinked) > removed:
                report.updated(CLASSIFIED, len(unlinked) - removed)
            if removed:
                report.deleted(ORPHANS, removed)

        # 2. Articles the file does not name are deleted, unless something
        #    kept still holds them.
        classified = set(Product.objects.filter(stock_type__isnull=False).values_list("stock_type_id", flat=True))
        written_down = set(
            StockMovement.objects.filter(kind__in=[MovementKind.LOSS, MovementKind.CORRECTION]).values_list(
                "stock_type_id", flat=True
            )
        )
        for article in StockType.objects.order_by("name"):
            if fold(article.name) in self._named_articles:
                continue
            if article.pk in classified:
                count = Product.objects.filter(stock_type=article).count()
                report.keep(
                    f"Article « {article.name} » : {count} produit{'s' if count > 1 else ''} y "
                    f"{'sont' if count > 1 else 'est'} encore rangé{'s' if count > 1 else ''}"
                )
                continue
            if article.pk in written_down:
                report.keep(f"Article « {article.name} » : des pertes ou corrections y sont saisies (Inventaires)")
                continue
            holders = _holders(article)
            if holders:
                report.keep(f"Article « {article.name} » : encore utilisé ({holders})")
                continue
            try:
                with transaction.atomic():
                    article.delete()
            except ProtectedError:
                report.keep(f"Article « {article.name} » : encore utilisé")
                continue
            report.deleted(ARTICLES)

    # -- clear -----------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        """unlink_product on every classified product, in bulk (proven equal
        by a test), then the products nothing names and the articles. Runs
        after recettes, liens, ventes and inventaires are cleared: nothing
        holds an article any more.

        Each product counted once: « produits classés » modified are those
        that stay, « à classer » again (their invoice lines hold them); the
        ones that go are « produits sans facture » deleted."""
        from invoices.deletion import remove_orphan_products

        _count, per_model = StockMovement.objects.filter(
            invoice_line__isnull=False, kind=MovementKind.PURCHASE
        ).delete()
        movements = per_model.get(StockMovement._meta.label, 0)
        if movements:
            report.deleted(MOVEMENTS, movements)
        classified = set(Product.objects.filter(stock_type__isnull=False).values_list("id", flat=True))
        if classified:
            Product.objects.filter(stock_type__isnull=False).update(stock_type=None)
        product_ids = list(Product.objects.values_list("id", flat=True))
        ctx.dirty.status_products.update(product_ids)
        removed = remove_orphan_products(product_ids)
        kept = len(classified & set(Product.objects.values_list("id", flat=True)))
        if kept:
            report.updated(CLASSIFIED, kept)
        if removed:
            report.deleted(ORPHANS, removed)
        _count, per_model = StockType.objects.all().delete()
        articles = per_model.get(StockType._meta.label, 0)
        if articles:
            report.deleted(ARTICLES, articles)


def _holders(article) -> str:
    """What still names an article, in the words of the pages that do."""
    from recipes.models import RecipeIngredient, SaleDocumentLine

    parts = []
    recipes = sorted(set(RecipeIngredient.objects.filter(stock_type=article).values_list("recipe__name", flat=True)))
    if recipes:
        parts.append(("recette " if len(recipes) == 1 else "recettes ") + ", ".join(recipes))
    takes = sorted(
        {
            timezone.localtime(taken_at).date()
            for taken_at in StockTakeLine.objects.filter(stock_type=article).values_list(
                "stock_take__taken_at", flat=True
            )
        }
    )
    if takes:
        parts.append(
            ("inventaire du " if len(takes) == 1 else "inventaires du ")
            + ", ".join(f"{day:%d/%m/%Y}" for day in takes)
        )
    documents = SaleDocumentLine.objects.filter(stock_type=article).values("document_id").distinct().count()
    if documents:
        parts.append(f"{documents} bon{'s' if documents > 1 else ''} de vente")
    return " ; ".join(parts)
