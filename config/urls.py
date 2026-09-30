from django.contrib import admin
from django.urls import include, path

# No /media/ route, whatever DEBUG says: every bar's stored files go through
# the logged-in view of its own tenant (accounts.views.media, /fichiers/…).
# The old single mode served media publicly here when DEBUG was on; it was
# removed on 29/09/2026 (accounts/tests/test_no_single_mode.py).
urlpatterns = [
    path("admin/", admin.site.urls),
    path("invoices/", include("invoices.urls")),
    path("recipes/", include("recipes.urls")),
    path("banque/", include("bank.urls")),
    path("marges/", include("margins.urls")),
    path("donnees/", include("transfer.urls")),
    path("personnel/", include("staff.urls")),
    path("consignes/", include("returnables.urls")),
    # Logins and a tenant's stored files (accounts/urls.py).
    path("", include("accounts.urls")),
    path("", include("inventory.urls")),
]
