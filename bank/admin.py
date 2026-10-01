from django.contrib import admin

from .models import BankTransaction, CounterpartyAlias, IncomePayer, InvoicePayment


@admin.register(BankTransaction)
class BankTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "operation_date",
        "counterparty",
        "amount",
        "kind",
        "no_invoice",
        "settled_by_hand",
        "income_source",
    )
    list_filter = ("kind", "no_invoice", "settled_by_hand", "income_source")
    search_fields = ("label", "counterparty")


@admin.register(InvoicePayment)
class InvoicePaymentAdmin(admin.ModelAdmin):
    list_display = ("transaction", "invoice", "method", "created_at")
    list_filter = ("method",)


@admin.register(CounterpartyAlias)
class CounterpartyAliasAdmin(admin.ModelAdmin):
    list_display = ("name", "supplier")


@admin.register(IncomePayer)
class IncomePayerAdmin(admin.ModelAdmin):
    list_display = ("key", "source", "created_at")
    list_filter = ("source",)
