from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.StockListView.as_view(), name="stock_list"),
    path("stock-types/new/", views.StockTypeCreateView.as_view(), name="stock_type_create"),
    path("stock-types/<int:pk>/edit/", views.StockTypeUpdateView.as_view(), name="stock_type_update"),
    path("stock-types/<int:pk>/delete/", views.delete_stock_type, name="stock_type_delete"),
    path("stock-types/clear-empty/", views.clear_empty_stock_types, name="clear_empty_stock_types"),
    path("products/<int:product_id>/remove/", views.remove_product, name="remove_product"),
    path("products/<int:product_id>/edit-conversion/", views.edit_product_conversion, name="edit_product_conversion"),
    path("review/", views.ReviewQueueView.as_view(), name="review_queue"),
    path("review/suggest/", views.trigger_suggest_products, name="suggest_products"),
    path("review/suggest/<int:job_id>/status/", views.suggestion_job_status, name="suggestion_job_status"),
    path("review/suggest/<int:job_id>/cancel/", views.cancel_suggestion_job, name="cancel_suggestion_job"),
    path("review/approve-all/", views.approve_all_suggestions, name="approve_all_suggestions"),
    path("review/<int:product_id>/assign/", views.assign_product, name="assign_product"),
]
