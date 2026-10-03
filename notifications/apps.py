from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"
    verbose_name = "Notifications"

    def ready(self):
        # The receivers: another login on a browser that receives
        # notifications, a membership moved to another espace. Nothing is
        # started here: the scheduler is `manage.py serve`'s
        # (notifications/scheduler.py).
        from notifications import signals  # noqa: F401
