"""Plain-function object factories for tests - no factory_boy, no new
dependency.

Each one gives every required field a sane default so a test only has to
state what it actually cares about: `make_product(stock_type=vodka,
stock_equivalent="0.7")` reads as the one fact the test is about, and the
supplier/name/unit noise stays out of the way. Names auto-increment so
uniqueness constraints never bite a test that doesn't care about names.

Decimal-typed arguments accept strings ("0.7") as well as Decimals; they're
coerced here so tests can stay readable and never accidentally introduce a
float into money arithmetic.
"""

from __future__ import annotations

import itertools
from datetime import date, datetime
from decimal import Decimal

from django.db import models
from django.utils import timezone

from bank.models import BankTransaction
from inventory.models import (
    Product,
    StockMovement,
    StockTake,
    StockTakeLine,
    StockType,
    UnitChoices,
)
from invoices.models import EmailInvoiceSource, Invoice, InvoiceLine, InvoiceType, Supplier
from recipes.models import Recipe, RecipeIngredient, SaleDocument, SaleDocumentLine, SaleDocumentPayment

_counter = itertools.count(1)


def _d(value) -> Decimal:
    """Decimal("0.7") from either a string or a Decimal - never from a float,
    which would silently introduce binary rounding into money maths."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _decimals(model, fields: dict) -> dict:
    """`fields` with every value meant for one of `model`'s DecimalFields
    read by `_d`, so "7.50" may be passed; a name the model does not have
    raises FieldDoesNotExist rather than being dropped."""
    return {
        name: _d(value) if value is not None and isinstance(model._meta.get_field(name), models.DecimalField) else value
        for name, value in fields.items()
    }


def make_supplier(code: str = "", name: str = "", parser_key: str = "", **kwargs) -> Supplier:
    """A supplier, with a generated unique code unless one is given.

    Passing an explicit code updates the existing supplier rather than
    failing: a data migration (invoices/0002_seed_suppliers) already seeds
    METRO and UBA into every database including the test one, so a test that
    asks for "the METRO supplier" means exactly that one.
    """
    n = next(_counter)
    if code:
        supplier, _ = Supplier.objects.update_or_create(
            code=code,
            defaults={"name": name or f"Fournisseur {n}", "parser_key": parser_key, **kwargs},
        )
        return supplier
    return Supplier.objects.create(
        code=f"SUP{n}",
        name=name or f"Fournisseur {n}",
        parser_key=parser_key,
        **kwargs,
    )


def make_stock_type(name: str = "", unit: str = UnitChoices.LITRE, **kwargs) -> StockType:
    n = next(_counter)
    return StockType.objects.create(name=name or f"Type {n}", unit=unit, **kwargs)


def make_product(
    supplier: Supplier | None = None,
    raw_name: str = "",
    stock_type: StockType | None = None,
    unit: str = UnitChoices.UNIT,
    stock_equivalent="1",
    **kwargs,
) -> Product:
    n = next(_counter)
    return Product.objects.create(
        supplier=supplier or make_supplier(),
        raw_name=raw_name or f"Produit {n}",
        stock_type=stock_type,
        unit=unit,
        stock_equivalent=_d(stock_equivalent),
        **kwargs,
    )


def make_invoice(
    supplier: Supplier | None = None,
    invoice_date: date | None = None,
    invoice_number: str = "",
    **kwargs,
) -> Invoice:
    n = next(_counter)
    return Invoice.objects.create(
        supplier=supplier or make_supplier(),
        invoice_number=invoice_number or f"INV{n}",
        invoice_date=invoice_date if invoice_date is not None else date(2026, 1, 1),
        **kwargs,
    )


def make_invoice_line(
    invoice: Invoice | None = None,
    product: Product | None = None,
    quantity: int = 1,
    total_ht="10",
    unit_cost_ht=None,
    total_volume="0",
    **kwargs,
) -> InvoiceLine:
    invoice = invoice if invoice is not None else make_invoice()
    product = product if product is not None else make_product(supplier=invoice.supplier)
    total_ht = _d(total_ht)
    if unit_cost_ht is None:
        unit_cost_ht = (total_ht / quantity) if quantity else Decimal("0")
    return InvoiceLine.objects.create(
        invoice=invoice,
        product=product,
        raw_name=kwargs.pop("raw_name", product.raw_name),
        quantity=quantity,
        total_volume=_d(total_volume),
        unit_cost_ht=_d(unit_cost_ht),
        total_ht=total_ht,
        **kwargs,
    )


def make_purchase_history(product: Product, purchases: list[tuple[str, int, str]]) -> list[InvoiceLine]:
    """The usual setup for a FIFO/valuation test: several dated purchases of
    one product, newest last. Each entry is (iso date, quantity, total_ht).

    Every purchase gets its own Invoice, which is what really happens - and
    matters, since the FIFO walk orders by the *invoice's* date.
    """
    lines = []
    for iso_date, quantity, total_ht in purchases:
        invoice = make_invoice(supplier=product.supplier, invoice_date=date.fromisoformat(iso_date))
        lines.append(make_invoice_line(invoice=invoice, product=product, quantity=quantity, total_ht=total_ht))
    return lines


def make_movement(
    stock_type: StockType | None = None,
    quantity="1",
    unit_cost_ht="10",
    invoice_line: InvoiceLine | None = None,
    **kwargs,
) -> StockMovement:
    return StockMovement.objects.create(
        stock_type=stock_type if stock_type is not None else make_stock_type(),
        quantity=_d(quantity),
        unit_cost_ht=_d(unit_cost_ht),
        invoice_line=invoice_line,
        **kwargs,
    )


def make_stock_take(taken_at: datetime | None = None, **kwargs) -> StockTake:
    if taken_at is None:
        taken_at = timezone.now()
    elif timezone.is_naive(taken_at):
        taken_at = timezone.make_aware(taken_at)
    return StockTake.objects.create(taken_at=taken_at, **kwargs)


def make_stock_take_line(
    stock_take: StockTake | None = None,
    product: Product | None = None,
    stock_type: StockType | None = None,
    counted_quantity="1",
    unit: str = UnitChoices.UNIT,
    value_ht="0",
    **kwargs,
) -> StockTakeLine:
    if product is None and stock_type is None:
        product = make_product()
    return StockTakeLine.objects.create(
        stock_take=stock_take if stock_take is not None else make_stock_take(),
        product=product,
        stock_type=stock_type,
        counted_quantity=_d(counted_quantity),
        unit=unit,
        value_ht=_d(value_ht),
        **kwargs,
    )


def make_recipe(
    name: str = "",
    selling_price_ttc="10",
    vat_rate="0.20",
    yield_quantity="1",
    **kwargs,
) -> Recipe:
    n = next(_counter)
    return Recipe.objects.create(
        name=name or f"Recette {n}",
        # None for a preparation that is not sold as itself.
        selling_price_ttc=_d(selling_price_ttc) if selling_price_ttc is not None else None,
        vat_rate=_d(vat_rate),
        yield_quantity=_d(yield_quantity),
        **kwargs,
    )


def make_ingredient(
    recipe: Recipe,
    stock_type: StockType | None = None,
    sub_recipe: Recipe | None = None,
    quantity="1",
    group: int = 0,
) -> RecipeIngredient:
    if stock_type is None and sub_recipe is None:
        stock_type = make_stock_type()
    return RecipeIngredient.objects.create(
        recipe=recipe,
        stock_type=stock_type,
        sub_recipe=sub_recipe,
        quantity=_d(quantity),
        group=group,
    )


def make_sale_document(**fields) -> SaleDocument:
    """A sale document (« facture de vente »), sold on 05/03/2026 unless
    `sold_on` says otherwise; everything else at its default unless given -
    « Compte dans les marges et le stock », no file, nothing stated: what a
    document typed by hand was before the sales invoices."""
    fields.setdefault("sold_on", date(2026, 3, 5))
    return SaleDocument.objects.create(**_decimals(SaleDocument, fields))


def make_sale_line(document: SaleDocument, **fields) -> SaleDocumentLine:
    """One line of `document`: a quantity of 1 unless given, of the `recipe`
    or `stock_type` given - else tied to nothing, which needs a label of its
    own (« Ligne N » unless one is given, an empty one included)."""
    fields.setdefault("quantity", "1")
    if fields.get("recipe") is None and fields.get("stock_type") is None:
        fields.setdefault("label", f"Ligne {next(_counter)}")
    return SaleDocumentLine.objects.create(document=document, **_decimals(SaleDocumentLine, fields))


def make_credit(
    amount, day: date, counterparty: str = "", label: str = "VIR SEPA EXEMPLE", **fields
) -> BankTransaction:
    """A credit of the statement - money in - of `amount` on `day`, from
    `counterparty` (blank: the payer is then read off the label's words), a
    received transfer unless `fields` say otherwise. A fingerprint of its
    own, so two credits of one shape are two lines."""
    fields.setdefault("bank_type", "VIREMENT")
    fields.setdefault("kind", BankTransaction.Kind.TRANSFER)
    fields.setdefault("fingerprint", f"credit-exemple-{next(_counter)}")
    return BankTransaction.objects.create(
        operation_date=day, counterparty=counterparty, label=label, amount=_d(amount), **fields
    )


def make_sale_payment(document: SaleDocument, credit: BankTransaction, method: str = "MANUAL") -> SaleDocumentPayment:
    """`credit` paying `document` - linked by hand unless `method` says AUTO."""
    return SaleDocumentPayment.objects.create(document=document, transaction=credit, method=method)


def make_priced_stock_type(name: str = "", unit_cost_ht="10", quantity="1", **kwargs) -> StockType:
    """A stock type with stock on hand at a known average cost - the shape a
    recipe-costing test needs, since an ingredient's cost comes from
    StockType.current_unit_cost_ht (value / quantity across movements)."""
    stock_type = make_stock_type(name=name, **kwargs)
    make_movement(stock_type=stock_type, quantity=quantity, unit_cost_ht=unit_cost_ht)
    return stock_type


def make_invoice_type(
    supplier: Supplier | None = None,
    name: str = "",
    parser_key: str = "",
    sender_pattern: str = "",
    **kwargs,
) -> InvoiceType:
    """An InvoiceType plus its EmailInvoiceSource - the only combination the
    gather flow can actually use, so they're created together."""
    n = next(_counter)
    invoice_type = InvoiceType.objects.create(
        supplier=supplier or make_supplier(),
        name=name or f"Type facture {n}",
        parser_key=parser_key,
        **kwargs,
    )
    EmailInvoiceSource.objects.create(
        invoice_type=invoice_type,
        sender_pattern=sender_pattern or rf"facture{n}@exemple\.fr",
    )
    return invoice_type
