"""« Recettes » (§7.4): every recipe - prices, VAT, yield - and its
ingredients, a recipe known by its folded name.

Its happy-hour name is left out on purpose: it routes the till's sales, so it
is « Liens recettes ↔ ventes »' data (§7.5). Carried here, a recipe imported
on its own would bring till routing nobody asked for, and clearing the links
would leave a name still routing sales.

A recipe comes in whole or not at all (§6.3). An ingredient that cannot be
found here - an article this database lacks, a sub-recipe that was skipped -
skips its recipe: a recipe missing one ingredient is a cost too low that
nothing on screen would ever show.

Ingredients have no key of their own. They are an ordered list per recipe,
written by (group, id) and created again in that order, because the detail
page's `?v=` option indices count through a group in id order: re-created in
another order, « v=1 » would quietly price another drink.
"""

from __future__ import annotations

import copy
from collections import defaultdict, deque
from decimal import Decimal

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db.models import Count
from django.utils import translation

from recipes.forms import MANUAL_SALE_SOURCE
from recipes.models import (
    PosProduct,
    Recipe,
    RecipeIngredient,
    RecipeSale,
    SaleDocumentLine,
)
from recipes.services import assert_no_cycle
from transfer import codec, registry
from transfer.archive import ArchiveError
from transfer.keys import fold
from transfer.sections.base import Section

RECIPE_FIELDS = ("category", "yield_quantity", "yield_unit", "selling_price_ttc", "happy_hour_price_ttc", "vat_rate")
STAMP = "created_at"
RECIPE_KEYS = ("name", *RECIPE_FIELDS, STAMP, "ingredients")
INGREDIENT_KEYS = ("group", "article", "recipe", "quantity")
#: Every concrete field of the two models is exported or named here, with
#: why; a guard test holds it, so a field added later cannot be left out
#: without anyone deciding it.
NOT_EXPORTED = {
    "Recipe": {"id": "pk", "happy_hour_name": "Liens recettes ↔ ventes (§7.5)"},
    "RecipeIngredient": {
        "id": "pk",
        "recipe": "its parent",
        "stock_type": "by the article's name, as « article »",
        "sub_recipe": "by the recipe's name, as « recipe »",
    },
}

#: What a person reads for a field, in a conflict or a refusal.
LABELS = {
    "name": "nom",
    "category": "catégorie",
    "yield_quantity": "quantité produite",
    "yield_unit": "unité produite",
    "selling_price_ttc": "prix de vente",
    "happy_hour_price_ttc": "prix happy hour",
    "vat_rate": "TVA",
    "happy_hour_name": "nom happy hour",
    "ingredients": "ingrédients",
    "group": "groupe",
    "quantity": "quantité",
    "stock_type": "article",
    "sub_recipe": "sous-recette",
    "typology": "typologie",
    "ignored": "ignoré",
    "recipe": "recette",
    "unit_price_ttc": "prix unitaire",
    "reference": "référence",
    "note": "note",
}

RECIPES, INGREDIENTS = "recettes", "ingrédients"
_QUANTITY = Decimal("0.0001")


class Skip(Exception):
    """One record cannot come in: the message says why, in French."""


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def validation_text(error: ValidationError, labels: dict[str, str] = LABELS) -> str:
    """A model's refusal as a person reads it. LANGUAGE_CODE is en-us, so
    Django's own messages (« Ensure this value is… ») are asked for in
    French here - they are formatted when read, not when raised."""
    with translation.override("fr"):
        if not hasattr(error, "error_dict"):
            return " ".join(error.messages)
        parts = []
        for name, messages in error.message_dict.items():
            text = " ".join(messages)
            label = labels.get(name) if name != NON_FIELD_ERRORS else None
            parts.append(f"{label} : {text}" if label else text)
        return " ; ".join(parts)


# -- the file's sub-recipe graph (pure) --------------------------------------------

def find_cycles(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    """node → a loop through it (["a", "b", "a"]), for every node on one.

    `graph` maps a recipe to the recipes it uses; edges to nodes the graph
    does not hold are ignored. A loop would make costing recurse forever -
    the form refuses one (`services.assert_no_cycle`), and a hand-edited
    archive must not slip one in. Tarjan's strongly connected components,
    iteratively, so a long chain in a forged file cannot exhaust Python's
    recursion limit."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []
    counter = 0

    def enter(node: str) -> None:
        nonlocal counter
        index[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)

    for root, uses in graph.items():
        if root in index:
            continue
        enter(root)
        work = [(root, iter(uses))]
        while work:
            node, children = work[-1]
            descended = False
            for child in children:
                if child not in graph:
                    continue
                if child not in index:
                    enter(child)
                    work.append((child, iter(graph[child])))
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)

    loops: dict[str, list[str]] = {}
    for component in components:
        members = set(component)
        if len(component) == 1 and component[0] not in graph[component[0]]:
            continue
        for member in (node for node in graph if node in members):  # the file's order
            loops[member] = _loop_from(member, graph, members)
    return loops


def _loop_from(start: str, graph: dict[str, list[str]], members: set[str]) -> list[str]:
    """The shortest way from `start` back to itself inside its component."""
    parents: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for child in graph[node]:
            if child == start:
                path = []
                walk: str | None = node
                while walk is not None:
                    path.append(walk)
                    walk = parents[walk]
                return [*reversed(path), start]
            if child in members and child not in parents:
                parents[child] = node
                queue.append(child)
    return [start, start]  # pragma: no cover - a component always loops back


def dependency_order(graph: dict[str, list[str]], keys: list[str]) -> list[str]:
    """`keys` with every recipe after the ones it uses among `keys` - the
    sub-recipes are created first, so their users find them - and the file's
    order otherwise. Loops were taken out before (find_cycles); should one
    remain, it is broken rather than followed for ever."""
    wanted = set(keys)
    placed: list[str] = []
    done: set[str] = set()
    for root in keys:
        if root in done:
            continue
        visiting = {root}
        work = [(root, iter(graph.get(root, ())))]
        while work:
            node, children = work[-1]
            for child in children:
                if child in wanted and child not in done and child not in visiting:
                    visiting.add(child)
                    work.append((child, iter(graph.get(child, ()))))
                    break
            else:
                work.pop()
                visiting.discard(node)
                done.add(node)
                placed.append(node)
    return placed


# -- the section --------------------------------------------------------------------

def _ingredients_by_recipe() -> dict[int, list[dict]]:
    """recipe pk → its ingredients as the archive writes them, by (group, id)."""
    found: dict[int, list[dict]] = defaultdict(list)
    for ingredient in RecipeIngredient.objects.select_related("stock_type", "sub_recipe").order_by(
        "recipe_id", "group", "id"
    ):
        entry: dict = {"group": ingredient.group}
        if ingredient.stock_type_id:
            entry["article"] = ingredient.stock_type.name
        else:
            entry["recipe"] = ingredient.sub_recipe.name
        entry["quantity"] = codec.dump(ingredient, "quantity")
        found[ingredient.recipe_id].append(entry)
    return found


def _records() -> list[dict]:
    ingredients = _ingredients_by_recipe()
    return [
        {"name": recipe.name, **codec.record(recipe, (*RECIPE_FIELDS, STAMP)), "ingredients": ingredients[recipe.pk]}
        for recipe in Recipe.objects.order_by("name")
    ]


def _shape(group, stock_type_id, sub_recipe_id, quantity) -> tuple:
    """An ingredient as compared: what it is, and how much (at the field's
    four places, so « 0.04 » and « 0.0400 » are one quantity)."""
    return (group, stock_type_id, sub_recipe_id, Decimal(quantity).quantize(_QUANTITY))


def _differing(names: list[str]) -> str:
    shown = [LABELS.get(name, name) for name in names[:3]]
    return ", ".join(shown) + (", …" if len(names) > 3 else "")


@registry.register
class RecipesSection(Section):
    key = "recettes"

    # -- what this database holds ---------------------------------------------------
    def count(self) -> dict[str, int]:
        return {RECIPES: Recipe.objects.count(), INGREDIENTS: RecipeIngredient.objects.count()}

    def snapshot(self):
        return sorted(_records(), key=lambda record: (fold(record["name"]), record["name"]))

    # -- export ------------------------------------------------------------------------
    def export(self, out) -> None:
        records = _records()
        out.write(
            {"recipes": records},
            {RECIPES: len(records), INGREDIENTS: sum(len(record["ingredients"]) for record in records)},
        )

    # -- import ------------------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        if not isinstance(payload.get("recipes"), list):
            raise ArchiveError("Archive refusée : recettes.json n'a pas de liste « recipes ».")
        self.payload = payload
        self.records = payload["recipes"]

    def apply(self, ctx, report) -> None:
        codec.note_unknown(report, self.payload, ("recipes", "supplier_names"))
        self.report = report
        self.replacing = ctx.replacing(self.key)
        self.articles = ctx.articles()
        self.recipes = ctx.recipes()
        self.stamped: list[Recipe] = []
        self.current = defaultdict(list)
        for recipe_id, *shape in RecipeIngredient.objects.order_by("recipe_id", "group", "id").values_list(
            "recipe_id", "group", "stock_type_id", "sub_recipe_id", "quantity"
        ):
            self.current[recipe_id].append(_shape(*shape))

        planned = self._planned(report)
        graph = {
            key: [
                fold(item["recipe"])
                for item in (record.get("ingredients") if isinstance(record.get("ingredients"), list) else [])
                if isinstance(item, dict) and isinstance(item.get("recipe"), str)
            ]
            for key, (_name, record) in planned.items()
        }
        graph = {key: [used for used in uses if used in graph] for key, uses in graph.items()}
        loops = find_cycles(graph)
        refused: set[str] = set()
        for key in (key for key in planned if key in loops):  # the file's order
            report.skip(
                f"Recette « {planned[key][0]} » : sous-recettes en boucle : "
                + " → ".join(planned[step][0] for step in loops[key])
            )
            refused.add(key)

        for key in dependency_order(graph, [key for key in planned if key not in loops]):
            name, record = planned[key]
            try:
                self._apply_one(name, record, refused)
            except Skip as exc:
                report.skip(f"Recette « {name} » : {exc}")
                refused.add(key)

        # auto_now_add stamped every created recipe "now"; the archive's
        # date is the real one, and bulk_update does not stamp again.
        if self.stamped:
            Recipe.objects.bulk_update(self.stamped, [STAMP])

    def _planned(self, report) -> dict[str, tuple[str, dict]]:
        """folded name → (name, record), in the file's order; what prune
        must leave alone is every name the file holds, even a record
        skipped later - « skipped » means left as it is here, not deleted."""
        planned: dict[str, tuple[str, dict]] = {}
        self.file_keys: set[str] = set()
        for position, record in enumerate(self.records, start=1):
            if not isinstance(record, dict):
                report.skip(f"recette n° {position} de l'archive : illisible")
                continue
            name = record.get("name")
            if not isinstance(name, str) or not name.strip():
                report.skip(f"recette n° {position} de l'archive : sans nom")
                continue
            key = fold(name)
            self.file_keys.add(key)
            if key in planned:
                report.skip(f"Recette « {name} » : deux fois dans l'archive, la seconde est ignorée")
                continue
            planned[key] = (name, record)
        return planned

    def _apply_one(self, name: str, record: dict, refused: set[str]) -> None:
        codec.note_unknown(self.report, record, RECIPE_KEYS)
        try:
            values = {field: codec.load(Recipe, field, record[field]) for field in RECIPE_FIELDS if field in record}
            stamp = codec.load(Recipe, STAMP, record[STAMP]) if record.get(STAMP) is not None else None
        except codec.FieldValueError as exc:
            raise Skip(str(exc)) from None
        ingredients = self._ingredients(record, refused)

        existing = self.recipes.resolve(name)
        # Checked on a copy: a refused record must leave the recipe here as
        # it was. The happy-hour name is the links' (they check it with
        # Recipe.clean()), and the name is the key, unique by construction.
        probe = copy.copy(existing) if existing is not None else Recipe(name=name)
        for field, value in values.items():
            setattr(probe, Recipe._meta.get_field(field).attname, value)
        try:
            probe.clean_fields(exclude=["happy_hour_name"])
        except ValidationError as exc:
            raise Skip(validation_text(exc)) from None

        if existing is None:
            self._create(probe, ingredients or [], stamp)
        else:
            self._update(existing, record, ingredients, stamp)

    def _ingredients(self, record: dict, refused: set[str]) -> list[RecipeIngredient] | None:
        """The ingredients, resolved and checked, grouped in the order they
        are created; None when the record does not say (§4.3)."""
        if "ingredients" not in record:
            return None
        items = record["ingredients"]
        if not isinstance(items, list):
            raise Skip("ingrédients illisibles")
        planned = []
        for position, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise Skip(f"ingrédient n° {position} illisible")
            codec.note_unknown(self.report, item, INGREDIENT_KEYS, "ingrédients : ")
            article, recipe = item.get("article"), item.get("recipe")
            source = article if article is not None else recipe
            if (article is None) == (recipe is None) or not isinstance(source, str):
                raise Skip(f"ingrédient n° {position} : un article ou une sous-recette, jamais les deux ni aucun")
            try:
                group = codec.load(RecipeIngredient, "group", item["group"]) if "group" in item else 0
                quantity = codec.load(RecipeIngredient, "quantity", item.get("quantity"))
            except codec.FieldValueError as exc:
                raise Skip(f"ingrédient n° {position} : {exc}") from None
            if article is not None:
                stock_type = self.articles.resolve(article)
                if stock_type is None:
                    raise Skip(f"article inconnu « {article} »")
                ingredient = RecipeIngredient(group=group, stock_type=stock_type, quantity=quantity)
            else:
                sub_recipe = self.recipes.resolve(recipe)
                if sub_recipe is None:
                    raise Skip(
                        f"sous-recette « {recipe} » ignorée" if fold(recipe) in refused
                        else f"sous-recette inconnue « {recipe} »"
                    )
                ingredient = RecipeIngredient(group=group, sub_recipe=sub_recipe, quantity=quantity)
            try:
                # The targets were just resolved here; clean() still holds
                # the exactly-one rule the check constraint holds.
                ingredient.full_clean(
                    exclude=["recipe", "stock_type", "sub_recipe"], validate_unique=False, validate_constraints=False
                )
            except ValidationError as exc:
                raise Skip(f"ingrédient n° {position} : {validation_text(exc)}") from None
            planned.append(ingredient)
        # Stable: within a group, the file's order - which is the id order the
        # ?v= indices count in once they are created.
        return sorted(planned, key=lambda ingredient: ingredient.group)

    def _create(self, recipe: Recipe, ingredients: list[RecipeIngredient], stamp) -> None:
        recipe.save()
        for ingredient in ingredients:
            ingredient.recipe = recipe
        RecipeIngredient.objects.bulk_create(ingredients)
        self.recipes.add(recipe)
        if stamp is not None:
            recipe.created_at = stamp
            self.stamped.append(recipe)
        self.report.created(RECIPES)
        if ingredients:
            self.report.created(INGREDIENTS, len(ingredients))

    def _update(self, recipe: Recipe, record: dict, ingredients, stamp) -> None:
        current = self.current.get(recipe.pk, [])
        different = codec.differences(recipe, record, RECIPE_FIELDS)
        new_list = ingredients is not None and [
            _shape(i.group, i.stock_type_id, i.sub_recipe_id, i.quantity) for i in ingredients
        ] != current
        if new_list:
            different.append("ingredients")
        if not different:
            self.report.unchanged(RECIPES)
            if current:
                self.report.unchanged(INGREDIENTS, len(current))
            return
        if not self.replacing:
            self.report.conflict(
                f"Recette « {recipe.name} » : différente dans l'archive ({_differing(different)}) — gardée telle quelle"
            )
            return
        if new_list:
            for ingredient in ingredients:
                if ingredient.sub_recipe_id:
                    try:
                        assert_no_cycle(recipe, ingredient.sub_recipe)
                    except ValidationError as exc:
                        raise Skip(validation_text(exc)) from None
        changed = codec.assign(recipe, record, RECIPE_FIELDS)
        # created_at is never compared (§6.4), and written only on a record
        # that changes for another reason.
        if stamp is not None and recipe.created_at != stamp:
            recipe.created_at = stamp
            changed.append(STAMP)
        if changed:
            recipe.save(update_fields=[Recipe._meta.get_field(field).attname for field in changed])
        self.report.updated(RECIPES)
        if new_list:
            removed, _ = RecipeIngredient.objects.filter(recipe=recipe).delete()
            for ingredient in ingredients:
                ingredient.recipe = recipe
            RecipeIngredient.objects.bulk_create(ingredients)
            if removed:
                self.report.deleted(INGREDIENTS, removed)
            if ingredients:
                self.report.created(INGREDIENTS, len(ingredients))
        elif current:
            self.report.unchanged(INGREDIENTS, len(current))

    def prune(self, ctx, report) -> None:
        """A recipe the file does not have goes, unless something kept still
        needs it (§6.2) - each one kept is said, with what holds it."""
        names = dict(Recipe.objects.values_list("pk", "name"))
        doomed = {pk for pk, name in names.items() if fold(name) not in self.file_keys}
        if not doomed:
            return
        reasons: dict[int, str] = {}
        sales_kept = "" if ctx.replacing("ventes") else " (Ventes non remplacées)"
        for recipe_id, n in (
            SaleDocumentLine.objects.filter(recipe_id__in=doomed).values_list("recipe_id").annotate(n=Count("id"))
        ):
            reasons[recipe_id] = f"{plural(n, 'ligne de bon de vente la cite', 'lignes de bons de vente la citent')}{sales_kept}"
        for recipe_id, n in (
            RecipeSale.objects.filter(recipe_id__in=doomed, source=MANUAL_SALE_SOURCE)
            .values_list("recipe_id").annotate(n=Count("id"))
        ):
            reasons.setdefault(
                recipe_id, f"{plural(n, 'vente saisie à la main', 'ventes saisies à la main')} s'y rapporte{'nt' if n > 1 else ''}{sales_kept}"
            )
        links_kept = "" if ctx.replacing("liens_ventes") else " (liens non remplacés)"
        for recipe_id, n in (
            PosProduct.objects.filter(recipe_id__in=doomed).values_list("recipe_id").annotate(n=Count("id"))
        ):
            reasons.setdefault(recipe_id, f"liée à {plural(n, 'produit caisse', 'produits caisse')}{links_kept}")
        # A sub-recipe of a recipe that stays stays too (PROTECT); a recipe
        # kept for any reason keeps what it uses, so this runs to a fixpoint.
        users: dict[int, set[int]] = defaultdict(set)
        for recipe_id, sub_recipe_id in RecipeIngredient.objects.filter(sub_recipe_id__in=doomed).values_list(
            "recipe_id", "sub_recipe_id"
        ):
            users[sub_recipe_id].add(recipe_id)
        grew = True
        while grew:
            grew = False
            for pk in sorted(doomed - set(reasons)):
                keeping = sorted(
                    (names[user] for user in users[pk] if user not in doomed or user in reasons), key=fold
                )
                if keeping:
                    others = f" et de {plural(len(keeping) - 1, 'autre', 'autres')}" if len(keeping) > 1 else ""
                    reasons[pk] = f"sous-recette de « {keeping[0]} »{others}, gardée"
                    grew = True
        for pk in sorted(reasons, key=lambda pk: fold(names[pk])):
            report.keep(f"Recette « {names[pk]} » : {reasons[pk]}")

        gone = doomed - set(reasons)
        if not gone:
            return
        happy_hour = Recipe.objects.filter(pk__in=gone).exclude(happy_hour_name="").count()
        # Ingredients first: a sub-recipe is PROTECTed by the rows using it,
        # and those users may be going too.
        ingredients, _ = RecipeIngredient.objects.filter(recipe_id__in=gone).delete()
        _total, deleted = Recipe.objects.filter(pk__in=gone).delete()
        report.deleted(RECIPES, deleted.get(Recipe._meta.label, 0))
        if ingredients:
            report.deleted(INGREDIENTS, ingredients)
        if happy_hour:
            # The name went with its recipe: the links' data, said in their
            # report so their section is in the safety export.
            ctx.report("liens_ventes").deleted("noms happy hour", happy_hour)

    # -- clear ---------------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        """After the links and the sales (the closure puts them first): every
        ingredient, then every recipe. What the recipes still carry of those
        sections (only when a test clears this one alone) is said in their
        reports."""
        manual = RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).count()
        linked = PosProduct.objects.filter(recipe__isnull=False).count()
        happy_hour = Recipe.objects.exclude(happy_hour_name="").count()
        ingredients, _ = RecipeIngredient.objects.all().delete()
        _total, deleted = Recipe.objects.all().delete()
        if deleted.get(Recipe._meta.label):
            report.deleted(RECIPES, deleted[Recipe._meta.label])
        if ingredients:
            report.deleted(INGREDIENTS, ingredients)
        if manual:
            ctx.report("ventes").deleted("ventes saisies", manual)
        if linked:
            # SET_NULL took the links, not the till products: counted as
            # links, as the links' own clear counts them (till_links.LINKS_GONE).
            ctx.report("liens_ventes").deleted("liens retirés", linked)
        if happy_hour:
            ctx.report("liens_ventes").deleted("noms happy hour", happy_hour)
