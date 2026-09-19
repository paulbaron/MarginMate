from django.urls import path

from . import views

app_name = "transfer"

urlpatterns = [
    path("", views.data_home, name="data_home"),
    path("exporter/", views.data_export, name="data_export"),
    path("importer/", views.data_import, name="data_import"),
    # Before the token route: « sauvegarde » is no token, and would be
    # answered « plus en attente ».
    path("importer/sauvegarde/", views.data_import_backup, name="data_import_backup"),
    path("importer/<str:token>/", views.data_import_stage, name="data_import_stage"),
    path("effacer/", views.data_clear, name="data_clear"),
]
