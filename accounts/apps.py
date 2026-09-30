from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
    verbose_name = "Comptes et espaces"

    def ready(self):
        # checks: the tenants' and the production server's system checks.
        # middleware: every login pins its tenant in its session
        # (pin_the_tenant, on user_logged_in) - connected here, not when the
        # middleware is first loaded: a test's force_login may come first.
        from . import checks, middleware  # noqa: F401
