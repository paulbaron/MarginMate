from django.urls import path

from . import views

app_name = "bank"

urlpatterns = [
    path("", views.bank_home, name="bank_home"),
    path("depenses/", views.spending_home, name="spending_home"),
    # What came in on the account, beside what the till took.
    path("entrees/", views.income_home, name="income_home"),
    path("rapprocher/", views.bank_reconcile, name="bank_reconcile"),
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
]
