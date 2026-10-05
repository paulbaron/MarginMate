from django.contrib import admin

from .models import PosProduct, Recipe, RecipeIngredient, RecipeSale, SalesImportJob


class RecipeIngredientInline(admin.TabularInline):
    model = RecipeIngredient
    extra = 1
    fk_name = "recipe"


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "happy_hour_name",
        "category",
        "yield_quantity",
        "yield_unit",
        "sale_quantity",
        "selling_price_ttc",
        "cost_ht",
    ]
    search_fields = ["name", "category"]
    inlines = [RecipeIngredientInline]


@admin.register(RecipeSale)
class RecipeSaleAdmin(admin.ModelAdmin):
    """The admin's view of the stored rows. The screens are « Ventes » (a sale
    typed by hand, the till's import - L'Addition's or a file of any till);
    everything the till sells arrives through recipes.sales.record_sales,
    never through here. `source` is the stored key (« laddition » for every
    till, « manual » for a sale typed by hand)."""

    list_display = ["sold_on", "recipe", "quantity", "source"]
    list_filter = ["source", "recipe"]
    date_hierarchy = "sold_on"


@admin.register(PosProduct)
class PosProductAdmin(admin.ModelAdmin):
    list_display = ["name", "recipe", "ignored", "total_quantity", "category", "last_seen"]
    list_filter = ["ignored", "category", "typology"]
    search_fields = ["name"]


@admin.register(SalesImportJob)
class SalesImportJobAdmin(admin.ModelAdmin):
    list_display = ["started_at", "status", "range_start", "range_end", "items_sold", "recorded", "unmatched"]
    list_filter = ["status"]
