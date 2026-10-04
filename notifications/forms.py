"""The forms of the notifications' pages: the bar's night (`NightForm`), a
reminder (`ReminderForm`) and an event's alert (`EventForm`).

Plain forms, not model forms: a reminder's days are boxes stored as "0,2,5",
its times a typed line stored normalised (notifications/schedule.py), its
page a choice of the registry's pages or a path of this site. Each form
writes the columns it shows and nothing else (`model_values`): the
scheduler's own columns are never a form's.

Django's own messages are English (LANGUAGE_CODE is en-us): every field
carries its French ones, the NUL character included. Recipients are user ids
of the espace's ACTIVE members, read as ids (`common.is_id`) and checked
against them - a member who left, a login switched off, an id typed by hand
is « destinataire inconnu ».
"""

from __future__ import annotations

from datetime import time

from django import forms

from accounts.models import Membership
from common import is_id, local_path

from . import registry, schedule
from .models import (
    BODY_MAX_LENGTH,
    DEFAULT_SKIP_HOURS,
    MAX_REMINDERS,
    SKIP_HOURS_MAX,
    SKIP_HOURS_MIN,
    TARGET_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    TOO_MANY_REMINDERS,
    EventRule,
    Reminder,
    clean_ids,
)

#: A NUL in a text field (pasted, or posted by hand): Django's own refusal
#: is English. The sentence of bank/forms.py.
NUL_REFUSED = "Caractère interdit (NUL) : retapez ce champ."
TOO_LONG = "%(limit_value)d caractères au plus (%(show_value)d ici)."
PAGE_REFUSED = "Choisissez une page du site."
NO_DAY = "Cochez au moins un jour."
UNKNOWN_DAY = "Jour inconnu."
NO_RECIPIENT = "Choisissez au moins un destinataire."
UNKNOWN_RECIPIENT = "Destinataire inconnu."
NO_OUTCOME = "Cochez au moins un résultat."
UNKNOWN_OUTCOME = "Résultat inconnu."
SKIP_HOURS_REFUSED = f"Un nombre d'heures entre {SKIP_HOURS_MIN} et {SKIP_HOURS_MAX}."
UNKNOWN_CHOICE = "Choix inconnu."
ONE_NIGHT_END = "Une seule heure, ex. 06:00."
UNKNOWN_SUPPLIER = "Fournisseur inconnu."

#: The new reminder's card: a product's defaults for any bar (counting the
#: empties before a delivery), no day ticked - each bar ticks its own.
NEW_REMINDER = {
    "name": "Consignes avant livraison",
    "title": "Consignes",
    "body": "Photographiez et comptez les vides avant la livraison.",
    "page": "consignes-reprise",
    "times": "00:00",
    "skip_if": registry.RECENT_PICKUP,
    "skip_hours": DEFAULT_SKIP_HOURS,
    "all_members": True,
    "is_active": True,
}
NEW_REMINDER_HELP = "Livraison le matin ? Cochez le soir d'avant : pour une livraison le mardi, le lundi soir."


def _text(label, max_length, *, required=True, required_message="", help_text="", widget=None, **attrs):
    attrs.setdefault("autocomplete", "off")
    return forms.CharField(
        label=label,
        max_length=max_length,
        required=required,
        help_text=help_text,
        widget=widget or forms.TextInput(attrs=attrs),
        error_messages={
            "required": required_message,
            "max_length": TOO_LONG,
            "null_characters_not_allowed": NUL_REFUSED,
        },
    )


def active_members(tenant_id) -> list[tuple[int, str]]:
    """(user id, address) of the espace's members whose login is active, by
    address: who a notification may go to."""
    rows = (
        Membership.objects.filter(tenant_id=tenant_id, user__is_active=True)
        .select_related("user")
        .order_by("user__email", "user_id")
    )
    return [(row.user_id, row.user.email or row.user.username) for row in rows]


class FormPart:
    """Some of a form's fields, for `_form_fields.html` (`with form=part`):
    a card draws its fields in several places, around boxes it draws by hand,
    and the form's own errors (tied to no field) are said once - by the
    first part."""

    def __init__(self, form, names, *, first=False):
        self.form = form
        self.names = tuple(names)
        self.first = first

    def __iter__(self):
        return (self.form[name] for name in self.names if name in self.form.fields)

    def non_field_errors(self):
        return self.form.non_field_errors() if self.first else self.form.error_class()


class _TargetForm(forms.Form):
    """« Page à ouvrir »: one of the registry's pages, or « Autre page du
    site… » and its path (a path of this site only: `common.local_path`).
    The script shows the path field with « autre » only; without it both
    show."""

    #: The empty choice's label (an event's own page), "" for none.
    default_page_label = ""

    page = forms.ChoiceField(
        label="Page à ouvrir",
        widget=forms.Select(attrs={"data-page-select": ""}),
        error_messages={"required": PAGE_REFUSED, "invalid_choice": PAGE_REFUSED},
    )
    path = _text(
        "Adresse de la page",
        TARGET_MAX_LENGTH,
        required=False,
        help_text="Avec « Autre page du site… » : ex. /consignes/",
        **{"data-page-path": ""},
    )

    def __init__(self, *args, request, members=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request
        self.members = list(members)
        choices = registry.page_choices()
        if self.default_page_label:
            choices = [("", self.default_page_label), *choices]
        self.fields["page"].choices = choices
        self.fields["page"].required = not self.default_page_label
        if "recipients" in self.fields:
            self.fields["recipients"].choices = [(str(user_id), address) for user_id, address in self.members]

    @staticmethod
    def initial_page(target) -> dict:
        """The page and path fields drawing a stored target."""
        key = registry.page_key_for(target)
        return {"page": key, "path": target if key == registry.OTHER_PAGE else ""}

    def clean_target(self, cleaned) -> str:
        """The stored path the chosen page is; "" for an event's own page."""
        key = cleaned.get("page")
        if key is None:
            return ""
        if key == "":
            return ""
        if key == registry.OTHER_PAGE:
            path = local_path(self.request, (cleaned.get("path") or "").strip())
            if not path or len(path) > TARGET_MAX_LENGTH:
                self.add_error("path", PAGE_REFUSED)
            return path
        path = registry.page_target(key)
        if not path:
            self.add_error("page", PAGE_REFUSED)
        return path

    # -- Recipients ------------------------------------------------------------------------------------------

    @property
    def shows_recipients(self) -> bool:
        """With one member there is nobody to choose."""
        return len(self.members) > 1

    def clean_recipient_ids(self, cleaned) -> list[int]:
        """The chosen members' ids. The field took only the members' own
        values already (« destinataire inconnu » otherwise)."""
        ids = []
        for value in cleaned.get("recipients") or []:
            if not is_id(value):
                self.add_error("recipients", UNKNOWN_RECIPIENT)
                return []
            if int(value) not in ids:
                ids.append(int(value))
        return ids

    def recipient_boxes(self) -> list[dict]:
        """The members' boxes as the card draws them."""
        chosen = {str(value) for value in (self["recipients"].value() or [])}
        return [
            {"value": str(user_id), "label": address, "checked": str(user_id) in chosen}
            for user_id, address in self.members
        ]

    def all_members_checked(self) -> bool:
        return bool(self["all_members"].value())


def _recipients_fields():
    return {
        "all_members": forms.BooleanField(label="Tous les membres", required=False),
        "recipients": forms.MultipleChoiceField(
            label="Destinataires",
            required=False,
            widget=forms.CheckboxSelectMultiple,
            error_messages={"invalid_choice": UNKNOWN_RECIPIENT, "invalid_list": UNKNOWN_RECIPIENT},
        ),
    }


class NightForm(forms.Form):
    """« La nuit se termine à »: 00:00 (the calendar) or 04:00 to 12:00."""

    night_ends_at = _text(
        "La nuit se termine à",
        20,
        required_message="Indiquez une heure, ex. 06:00.",
        help_text=(
            "Une heure avant celle-ci compte pour la soirée cochée : samedi soir + 02:00 part le dimanche à 02:00. "
            "00:00 = calendrier."
        ),
    )

    def clean_night_ends_at(self) -> time:
        try:
            times = schedule.parse_times(self.cleaned_data["night_ends_at"])
        except ValueError as refusal:
            raise forms.ValidationError(str(refusal)) from None
        if len(times) != 1:
            raise forms.ValidationError(ONE_NIGHT_END)
        refusal = schedule.check_night_end(times[0])
        if refusal:
            raise forms.ValidationError(refusal)
        return times[0]


class ReminderForm(_TargetForm):
    """A reminder's card. `instance`: the reminder edited (None: a new one,
    refused past MAX_REMINDERS)."""

    name = _text("Nom", 80, required_message="Donnez un nom au rappel.")
    title = _text("Titre de la notification", TITLE_MAX_LENGTH, required_message="Donnez un titre à la notification.")
    body = _text("Message", BODY_MAX_LENGTH, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    days = forms.MultipleChoiceField(
        label="Soirs",
        choices=[(str(day), schedule.DAY_SHORT[day]) for day in range(7)],
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": NO_DAY, "invalid_choice": UNKNOWN_DAY, "invalid_list": UNKNOWN_DAY},
    )
    times = _text(
        "Heures",
        120,
        required_message=schedule.NO_TIME,
        help_text="ex. 0h et 2h, ou 00:00 02:00",
    )
    skip_if = forms.ChoiceField(
        label="Ne pas envoyer si…",
        required=False,
        error_messages={"invalid_choice": UNKNOWN_CHOICE},
    )
    skip_hours = forms.IntegerField(
        label="Depuis (heures)",
        required=False,
        min_value=SKIP_HOURS_MIN,
        max_value=SKIP_HOURS_MAX,
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off"}),
        error_messages={
            "invalid": SKIP_HOURS_REFUSED,
            "min_value": SKIP_HOURS_REFUSED,
            "max_value": SKIP_HOURS_REFUSED,
        },
    )
    skip_supplier = forms.ChoiceField(
        label="Repris par",
        required=False,
        error_messages={"invalid_choice": UNKNOWN_SUPPLIER},
    )
    all_members = _recipients_fields()["all_members"]
    recipients = _recipients_fields()["recipients"]
    is_active = forms.BooleanField(label="Actif", required=False)

    #: Where each part of the card is drawn (the boxes come between).
    PARTS = (
        ("name", "title", "body", "page", "path"),
        ("times", "skip_if", "skip_hours", "skip_supplier"),
        ("is_active",),
    )

    def __init__(self, *args, request, instance: Reminder | None = None, members=(), suppliers=(), **kwargs):
        if instance is not None and "initial" not in kwargs and not args:
            kwargs["initial"] = self.initial_for(instance)
        super().__init__(*args, request=request, members=members, **kwargs)
        self.instance = instance
        self.fields["skip_if"].choices = registry.skip_choices()
        self.fields["skip_supplier"].choices = [("", "n'importe lequel"), *suppliers]

    @classmethod
    def initial_for(cls, reminder: Reminder) -> dict:
        try:
            days = [str(day) for day in reminder.weekday_list()]
        except ValueError:
            days = []
        return {
            "name": reminder.name,
            "title": reminder.title,
            "body": reminder.body,
            **cls.initial_page(reminder.target),
            "days": days,
            "times": reminder.times,
            "skip_if": reminder.skip_if,
            "skip_hours": reminder.skip_hours,
            "skip_supplier": str(reminder.skip_supplier_id or ""),
            "all_members": reminder.all_members,
            "recipients": [str(value) for value in clean_ids(reminder.recipient_ids)],
            "is_active": reminder.is_active,
        }

    def parts(self) -> list[FormPart]:
        return [FormPart(self, names, first=not number) for number, names in enumerate(self.PARTS)]

    def clean_days(self) -> tuple[int, ...]:
        try:
            return schedule.parse_weekdays(self.cleaned_data["days"])
        except ValueError:
            raise forms.ValidationError(UNKNOWN_DAY) from None

    def clean_times(self) -> tuple[time, ...]:
        try:
            return schedule.parse_times(self.cleaned_data["times"])
        except ValueError as refusal:
            raise forms.ValidationError(str(refusal)) from None

    def clean(self):
        cleaned = super().clean()
        cleaned["target"] = self.clean_target(cleaned)
        if cleaned.get("skip_hours") is None and not self.has_error("skip_hours"):
            cleaned["skip_hours"] = DEFAULT_SKIP_HOURS
        supplier = cleaned.get("skip_supplier") or ""
        cleaned["skip_supplier_id"] = int(supplier) if is_id(supplier) else None
        if self.shows_recipients:
            ids = self.clean_recipient_ids(cleaned)
            if not cleaned.get("all_members") and not ids and not self.has_error("recipients"):
                self.add_error("recipients", NO_RECIPIENT)
            cleaned["recipient_ids"] = ids
        else:
            cleaned["all_members"] = True
            cleaned["recipient_ids"] = []
        if self.instance is None and Reminder.objects.count() >= MAX_REMINDERS:
            raise forms.ValidationError(TOO_MANY_REMINDERS)
        return cleaned

    def model_values(self) -> dict:
        data = self.cleaned_data
        return {
            "name": " ".join(data["name"].split()),
            "title": data["title"].strip(),
            "body": data["body"].strip(),
            "target": data["target"],
            "weekdays": schedule.weekdays_value(data["days"]),
            "times": schedule.times_value(data["times"]),
            "skip_if": data["skip_if"] or "",
            "skip_hours": data["skip_hours"],
            "skip_supplier_id": data["skip_supplier_id"],
            "all_members": data["all_members"],
            "recipient_ids": data["recipient_ids"],
            "is_active": data["is_active"],
        }

    def save(self) -> Reminder:
        values = self.model_values()
        if self.instance is None:
            return Reminder.objects.create(**values)
        for name, value in values.items():
            setattr(self.instance, name, value)
        self.instance.save(update_fields=[*values, "updated_at"])
        return self.instance

    # -- What the card draws by hand -------------------------------------------------------------------------

    def chosen_days(self) -> set[str]:
        return {str(value) for value in (self["days"].value() or [])}

    def shown_times(self) -> tuple[time, ...]:
        """The times the evenings' labels say: the posted ones when they
        read, else the saved ones, else none."""
        for text in (self["times"].value(), self.instance.times if self.instance else ""):
            try:
                return schedule.parse_times(text)
            except ValueError:
                continue
        return ()

    def day_boxes(self, night_ends_at: time) -> list[dict]:
        times, chosen = self.shown_times(), self.chosen_days()
        return [
            {
                "value": str(day),
                "label": schedule.day_label(day, night_ends_at),
                "sends": schedule.sends_label(day, times, night_ends_at) if times else "",
                "checked": str(day) in chosen,
            }
            for day in range(7)
        ]


class EventForm(_TargetForm):
    """An event's alert (one per registry event). On its first save the
    recipients default to the owner saving it."""

    is_active = forms.BooleanField(label="Active", required=False)
    outcomes = forms.MultipleChoiceField(
        label="Résultats",
        widget=forms.CheckboxSelectMultiple,
        error_messages={"required": NO_OUTCOME, "invalid_choice": UNKNOWN_OUTCOME, "invalid_list": UNKNOWN_OUTCOME},
    )
    all_members = _recipients_fields()["all_members"]
    recipients = _recipients_fields()["recipients"]

    PARTS = (("is_active",), ("page", "path"))

    def __init__(self, *args, request, event: registry.Event, rule: EventRule | None = None, members=(), **kwargs):
        self.default_page_label = f"La page de l'événement ({event.page_label})"
        if "initial" not in kwargs and not args:
            kwargs["initial"] = self.initial_for(event, rule, request)
        super().__init__(*args, request=request, members=members, **kwargs)
        self.event = event
        self.rule = rule
        self.fields["outcomes"].choices = [(outcome.key, outcome.label) for outcome in event.outcomes]

    @classmethod
    def initial_for(cls, event, rule, request) -> dict:
        if rule is None:
            user = getattr(request, "user", None)
            return {
                "is_active": False,
                "outcomes": list(event.default_outcomes),
                "page": "",
                "all_members": False,
                "recipients": [str(user.pk)] if user is not None and user.is_authenticated else [],
            }
        target_page = cls.initial_page(rule.target) if rule.target else {"page": "", "path": ""}
        return {
            "is_active": rule.is_active,
            "outcomes": [key for key in event.outcome_keys if rule.wants(key)],
            **target_page,
            "all_members": rule.all_members,
            "recipients": [str(value) for value in clean_ids(rule.recipient_ids)],
        }

    def parts(self) -> list[FormPart]:
        return [FormPart(self, names, first=not number) for number, names in enumerate(self.PARTS)]

    def clean(self):
        cleaned = super().clean()
        cleaned["target"] = self.clean_target(cleaned)
        if self.shows_recipients:
            ids = self.clean_recipient_ids(cleaned)
            if not cleaned.get("all_members") and not ids and not self.has_error("recipients"):
                self.add_error("recipients", NO_RECIPIENT)
            cleaned["recipient_ids"] = ids
        else:
            # The one member: the owner saving it, by name (another member
            # joining later is not added to it without being chosen).
            cleaned["all_members"] = False
            cleaned["recipient_ids"] = [self.request.user.pk]
        return cleaned

    def model_values(self) -> dict:
        data = self.cleaned_data
        return {
            "outcomes": [key for key in self.event.outcome_keys if key in data["outcomes"]],
            "target": data["target"],
            "all_members": data["all_members"],
            "recipient_ids": data["recipient_ids"],
            "is_active": data["is_active"],
        }

    def save(self) -> EventRule:
        """The event's row, made by its first save."""
        values = self.model_values()
        rule = EventRule.objects.filter(event=self.event.key).first()
        if rule is None:
            rule, created = EventRule.objects.get_or_create(event=self.event.key, defaults=values)
            if created:
                return rule
        for name, value in values.items():
            setattr(rule, name, value)
        rule.save(update_fields=[*values, "updated_at"])
        return rule

    def outcome_boxes(self) -> list[dict]:
        chosen = {str(value) for value in (self["outcomes"].value() or [])}
        return [
            {"value": outcome.key, "label": outcome.label, "checked": outcome.key in chosen}
            for outcome in self.event.outcomes
        ]
