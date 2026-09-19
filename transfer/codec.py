"""Model fields to JSON and back (§4.3), one rule per kind of field, so no
section invents its own.

Every bug this codebase has had was silently wrong money, so a value that
does not fit its field is refused with a reason (the record is skipped),
never rounded or cut to fit. And comparing goes through the same loading:
"0.7000" in the archive and Decimal("0.7") in memory are the same value -
merging a fresh export of the same data must say « inchangé » for every
record, which is what catches a Decimal's places or a time zone.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import models
from django.utils import timezone


class FieldValueError(ValueError):
    """French reason."""


def _field(model, name: str) -> models.Field:
    model_field = model._meta.get_field(name)
    if model_field.is_relation:
        raise TypeError(f"{model.__name__}.{name} is a relation: the section encodes it by natural key")
    if isinstance(model_field, models.FileField):
        raise TypeError(f"{model.__name__}.{name} is a file: the section encodes it as a file ref")
    return model_field


def _places(model_field: models.DecimalField) -> Decimal:
    return Decimal(1).scaleb(-model_field.decimal_places)


# -- dump: model attribute → JSON value ---------------------------------------------

def dump(obj, name: str):
    model_field = _field(type(obj), name)
    value = getattr(obj, model_field.attname)
    if value is None:
        return None
    if isinstance(model_field, models.DecimalField):
        value = Decimal(value)
        try:
            return str(value.quantize(_places(model_field)))
        except InvalidOperation:
            return str(value)
    if isinstance(model_field, models.DateTimeField):
        if value.tzinfo is None or value.utcoffset() is None:
            raise FieldValueError(f"« {name} » : date sans fuseau horaire")
        return value.astimezone(dt_timezone.utc).isoformat()
    if isinstance(model_field, models.DateField):
        return value.isoformat()
    return value


def record(obj, names: Sequence[str]) -> dict:
    return {name: dump(obj, name) for name in names}


# -- load: JSON value → Python, validated against the field -----------------------

def _missing(model_field, name) -> None:
    """None where the field takes NULL; refused where it does not."""
    if not model_field.null:
        raise FieldValueError(f"« {name} » : valeur manquante")


def load(model, name: str, value):
    model_field = _field(model, name)
    if value is None:
        return _missing(model_field, name)

    if isinstance(model_field, models.DecimalField):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise FieldValueError(f"« {name} » : un nombre s'écrit entre guillemets (« {value} »)")
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation:
            raise FieldValueError(f"« {name} » : « {value} » n'est pas un nombre") from None
        if not number.is_finite():
            raise FieldValueError(f"« {name} » : « {value} » n'est pas un nombre")
        try:
            quantized = number.quantize(_places(model_field))
        except InvalidOperation:
            raise FieldValueError(f"« {name} » : « {value} » a trop de chiffres") from None
        if quantized != number:
            raise FieldValueError(
                f"« {name} » : « {value} » a plus de {model_field.decimal_places} décimales"
            )
        if abs(quantized) >= Decimal(10) ** (model_field.max_digits - model_field.decimal_places):
            raise FieldValueError(f"« {name} » : « {value} » a trop de chiffres")
        value = quantized

    elif isinstance(model_field, models.DateTimeField):
        if not isinstance(value, str):
            raise FieldValueError(f"« {name} » : date illisible (« {value} »)")
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise FieldValueError(f"« {name} » : date illisible (« {value} »)") from None
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise FieldValueError(f"« {name} » : date sans fuseau horaire (« {value} »)")
        try:
            utc = moment.astimezone(dt_timezone.utc)
            timezone.localtime(utc)
        except (OverflowError, ValueError, OSError):
            # A moment on the calendar's first or last day: it has no local
            # time here, and every page saying its date asks for one
            # (Django's date filter). Read into the database, it would be a
            # 500 on the page that shows it; refused here, its record is
            # skipped with this reason, like any other unreadable field.
            raise FieldValueError(f"« {name} » : date hors calendrier (« {value} »)") from None
        value = utc

    elif isinstance(model_field, models.DateField):
        if not isinstance(value, str):
            raise FieldValueError(f"« {name} » : date illisible (« {value} »)")
        try:
            value = date.fromisoformat(value)
        except ValueError:
            raise FieldValueError(f"« {name} » : date illisible (« {value} »)") from None

    elif isinstance(model_field, models.JSONField):
        pass

    elif isinstance(model_field, models.BooleanField):
        if not isinstance(value, bool):
            raise FieldValueError(f"« {name} » : oui ou non attendu (« {value} »)")

    elif isinstance(model_field, (models.IntegerField, models.AutoField)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise FieldValueError(f"« {name} » : nombre entier attendu (« {value} »)")
        if model_field.get_internal_type().startswith("Positive") and value < 0:
            raise FieldValueError(f"« {name} » : nombre positif attendu (« {value} »)")

    elif isinstance(model_field, (models.CharField, models.TextField)):
        if not isinstance(value, str):
            raise FieldValueError(f"« {name} » : texte attendu (« {value} »)")
        # A choice is judged by the choices below, which say more than its length.
        if model_field.max_length is not None and len(value) > model_field.max_length and not model_field.choices:
            raise FieldValueError(f"« {name} » : plus de {model_field.max_length} caractères")

    else:  # pragma: no cover - a kind of field no section exports yet
        raise TypeError(f"{model.__name__}.{name}: no JSON rule for {type(model_field).__name__}")

    if model_field.choices and not isinstance(model_field, models.JSONField):
        allowed = {choice for choice, _label in model_field.flatchoices}
        if value not in allowed:
            raise FieldValueError(f"« {name} » : valeur inconnue (« {value} »)")
    return value


# -- comparing and assigning ---------------------------------------------------------

def _same(model_field, current, new) -> bool:
    if isinstance(model_field, models.JSONField):
        return json.dumps(current, sort_keys=True, default=str) == json.dumps(new, sort_keys=True, default=str)
    if isinstance(model_field, (models.CharField, models.TextField)) and not model_field.choices:
        # '' and NULL are the same text.
        return (current or "") == (new or "")
    if current is None or new is None:
        return current is None and new is None
    if isinstance(model_field, models.DecimalField):
        places = _places(model_field)
        try:
            return Decimal(current).quantize(places) == Decimal(new).quantize(places)
        except InvalidOperation:
            return Decimal(current) == Decimal(new)
    if isinstance(model_field, models.DateTimeField):
        return current == new  # aware on both sides: equal whatever the offset
    return current == new


def _loaded(model, data: dict, names) -> dict:
    return {name: load(model, name, data[name]) for name in names if name in data}


def differences(obj, data: dict, names) -> list[str]:
    """Present fields whose value differs (normalized). A field absent from
    `data` is "not said": never compared, so never a conflict."""
    model = type(obj)
    new_values = _loaded(model, data, names)
    return [
        name
        for name, new in new_values.items()
        if not _same(_field(model, name), getattr(obj, _field(model, name).attname), new)
    ]


def assign(obj, data: dict, names) -> list[str]:
    """Sets the present, known fields; returns the names that changed.
    Every value is checked before any is set, so a bad record leaves `obj`
    as it was."""
    model = type(obj)
    new_values = _loaded(model, data, names)
    changed = []
    for name, new in new_values.items():
        model_field = _field(model, name)
        if not _same(model_field, getattr(obj, model_field.attname), new):
            setattr(obj, model_field.attname, new)
            changed.append(name)
    return changed


def is_blank(value) -> bool:
    """"", [], {}, None - what « Fusionner » may fill (§6.1). 0 and False
    are values."""
    return value is None or (isinstance(value, (str, list, dict, tuple)) and not value)


def unknown_fields(data: dict, names) -> set[str]:
    return set(data) - set(names)


def note_unknown(report, data: dict, names, where: str = "") -> None:
    """One « À savoir » line per (section, field), never one per record."""
    for name in sorted(unknown_fields(data, names)):
        report.note_once(f"champ inconnu ignoré : {where}{name}")
