from django.urls import path

from . import credentials, members, pages, sudo, views

app_name = "accounts"

urlpatterns = [
    # Public (@login_not_required): the login, the logout (a POST), the
    # signup with an invitation code.
    path("connexion/", pages.login_view, name="login"),
    path("deconnexion/", pages.logout_view, name="logout"),
    path("inscription/", pages.signup_view, name="signup"),
    # An invited employee chooses his password (accounts/members.py): public,
    # the link's token is the key.
    path("invitation/<str:token>/", members.invitation_page, name="member_invitation"),
    # A tenant's stored files, behind the login (there is no /media/).
    path("fichiers/<path:name>", views.media, name="media"),
    # The logins and passwords the gathers sign in with (accounts/credentials.py).
    path("identifiants/", credentials.credentials_page, name="credentials"),
    # The MarginMate password asked again before it (accounts/sudo.py).
    path("identifiants/confirmer/", sudo.confirm_password, name="confirm_password"),
    # « Accès des employés »: the owner's (accounts/members.py, accounts/access.py).
    path("acces-employes/", members.members_page, name="members"),
    # An employee to whom no page is open yet.
    path("aucun-acces/", members.no_access, name="no_access"),
]
