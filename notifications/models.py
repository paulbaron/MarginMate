"""The espace's rules and history: its night (`NotificationSettings`), the
reminders on a weekly schedule (`Reminder`), the alerts on an event
(`EventRule`) and every notification made of them (`Dispatch`).

Tenant tables, in each bar's database. Recipients are plain user ids - no
foreign key reaches the central logins (accounts/tests/test_router.py), and
none reaches a slip, a reprise or an invoice either: « Données » may clear
those whatever the history says. Nothing here is exported, imported or
cleared by « Données ».

**`Dispatch` is the outbox AND the history.** A job never sends: it inserts a
`pending` row (`create_dispatch`, each insert in its own savepoint, its
`dedupe_key` unique - a second insert of the same reminder instant or the
same event result is « already done »), and a delivery thread claims it
(`pending → sending`, a conditional UPDATE) and sends outside any
transaction (notifications/sending.py). A row that is never sent says why:
missed, skipped, disabled, no device, expired, interrupted.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from . import schedule

#: Caps per espace (another bar's owner must not fill the shared scheduler).
MAX_REMINDERS = 50
TOO_MANY_REMINDERS = "50 rappels au plus."
SKIP_HOURS_MIN = 1
SKIP_HOURS_MAX = 48
DEFAULT_SKIP_HOURS = 6

TITLE_MAX_LENGTH = 80
BODY_MAX_LENGTH = 400
TARGET_MAX_LENGTH = 500
DEDUPE_KEY_MAX_LENGTH = 200
DETAIL_MAX_LENGTH = 300


class NotificationSettings(models.Model):
    """The espace's settings, one row (`get_solo`). `last_tick_at` is the
    scheduler's alone (its heartbeat, and where the next tick's reminders
    start from): a form never writes it."""

    SINGLETON_PK = 1

    night_ends_at = models.TimeField("la nuit se termine à", default=schedule.DEFAULT_NIGHT_END)
    last_tick_at = models.DateTimeField("dernier passage du planificateur", null=True, blank=True)

    class Meta:
        verbose_name = "réglage des notifications"
        verbose_name_plural = "réglages des notifications"

    def __str__(self):
        return f"La nuit se termine à {self.night_ends_at:%H:%M}"

    @classmethod
    def get_solo(cls) -> NotificationSettings:
        settings, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return settings


class Reminder(models.Model):
    """A notification on a weekly schedule: its evenings (`weekdays`, "0,2,5",
    0 = lundi) and times ("00:00 02:00", normalised), read through the
    espace's night (notifications/schedule.py)."""

    name = models.CharField("nom", max_length=80)
    title = models.CharField("titre", max_length=TITLE_MAX_LENGTH)
    body = models.CharField("message", max_length=BODY_MAX_LENGTH, blank=True)
    target = models.CharField("page à ouvrir", max_length=TARGET_MAX_LENGTH)
    weekdays = models.CharField("soirs", max_length=14)
    times = models.CharField("heures", max_length=120)
    skip_if = models.CharField("ne pas envoyer si", max_length=40, blank=True)
    skip_hours = models.PositiveSmallIntegerField(
        "depuis (heures)",
        default=DEFAULT_SKIP_HOURS,
        validators=[MinValueValidator(SKIP_HOURS_MIN), MaxValueValidator(SKIP_HOURS_MAX)],
    )
    #: « Repris par »: a supplier's id, None for any.
    skip_supplier_id = models.PositiveIntegerField("repris par", null=True, blank=True)
    all_members = models.BooleanField("tous les membres", default=True)
    recipient_ids = models.JSONField("destinataires", default=list, blank=True)
    is_active = models.BooleanField("actif", default=True)
    created_at = models.DateTimeField("créé le", default=timezone.now)
    updated_at = models.DateTimeField("modifié le", auto_now=True)

    class Meta:
        verbose_name = "rappel"
        verbose_name_plural = "rappels"
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name

    def weekday_list(self) -> tuple[int, ...]:
        """The stored evenings; ValueError when the row holds something else."""
        return schedule.parse_weekdays(self.weekdays)

    def time_list(self) -> tuple:
        """The stored times; ValueError when the row holds something else."""
        return schedule.parse_times(self.times)

    def recipients(self) -> list[int] | None:
        """The user ids a dispatch goes to; None: every member."""
        return None if self.all_members else clean_ids(self.recipient_ids)


class EventRule(models.Model):
    """An alert on an event of notifications.registry.EVENTS: one row per
    event, made by the first save of that event's card."""

    event = models.CharField("événement", max_length=60, unique=True)
    outcomes = models.JSONField("résultats", default=list)
    #: Blank: the event's own page (the slip, Factures).
    target = models.CharField("page à ouvrir", max_length=TARGET_MAX_LENGTH, blank=True)
    all_members = models.BooleanField("tous les membres", default=False)
    recipient_ids = models.JSONField("destinataires", default=list, blank=True)
    is_active = models.BooleanField("active", default=True)
    created_at = models.DateTimeField("créée le", default=timezone.now)
    updated_at = models.DateTimeField("modifiée le", auto_now=True)

    class Meta:
        verbose_name = "alerte"
        verbose_name_plural = "alertes"
        ordering = ["event"]

    def __str__(self):
        return self.event

    def recipients(self) -> list[int] | None:
        return None if self.all_members else clean_ids(self.recipient_ids)

    def wants(self, outcome) -> bool:
        return isinstance(self.outcomes, list) and outcome in self.outcomes


class Dispatch(models.Model):
    """One notification: queued, sent, or why not (the module's docstring)."""

    class Kind(models.TextChoices):
        REMINDER = "reminder", "rappel"
        EVENT = "event", "alerte"
        TEST = "test", "essai"

    class Status(models.TextChoices):
        PENDING = "pending", "en attente"
        SENDING = "sending", "en cours"
        SENT = "sent", "envoyé"
        PARTIAL = "partial", "partiel"
        FAILED = "failed", "échec"
        NO_DEVICE = "no_device", "aucun appareil"
        SKIPPED = "skipped", "sauté"
        MISSED = "missed", "manqué"
        DISABLED = "disabled", "désactivé"

    kind = models.CharField("type", max_length=10, choices=Kind.choices)
    reminder_id = models.PositiveIntegerField("rappel", null=True, blank=True)
    event_rule_id = models.PositiveIntegerField("alerte", null=True, blank=True)
    rule_name = models.CharField("règle", max_length=120)
    event = models.CharField("événement", max_length=60, blank=True)
    outcome = models.CharField("résultat", max_length=30, blank=True)
    #: The reminder's instant (UTC), None for an event or a test.
    scheduled_for = models.DateTimeField("prévu pour", null=True, blank=True)
    dedupe_key = models.CharField(max_length=DEDUPE_KEY_MAX_LENGTH, unique=True)
    title = models.CharField("titre", max_length=TITLE_MAX_LENGTH)
    body = models.CharField("message", max_length=BODY_MAX_LENGTH, blank=True)
    #: The local path, as configured: SITE_URL is put in front when sent.
    target = models.CharField("page à ouvrir", max_length=TARGET_MAX_LENGTH, blank=True)
    ttl = models.PositiveIntegerField("durée de vie (s)")
    topic = models.CharField(max_length=32, blank=True)
    #: None: every member of the espace.
    recipient_ids = models.JSONField("destinataires", null=True, blank=True)
    status = models.CharField("état", max_length=12, choices=Status.choices, default=Status.PENDING)
    detail = models.CharField("détail", max_length=DETAIL_MAX_LENGTH, blank=True)
    devices = models.PositiveSmallIntegerField("appareils", default=0)
    delivered = models.PositiveSmallIntegerField("reçus", default=0)
    created_at = models.DateTimeField("créé le", default=timezone.now, db_index=True)
    #: When its sending began (the claim), then when it ended.
    sent_at = models.DateTimeField("envoyé le", null=True, blank=True)

    class Meta:
        verbose_name = "envoi"
        verbose_name_plural = "envois"
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.rule_name} - {self.get_status_display()}"


#: A dispatch the delivery thread has not finished with: never pruned.
ACTIVE_STATUSES = (Dispatch.Status.PENDING, Dispatch.Status.SENDING)


def clean_ids(values) -> list[int]:
    """The ints of a stored list of ids (a JSON value from a form or the
    admin): anything else left out, each once, in order."""
    found = []
    for value in values if isinstance(values, list) else []:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            continue
        if value not in found:
            found.append(value)
    return found


def reminder_key(reminder_pk: int, instant_utc) -> str:
    """One reminder instant, whoever ticks: two ticks (two processes) of the
    same minute insert it once."""
    return f"reminder:{reminder_pk}:{instant_utc.astimezone(UTC):%Y%m%dT%H%MZ}"


def event_key(rule_pk: int, content_key: str) -> str:
    """One event result per rule; a content key too long for the column is
    replaced by its hash (the same key always gives the same hash)."""
    key = f"event:{rule_pk}:{content_key}"
    if len(key) <= DEDUPE_KEY_MAX_LENGTH:
        return key
    return f"event:{rule_pk}:#{hashlib.sha256(str(content_key).encode('utf-8')).hexdigest()}"


def test_key() -> str:
    return f"test:{uuid.uuid4().hex}"


def clip(text, limit: int) -> str:
    """`text` cut to a column, with « … »."""
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def create_dispatch(**fields) -> Dispatch | None:
    """Insert one dispatch in a savepoint of its own; None when its
    `dedupe_key` is already there (already done: by an earlier tick, by
    another process, by the same event read twice). Never poisons a
    caller's transaction. The texts are cut to their columns."""
    for name, limit in (
        ("title", TITLE_MAX_LENGTH),
        ("body", BODY_MAX_LENGTH),
        ("detail", DETAIL_MAX_LENGTH),
        ("rule_name", 120),
    ):
        if name in fields:
            fields[name] = clip(fields[name], limit)
    try:
        with transaction.atomic():
            return Dispatch.objects.create(**fields)
    except IntegrityError:
        if Dispatch.objects.filter(dedupe_key=fields.get("dedupe_key")).exists():
            return None
        raise
