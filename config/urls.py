from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
    path("invoices/", include("invoices.urls")),
    path("recipes/", include("recipes.urls")),
    path("banque/", include("bank.urls")),
    path("donnees/", include("transfer.urls")),
    path("", include("inventory.urls")),
]

if settings.DEBUG:
    # An invoice's PDF is shown inside its own correction page, so this site
    # may frame its uploaded files; every other page keeps DENY.
    urlpatterns += [
        re_path(
            rf"^{settings.MEDIA_URL.lstrip('/')}(?P<path>.*)$",
            xframe_options_sameorigin(serve),
            {"document_root": settings.MEDIA_ROOT},
        )
    ]
