from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import (
    AutoGather,
    EmailInvoiceSource,
    GatherCoverage,
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


#: Said on a portal source's admin page, above its read-only fields.
PORTAL_IN_SOURCES = (
    "Source d'un espace client : son canal et « Actif » se changent dans Achats → Sources, "
    "par le propriétaire de l'espace, son mot de passe confirmé."
)


@admin.register(InvoiceType)
class InvoiceTypeAdmin(admin.ModelAdmin):
    """A source fetching from a customer portal decides where a stored
    password is typed (its WebsiteInvoiceSource, which the admin does not
    show): it is created and switched on from Achats → Sources, by the
    espace's owner, his MarginMate password confirmed (invoices/views.py).
    Here its channel and « actif » are read-only - a superuser is not the
    owner, and turning it into a mailbox source or back on, behind the
    page's checks, is exactly what that page guards."""

    list_display = ["name", "supplier", "source_kind", "parser_key", "is_active"]
    list_filter = ["source_kind", "is_active", "supplier"]
    inlines = [EmailInvoiceSourceInline]

    @staticmethod
    def _is_a_portal(obj) -> bool:
        return obj is not None and hasattr(obj, "website_source")

    def get_readonly_fields(self, request, obj=None):
        fields = tuple(super().get_readonly_fields(request, obj))
        if self._is_a_portal(obj):
            fields += ("source_kind", "is_active")
        return fields

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if not self._is_a_portal(obj):
            return fieldsets
        where = reverse("invoices:invoice_type_update", args=[obj.pk])
        description = format_html(
            '{} <a href="{}">Ouvrir cette source dans Achats → Sources</a>', PORTAL_IN_SOURCES, where
        )
        return [(name, {**options, "description": description}) for name, options in fieldsets]


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
    list_display = [
        "id",
        "kind",
        "trigger",
        "status",
        "invoices_found",
        "invoices_created",
        "started_at",
        "finished_at",
    ]
    list_filter = ["kind", "trigger", "status"]
    readonly_fields = ["log"]


@admin.register(AutoGather)
class AutoGatherAdmin(admin.ModelAdmin):
    """For looking: the rules are made on /invoices/recuperation-auto/, and
    the scheduler alone writes the slot it last ran and what came of it."""

    list_display = ["name", "weekdays", "start_time", "end_time", "every_minutes", "is_active", "last_result"]
    list_filter = ["is_active"]
    readonly_fields = ["last_slot_at", "last_result", "last_failed_codes", "created_at", "updated_at"]


@admin.register(GatherCoverage)
class GatherCoverageAdmin(admin.ModelAdmin):
    """For looking: how far each source was searched without a gap, and a
    stretch left to catch up by hand - written by the gather alone
    (invoices/coverage.py)."""

    list_display = ["code", "searched_until", "pending_from", "pending_until", "updated_at"]
    readonly_fields = ["code", "searched_until", "pending_from", "pending_until", "updated_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
