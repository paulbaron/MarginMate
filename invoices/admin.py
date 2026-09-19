from django.contrib import admin

from .models import (
    EmailInvoiceSource,
    Invoice,
    InvoiceLine,
    InvoiceType,
    ScrapeJob,
    ShopItemPrice,
    Supplier,
    SupplierChange,
)


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0


class EmailInvoiceSourceInline(admin.StackedInline):
    model = EmailInvoiceSource
    can_delete = False
    max_num = 1


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "parser_key", "ticket_header", "is_scrapable"]
    # What names a supplier changes on its own page, where every change is
    # recorded (SupplierChange) - never here, around the history.
    readonly_fields = ["code", "ticket_identifiers", "refused_identifiers"]


@admin.register(SupplierChange)
class SupplierChangeAdmin(admin.ModelAdmin):
    list_display = ["created_at", "supplier", "kind", "summary", "cause", "needs_review", "undone_at"]
    list_filter = ["kind", "needs_review", "supplier"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(InvoiceType)
class InvoiceTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "supplier", "source_kind", "parser_key", "is_active"]
    list_filter = ["source_kind", "is_active", "supplier"]
    inlines = [EmailInvoiceSourceInline]


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ["__str__", "supplier", "invoice_date", "status", "total_ht"]
    list_filter = ["supplier", "status"]
    inlines = [InvoiceLineInline]


@admin.register(ShopItemPrice)
class ShopItemPriceAdmin(admin.ModelAdmin):
    """Where a wrong "Prix connu" is deleted: the review screen only adds
    them, since the price is the key (see ShopItemPriceForm)."""

    list_display = ["unit_price_ttc", "label", "supplier", "valid_from", "created_at"]
    list_filter = ["supplier"]
    search_fields = ["label"]


@admin.register(ScrapeJob)
class ScrapeJobAdmin(admin.ModelAdmin):
    list_display = ["id", "kind", "status", "invoices_found", "invoices_created", "started_at", "finished_at"]
    list_filter = ["kind", "status"]
    readonly_fields = ["log"]
