from django.contrib import admin

from .models import EmailInvoiceSource, Invoice, InvoiceLine, InvoiceType, ScrapeJob, ShopItemPrice, Supplier


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
