"""Derived data, rebuilt once after every section has applied (§5.6).

Never exported, because the rows it is made from are, and a copy could only
disagree with them: every PURCHASE movement is exactly
`compute_movement_amounts(line)`, and every « laddition » sale is the sum of
the till's daily quantities through the links. The sections say what they
touched (`Dirty`); this rebuilds that much, in bulk, with helpers each
proven equal to the per-object service it stands for (§10.3).

The helpers are imported here, lazily: they belong to the inventory and
recipes apps, and the core must import before they exist. A run that needs
one that is missing fails - and rolls back - rather than leave the derived
data wrong.
"""

from __future__ import annotations

from transfer.sections.base import Dirty


def rebuild(dirty: Dirty) -> dict[str, int]:
    created = statuses = resynced = recounted = 0
    if dirty.lines or dirty.products:
        from inventory.services import rebuild_purchase_movements

        _deleted, created = rebuild_purchase_movements(line_ids=dirty.lines, product_ids=dirty.products)
    if dirty.products or dirty.status_products:
        from inventory.services import refresh_invoice_statuses

        statuses = refresh_invoice_statuses(dirty.products | dirty.status_products)
    if dirty.recipes:
        from recipes.models import Recipe
        from recipes.sales import resync_recipe_from_daily_quantities

        # The recipes still here: a prune or a clear of the same run may have
        # deleted one it had marked, and « Recalculé » says what was rebuilt.
        for recipe in Recipe.objects.filter(pk__in=dirty.recipes):
            resync_recipe_from_daily_quantities(recipe)
            resynced += 1
    if dirty.pos_products:
        from recipes.sales import recount_pos_products

        recounted = recount_pos_products(dirty.pos_products)
    return {
        "mouvements de stock": created,
        "statuts de factures": statuses,
        "ventes par recette": resynced,
        "produits caisse": recounted,
    }
