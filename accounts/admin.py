"""The central rows in the admin (superusers only, accounts/admin_site.py):
the tenants, who is in which, the invitations.

What is not done here, on purpose:
- a tenant is never ADDED here (its folder and database would not exist:
  the signup and `manage.py adopt_database` make them) nor deleted (its
  files would stay behind with nothing naming them): it is closed
  (« actif » unticked), which the pages and the signing links obey;
- « utilise les accès du serveur » is read-only: ticked, it hands the
  owner's Metro, mailbox, till and AI accounts to that bar. And « actif »
  never reopens a tenant using them while another one is open
  (`TenantAdminForm`): one Metro account, one pause - accounts.E005's rule,
  which the checks only say at the next start;
- the superuser's OWN tenant cannot be closed here: the admin needs an open
  tenant like every page, and it would shut the admin on him - the very
  place a tenant is reopened;
- an invitation is never added here - its code would never be shown
  (`manage.py create_invitation`); one can be deleted, which revokes it.
"""

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError

from .models import Invitation, Membership, Tenant


class TenantAdminForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = "__all__"

    def clean_is_active(self):
        active = self.cleaned_data["is_active"]
        tenant = self.instance
        if active and tenant.uses_server_integrations:
            others = Tenant.objects.filter(is_active=True, uses_server_integrations=True).exclude(pk=tenant.pk)
            if others.exists():
                named = ", ".join(f"« {other.name} » (dossier {other.dir_name})" for other in others.order_by("pk"))
                raise ValidationError(
                    f"L'espace {named} utilise déjà les accès du serveur (un seul compte Metro, une seule "
                    "pause) : fermez-le d'abord."
                )
        return active


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    form = TenantAdminForm
    list_display = ("name", "dir_name", "is_active", "uses_server_integrations", "created_at")
    list_filter = ("is_active", "uses_server_integrations")
    search_fields = ("name", "dir_name")
    readonly_fields = ("dir_name", "uses_server_integrations", "created_at")

    def get_readonly_fields(self, request, obj=None):
        fields = tuple(super().get_readonly_fields(request, obj))
        if obj is not None and obj.pk == getattr(getattr(request, "tenant", None), "pk", None):
            # His own tenant: closed, the admin would answer « Aucun espace ».
            fields += ("is_active",)
        return fields

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__email", "tenant__name")
    list_select_related = ("user", "tenant")


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    # « utilisée le » with nobody « utilisée par »: voided by its refused
    # addresses (accounts.signup, security audit ANON-5).
    list_display = ("__str__", "created_at", "expires_at", "used_at", "used_by", "refused_addresses")
    list_filter = ("used_at",)
    readonly_fields = ("code_hash", "created_at", "used_at", "used_by", "refused_addresses")
    list_select_related = ("used_by",)

    def has_add_permission(self, request):
        return False
