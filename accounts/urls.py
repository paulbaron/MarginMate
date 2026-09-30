from django.urls import path

from . import pages, views

app_name = "accounts"

urlpatterns = [
    # Public (@login_not_required): the login, the logout (a POST), the
    # signup with an invitation code.
    path("connexion/", pages.login_view, name="login"),
    path("deconnexion/", pages.logout_view, name="logout"),
    path("inscription/", pages.signup_view, name="signup"),
    # A tenant's stored files, behind the login (there is no /media/).
    path("fichiers/<path:name>", views.media, name="media"),
]
