"""The espace's notification rows in the admin (superusers only, the
superuser's own espace). The pages (`/notifications/`) are where rules are
made; the admin is for looking. The history is never added nor edited
here: it says what was sent."""

from django.contrib import admin

from .models import Dispatch, EventRule, NotificationSettings, Reminder


@admin.register(NotificationSettings)
class NotificationSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "last_tick_at")
    readonly_fields = ("last_tick_at",)

    def has_add_permission(self, request):
        return False


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ("name", "title", "weekdays", "times", "skip_if", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "title")
    readonly_fields = ("created_at", "updated_at")


@admin.register(EventRule)
class EventRuleAdmin(admin.ModelAdmin):
    list_display = ("event", "outcomes", "all_members", "is_active", "updated_at")
    list_filter = ("is_active",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Dispatch)
class DispatchAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "rule_name", "title", "status", "devices", "delivered", "detail")
    list_filter = ("status", "kind")
    search_fields = ("rule_name", "title", "detail")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
