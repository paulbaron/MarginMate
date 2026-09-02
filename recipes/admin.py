from django.contrib import admin

from .models import Recipe, RecipeIngredient


class RecipeIngredientInline(admin.TabularInline):
    model = RecipeIngredient
    extra = 1
    fk_name = "recipe"


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "yield_quantity", "yield_unit", "selling_price_ttc", "cost_ht"]
    search_fields = ["name", "category"]
    inlines = [RecipeIngredientInline]
