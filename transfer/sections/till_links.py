"""« Liens recettes ↔ ventes » (§7.5): which till product sells as which
recipe, which ones are ignored, and each recipe's happy-hour till name.

Only till products that are linked or ignored are configuration; a pending
one carries nothing (it is « Ventes »' data, with its days). The happy-hour
names are here and not with the recipes because they route the till's sales:
"Pinte IPA HH" sells as « Pinte IPA » whether or not a till product of that
name exists yet.

The sales per recipe are never copied. Every « laddition » RecipeSale is the
till's per-day quantities summed through these links, so the links are
written directly and every recipe whose set of linked till products changed
- the one it left and the one it joined - is rebuilt once, at the end
(`resync_recipe_from_daily_quantities`, through rebuild.py): what
`links._settle` does after each link, in bulk. Not through `links.link`: its
happy-hour side effects (a name set along with a link, a departing name
cleared) would fight the file's explicit names.
"""

from __future__ import annotations

from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db.models import Q

from recipes.models import PosProduct, Recipe, RecipeSale
from transfer import codec, registry
from transfer.archive import ArchiveError
from transfer.keys import fold
from transfer.sections.base import Section
from transfer.sections.recipes import LABELS, Skip, plural, validation_text

TILL_FIELDS = ("ignored", "category", "typology")
#: What the till's own export says of a product (its TAG_ columns); « Ventes »
#: carries them too, for the products its days name.
DESCRIPTIVE = ("category", "typology")
TILL_KEYS = ("name", "recipe", *TILL_FIELDS)
HAPPY_HOUR_KEYS = ("recipe", "name")
PRODUCTS, HAPPY_HOUR = "produits caisse", "noms happy hour"
#: What a prune or a clear of this section takes away: a link or an
#: « ignoré », never the till product, which stays with its days (« Ventes »'
#: data). Counted as « produits caisse » deleted, a clear of both sections
#: announced 279 till products gone for 211 (UX review, 19/09). Shared by
#: « Recettes », whose clear takes the links with the recipes.
LINKS_GONE, IGNORED_GONE = "liens retirés", "statuts « ignoré » retirés"
#: Said once, above the till products a merge links: see _till_product.
RELINK_WHY = (
    "Fusionner lie (ou ignore) comme dans l'archive les produits caisse « à lier » ici : rien ne distingue un "
    "produit remis « à lier » à la main d'un produit jamais lié. Si l'un d'eux avait été remis « à lier » exprès, "
    "le détacher de nouveau (Recettes & ventes › À lier, « Rattachés » ou « Ignorés »), ou importer sans "
    "« Liens recettes ↔ ventes »."
)
LADDITION = "laddition"


class TillProducts:
    """Till products by name: exactly, then folded when exactly one product
    here folds that way and the archive does not name that one itself. The
    unique constraint is on the exact name, so a till may have « Mojito » and
    « MOJITO » as two products, and an import must not pour one's days or
    link into the other. Shared by « Ventes »."""

    def __init__(self, archive_names=()):
        self._exact: dict[str, PosProduct] = {}
        self._folded: dict[str, list[PosProduct]] = defaultdict(list)
        self._archive_names = set(archive_names)
        for product in PosProduct.objects.all():
            self.add(product)

    def add(self, product: PosProduct) -> None:
        self._exact[product.name] = product
        self._folded[fold(product.name)].append(product)

    def resolve(self, name) -> PosProduct | None:
        if not isinstance(name, str):
            return None
        found = self._exact.get(name)
        if found is not None:
            return found
        candidates = [
            product for product in self._folded.get(fold(name), []) if product.name not in self._archive_names
        ]
        return candidates[0] if len(candidates) == 1 else None


def merge_descriptive(product: PosProduct, values: dict, replacing: bool) -> tuple[list[str], list[str]]:
    """category / typology from the file: set under « Remplacer »; under
    « Fusionner » a blank here is filled and a different value is a conflict
    (§6.1). Returns (the fields changed on `product`, the labels differing)."""
    changed, differing = [], []
    for name in DESCRIPTIVE:
        if name not in values:
            continue
        here, there = getattr(product, name), values[name]
        if here == there:
            continue
        if replacing or codec.is_blank(here):
            setattr(product, name, there)
            changed.append(name)
        elif not codec.is_blank(there):
            differing.append(LABELS[name])
    return changed, differing


def till_names(records) -> set[str]:
    return {record["name"] for record in records if isinstance(record, dict) and isinstance(record.get("name"), str)}


def laddition_rows() -> list[tuple[str, str, int]]:
    """The till's sales per recipe, derived: (recipe, day, quantity)."""
    return sorted(
        (name, sold_on.isoformat(), quantity)
        for name, sold_on, quantity in RecipeSale.objects.filter(source=LADDITION).values_list(
            "recipe__name", "sold_on", "quantity"
        )
    )


def _link_words(recipe_name: str | None, ignored: bool) -> str:
    if recipe_name:
        return f"lié à « {recipe_name} »"
    return "ignoré" if ignored else "à lier"


def _configured():
    return PosProduct.objects.filter(Q(recipe__isnull=False) | Q(ignored=True))


def release(products, report) -> int:
    """`products` back to « à lier », counted as what goes - links and
    « ignoré » statuses (LINKS_GONE) - in `report`. Returns how many till
    products that was."""
    linked = products.filter(recipe__isnull=False).count()
    ignored = products.filter(ignored=True).count()
    released = products.update(recipe=None, ignored=False)
    if linked:
        report.deleted(LINKS_GONE, linked)
    if ignored:
        report.deleted(IGNORED_GONE, ignored)
    return released


@registry.register
class TillLinksSection(Section):
    key = "liens_ventes"

    # -- what this database holds ---------------------------------------------------
    def count(self) -> dict[str, int]:
        return {
            "produits caisse liés": PosProduct.objects.filter(recipe__isnull=False).count(),
            "ignorés": PosProduct.objects.filter(ignored=True).count(),
            HAPPY_HOUR: Recipe.objects.exclude(happy_hour_name="").count(),
        }

    def snapshot(self):
        payload = self._payload()
        return {
            "till_products": sorted(
                (entry["name"], entry["recipe"], entry["ignored"], entry["category"], entry["typology"])
                for entry in payload["till_products"]
            ),
            "happy_hour_names": sorted((entry["recipe"], entry["name"]) for entry in payload["happy_hour_names"]),
            # Derived from the links and the days: a round trip must give
            # the same sales per recipe back, day by day.
            "laddition": laddition_rows(),
        }

    # -- export ------------------------------------------------------------------------
    def _payload(self) -> dict:
        till_products = [
            {
                "name": product.name,
                "recipe": product.recipe.name if product.recipe_id else None,
                "ignored": product.ignored,
                "category": product.category,
                "typology": product.typology,
            }
            for product in _configured().select_related("recipe").order_by("name")
        ]
        happy_hour_names = [
            {"recipe": name, "name": happy_hour_name}
            for name, happy_hour_name in Recipe.objects.exclude(happy_hour_name="").order_by("name").values_list(
                "name", "happy_hour_name"
            )
        ]
        return {"till_products": till_products, "happy_hour_names": happy_hour_names}

    def export(self, out) -> None:
        payload = self._payload()
        out.write(
            payload,
            {
                "produits caisse liés": sum(1 for entry in payload["till_products"] if entry["recipe"]),
                "ignorés": sum(1 for entry in payload["till_products"] if entry["ignored"]),
                HAPPY_HOUR: len(payload["happy_hour_names"]),
            },
        )

    # -- import ------------------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        for name in ("till_products", "happy_hour_names"):
            if not isinstance(payload.get(name), list):
                raise ArchiveError(f"Archive refusée : liens_ventes.json n'a pas de liste « {name} ».")
        self.payload = payload
        self.till_products = payload["till_products"]
        self.happy_hour_names = payload["happy_hour_names"]

    def apply(self, ctx, report) -> None:
        codec.note_unknown(report, self.payload, ("till_products", "happy_hour_names", "supplier_names"))
        self.replacing = ctx.replacing(self.key)
        self.recipes = ctx.recipes()
        self.tills = TillProducts(till_names(self.till_products))
        #: This database's till products the file names, skipped or not:
        #: prune leaves them as they are (a skipped record is left alone).
        self.named: set[int] = set()
        for position, record in enumerate(self.till_products, start=1):
            if not isinstance(record, dict) or not isinstance(record.get("name"), str) or not record["name"].strip():
                report.skip(f"produit caisse n° {position} de l'archive : illisible")
                continue
            product = self.tills.resolve(record["name"])
            if product is not None and product.pk in self.named:
                report.skip(f"Produit caisse « {record['name']} » : deux fois dans l'archive, la seconde est ignorée")
                continue
            if product is not None:
                self.named.add(product.pk)
            codec.note_unknown(report, record, TILL_KEYS, "produits caisse : ")
            try:
                self._till_product(ctx, report, record, product)
            except Skip as exc:
                report.skip(f"Produit caisse « {record['name']} » : {exc}")
        self._happy_hour_names(report)

    def _till_product(self, ctx, report, record: dict, product: PosProduct | None) -> None:
        name = record["name"]
        try:
            codec.load(PosProduct, "name", name)
            values = {field: codec.load(PosProduct, field, record[field]) for field in TILL_FIELDS if field in record}
        except codec.FieldValueError as exc:
            raise Skip(str(exc)) from None
        recipe = None
        if record.get("recipe") is not None:
            if not isinstance(record["recipe"], str):
                raise Skip("recette illisible")
            recipe = self.recipes.resolve(record["recipe"])
            if recipe is None:
                raise Skip(f"recette inconnue « {record['recipe']} »")
        ignored = values.get("ignored", False)
        if recipe is not None and ignored:
            raise Skip("lié à une recette et ignoré à la fois")
        if recipe is None and not ignored:
            raise Skip("ni lié à une recette ni ignoré : rien à importer")

        if product is None:
            product = PosProduct.objects.create(
                name=name, recipe=recipe, ignored=ignored,
                category=values.get("category", ""), typology=values.get("typology", ""),
            )
            self.tills.add(product)
            self.named.add(product.pk)
            if recipe is not None:
                ctx.dirty.recipes.add(recipe.pk)
            report.created(PRODUCTS)
            return

        before = product.recipe_id
        link_here = (product.recipe_id, product.ignored)
        link_file = (recipe.pk if recipe is not None else None, ignored)
        pending = product.recipe_id is None and not product.ignored
        changed, labels = merge_descriptive(product, values, self.replacing)
        differing = [f"différent dans l'archive ({', '.join(labels)})"] if labels else []
        if link_here != link_file:
            if self.replacing or pending:
                product.recipe = recipe
                product.ignored = ignored
                changed += ["recipe", "ignored"]
                if not self.replacing:
                    # A blank filled (§7.5), and it has to be: after
                    # « Effacer » every product is « à lier » and a merge
                    # must bring the links back. But « à lier » is also what
                    # links.set_aside leaves when a person detaches a
                    # product, and nothing records which it was - so the
                    # link, and the sales it brings back, are named, not
                    # only counted as « modifié » (integrity review, 19/09).
                    report.note_once(RELINK_WHY)
                    report.note(
                        f"Produit caisse « {product.name} » : « à lier » ici, "
                        f"{_link_words(recipe.name if recipe else None, ignored)} comme dans l'archive"
                    )
            else:
                here_name = self.recipes_by_pk().get(product.recipe_id)
                differing.insert(
                    0,
                    f"{_link_words(here_name, product.ignored)} ici, "
                    f"{_link_words(recipe.name if recipe else None, ignored)} dans l'archive",
                )
        if changed:
            product.save(update_fields=changed)
            if product.recipe_id != before:
                # Both recipes' sales are rebuilt: the one it left loses its
                # days, the one it joined gains them.
                ctx.dirty.recipes.update(pk for pk in (before, product.recipe_id) if pk is not None)
            report.updated(PRODUCTS)
        if differing:
            report.conflict(f"Produit caisse « {product.name} » : {' ; '.join(differing)} — gardé tel quel")
        elif not changed:
            report.unchanged(PRODUCTS)

    def recipes_by_pk(self) -> dict[int, str]:
        if not hasattr(self, "_recipe_names"):
            self._recipe_names = dict(Recipe.objects.values_list("pk", "name"))
        return self._recipe_names

    def _happy_hour_names(self, report) -> None:
        """Each recipe's happy-hour till name. The model's rule (Recipe.clean:
        one till name, one recipe) is checked against the state the import
        ends in: under « Remplacer » every name that is going - one not in the
        file, or about to become another - is blanked first, so two recipes
        swapping their names, or a name moving from a recipe the file drops
        to one it keeps, is not refused for a clash that will not exist. That
        blanking is the prune of these names, done here for that reason."""
        entries: list[tuple[Recipe, str]] = []
        listed: set[int] = set()
        for position, record in enumerate(self.happy_hour_names, start=1):
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("recipe"), str)
                or not isinstance(record.get("name"), str)
            ):
                report.skip(f"nom happy hour n° {position} de l'archive : illisible")
                continue
            codec.note_unknown(report, record, HAPPY_HOUR_KEYS, "noms happy hour : ")
            recipe_name, name = record["recipe"], record["name"]
            if not name.strip():
                report.skip(f"Recette « {recipe_name} » : nom happy hour vide")
                continue
            recipe = self.recipes.resolve(recipe_name)
            if recipe is None:
                report.skip(f"Nom happy hour « {name} » : recette inconnue « {recipe_name} »")
                continue
            if recipe.pk in listed:
                report.skip(f"Recette « {recipe.name} » : deux noms happy hour dans l'archive, « {name} » est ignoré")
                continue
            listed.add(recipe.pk)
            entries.append((recipe, name))

        before = dict(Recipe.objects.values_list("pk", "happy_hour_name"))
        blanked: set[int] = set()
        if self.replacing:
            targets = {recipe.pk: name for recipe, name in entries}
            blanked = {pk for pk, current in before.items() if current and targets.get(pk) != current}
            if blanked:
                Recipe.objects.filter(pk__in=blanked).update(happy_hour_name="")

        in_conflict: set[int] = set()
        for recipe, name in entries:
            current = "" if recipe.pk in blanked else before[recipe.pk]
            if current == name:
                continue
            if current and not self.replacing:
                report.conflict(
                    f"Recette « {recipe.name} » : nom happy hour « {current} » ici, « {name} » dans l'archive "
                    "— gardé tel quel"
                )
                in_conflict.add(recipe.pk)
                continue
            refusal = self._set_happy_hour(recipe, name)
            if refusal:
                report.skip(f"Recette « {recipe.name} » : nom happy hour « {name} » refusé : {refusal}")
                if before[recipe.pk] and recipe.pk in blanked:
                    # Skipped means left as it was: its own name comes back,
                    # if it still may.
                    self._set_happy_hour(recipe, before[recipe.pk])

        seen = (listed | blanked) - in_conflict
        after = dict(Recipe.objects.filter(pk__in=seen).values_list("pk", "happy_hour_name"))
        for pk in seen:
            old, new = before.get(pk, ""), after.get(pk, "")
            if old == new:
                if old:
                    report.unchanged(HAPPY_HOUR)
            elif not old:
                report.created(HAPPY_HOUR)
            elif not new:
                report.deleted(HAPPY_HOUR)
            else:
                report.updated(HAPPY_HOUR)

    def _set_happy_hour(self, recipe: Recipe, name: str) -> str:
        """Sets and saves it through the model's own rule; returns why it
        was refused, or ""."""
        recipe.happy_hour_name = name
        others = [field.name for field in Recipe._meta.concrete_fields if field.name != "happy_hour_name"]
        try:
            recipe.full_clean(exclude=others, validate_unique=False, validate_constraints=False)
        except ValidationError as exc:
            recipe.refresh_from_db(fields=["happy_hour_name"])
            return validation_text(exc)
        recipe.save(update_fields=["happy_hour_name"])
        return ""

    def prune(self, ctx, report) -> None:
        """A linked or ignored till product the file does not name goes back
        to « à lier »: the product and its days are « Ventes »', only the link
        was this section's. (Happy-hour names were pruned by apply.)"""
        loose = _configured().exclude(pk__in=self.named)
        ids = list(loose.values_list("pk", flat=True))
        recipes = set(loose.filter(recipe__isnull=False).values_list("recipe_id", flat=True))
        released = release(loose, report)
        if not released:
            return
        report.note(f"{plural(released, 'produit caisse remis', 'produits caisse remis')} « à lier »")
        ctx.dirty.recipes.update(recipes)
        if ctx.replacing("ventes"):
            # « Ventes » deletes a till product left with no day and no link
            # (pure data), but it pruned before this - prunes run in reverse
            # order - while the product still had its link. Its rule, applied
            # to what this prune just released, and said in its report.
            _total, per_model = PosProduct.objects.filter(pk__in=ids, daily_quantities__isnull=True).delete()
            if per_model.get(PosProduct._meta.label):
                ctx.report("ventes").deleted(PRODUCTS, per_model[PosProduct._meta.label])

    # -- clear ---------------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        """Every till product back to « à lier » (it and its days stay: they
        are « Ventes »', unless « Ventes » is cleared too), every happy-hour
        name blank, and every recipe's sales rebuilt - with no link left, to
        none."""
        linked = [
            name.strip().lower()
            for name in PosProduct.objects.filter(recipe__isnull=False).values_list("name", flat=True)
        ]
        ids = list(_configured().values_list("pk", flat=True))
        released = release(_configured(), report)
        happy_hour = Recipe.objects.exclude(happy_hour_name="").update(happy_hour_name="")
        if happy_hour:
            report.deleted(HAPPY_HOUR, happy_hour)
        if "ventes" in ctx.clearing:
            # « Ventes », cleared before this (reverse order), kept these as
            # the links' - and deletes a till product with no link and no
            # day. Now they have neither: its rule, said in its report, so a
            # clear of both leaves no empty product « à lier » behind.
            _total, per_model = PosProduct.objects.filter(pk__in=ids, daily_quantities__isnull=True).delete()
            if per_model.get(PosProduct._meta.label):
                ctx.report("ventes").deleted(PRODUCTS, per_model[PosProduct._meta.label])
        elif released:
            # The rows count links; this says the products they were on stay.
            report.note(
                plural(released, "produit caisse remis « à lier » (ses ventes par jour restent)",
                       "produits caisse remis « à lier » (leurs ventes par jour restent)")
            )
        if "recettes" in ctx.clearing:
            return  # the recipes go too, and their sales with them
        ctx.dirty.recipes.update(Recipe.objects.values_list("pk", flat=True))
        # The next till import links again, by itself, a till product named
        # like a recipe (tasks._sync_pos_products): say how many of today's
        # links that is, so a clear is not mistaken for a lasting one.
        names = {name.strip().lower() for name in Recipe.objects.values_list("name", flat=True)}
        relinked = sum(1 for name in linked if name in names)
        if relinked:
            report.note(
                "Le prochain import de la caisse relie de nouveau d'office les produits caisse qui portent le nom "
                f"d'une recette ({relinked} des {len(linked)} liens d'aujourd'hui)."
            )
