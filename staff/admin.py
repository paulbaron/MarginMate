from django.contrib import admin

from .models import WEEKDAY_FIELDS, Employee, Establishment, SignatureEvent, SignatureRequest, Timesheet, TimesheetDay


@admin.register(Establishment)
class EstablishmentAdmin(admin.ModelAdmin):
    list_display = ("name",)


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("last_name", "first_name", "is_active", "weekly_hours")
    list_filter = ("is_active",)
    search_fields = ("last_name", "first_name")


class TimesheetDayInline(admin.TabularInline):
    model = TimesheetDay
    extra = 0


@admin.register(Timesheet)
class TimesheetAdmin(admin.ModelAdmin):
    list_display = ("employee", "month", "weekly_hours", "updated_at")
    list_filter = ("employee",)
    # The week the month was saved with: what its sheet prints as « Semaine
    # type » and plans against. Only « Revenir à la semaine type » replaces it.
    readonly_fields = WEEKDAY_FIELDS
    inlines = [TimesheetDayInline]


class _ReadOnly(admin.ModelAdmin):
    """Evidence is looked at here, never written: a request moves only
    through staff/signature_requests.py, and an event edited by hand is
    exactly what the hash chain exists to reveal."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SignatureRequest)
class SignatureRequestAdmin(_ReadOnly):
    list_display = ("timesheet", "version", "status", "created_at", "employee_signed_at", "employer_signed_at")
    list_filter = ("status",)
    exclude = ("token_hash", "code_hash")


@admin.register(SignatureEvent)
class SignatureEventAdmin(_ReadOnly):
    list_display = ("request", "at", "kind", "ip")
    list_filter = ("kind",)
