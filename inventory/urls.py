from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.StockListView.as_view(), name="stock_list"),
    path("stock-types/new/", views.StockTypeCreateView.as_view(), name="stock_type_create"),
    path("stock-types/<int:pk>/edit/", views.StockTypeUpdateView.as_view(), name="stock_type_update"),
    path("stock-types/<int:pk>/merge/", views.merge_stock_type, name="stock_type_merge"),
    path("stock-types/<int:pk>/delete/", views.delete_stock_type, name="stock_type_delete"),
    path("stock-types/<int:pk>/movements/", views.stock_type_movements, name="stock_type_movements"),
    path("stock-types/<int:pk>/price-history/", views.stock_type_price_history, name="stock_type_price_history"),
    path("stock-types/clear-empty/", views.clear_empty_stock_types, name="clear_empty_stock_types"),
    path("search/", views.search_stock_types, name="search_stock_types"),
    path("export-associations/", views.export_associations, name="export_associations"),
    path("import-associations/", views.import_associations, name="import_associations"),
    path("products/<int:product_id>/remove/", views.remove_product, name="remove_product"),
    path("products/<int:product_id>/edit-conversion/", views.edit_product_conversion, name="edit_product_conversion"),
    path("review/", views.ReviewQueueView.as_view(), name="review_queue"),
    path("review/approve-all/", views.approve_all_suggestions, name="approve_all_suggestions"),
    path("review/<int:product_id>/assign/", views.assign_product, name="assign_product"),
    path("stock-takes/", views.StockTakeListView.as_view(), name="stock_take_list"),
    path("stock-takes/new/", views.stock_take_create, name="stock_take_create"),
    path("stock-takes/<int:pk>/", views.stock_take_detail, name="stock_take_detail"),
    path("stock-takes/<int:pk>/edit/", views.stock_take_update, name="stock_take_update"),
    path("stock-takes/<int:pk>/variance/", views.stock_take_variance, name="stock_take_variance"),
    path("stock-takes/<int:pk>/delete/", views.stock_take_delete, name="stock_take_delete"),
]
