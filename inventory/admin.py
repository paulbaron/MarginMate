from django.contrib import admin

from .models import Product, StockMovement, StockType


@admin.register(StockType)
class StockTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "unit", "category", "current_quantity", "current_value_ht"]
    search_fields = ["name", "category"]


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["raw_name", "supplier", "stock_type", "ean"]
    list_filter = ["supplier", "stock_type"]
    search_fields = ["raw_name", "ean"]


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    """Also where a known loss gets recorded for now - a broken bottle, a
    drink offered, a spill: kind=LOSS with a NEGATIVE quantity and the date
    it really happened. Recording it keeps the shelf honest and keeps it out
    of the variance report's unexplained figure."""

    list_display = ["stock_type", "kind", "quantity", "unit_cost_ht", "occurred_on", "note", "created_at"]
    list_filter = ["kind", "stock_type"]
    search_fields = ["note"]
    date_hierarchy = "created_at"
