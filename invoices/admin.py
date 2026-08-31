from django.contrib import admin

from .models import Invoice, InvoiceLine, ScrapeJob, Supplier


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "parser_key", "is_scrapable"]


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ["__str__", "supplier", "invoice_date", "status", "total_ht"]
    list_filter = ["supplier", "status"]
    inlines = [InvoiceLineInline]


@admin.register(ScrapeJob)
class ScrapeJobAdmin(admin.ModelAdmin):
    list_display = ["id", "status", "invoices_found", "invoices_created", "started_at", "finished_at"]
    readonly_fields = ["log"]
