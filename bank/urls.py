from django.urls import path

from . import views

app_name = "bank"

urlpatterns = [
    path("", views.bank_home, name="bank_home"),
    path("depenses/", views.spending_home, name="spending_home"),
    # What came in on the account, beside what the till took.
    path("entrees/", views.income_home, name="income_home"),
    # « En caisse » on one credit, and « Oublier » on a payer retained.
    path("entrees/operations/<int:pk>/", views.income_source, name="income_source"),
    path("entrees/payeurs/<int:pk>/oublier/", views.income_payer_forget, name="income_payer_forget"),
    # The account's balance on every day (bank/treasury.py): the page, and a
    # balance typed (POST). The view is `treasury_home` - `treasury` is the
    # module views imports.
    path("tresorerie/", views.treasury_home, name="treasury"),
    # An adjustment settling a gap between two points (POST only).
    path("tresorerie/ajustements/", views.treasury_adjustment_add, name="treasury_adjustment_add"),
    # One point: deleted, or dated the day before (POST only).
    path("tresorerie/points/<int:pk>/", views.treasury_point, name="treasury_point"),
    # One adjustment: deleted (POST only).
    path("tresorerie/ajustements/<int:pk>/", views.treasury_adjustment, name="treasury_adjustment"),
    path("rapprocher/", views.bank_reconcile, name="bank_reconcile"),
    # Every invoice the period's spending paid, in one zip.
    path("factures/", views.invoice_zip, name="invoice_zip"),
    # Every proposal still open, on one screen, by certainty tier, to
    # accept in bulk; the POST links them one by one.
    path("propositions/", views.proposals, name="proposals"),
    path("propositions/rattacher/", views.link_proposals, name="link_proposals"),
    path("operations/<int:pk>/", views.bank_line_action, name="bank_line_action"),
    # The results of an invoice search for this operation, alone: the
    # box swaps them into its own row instead of reloading the page.
    path("operations/<int:pk>/chercher/", views.invoice_search, name="invoice_search"),
    path("regles/", views.rule_list, name="rule_list"),
    path("regles/<int:pk>/", views.rule_action, name="rule_action"),
    # « Reconnaissance des opérations »: the rules saying what an operation
    # is and what a credit is in the till (bank/recognition.py); one rule;
    # and the operations already imported read again by them.
    path("reconnaissance/", views.recognition_page, name="recognition"),
    path("reconnaissance/relire/", views.recognition_reapply, name="recognition_reapply"),
    path("reconnaissance/<int:pk>/", views.recognition_rule, name="recognition_rule"),
    # « Format du relevé »: how the bank's CSV export is laid out
    # (bank/statements.py) - the formats and a new one; one format.
    path("format/", views.statement_formats, name="statement_formats"),
    path("format/<int:pk>/", views.statement_format, name="statement_format"),
]
