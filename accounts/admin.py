"""The central rows in the admin (superusers only, accounts/admin_site.py):
the tenants, who is in which, the invitations.

What is not done here, on purpose:
- a tenant is never ADDED here (its folder and database would not exist:
  the signup and `manage.py adopt_database` make them) nor deleted (its
  files would stay behind with nothing naming them): it is closed
  (« actif » unticked), which the pages and the signing links obey;
- « utilise les accès du serveur » is read-only: ticked, it hands that bar
  the server's own accounts - Metro, the supplier portals, the .env's
  mailbox and till values - and the server's names on its pages (every bar
  runs the mailbox and the till with its own « Identifiants » anyway,
  accounts.tenancy.integrations_allowed). And « actif »
  never reopens a tenant using them while another one is open
  (`TenantAdminForm`): one Metro account, one pause - accounts.E005's rule,
  which the checks only say at the next start;
- the superuser's OWN tenant cannot be closed here: the admin needs an open
  tenant like every page, and it would shut the admin on him - the very
  place a tenant is reopened;
- an invitation is never added here - its code would never be shown
  (`manage.py create_invitation`); one can be deleted, which revokes it;
- a membership is never made an owner by default here (`MembershipAdmin`):
  an owner reaches the espace's third-party passwords. A membership added
  here is an employee who opens no page until its boxes are ticked
  (accounts/access.py) - the owner does it from « Accès des employés »;
- an employee's invitation is never added nor changed here: its link is
  shown once, to the owner who makes it (accounts/members.py).
"""

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from .access import AREAS
from .models import Invitation, MemberInvitation, Membership, Tenant


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


#: What being the espace's owner gives, under the role field: the
#: « Identifiants » page and a customer portal's source are the owner's
#: alone (accounts/credentials.py, invoices/views.py), and an employee
#: opens only the pages ticked below (accounts/access.py).
ROLE_HELP = (
    "Propriétaire : voit et modifie les identifiants des comptes (boîte mail, Metro, caisse, espaces clients) et "
    "ouvre toutes les pages. Employé : ouvre seulement les pages cochées ci-dessous."
)


class MembershipAdminForm(forms.ModelForm):
    """The pages as the boxes of « Accès des employés », not a JSON text:
    emptied, the text was a null the column refuses."""

    pages = forms.MultipleChoiceField(
        label="Pages ouvertes",
        choices=[(area.key, area.label) for area in AREAS],
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Pour un employé ; un propriétaire ouvre toutes les pages.",
    )

    class Meta:
        model = Membership
        fields = "__all__"

    def clean_pages(self):
        chosen = set(self.cleaned_data["pages"])
        return [area.key for area in AREAS if area.key in chosen]


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    """A login's place in an espace. A membership added here starts as a
    MEMBER: the model's default is OWNER (the signup's, made with its espace
    - changing it is a migration), and an owner reaches every third-party
    password the espace keeps. Making a second owner of one espace is
    allowed - a bar may have two managers - and said."""

    form = MembershipAdminForm
    list_display = ("user", "tenant", "role", "opened_pages", "created_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__email", "tenant__name")
    list_select_related = ("user", "tenant")

    @admin.display(description="pages ouvertes")
    def opened_pages(self, obj):
        if obj.role == Membership.Role.OWNER:
            return "toutes"
        labels = [area.label for area in AREAS if area.key in (obj.pages or [])]
        return ", ".join(labels) or "aucune"

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        initial.setdefault("role", Membership.Role.MEMBER)
        return initial

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "role":
            kwargs["help_text"] = ROLE_HELP
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.role != Membership.Role.OWNER:
            return
        owners = list(
            Membership.objects.filter(tenant_id=obj.tenant_id, role=Membership.Role.OWNER)
            .select_related("user")
            .order_by("pk")
        )
        if len(owners) > 1:
            named = ", ".join(str(owner.user) for owner in owners)
            messages.warning(
                request,
                f"L'espace « {obj.tenant} » a maintenant {len(owners)} propriétaires ({named}) : chacun voit et "
                "modifie les identifiants des comptes et les espaces clients. Si ce n'est pas voulu, passez ce "
                "membre en « Employé ».",
            )


@admin.register(MemberInvitation)
class MemberInvitationAdmin(admin.ModelAdmin):
    """An employee's invitation, read only: its link was shown once, to
    the owner who made it. Deleting one revokes the link."""

    list_display = ("membership", "created_at", "expires_at")
    readonly_fields = ("membership", "token_hash", "created_at", "expires_at")
    list_select_related = ("membership__user", "membership__tenant")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


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
