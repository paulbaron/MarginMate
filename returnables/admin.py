from django.contrib import admin

from .models import (
    Pickup,
    PickupCount,
    PickupPhoto,
    ReturnableType,
    Slip,
    SlipFormat,
    SlipLine,
    delete_with_files,
)


class _FilesOnCommit(admin.ModelAdmin):
    """A pickup, a photo or a slip deleted here takes its files along -
    once the deletion commits, like everywhere else (models.delete_with_files).
    Deleted by the admin's own code, the rows would go and the files stay."""

    def delete_model(self, request, obj):
        delete_with_files(obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            delete_with_files(obj)


@admin.register(ReturnableType)
class ReturnableTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "position", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(SlipFormat)
class SlipFormatAdmin(admin.ModelAdmin):
    list_display = ("name", "supplier", "is_active", "sender_pattern")
    list_filter = ("is_active", "supplier")
    search_fields = ("name",)


class SlipLineInline(admin.TabularInline):
    model = SlipLine
    extra = 0


@admin.register(Slip)
class SlipAdmin(_FilesOnCommit):
    list_display = ("__str__", "format", "origin", "delivery_date", "received_at", "read_error")
    list_filter = ("format", "origin")
    search_fields = ("number", "original_name", "sha256")
    # What the slip IS - its file and the bytes' fingerprint - is never
    # edited by hand; its reading is rewritten by « Relire » on the page.
    readonly_fields = ("sha256", "received_at", "read_at")
    inlines = [SlipLineInline]


class PickupCountInline(admin.TabularInline):
    model = PickupCount
    extra = 0


class PickupPhotoInline(admin.TabularInline):
    model = PickupPhoto
    extra = 0
    # Deleted from here the photo's row would go and its two files stay:
    # a photo is removed from the pickup's page, or with its PickupPhoto.
    can_delete = False


@admin.register(Pickup)
class PickupAdmin(_FilesOnCommit):
    list_display = ("__str__", "date", "supplier", "created_at")
    list_filter = ("supplier",)
    # The natural key « Données » names a pickup by: never edited.
    readonly_fields = ("reference", "created_at", "updated_at")
    inlines = [PickupCountInline, PickupPhotoInline]


@admin.register(PickupPhoto)
class PickupPhotoAdmin(_FilesOnCommit):
    list_display = ("__str__", "pickup", "taken_at", "width", "height", "created_at")


@admin.register(PickupCount)
class PickupCountAdmin(admin.ModelAdmin):
    list_display = ("pickup", "returnable_type", "quantity")


@admin.register(SlipLine)
class SlipLineAdmin(admin.ModelAdmin):
    list_display = ("slip", "position", "designation", "quantity", "unit_amount", "amount")
