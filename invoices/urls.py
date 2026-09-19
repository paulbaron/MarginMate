from django.urls import path

from . import supplier_views, views

app_name = "invoices"

urlpatterns = [
    path("", views.invoice_list, name="invoice_list"),
    path("upload/", views.upload_invoice, name="invoice_upload"),
    path("new/", views.create_manual_invoice, name="invoice_create_manual"),
    path("gather/", views.trigger_gather, name="gather"),
    path("gather/<int:job_id>/status/", views.gather_status, name="gather_status"),
    path("gather/<int:job_id>/cancel/", views.cancel_gather, name="gather_cancel"),
    path("types/", views.invoice_type_list, name="invoice_type_list"),
    path("fournisseurs/", supplier_views.supplier_list, name="supplier_list"),
    path("fournisseurs/nouveau/", supplier_views.supplier_create, name="supplier_create"),
    path("fournisseurs/<int:pk>/", supplier_views.supplier_detail, name="supplier_detail"),
    path("fournisseurs/<int:pk>/modifier/", supplier_views.supplier_edit, name="supplier_edit"),
    path("fournisseurs/<int:pk>/supprimer/", supplier_views.supplier_delete, name="supplier_delete"),
    path("fournisseurs/<int:pk>/identifiants/", supplier_views.supplier_identifiers, name="supplier_identifiers"),
    path(
        "fournisseurs/<int:pk>/historique/<int:change_pk>/annuler/",
        supplier_views.supplier_change_undo,
        name="supplier_change_undo",
    ),
    path("fournisseurs/<int:pk>/historique/<int:change_pk>/vu/", supplier_views.supplier_change_seen, name="supplier_change_seen"),
    path("fournisseurs/<int:pk>/charges/", views.supplier_expenses, name="supplier_expenses"),
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
    path("<int:pk>/apercu/", views.invoice_preview, name="invoice_preview"),
    path("<int:pk>/supprimer/", views.invoice_delete, name="invoice_delete"),
    path("<int:pk>/lignes/", views.edit_invoice_lines, name="invoice_edit_lines"),
]
