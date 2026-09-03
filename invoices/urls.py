from django.urls import path

from . import views

app_name = "invoices"

urlpatterns = [
    path("", views.InvoiceListView.as_view(), name="invoice_list"),
    path("upload/", views.upload_invoice, name="invoice_upload"),
    path("new/", views.create_manual_invoice, name="invoice_create_manual"),
    path("gather/", views.trigger_gather, name="gather"),
    path("gather/<int:job_id>/status/", views.gather_status, name="gather_status"),
    path("gather/<int:job_id>/cancel/", views.cancel_gather, name="gather_cancel"),
    path("types/", views.InvoiceTypeListView.as_view(), name="invoice_type_list"),
    path("types/new/", views.invoice_type_form, name="invoice_type_create"),
    path("types/<int:pk>/edit/", views.invoice_type_form, name="invoice_type_update"),
    path("<int:pk>/", views.InvoiceDetailView.as_view(), name="invoice_detail"),
    path("<int:pk>/lignes/", views.edit_invoice_lines, name="invoice_edit_lines"),
]
