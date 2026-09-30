from django.apps import AppConfig


class StaffConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "staff"
    verbose_name = "Personnel"

    def ready(self):
        # The signing passphrase's check, staff.W001 (staff/checks.py).
        from . import checks  # noqa: F401
