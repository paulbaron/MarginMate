"""« Consignes » (`/consignes/`, config/urls.py). French path segments,
English names; `<int:pk>` only - no path converter of the app's own (a new
one would need its sample in accounts/tests/test_middleware.py). Every
route wants a login, like the rest of the app. « Récupérer les bons » has
no route here: the page posts to invoices:gather, which comes back with
`retour`."""

from django.urls import path

from . import views

app_name = "returnables"

urlpatterns = [
    path("", views.home, name="home"),
    # A reprise: its page (edit, photos, comparison), and three POST-only actions.
    path("reprises/<int:pk>/", views.pickup_detail, name="pickup_detail"),
    path("reprises/<int:pk>/supprimer/", views.pickup_delete, name="pickup_delete"),
    path("reprises/<int:pk>/date/", views.pickup_date, name="pickup_date"),
    path("photos/<int:pk>/supprimer/", views.photo_delete, name="photo_delete"),
    # The bons: dropped by hand, read, read again, deleted.
    path("bons/deposer/", views.slip_upload, name="slip_upload"),
    path("bons/<int:pk>/", views.slip_detail, name="slip_detail"),
    path("bons/<int:pk>/supprimer/", views.slip_delete, name="slip_delete"),
    path("bons/<int:pk>/relire/", views.slip_reread, name="slip_reread"),
    path("lignes/<int:pk>/classer/", views.line_classify, name="line_classify"),
    # The formats of bon (`?depuis=<pk>` duplicates one) and the types of consigne.
    path("formats/", views.format_list, name="format_list"),
    path("formats/nouveau/", views.format_create, name="format_create"),
    path("formats/<int:pk>/", views.format_edit, name="format_edit"),
    path("formats/<int:pk>/supprimer/", views.format_delete, name="format_delete"),
    path("formats/<int:pk>/relire/", views.format_reread, name="format_reread"),
    path("types/", views.type_list, name="type_list"),
    path("types/<int:pk>/", views.type_edit, name="type_edit"),
    path("types/<int:pk>/supprimer/", views.type_delete, name="type_delete"),
]
