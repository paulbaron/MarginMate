from django.urls import path

from . import views

app_name = "recipes"

urlpatterns = [
    path("", views.RecipeListView.as_view(), name="recipe_list"),
    path("new/", views.recipe_create, name="recipe_create"),
    path("<int:pk>/", views.recipe_detail, name="recipe_detail"),
    path("<int:pk>/edit/", views.recipe_update, name="recipe_update"),
    path("<int:pk>/delete/", views.recipe_delete, name="recipe_delete"),
    # Sales from the till (L'Addition).
    path("caisse/", views.pos_product_list, name="pos_product_list"),
    path("caisse/<int:pk>/assign/", views.pos_product_assign, name="pos_product_assign"),
    path("caisse/bulk/", views.pos_products_bulk, name="pos_products_bulk"),
    path("caisse/ventes/", views.sales_list, name="sales_list"),
    path("caisse/ventes/facture/", views.sale_document_form, name="sale_document_create"),
    path("caisse/ventes/facture/<int:pk>/", views.sale_document_form, name="sale_document_update"),
    path("caisse/ventes/facture/<int:pk>/delete/", views.sale_document_delete, name="sale_document_delete"),
    path("caisse/ventes/<int:pk>/delete/", views.sales_delete, name="sales_delete"),
    path("caisse/import/", views.sales_import, name="sales_import"),
    path("caisse/import/run/", views.trigger_sales_import, name="trigger_sales_import"),
    path("caisse/import/<int:job_id>/status/", views.sales_import_status, name="sales_import_status"),
    path("caisse/import/<int:job_id>/cancel/", views.cancel_sales_import, name="cancel_sales_import"),
]
