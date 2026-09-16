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
    path("tickets/", views.receipt_upload, name="receipt_upload"),
    path("tickets/lots/<int:pk>/", views.receipt_batch, name="receipt_batch"),
    path("tickets/lots/<int:pk>/statut/", views.receipt_batch_status, name="receipt_batch_status"),
    path("tickets/lots/<int:pk>/arreter/", views.receipt_batch_cancel, name="receipt_batch_cancel"),
    path("tickets/lots/<int:pk>/reprendre/", views.receipt_batch_resume, name="receipt_batch_resume"),
    path(
        "tickets/lots/<int:pk>/fichiers/<int:index>/enseigne/",
        views.receipt_batch_assign,
        name="receipt_batch_assign",
    ),
    path("tickets/verification/", views.receipt_queue, name="receipt_queue"),
    path("tickets/<int:pk>/verifier/", views.receipt_review, name="receipt_review"),
    path("supprimer/", views.invoice_bulk_delete, name="invoice_bulk_delete"),
    path("<int:pk>/", views.InvoiceDetailView.as_view(), name="invoice_detail"),
    path("<int:pk>/supprimer/", views.invoice_delete, name="invoice_delete"),
    path("<int:pk>/lignes/", views.edit_invoice_lines, name="invoice_edit_lines"),
]
