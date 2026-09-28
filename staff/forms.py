"""The forms of « Personnel »: the establishment's header, and an employee
with their typical week.

The typical week's seven fields read hours the way the month's grid does
(`timesheet.parse_hours`): « 7 », « 7,5 », « 7h30 ». A plain DecimalField
would refuse « 7,5 » - LANGUAGE_CODE is en-us, so Django reads the comma as
garbage, in English - on the first form a French owner fills in. And 0, a
day off, is drawn as an empty field reading « repos »: a row of seven
fields where the days off say « 0 » reads like seven figures to check.

Django's own messages are English for the same reason (en-us), so every
message a person can meet here is written out in French.
"""

from decimal import Decimal

from django import forms

from .models import WEEKDAY_FIELDS, Employee, Establishment
from .timesheet import DAY_NAMES, ZERO, HoursError, format_hours, parse_hours

NAME_FIELDS = ("last_name", "first_name")
#: Drawn beside the name: where the signing link, the one-time code and the
#: signed copy go (staff/signature_mail.py). Optional.
EMAIL_FIELD = "email"


class HoursInput(forms.TextInput):
    """One day's hours as the pages write them: « 7,5 », and 0 as an empty
    field (its placeholder says « repos »). What a person typed - a bound
    form drawn back with an error - is shown exactly as typed."""

    def __init__(self, attrs=None):
        defaults = {
            "inputmode": "decimal",
            "autocomplete": "off",
            "placeholder": "repos",
            "class": "hours-input",
            # static/js/timesheet.js adds these up as they are typed.
            "data-week-hours": "",
        }
        super().__init__({**defaults, **(attrs or {})})

    def format_value(self, value):
        if isinstance(value, (Decimal, int)) and not isinstance(value, bool):
            return None if value == 0 else format_hours(value)
        return super().format_value(value)


class HoursField(forms.Field):
    """A day of the typical week: blank is 0 (a day off), and anything
    `parse_hours` refuses is refused with its French sentence."""

    widget = HoursInput

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(**kwargs)

    def to_python(self, value):
        try:
            return parse_hours(value)
        except HoursError as error:
            raise forms.ValidationError(str(error), code="hours") from None


def _week_field(weekday: int) -> HoursField:
    return HoursField(label=DAY_NAMES[weekday])


class FrenchEmailField(forms.EmailField):
    """An address, or blank. Refused in French, naming what was typed:
    LANGUAGE_CODE is en-us, and Django's own « Enter a valid email address. »
    is not what this page says."""

    def run_validators(self, value):
        try:
            super().run_validators(value)
        except forms.ValidationError as error:
            if any(code == "max_length" for code in (getattr(item, "code", "") for item in error.error_list)):
                raise forms.ValidationError("Pas plus de 254 caractères.", code="max_length") from None
            raise forms.ValidationError(f"« {value} » n'est pas une adresse e-mail.", code="invalid") from None


class EmployeeForm(forms.ModelForm):
    """The name, the e-mail address and the typical week - « Ajouter un
    salarié » on the list, and the employee's own page. The week is what a
    month nobody saved yet is drawn from; a month already saved keeps its own
    days, and its own copy of the week it was saved with. The address is
    where the signing link and its code go - optional: without it the owner
    hands them over himself."""

    monday_hours = _week_field(0)
    tuesday_hours = _week_field(1)
    wednesday_hours = _week_field(2)
    thursday_hours = _week_field(3)
    friday_hours = _week_field(4)
    saturday_hours = _week_field(5)
    sunday_hours = _week_field(6)

    class Meta:
        model = Employee
        fields = [*NAME_FIELDS, EMAIL_FIELD, *WEEKDAY_FIELDS]
        field_classes = {EMAIL_FIELD: FrenchEmailField}
        labels = {"last_name": "Nom", "first_name": "Prénom", EMAIL_FIELD: "E-mail (facultatif)"}
        help_texts = {
            "last_name": "Imprimé en majuscules sur la fiche : « DUPONT Jeanne ».",
            EMAIL_FIELD: "Pour lui envoyer le lien de signature de ses fiches et son code. Sans adresse, vous les "
            "lui transmettez vous-même.",
        }
        error_messages = {
            "last_name": {
                "required": "Le nom est obligatoire : c'est lui qui s'imprime sur la fiche.",
                "max_length": "Pas plus de 100 caractères.",
            },
            "first_name": {"max_length": "Pas plus de 100 caractères."},
        }
        widgets = {
            "last_name": forms.TextInput(attrs={"autocomplete": "off"}),
            "first_name": forms.TextInput(attrs={"autocomplete": "off"}),
            EMAIL_FIELD: forms.EmailInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
        }

    @property
    def name_fields(self):
        return [self[name] for name in NAME_FIELDS]

    @property
    def identity_fields(self):
        """The name, then the address: the first row of the form."""
        return [*self.name_fields, self[EMAIL_FIELD]]

    @property
    def week_fields(self):
        return [self[name] for name in WEEKDAY_FIELDS]

    @property
    def week_total_text(self) -> str:
        """The week's total as the seven fields hold it right now - saved,
        or as typed on a page answering an error. "" when one of them is no
        figure: a total that skipped it would be a wrong one."""
        total = ZERO
        for field in self.week_fields:
            value = field.value()
            try:
                total += value if isinstance(value, Decimal) else parse_hours(value)
            except HoursError:
                return ""
        return format_hours(total)


class EstablishmentForm(forms.ModelForm):
    """What every sheet prints at its top. Blank until the owner fills it
    in, and a blank one prints nothing rather than a placeholder."""

    class Meta:
        model = Establishment
        fields = ["name", "address"]
        labels = {"name": "Nom de l'établissement", "address": "Adresse"}
        help_texts = {
            "address": "Comme elle doit s'imprimer : la rue sur une ligne, le code postal et la ville sur la suivante."
        }
        error_messages = {"name": {"max_length": "Pas plus de 255 caractères."}}
        widgets = {
            "name": forms.TextInput(attrs={"autocomplete": "organization"}),
            "address": forms.Textarea(attrs={"rows": 3, "autocomplete": "street-address"}),
        }
