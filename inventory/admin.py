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
    list_display = ["stock_type", "quantity", "unit_cost_ht", "invoice_line", "created_at"]
    list_filter = ["stock_type"]
    date_hierarchy = "created_at"
