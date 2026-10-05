"""What an electronic sales invoice's page proposes for its lines, and what
it doubts of them (« Factures de vente », recipes/views.py).

**Proposals** (`proposals`): for each line the invoice does not tie, a
recipe or an article it may have sold - what a line of the same label was
tied to before, else a sellable recipe, else an article, of that very name -
with the consumed quantity of that earlier tie scaled to this one. Drawn as
the select's choice on the page, never written: « Enregistrer » saves what
is posted, so a proposal left as drawn is saved and one set to « rien » is
proposed again next time (nothing records a refusal).

A wrong tie moves stock in silence, so a proposal is made only where it is
safe: an untied line with a label and a POSITIVE quantity and amount, on a
document that counts. A credit note repeats its invoice's labels: proposed
there, the earlier tie and its ratio would put phantom stock back on one
« Enregistrer » over a price correction - the usual credit note returns
nothing to the shelf. A line rebuilt from a VAT table, or of the mirror shape
(a quantity and an amount of opposite signs), is never tied at all.

**The consumption doubt** (`consumption_doubt`): a tied line whose money per
unit consumed is ten times its reference or a tenth of it - a recipe's menu
price HT, an article's unit cost - is said under its cell (« 30 » typed in
litres on a recipe, a keg's 150 € « consumed » 1). Never enforced.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Q

from common import fits_column, format_money, search_key
from inventory.models import StockType, UnitChoices

from .models import Recipe, SaleDocument, SaleDocumentLine, cents, vat_divisor

#: Money per unit consumed this many times its reference, or this many times
#: smaller: said under the line.
CONSUMPTION_DOUBT_RATIO = Decimal("10")

#: Why a proposal is made, said under the select.
WHY_EARLIER = "relié ainsi sur la facture du {day}"
WHY_RECIPE = "même nom qu'une recette"
WHY_ARTICLE = "même nom qu'un article"

#: A tied line's money per unit consumed against its reference.
DOUBT_RECIPE = "Quantité consommée ? {per_unit} € HT pour 1 portion (carte : {reference} € HT)."
DOUBT_ARTICLE = "Quantité consommée ? {per_unit} € HT pour 1 {unit} (coût : {reference} € {per})."

#: An article's unit as the doubt writes it: « pour 1 L », « 3,10 € le L ».
UNIT_WORDS = {
    UnitChoices.LITRE: ("L", "le L"),
    UnitChoices.KILOGRAM: ("kg", "le kg"),
    UnitChoices.UNIT: ("unité", "l'unité"),
}

UNIT = Decimal("0.0001")


@dataclass(frozen=True)
class Proposal:
    value: str  # "recipe:<pk>" | "stock:<pk>", a choice of the line's select
    why: str  # French, said under the select
    consumed: Decimal | None  # the consumed quantity proposed with it


def key(text: str) -> str:
    """A label or a name as a proposal compares it: case, accents and spaces
    aside."""
    return " ".join(search_key(text or "").split())


def money_of(line: SaleDocumentLine) -> Decimal:
    """The line's money as its sign is read: its stated HT, else its TTC."""
    return line.total_ht if line.total_ht is not None else line.total_ttc


def is_mirror(line: SaleDocumentLine) -> bool:
    """A quantity and an amount of opposite signs - neither a sale nor a
    refund (einvoice's LINE_SIGN_CHECK fails on it)."""
    money = money_of(line)
    return (line.quantity > 0 and money < 0) or (line.quantity < 0 and money > 0)


def tieable(line: SaleDocumentLine) -> bool:
    """Whether an electronic invoice's line may be tied to a recipe or an
    article: never one rebuilt from its VAT table (money at a rate, not what
    was sold), never the mirror shape."""
    return not line.rebuilt and not is_mirror(line)


def proposals(document: SaleDocument, lines) -> dict[int, Proposal]:
    """{line pk: Proposal} for the UNTIED, tieable lines of `document` with
    a label and a POSITIVE quantity and amount: what a line of the same
    label was tied to before, else a sellable recipe, else an article, whose
    name reads the same (case, accents and spaces aside) - one recipe or one
    article of that name, two propose nothing. Three queries whatever the
    number of lines; never written."""
    if not document.counts:
        return {}
    wanted = {
        line.pk: key(line.label)
        for line in lines
        if not line.is_tied and tieable(line) and key(line.label) and line.quantity > 0 and money_of(line) > 0
    }
    if not wanted:
        return {}
    labels = set(wanted.values())
    sellable = dict(Recipe.objects.exclude(selling_price_ttc=None).order_by().values_list("pk", "name"))
    earlier: dict[str, Proposal] = {}
    for label, recipe_id, stock_type_id, consumed, quantity, day in (
        SaleDocumentLine.objects.exclude(document=document)
        .filter(Q(recipe__isnull=False) | Q(stock_type__isnull=False), quantity__gt=0)
        .exclude(label="")
        .order_by("-document__sold_on", "-id")
        .values_list("label", "recipe_id", "stock_type_id", "consumed_quantity", "quantity", "document__sold_on")
    ):
        label_key = key(label)
        # The latest tie of a label is the one; one no longer offered (a
        # recipe turned preparation) is passed over for an older one.
        if label_key not in labels or label_key in earlier or (recipe_id and recipe_id not in sellable):
            continue
        earlier[label_key] = Proposal(
            f"recipe:{recipe_id}" if recipe_id else f"stock:{stock_type_id}",
            WHY_EARLIER.format(day=f"{day:%d/%m/%Y}"),
            consumed / quantity if consumed is not None else None,
        )
    recipes_by_name = _alone({pk: key(name) for pk, name in sellable.items()})
    articles_by_name = _alone({pk: key(name) for pk, name in StockType.objects.order_by().values_list("pk", "name")})
    found: dict[int, Proposal] = {}
    lines_by_pk = {line.pk: line for line in lines}
    for pk, label_key in wanted.items():
        if label_key in earlier:
            proposal = earlier[label_key]
            ratio = proposal.consumed
            consumed = None
            if ratio is not None:
                scaled = (ratio * lines_by_pk[pk].quantity).quantize(UNIT, rounding=ROUND_HALF_UP)
                consumed = scaled if fits_column(SaleDocumentLine, "consumed_quantity", scaled) else None
            found[pk] = Proposal(proposal.value, proposal.why, consumed)
        elif label_key in recipes_by_name:
            found[pk] = Proposal(f"recipe:{recipes_by_name[label_key]}", WHY_RECIPE, None)
        elif label_key in articles_by_name:
            found[pk] = Proposal(f"stock:{articles_by_name[label_key]}", WHY_ARTICLE, None)
    return found


def _alone(names: dict[int, str]) -> dict[str, int]:
    """{name: pk} for the names one row alone bears."""
    seen: dict[str, list[int]] = {}
    for pk, name in names.items():
        seen.setdefault(name, []).append(pk)
    return {name: pks[0] for name, pks in seen.items() if len(pks) == 1}


def _euros(value: Decimal) -> str:
    """« 7,50 » - an amount in a sentence: to the cent, grouped, a comma."""
    return format_money(cents(value)).replace(".", ",")


def consumption_doubt(line: SaleDocumentLine, unit_cost: Decimal | None = None) -> str:
    """What is said under a tied line whose money per unit consumed is more
    than CONSUMPTION_DOUBT_RATIO times its reference, or less than its
    tenth: for a recipe its menu price HT (a sale's), for an article
    `unit_cost` (its current cost, read once for the page). "" when nothing
    is to be said - an untied line, nothing consumed, no reference."""
    if not line.is_tied or not line.consumption:
        return ""
    money = money_of(line)
    if line.total_ht is None:
        if line.divisor is None:
            return ""
        money = money / line.divisor
    per_unit = money / line.consumption
    if per_unit <= 0:
        return ""
    if line.recipe_id:
        price = line.recipe.selling_price_ttc
        if price is None or price <= 0:
            return ""
        reference = price / vat_divisor(line.recipe.vat_rate)
        sentence = DOUBT_RECIPE.format(per_unit=_euros(per_unit), reference=_euros(reference))
    else:
        if unit_cost is None or unit_cost <= 0:
            return ""
        reference = unit_cost
        unit, per = UNIT_WORDS.get(line.stock_type.unit, (line.stock_type.get_unit_display(), ""))
        sentence = DOUBT_ARTICLE.format(per_unit=_euros(per_unit), unit=unit, reference=_euros(reference), per=per)
    ratio = per_unit / reference
    if ratio > CONSUMPTION_DOUBT_RATIO or ratio * CONSUMPTION_DOUBT_RATIO < 1:
        return sentence
    return ""
