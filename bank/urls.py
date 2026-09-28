from django.urls import path

from . import views

app_name = "bank"

urlpatterns = [
    path("", views.bank_home, name="bank_home"),
    path("depenses/", views.spending_home, name="spending_home"),
    # Ce qui est arrivé sur le compte, à côté de ce que la caisse a encaissé.
    path("entrees/", views.income_home, name="income_home"),
    path("rapprocher/", views.bank_reconcile, name="bank_reconcile"),
    # Toutes les propositions encore ouvertes sur un écran, par degré de
    # certitude, à accepter en série ; le POST les rattache une à une.
    path("propositions/", views.proposals, name="proposals"),
    path("propositions/rattacher/", views.link_proposals, name="link_proposals"),
    path("operations/<int:pk>/", views.bank_line_action, name="bank_line_action"),
    # Les résultats de la recherche d'une facture pour cette opération, seuls :
    # la boîte les échange dans sa propre ligne au lieu de recharger la page.
    path("operations/<int:pk>/chercher/", views.invoice_search, name="invoice_search"),
    path("regles/", views.rule_list, name="rule_list"),
    path("regles/<int:pk>/", views.rule_action, name="rule_action"),
]
