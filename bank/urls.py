from django.urls import path

from . import views

app_name = "bank"

urlpatterns = [
    path("", views.bank_home, name="bank_home"),
    path("rapprocher/", views.bank_reconcile, name="bank_reconcile"),
    path("operations/<int:pk>/", views.bank_line_action, name="bank_line_action"),
    path("regles/", views.rule_list, name="rule_list"),
    path("regles/<int:pk>/", views.rule_action, name="rule_action"),
]
