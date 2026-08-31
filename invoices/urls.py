from django.urls import path

from . import views

app_name = "invoices"

urlpatterns = [
    path("", views.InvoiceListView.as_view(), name="invoice_list"),
    path("upload/", views.upload_invoice, name="invoice_upload"),
    path("gather/", views.trigger_gather, name="gather"),
    path("gather/<int:job_id>/status/", views.gather_status, name="gather_status"),
    path("<int:pk>/", views.InvoiceDetailView.as_view(), name="invoice_detail"),
]
