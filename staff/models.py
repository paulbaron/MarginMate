"""« Personnel »: who works here, their typical week, and the month they sign.

A timesheet (« fiche de temps ») is what the employer keeps of the hours an
employee worked, one page a month, signed by the employee. The owner used to
type it by hand from a typical week and correct the days that were not
typical. So the data is exactly that: a typical week on the `Employee`
(seven hour figures, 0 = not a working day), and a `Timesheet` per month
whose `TimesheetDay` rows say what really happened, day by day. A month
nobody saved yet is not stored at all - it is the typical week, worked out
when it is drawn (`staff.timesheet.planned_days`).

A saved month also keeps **its own copy of the typical week** (`Timesheet`
is a `TypicalWeek` too), taken when it is first saved. The sheet prints
« Semaine type : 36 h », what the week planned and the difference from it:
worked out from the employee's week of today, a month already signed
came out with another header and an overtime nobody worked once the
contract changed (review, 28/09).

Hours are `Decimal`, never float, for the same reason money is (CLAUDE.md
« Money is always Decimal »): a week of 7,5 + 7,5 + 8 has to add up to 23
and print 23, not 22,999999.
"""

import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

#: A day has 24 hours; a figure beyond that is a typo, never overtime.
MAX_DAY_HOURS = Decimal("24")

#: The typical week's fields, Monday first - the order of `date.weekday()`,
#: so `WEEKDAY_FIELDS[day.weekday()]` is that day's field.
WEEKDAY_FIELDS = (
    "monday_hours",
    "tuesday_hours",
    "wednesday_hours",
    "thursday_hours",
    "friday_hours",
    "saturday_hours",
    "sunday_hours",
)

# French messages: these reach the page through a ModelForm, and the
# validators' own English (« Ensure this value is… ») is not what a French
# screen should say.
HOURS_VALIDATORS = [
    MinValueValidator(Decimal("0"), message="Les heures ne peuvent pas être négatives."),
    MaxValueValidator(MAX_DAY_HOURS, message="Pas plus de 24 h dans une journée."),
]


def _hours_field(verbose_name):
    return models.DecimalField(
        verbose_name, max_digits=4, decimal_places=2, default=Decimal("0"), validators=HOURS_VALIDATORS
    )


def validate_first_of_month(value):
    """A timesheet IS a month, and the month is stored as its 1st: two rows
    for June (the 1st and the 15th) would be two sheets for one month."""
    if value is not None and value.day != 1:
        raise ValidationError("Une fiche de temps est rangée au 1er du mois.")


class Establishment(models.Model):
    """What the sheets print at the top: the establishment's name and
    address. One row (`current()`), blank until the owner fills it in - and
    a blank one prints nothing rather than a placeholder, since a sheet
    headed « Nom de l'établissement » is worse than one with no header."""

    name = models.CharField("nom de l'établissement", max_length=255, blank=True)
    # Lines as typed (street, then postcode and town): printed one under the
    # other, exactly as the owner wrote them.
    address = models.TextField("adresse", blank=True)

    class Meta:
        verbose_name = "établissement"

    def __str__(self):
        return self.name or "Établissement"

    #: The one row's key: `current()` makes it, and a page that must not
    #: write (the PDF, a GET) reads it with `.filter(pk=SINGLETON_PK).first()`.
    SINGLETON_PK = 1

    @classmethod
    def current(cls):
        establishment, _created = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return establishment

    @property
    def address_lines(self) -> list[str]:
        """The address's non-blank lines, stripped - what a header prints."""
        return [line.strip() for line in self.address.splitlines() if line.strip()]


class TypicalWeek(models.Model):
    """Seven hour figures, Monday first; 0 = not a working day. The
    employee's week (what a month nobody saved is drawn from) and, copied
    from it, the week each saved month was saved with."""

    monday_hours = _hours_field("lundi")
    tuesday_hours = _hours_field("mardi")
    wednesday_hours = _hours_field("mercredi")
    thursday_hours = _hours_field("jeudi")
    friday_hours = _hours_field("vendredi")
    saturday_hours = _hours_field("samedi")
    sunday_hours = _hours_field("dimanche")

    class Meta:
        abstract = True

    def typical_hours(self, weekday: int) -> Decimal:
        """The typical week's hours for `weekday` (0 = Monday, as
        `date.weekday()` counts). 0 means not a working day."""
        if not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise ValueError(f"Jour de la semaine inconnu : {weekday!r} (0 = lundi … 6 = dimanche).")
        value = getattr(self, WEEKDAY_FIELDS[weekday])
        # An unsaved instance holds what it was given (an int, a string);
        # the rest of the app adds these up, so it gets a Decimal.
        return Decimal(str(value)) if value is not None else Decimal("0")

    @property
    def typical_week(self) -> tuple[Decimal, ...]:
        """The seven days' hours, Monday first."""
        return tuple(self.typical_hours(weekday) for weekday in range(7))

    @property
    def weekly_hours(self) -> Decimal:
        return sum(self.typical_week, Decimal("0"))

    def week_values(self) -> dict[str, Decimal]:
        """The seven fields and their hours - to copy this week onto another."""
        return {name: self.typical_hours(weekday) for weekday, name in enumerate(WEEKDAY_FIELDS)}


class Employee(TypicalWeek):
    last_name = models.CharField("nom", max_length=100)
    first_name = models.CharField("prénom", max_length=100, blank=True)
    # An employee who left keeps their sheets (a signed timesheet is a record
    # the employer must keep): they are deactivated, never deleted - which
    # `Timesheet.employee`'s PROTECT enforces.
    is_active = models.BooleanField("actif", default=True)
    # Where the signing link, the one-time code and the signed copy go -
    # optional: without one (or without a mail server) the owner hands the
    # link and the code over himself (staff/signature_requests.py).
    email = models.EmailField("e-mail", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        verbose_name = "salarié"

    def __str__(self):
        return self.display_name

    @property
    def display_name(self) -> str:
        """« DUPONT Jeanne » - the last name upper-cased, as the sheets print it."""
        return f"{self.last_name.strip().upper()} {self.first_name.strip()}".strip()


class Timesheet(TypicalWeek):
    """One employee's month, once somebody saved it. Every day of the month
    has its row (`staff.timesheet.save_month` writes them all), and the
    typical week the month was saved with is copied onto it (the seven
    `…_hours` fields, written with the rows on the first save and again by
    « Revenir à la semaine type » only) - so a sheet printed later never
    depends on a typical week edited since: not its days, not its
    « Semaine type », not what it planned nor the difference from it."""

    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="timesheets")
    month = models.DateField("mois", validators=[validate_first_of_month])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-month"]
        verbose_name = "fiche de temps"
        verbose_name_plural = "fiches de temps"
        constraints = [models.UniqueConstraint(fields=["employee", "month"], name="unique_timesheet_per_month")]

    def __str__(self):
        return f"{self.employee} {self.month:%m/%Y}"

    def save(self, *args, **kwargs):
        # Validators only run in full_clean; code saving a sheet directly
        # must not be able to file June under the 15th either.
        validate_first_of_month(self.month)
        super().save(*args, **kwargs)


class TimesheetDay(models.Model):
    class Kind(models.TextChoices):
        WORK = "travail", "Travail"
        # A day off of the typical week: printed BLANK, like the owner's
        # own sheets print their Sundays and Mondays.
        REST = "repos", "Repos"
        # The absences. Their hours are 0, always: a half day off is
        # « Travail » with its hours and a note, so there is one rule and no
        # « 4 h on a leave day » to interpret.
        PAID_LEAVE = "conges", "Congés payés"
        COMPENSATORY_REST = "repos_comp", "Repos compensateur"
        PUBLIC_HOLIDAY = "ferie", "Férié chômé"
        SICK_LEAVE = "maladie", "Arrêt maladie"
        ABSENCE = "absence", "Absence"

    timesheet = models.ForeignKey(Timesheet, on_delete=models.CASCADE, related_name="days")
    date = models.DateField()
    hours = _hours_field("heures")
    kind = models.CharField("motif", max_length=12, choices=Kind.choices, default=Kind.WORK)
    note = models.CharField("note", max_length=255, blank=True)

    class Meta:
        ordering = ["date"]
        verbose_name = "jour"
        constraints = [
            models.UniqueConstraint(fields=["timesheet", "date"], name="unique_timesheet_day"),
            # Hours live on a « Travail » day and nowhere else: only those are
            # summed, so hours stored on a leave day would print on the sheet
            # without counting - a figure that lies.
            models.CheckConstraint(
                condition=models.Q(kind="travail") | models.Q(hours=0), name="timesheet_day_hours_only_when_worked"
            ),
            models.CheckConstraint(
                condition=models.Q(hours__gte=0) & models.Q(hours__lte=24), name="timesheet_day_hours_within_a_day"
            ),
        ]

    def __str__(self):
        return f"{self.date:%d/%m/%Y} {self.get_kind_display()} {self.hours}"

    @property
    def is_absence(self) -> bool:
        return self.kind in ABSENCE_KINDS

    def save(self, *args, **kwargs):
        # The server enforces it whatever was posted (the admin included):
        # a day that is not worked has no hours.
        if self.kind != self.Kind.WORK:
            self.hours = Decimal("0")
        super().save(*args, **kwargs)


#: The kinds that are an absence from a day the employee would have worked:
#: counted by kind in the month's summary, printed in the note column. Plain
#: strings, so a kind read from a post or a row is found in it.
ABSENCE_KINDS = frozenset(
    kind.value
    for kind in (
        TimesheetDay.Kind.PAID_LEAVE,
        TimesheetDay.Kind.COMPENSATORY_REST,
        TimesheetDay.Kind.PUBLIC_HOLIDAY,
        TimesheetDay.Kind.SICK_LEAVE,
        TimesheetDay.Kind.ABSENCE,
    )
)


# -- The monthly electronic signature ---------------------------------------------------------------------------
# What these rows mean, and the rules they carry, is in staff/signature_requests.py
# (the workflow) and staff/signing.py (the cryptography). The files - the frozen
# document, the drawn signature, the signed PDFs, the proof file - are NOT in the
# database: they live in settings.STAFF_PRIVATE_DIR/signatures/<uuid>/, and the
# row keeps their SHA-256.


class SignatureRequest(models.Model):
    """One version of a month sent for signature: the document frozen as it
    was sent (`document_sha256`, `month_snapshot`), the link that reaches the
    employee (only its hash is kept - `token_hash`), the one-time code, and
    what each signature produced. A month is corrected by cancelling or
    superseding its request and sending a new one, `version` + 1: nothing
    signed is ever rewritten."""

    class Status(models.TextChoices):
        PENDING = "en_attente", "En attente de la signature du salarié"
        EMPLOYEE_SIGNED = "signee_salarie", "Signée par le salarié, à contresigner"
        COMPLETE = "terminee", "Signée et contresignée"
        CANCELLED = "annulee", "Annulée"
        SUPERSEDED = "remplacee", "Remplacée par une nouvelle version"
        EXPIRED = "expiree", "Expirée sans signature"

    class Identification(models.TextChoices):
        CODE_BY_EMAIL = "code_email", "code à usage unique envoyé par e-mail à l'adresse du salarié"
        CODE_HANDED_OVER = (
            "code_remis",
            "code à usage unique affiché à l'employeur, qui l'a transmis au salarié par un autre canal que le lien",
        )

    #: The public ID of the document: printed on the stamp and on the proof file.
    uuid = models.UUIDField("identifiant du document", default=uuid.uuid4, unique=True, editable=False)
    timesheet = models.ForeignKey(Timesheet, on_delete=models.PROTECT, related_name="signature_requests")
    version = models.PositiveIntegerField("version")
    status = models.CharField("état", max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField("créée le", default=timezone.now)
    expires_at = models.DateTimeField("lien valable jusqu'au")
    # sha256 of the link's secret: the secret itself is never stored.
    token_hash = models.CharField(max_length=64, unique=True)
    # The month as it was frozen - what the employee's page shows, whatever
    # is edited afterwards (signature_requests.month_snapshot).
    month_snapshot = models.JSONField(default=dict)
    document_sha256 = models.CharField("empreinte du document figé", max_length=64)

    employee_signed_at = models.DateTimeField(null=True, blank=True)
    employee_pdf_sha256 = models.CharField(max_length=64, blank=True)
    signature_png_sha256 = models.CharField(max_length=64, blank=True)
    employee_timestamp_at = models.DateTimeField(null=True, blank=True)
    employee_timestamp_authority = models.CharField(max_length=255, blank=True)

    employer_signed_at = models.DateTimeField(null=True, blank=True)
    final_pdf_sha256 = models.CharField(max_length=64, blank=True)
    employer_timestamp_at = models.DateTimeField(null=True, blank=True)
    employer_timestamp_authority = models.CharField(max_length=255, blank=True)

    proof_sha256 = models.CharField("empreinte du dossier de preuve", max_length=64, blank=True)

    # How the employee was identified: the method of the code HE verified,
    # set by `check_code` - never by a code merely issued (`code_method`).
    identification = models.CharField(max_length=16, choices=Identification.choices, blank=True)
    # The employee's own words when he signs « avec des réserves ».
    reservation = models.TextField("réserves", blank=True)
    # Which certification text he accepted (signature_requests.STATEMENTS).
    statement_version = models.CharField(max_length=16, blank=True)
    cancelled_reason = models.TextField(blank=True)

    # The one-time code: an HMAC of it (SECRET_KEY + this uuid), never the code.
    code_hash = models.CharField(max_length=64, blank=True)
    code_sent_at = models.DateTimeField(null=True, blank=True)
    code_attempts = models.PositiveSmallIntegerField(default=0)
    # The method of the code waiting to be typed (by e-mail, or handed over
    # by the employer): it becomes `identification` once that code is typed.
    code_method = models.CharField(max_length=16, choices=Identification.choices, blank=True)
    code_verified_at = models.DateTimeField(null=True, blank=True)

    # The newest event's hash: what makes a chain cut short at its end
    # visible (signature_requests.verify_event_chain).
    last_event_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "demande de signature"
        verbose_name_plural = "demandes de signature"
        constraints = [
            models.UniqueConstraint(fields=["timesheet", "version"], name="unique_signature_request_version"),
            # A month has one request that holds it at a time - waiting, signed
            # by the employee, or finished; a correction cancels or supersedes it
            # before the next version is sent.
            models.UniqueConstraint(
                fields=["timesheet"],
                condition=models.Q(status__in=["en_attente", "signee_salarie", "terminee"]),
                name="one_open_signature_request_per_month",
            ),
        ]

    def __str__(self):
        return f"{self.timesheet} v{self.version} ({self.get_status_display()})"

    #: The states that keep the month read-only in the editor.
    LOCKING_STATUSES = frozenset({Status.PENDING.value, Status.EMPLOYEE_SIGNED.value, Status.COMPLETE.value})

    @property
    def locks_month(self) -> bool:
        return self.status in self.LOCKING_STATUSES

    @property
    def document_id(self) -> str:
        return str(self.uuid)


class SignatureEvent(models.Model):
    """What happened to a request, in order: an append-only log whose every
    row carries the hash of the one before it and a hash of its own content
    (`signature_requests.event_hash`), so an event edited or removed after
    the fact no longer adds up (`verify_event_chain`). Never updated: `save`
    refuses a row that already exists."""

    class Kind(models.TextChoices):
        CREATED = "created", "Demande créée, document figé"
        LINK_SENT = "link_sent", "Lien envoyé par e-mail au salarié"
        LINK_RENEWED = "link_renewed", "Nouveau lien créé (le précédent ne fonctionne plus)"
        LINK_OPENED = "link_opened", "Lien ouvert"
        CODE_SENT = "code_sent", "Code à usage unique envoyé par e-mail"
        CODE_GIVEN = "code_given", "Code à usage unique affiché à l'employeur, à transmettre au salarié"
        CODE_FAILED = "code_failed", "Code erroné saisi"
        CODE_VERIFIED = "code_verified", "Code vérifié : identification faite"
        EMPLOYEE_SIGNED = "employee_signed", "Signé par le salarié"
        TIMESTAMP_FAILED = "timestamp_failed", "Horodatage impossible : signature refusée, rien n'a été enregistré"
        COUNTERSIGNED = "countersigned", "Contresigné par l'employeur"
        COPY_SENT = "copy_sent", "Copie signée envoyée par e-mail au salarié"
        MAIL_FAILED = "mail_failed", "Échec de l'envoi d'un e-mail"
        CANCELLED = "cancelled", "Demande annulée"
        SUPERSEDED = "superseded", "Remplacée par une nouvelle version du mois"
        EXPIRED = "expired", "Lien expiré sans signature"
        DOWNLOADED = "downloaded", "Document téléchargé"
        VERIFIED = "verified", "Signatures vérifiées"

    request = models.ForeignKey(SignatureRequest, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(default=timezone.now)
    kind = models.CharField(max_length=24, choices=Kind.choices)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    previous_hash = models.CharField(max_length=64)
    hash = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "événement de signature"
        verbose_name_plural = "événements de signature"

    def __str__(self):
        return f"{self.at:%d/%m/%Y %H:%M} {self.get_kind_display()}"

    def save(self, *args, **kwargs):
        if self.pk is not None and SignatureEvent.objects.filter(pk=self.pk).exists():
            raise ValueError("Un événement de signature ne se modifie pas : le journal est en ajout seul.")
        super().save(*args, **kwargs)
