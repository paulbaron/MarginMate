from datetime import date

from django.urls import path, re_path, register_converter

from . import public_views, signature_views, views
from .timesheet import month_slug, parse_month


class MonthConverter:
    """« 2026-06 » in an address, 1 June 2026 in the view.

    A month that is no month - « 2026-13 », « 2026-6 », a year the pages
    cannot link around (`timesheet.FIRST_YEAR`…`LAST_YEAR`) - does not
    match, so its address is a 404 and never reaches a view that would 500
    on it. `{% url %}` takes the month's date itself."""

    regex = "[0-9]{4}-[0-9]{2}"

    def to_python(self, value: str) -> date:
        month = parse_month(value)
        if month is None:
            raise ValueError(f"No month: {value!r}")
        return month

    def to_url(self, value) -> str:
        if isinstance(value, date):
            value = month_slug(value)
        if parse_month(value) is None:
            raise ValueError(f"No month: {value!r}")
        return value


# Named for this app: converters are registered for the whole project.
register_converter(MonthConverter, "staff_month")

app_name = "staff"

#: The owner's signature actions on a version of a month: POST only, a GET
#: goes back to the month's « Signature » section.
SIGNATURE = "<int:pk>/<staff_month:month>/signature/<int:version>/"

urlpatterns = [
    # THE EMPLOYEE'S PAGES - the only ones meant to stay reachable without an
    # account, ever: a secret link (only its SHA-256 is stored) and a one-time
    # code (staff/public_views.py). Nothing else is reachable from a token.
    path("signer/<str:token>/", public_views.sign, name="sign"),
    path("signer/<str:token>/code/", public_views.send_code, name="sign_send_code"),
    path("signer/<str:token>/verifier/", public_views.check_code, name="sign_check_code"),
    path("signer/<str:token>/signer/", public_views.submit, name="sign_submit"),
    path("signer/<str:token>/document/", public_views.document, name="sign_document"),
    path("signer/<str:token>/exemplaire/", public_views.copy, name="sign_copy"),
    # Anything else under signer/ - no token, a token cut or run into another
    # address - is a link that reaches nothing, said in French.
    re_path(r"^signer/(?P<rest>.*)$", public_views.unknown, name="sign_unknown"),

    # The establishment's header, the employees, « Ajouter un salarié ».
    path("", views.home, name="home"),
    # The name and the typical week (GET, POST), the months.
    path("<int:pk>/", views.employee, name="employee"),
    # Active or not. POST only; a GET goes back to the employee.
    path("<int:pk>/actif/", views.employee_active, name="employee_active"),
    # « Ouvrir un autre mois »: a GET form's month and year, redirected to
    # the month's own address.
    path("<int:pk>/mois/", views.open_month, name="open_month"),
    # The month's grid: GET draws it, POST saves it.
    path("<int:pk>/<staff_month:month>/", views.month, name="month"),
    # The three shortcuts beside the grid. POST only; a GET goes back to the
    # month.
    path("<int:pk>/<staff_month:month>/periode/", views.month_range, name="month_range"),
    path("<int:pk>/<staff_month:month>/feries/", views.month_holidays_off, name="month_holidays_off"),
    path("<int:pk>/<staff_month:month>/semaine-type/", views.month_reset, name="month_reset"),
    path("<int:pk>/<staff_month:month>/pdf/", views.month_pdf, name="month_pdf"),
    # « Signature » (staff/signature_views.py). POST only; a GET goes back to
    # the month's section.
    path("<int:pk>/<staff_month:month>/signature/", signature_views.signature_send, name="signature_send"),
    path("<int:pk>/<staff_month:month>/corriger/", signature_views.month_reopen, name="month_reopen"),
    path(f"{SIGNATURE}lien/", signature_views.signature_link, name="signature_link"),
    path(f"{SIGNATURE}code/", signature_views.signature_code, name="signature_code"),
    path(f"{SIGNATURE}contresigner/", signature_views.signature_countersign, name="signature_countersign"),
    path(f"{SIGNATURE}annuler/", signature_views.signature_cancel, name="signature_cancel"),
    path(f"{SIGNATURE}verifier/", signature_views.signature_verify, name="signature_verify"),
    # A version's files, checked against their hashes and logged (GET).
    path(f"{SIGNATURE}fichier/<slug:file>/", signature_views.signature_file, name="signature_file"),
    # « Supprimer… » a version, in two steps (staff/signature_deletion.py).
    # Step 1: GET says what goes and why it is dangerous; its POST (the box,
    # the phrase) deletes nothing and redirects to step 2 with a signed token.
    # Step 2: GET is the last check; only its POST, the token valid, deletes.
    path(f"{SIGNATURE}supprimer/", signature_views.signature_delete, name="signature_delete"),
    path(f"{SIGNATURE}supprimer/confirmer/", signature_views.signature_delete_confirm, name="signature_delete_confirm"),
]
