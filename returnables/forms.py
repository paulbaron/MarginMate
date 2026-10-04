"""« Consignes »: the forms of its pages - a pickup, a returnable type, a
slip format. French everywhere (LANGUAGE_CODE is en-us, so Django's own
messages would speak English), and every refusal has its twin in the
page's HTML where one exists (a date's min/max, a count's pattern and
maxlength): the pickup form carries photos a browser never gives back, so
a refusal the phone could have caught before sending is photos lost.

**A pickup is not a formset.** Each type's count is a field named after
the type (`nombre-<pk>`, `COUNT_PREFIX`), as the timesheet grid names its
days - no row index to shift a count onto its neighbour. Blank is 0; a count
is a whole number from 0 to 9 999, ASCII digits only (« ٣ » is a digit to
`str.isdigit`, never to a count).

**A pattern is checked before it is ever compiled** (returnables/patterns.py:
the structural guard that keeps `(?:x{65535}){65535}` from allocating 50 GB):
every pattern field goes through `patterns.compile_field` with its field's
rules, and the sender pattern through `patterns.check_sender_pattern` too.
"""

from __future__ import annotations

import datetime
import re
from datetime import date

from django import forms
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from common import is_id, search_key
from invoices.models import Supplier
from invoices.parsers import LLM_PARSER_KEY
from returnables import patterns
from returnables.models import MAX_COUNT, ReturnableType, SlipFormat
from returnables.patterns import PatternError

#: The oldest date a pickup may carry.
EARLIEST_DATE = date(2000, 1, 1)
#: A type's count is posted as `nombre-<type pk>`.
COUNT_PREFIX = "nombre-"
COUNT_ERROR = "Un nombre de 0 à 9 999."
_COUNT = re.compile(r"[0-9]{1,4}")

DATE_REQUIRED = "Saisissez la date de la reprise."
DATE_INVALID = "Date illisible : choisissez-la dans le calendrier."
#: The one refusal of the pickup form no HTML attribute can make first.
NOTHING_TO_SAVE = "Rien à enregistrer : comptez au moins un vide, ou ajoutez une photo."
NOTHING_LEFT = "Une reprise sans aucun nombre ni photo ne dit plus rien : supprimez-la plutôt."
NO_DATE_PATTERN = "Sans motif de date, un bon ne peut être rapproché d'aucune reprise."
NO_LINE_PATTERN = "Sans motif de ligne, aucune ligne du bon n'est lue."
NO_SUBJECT = (
    "Avec un motif d'expéditeur, le motif d'objet est obligatoire : sans lui, chaque mail de cet "
    "expéditeur serait lu comme un bon."
)

#: What every pattern field carries: a phone keyboard neither capitalises,
#: corrects nor « smartens » a regex.
PATTERN_ATTRS = {
    "class": "pattern-input",
    "autocapitalize": "off",
    "autocorrect": "off",
    "spellcheck": "false",
    "autocomplete": "off",
}


def check_pickup_date(value, today: date | None = None) -> str:
    """ "" when `value` may date a pickup, else the French reason: none, in
    the future, or before 2000 (a misread year)."""
    today = today or timezone.localdate()
    if value is None:
        return DATE_REQUIRED
    if not EARLIEST_DATE <= value <= today:
        return f"Date impossible : entre le {EARLIEST_DATE:%d/%m/%Y} et aujourd'hui ({today:%d/%m/%Y})."
    return ""


def name_key(name: str) -> str:
    """What two names are compared by: spaces collapsed, accents and case
    dropped - « Futs » is « Fûts »."""
    return search_key(" ".join((name or "").split()))


def supplier_choices(keep=None):
    """The suppliers a pickup or a format can name: those having a slip
    format first, then the others by name. Not the AI pseudo-supplier, nor
    a supplier of charges (the rent takes no empties back) - unless it is
    `keep`, the one already chosen."""
    with_format = SlipFormat.objects.filter(supplier=OuterRef("pk"))
    wanted = Q(expenses_only=False)
    if keep is not None:
        wanted |= Q(pk=keep)
    return (
        Supplier.objects.exclude(parser_key=LLM_PARSER_KEY)
        .filter(wanted)
        .annotate(has_format=Exists(with_format))
        .order_by("-has_format", "name", "pk")
    )


class CountInput(forms.TextInput):
    """A count, typed with the phone's digit keypad: text (a `number` input
    refuses what the keypad's locale writes, and spins), empty by default.

    Its keypad key reads « next » (PickupForm makes the last count's
    « done »): an Android keypad's key is an Enter, and Enter in a field
    sends the form - the pickup saved with only the kegs typed. returnables.js
    catches it and moves to the next count instead; the label says so."""

    def __init__(self, attrs=None):
        base = {
            "inputmode": "numeric",
            "pattern": "[0-9]*",
            "maxlength": "4",
            "autocomplete": "off",
            "enterkeyhint": "next",
            "placeholder": "0",
            "class": "count-input",
        }
        super().__init__({**base, **(attrs or {})})


class CountField(forms.CharField):
    """A type's count: blank is 0, else a whole number from 0 to 9 999."""

    widget = CountInput

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def clean(self, value):
        text = (value or "").strip()
        if not text:
            return 0
        if not _COUNT.fullmatch(text):
            raise forms.ValidationError(COUNT_ERROR, code="invalid")
        number = int(text)
        if number > MAX_COUNT:
            raise forms.ValidationError(COUNT_ERROR, code="invalid")
        return number


class PickupForm(forms.Form):
    """A pickup: its day, who took the empties, a note, and one count per
    type (`types`: the active ones - and, editing, the inactive ones the
    pickup already counts, so an edit never drops them). The photos are
    not a field: the view reads them off the request, after this has said
    yes."""

    date = forms.DateField(
        label="Date",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        error_messages={"required": DATE_REQUIRED, "invalid": DATE_INVALID},
    )
    supplier = forms.ModelChoiceField(
        label="Repris par",
        queryset=Supplier.objects.none(),
        required=False,
        empty_label="— non précisé —",
        error_messages={"invalid_choice": "Ce fournisseur n'existe plus : choisissez-en un dans la liste."},
    )
    note = forms.CharField(
        label="Note",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Facultatif : un fût abîmé, un livreur remplaçant…",
    )

    def __init__(self, *args, types=(), today: datetime.date | None = None, keep_supplier=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.today = today or timezone.localdate()
        if "date" not in self.initial:
            self.fields["date"].initial = self.today
        self.fields["date"].widget.attrs.update(
            {
                "min": f"{EARLIEST_DATE:%Y-%m-%d}",
                "max": f"{self.today:%Y-%m-%d}",
                "data-today": f"{self.today:%Y-%m-%d}",
            }
        )
        self.fields["supplier"].queryset = supplier_choices(keep=keep_supplier)
        self.types = list(types)
        for kind in self.types:
            self.fields[f"{COUNT_PREFIX}{kind.pk}"] = CountField(label=kind.name)
        if self.types:
            # The last count's key closes the keypad (CountInput).
            self.fields[f"{COUNT_PREFIX}{self.types[-1].pk}"].widget.attrs["enterkeyhint"] = "done"

    def clean_date(self):
        value = self.cleaned_data.get("date")
        problem = check_pickup_date(value, self.today)
        if problem:
            raise forms.ValidationError(problem)
        return value

    @property
    def count_rows(self) -> list:
        """[(type, bound field)] in the types' order - the page draws each
        with its stepper, the first (the kegs) bigger."""
        return [(kind, self[f"{COUNT_PREFIX}{kind.pk}"]) for kind in self.types]

    def counts(self) -> dict:
        """{type pk: count} of the counts above 0 (after is_valid)."""
        found = {}
        for kind in self.types:
            number = self.cleaned_data.get(f"{COUNT_PREFIX}{kind.pk}") or 0
            if number:
                found[kind.pk] = number
        return found

    @property
    def shown_date(self) -> str:
        """The date as the folded summary says it (« 12/03/2031 »)."""
        value = self["date"].value()
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value.strip())
            except ValueError:
                return value.strip() or "?"
        return f"{value:%d/%m/%Y}" if isinstance(value, date) else "?"

    @property
    def shown_supplier(self) -> str:
        """Who took the empties, as the folded summary says it."""
        value = self["supplier"].value()
        if value in (None, ""):
            return "non précisé"
        text = str(value)
        if not is_id(text):
            return "non précisé"
        found = self.fields["supplier"].queryset.filter(pk=int(text)).values_list("name", flat=True).first()
        return found or "non précisé"

    @property
    def details_have_errors(self) -> bool:
        """Whether the folded « Reprise du … · repris par … » part must be
        drawn open: one of its fields was refused."""
        return bool(self["date"].errors or self["supplier"].errors or self["note"].errors)


# -- Returnable types -----------------------------------------------------------------------------------------------


class TypeForm(forms.ModelForm):
    """One returnable type: its name (unique whatever its accents and
    case), its place in the form, whether it is offered there, and the
    patterns that recognise a slip's line as this type - one per line, checked
    before they are compiled."""

    position = forms.IntegerField(
        label="Ordre",
        min_value=0,
        max_value=32_767,
        help_text="Le premier type actif a le grand compteur.",
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "off", "class": "position-input"}),
        error_messages={
            "required": "Donnez son ordre (0 pour le premier).",
            "invalid": "Un nombre entier, 0 ou plus.",
            "min_value": "Un nombre entier, 0 ou plus.",
            "max_value": "32 767 au plus.",
        },
    )

    class Meta:
        model = ReturnableType
        fields = ["name", "position", "is_active", "slip_patterns"]
        labels = {
            "name": "Nom",
            "is_active": "Proposé dans le formulaire de reprise",
            "slip_patterns": "Motifs des bons",
        }
        help_texts = {
            "name": "Écrit tel quel partout : « Fûts », « Caisses verre ».",
            "slip_patterns": (
                "Un motif par ligne (50 au plus) : une ligne du bon dont la désignation contient l'un d'eux est de ce "
                "type. Majuscules et minuscules sont confondues."
            ),
        }
        error_messages = {
            "name": {
                "required": "Donnez un nom au type.",
                "max_length": "60 caractères au plus.",
                "unique": "Un type porte déjà ce nom.",
            },
        }
        widgets = {
            "name": forms.TextInput(attrs={"autocomplete": "off"}),
            "slip_patterns": forms.Textarea(attrs={"rows": 3, **PATTERN_ATTRS}),
        }

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").split())
        if not name:
            raise forms.ValidationError("Donnez un nom au type.")
        others = ReturnableType.objects.exclude(pk=self.instance.pk).only("pk", "name")
        for other in others:
            if name_key(other.name) == name_key(name):
                raise forms.ValidationError(f"Le type « {other.name} » existe déjà : choisissez un autre nom.")
        return name

    def clean_slip_patterns(self):
        value = (self.cleaned_data.get("slip_patterns") or "").strip()
        try:
            patterns.compile_field(patterns.TYPE_FIELD, value)
        except PatternError as error:
            raise forms.ValidationError(error.message) from None
        return value


# -- Slip formats -----------------------------------------------------------------------------------------------------

#: The page's groups, in order: (title, fields, folded). « Réglages avancés »
#: is a <details>, open when one of its fields was refused.
FORMAT_GROUPS = (
    ("Le bon", ("name", "supplier", "is_active"), False),
    ("Les lignes", ("section_start", "section_end", "line_pattern", "date_patterns"), False),
    ("Récupération par mail", ("sender_pattern", "subject_pattern", "attachment_pattern"), False),
    (
        "Réglages avancés",
        (
            "printed_patterns",
            "number_patterns",
            "reference_patterns",
            "replaces_pattern",
            "total_patterns",
            "remarks_start",
            "remarks_end",
        ),
        True,
    ),
)

#: One line of help per pattern: what it reads, and which named group.
FORMAT_HELP = {
    "section_start": "La ligne qui ouvre la partie des consignes (ex. « ^REPRISE VIDE ») ; vide = tout le texte.",
    "section_end": "La ligne qui la ferme ; vide = jusqu'à la fin du bon.",
    "line_pattern": (
        "Une ligne de consigne : (?P<designation>…) et (?P<quantite>…) obligatoires, (?P<prix>…) et "
        "(?P<montant>…) si le bon les imprime."
    ),
    "date_patterns": "La date de livraison, (?P<date>…) - un motif par ligne, essayés dans l'ordre.",
    "sender_pattern": (
        "L'adresse de l'expéditeur des mails (avec « @ ») ; vide = dépôt à la main seulement. Majuscules et "
        "minuscules confondues."
    ),
    "subject_pattern": "L'objet de ces mails - obligatoire avec un expéditeur.",
    "attachment_pattern": "Le nom de la pièce jointe lue comme un bon.",
    "printed_patterns": "Quand le bon a été imprimé : (?P<date>…) et, si possible, (?P<heure>…).",
    "number_patterns": "Le numéro du bon, (?P<numero>…).",
    "reference_patterns": "Les références partagées avec la facture (n° de BL), (?P<reference>…) - toutes lues.",
    "replaces_pattern": "Présent sur un bon qui annule et remplace un bon précédent.",
    "total_patterns": "Le total imprimé de la partie des consignes, (?P<total>…).",
    "remarks_start": "La ligne qui ouvre les remarques du bon (ex. « ^ANOMALIES »).",
    "remarks_end": "La ligne qui les ferme.",
}

_ONE_LINE = {
    "section_start",
    "section_end",
    "line_pattern",
    "sender_pattern",
    "subject_pattern",
    "attachment_pattern",
    "replaces_pattern",
    "remarks_start",
    "remarks_end",
}
PATTERN_FIELDS = tuple(field.attr for field in patterns.FORMAT_FIELDS)
DEFAULT_ATTACHMENT = r"(?i)\.pdf$"


class SlipFormatForm(forms.ModelForm):
    """A slip format: who sends it, and every pattern that reads it - each
    checked by returnables/patterns.py before anything compiles it. The mail
    part is optional (a blank sender: dropped by hand only), but a sender
    needs a subject, and must name an address or a domain."""

    class Meta:
        model = SlipFormat
        fields = ["name", "supplier", "is_active", *PATTERN_FIELDS]
        labels = {
            "name": "Nom",
            "supplier": "Fournisseur",
            "is_active": "Actif",
            **{field.attr: field.label for field in patterns.FORMAT_FIELDS},
        }
        help_texts = {
            "name": "Ex. « Grossiste — bon du livreur ».",
            "supplier": "Le fournisseur dont ce format lit les bons : ils sont comparés à ses reprises.",
            "is_active": "Inactif, il ne reconnaît plus aucun document et n'est plus récupéré.",
            **FORMAT_HELP,
        }
        error_messages = {
            "name": {
                "required": "Donnez un nom au format.",
                "max_length": "80 caractères au plus.",
                "unique": "Un format porte déjà ce nom.",
            },
            "supplier": {
                "required": "Choisissez le fournisseur dont ce format lit les bons.",
                "invalid_choice": "Ce fournisseur n'existe plus : choisissez-en un dans la liste.",
            },
            "line_pattern": {"required": NO_LINE_PATTERN},
            "date_patterns": {"required": NO_DATE_PATTERN},
        }
        widgets = {
            "name": forms.TextInput(attrs={"autocomplete": "off"}),
            **{
                attr: (
                    forms.TextInput(attrs=dict(PATTERN_ATTRS))
                    if attr in _ONE_LINE
                    else forms.Textarea(attrs={"rows": 2, **PATTERN_ATTRS})
                )
                for attr in PATTERN_FIELDS
            },
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        keep = self.instance.supplier_id if self.instance.pk else None
        supplier = self.fields["supplier"]
        supplier.queryset = supplier_choices(keep=keep)
        supplier.empty_label = "— choisissez —"
        self.fields["attachment_pattern"].required = False
        for name in PATTERN_FIELDS:
            self.fields[name].error_messages["max_length"] = "%(limit_value)d caractères au plus (%(show_value)d ici)."

    @property
    def groups(self) -> list:
        """[(title, [field names], folded, has errors)] for the page."""
        return [
            (title, list(names), folded, any(self[name].errors for name in names))
            for title, names, folded in FORMAT_GROUPS
        ]

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").split())
        if not name:
            raise forms.ValidationError("Donnez un nom au format.")
        for other in SlipFormat.objects.exclude(pk=self.instance.pk).only("pk", "name"):
            if name_key(other.name) == name_key(name):
                raise forms.ValidationError(f"Le format « {other.name} » existe déjà : choisissez un autre nom.")
        return name

    def _pattern(self, attr: str) -> str:
        value = (self.cleaned_data.get(attr) or "").strip()
        field = patterns.FIELD_BY_ATTR[attr]
        try:
            patterns.compile_field(field, value)
        except PatternError as error:
            raise forms.ValidationError(error.message) from None
        return value

    def clean_sender_pattern(self):
        value = self._pattern("sender_pattern")
        if value:
            try:
                patterns.check_sender_pattern(value)
            except PatternError as error:
                raise forms.ValidationError(error.message) from None
        return value

    def clean_attachment_pattern(self):
        value = self._pattern("attachment_pattern")
        return value or DEFAULT_ATTACHMENT

    def clean_line_pattern(self):
        value = self._pattern("line_pattern")
        if not value:
            raise forms.ValidationError(NO_LINE_PATTERN)
        return value

    def clean_date_patterns(self):
        value = self._pattern("date_patterns")
        if not value:
            raise forms.ValidationError(NO_DATE_PATTERN)
        return value

    def clean(self):
        cleaned = super().clean()
        if (
            cleaned.get("sender_pattern")
            and not cleaned.get("subject_pattern")
            and "subject_pattern" not in self.errors
        ):
            self.add_error("subject_pattern", NO_SUBJECT)
        return cleaned


def _pattern_cleaner(attr: str):
    def clean(self):
        return self._pattern(attr)

    clean.__name__ = f"clean_{attr}"
    return clean


for _attr in PATTERN_FIELDS:
    if not hasattr(SlipFormatForm, f"clean_{_attr}"):
        setattr(SlipFormatForm, f"clean_{_attr}", _pattern_cleaner(_attr))
