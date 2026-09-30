"""« Consignes »: the empties handed back to the delivery driver, counted
and photographed here, then compared with the bon he sends.

Three kinds of rows, and nothing stored that can be worked out:

- **What was handed back** - a `Pickup` (« reprise »): a day, who took the
  empties (« Repris par », a supplier), a note, a `PickupCount` per type of
  consigne (« Fûts 15 ») and up to `MAX_PHOTOS` photos taken before the
  lorry left (`PickupPhoto`, re-encoded on the server: the phone's original
  is never kept - returnables/photos.py).
- **What the seller says he took** - a `Slip` (« bon », the PDF his driver
  e-mails or somebody drops on the page) and its `SlipLine`s, read by the
  motifs of a `SlipFormat` (« format de bon »: user regexes, one set per
  seller - returnables/reading.py). The reading is stored on the bon and
  rewritten by every (re)reading; a line's TYPE is not stored: it is worked
  out when the line is drawn, from the `ReturnableType` motifs, so editing a
  motif reclassifies every line at once.
- **The vocabulary** - a `ReturnableType` (« type de consigne »: Fûts,
  Caisses verre, Bouteilles CO2…), seeded by migration 0002 in every
  database, the test one and every espace included.

The comparison of the two sides (returnables/comparison.py) and the check
against the seller's invoices (returnables/invoice_check.py) are computed
when a page is drawn, never stored: nothing about a bon is written on an
invoice.

Money is `Decimal`, never float (CLAUDE.md « Money is always Decimal »).
Every file is on the default storage - the espace's own media, served at
/fichiers/ - under consignes/, with `max_length` 100 (what « Données »
accepts), and is deleted ON COMMIT with its row (`delete_with_files`): a
rolled-back deletion must not have thrown the photo away already. No
foreign key points at a central model (auth, accounts…): in multi mode
they live in another SQLite file (accounts/tests/test_router.py).
"""

import secrets

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction

#: At most this many photos per reprise: the page refuses the eleventh, the
#: server keeps the first ten and says so.
MAX_PHOTOS = 10

#: A count is a whole number from 1 to this; zero is no row at all.
MAX_COUNT = 9_999

# French: Django's own (« Ensure this value is… ») is not what a French
# screen should say. A form leaves a blank or 0 count out rather than
# storing it, so what reaches these is 1 or more.
COUNT_VALIDATORS = [
    MinValueValidator(1, message="Un nombre repris va de 1 à 9 999 : zéro, c'est ne pas l'enregistrer."),
    MaxValueValidator(MAX_COUNT, message="Un nombre repris va de 1 à 9 999."),
]

#: The file folders, all under consignes/ - the prefix « Données » accepts
#: for this section. Short on purpose: a stored name is at most 100
#: characters, the storage's random suffix included.
SLIP_FOLDER = "consignes/bons/%Y/%m/"
PHOTO_FOLDER = "consignes/photos/%Y/%m/"


def new_reference() -> str:
    """A reprise's natural key: 16 hex characters, random, never shown.
    What « Données » names a reprise by - its date and its counts can be
    edited, so neither can be its key. A module function, not a lambda, so
    the migration can name it."""
    return secrets.token_hex(8)


class ReturnableType(models.Model):
    """A kind of returnable: « Fûts », « Caisses verre », « Bouteilles CO2 ».
    Its name is shown EXACTLY as typed - never lower-cased or singularised -
    and counts are written name then number (« Fûts 15 »).

    `slip_patterns` holds the motifs that recognise a bon's line as this
    type, one per line, searched case-insensitively in the line's
    designation. Classification uses EVERY type that has motifs, active or
    not, in (position, pk) order, first match wins; `is_active` only takes
    the type off the new-reprise form. A type that counts were made with
    cannot be deleted (PROTECT): it is deactivated instead."""

    name = models.CharField("nom", max_length=60, unique=True)
    # The form's order - and the FIRST active type gets the big stepper (the
    # kegs: almost every reprise is mostly kegs).
    position = models.PositiveSmallIntegerField("ordre", default=0)
    is_active = models.BooleanField("actif", default=True)
    slip_patterns = models.TextField(
        "motifs des bons",
        blank=True,
        help_text="Un motif par ligne : une ligne du bon dont la désignation contient l'un d'eux est de ce type.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "type de consigne"
        verbose_name_plural = "types de consigne"

    def __str__(self):
        return self.name


class SlipFormat(models.Model):
    """« Format de bon »: how one seller's bon is read, and where it comes
    from. Every rule is a motif (a regex, checked by returnables/patterns.py
    before it is ever compiled - never trusted as stored), and a « one per
    line » field holds several motifs tried in order.

    The mail motifs are optional: a blank sender means the format is never
    fetched from the mailbox, only dropped on the page. A format with bons
    cannot be deleted (PROTECT): it is deactivated instead."""

    name = models.CharField("nom", max_length=80, unique=True)
    supplier = models.ForeignKey(
        "invoices.Supplier",
        on_delete=models.PROTECT,
        related_name="slip_formats",
        verbose_name="fournisseur",
    )
    is_active = models.BooleanField("actif", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # -- Récupération par mail (blank sender = upload only) --
    sender_pattern = models.CharField("motif de l'expéditeur", max_length=300, blank=True)
    subject_pattern = models.CharField("motif de l'objet", max_length=300, blank=True)
    attachment_pattern = models.CharField("motif de la pièce jointe", max_length=200, default=r"(?i)\.pdf$")

    # -- Les lignes --
    section_start = models.CharField("début de la partie des consignes", max_length=300, blank=True)
    section_end = models.CharField("fin de la partie des consignes", max_length=300, blank=True)
    line_pattern = models.CharField("motif de ligne", max_length=500)
    date_patterns = models.TextField("motifs de la date de livraison")

    # -- Réglages avancés --
    printed_patterns = models.TextField("motifs de la date d'impression", blank=True)
    number_patterns = models.TextField("motifs du numéro", blank=True)
    reference_patterns = models.TextField("motifs des références", blank=True)
    replaces_pattern = models.CharField("motif « annule et remplace »", max_length=300, blank=True)
    total_patterns = models.TextField("motifs du total", blank=True)
    remarks_start = models.CharField("début des remarques", max_length=300, blank=True)
    remarks_end = models.CharField("fin des remarques", max_length=300, blank=True)

    class Meta:
        ordering = ["name", "id"]
        verbose_name = "format de bon"
        verbose_name_plural = "formats de bons"

    def __str__(self):
        return self.name


class Slip(models.Model):
    """A bon: the PDF as received (`file`, `sha256` - the same bytes twice
    are one bon) and the text its reading used (`text`, what « Relire »
    reads again). The fields under « Reading » are what the format's motifs
    found, rewritten by every (re)reading (returnables/slips.py); `checks`
    says what the reading verified (`[{"label", "passed", "detail"}]`), and
    `read_error` why it could read nothing (a bon whose reading failed is
    kept, with its file and text, so « Relire » can fix it)."""

    class Origin(models.TextChoices):
        MAIL = "MAIL", "Reçu par mail"
        UPLOAD = "UPLOAD", "Déposé à la main"

    format = models.ForeignKey(SlipFormat, on_delete=models.PROTECT, related_name="slips", verbose_name="format")
    origin = models.CharField("provenance", max_length=10, choices=Origin.choices)
    file = models.FileField("fichier", upload_to=SLIP_FOLDER, max_length=100)
    sha256 = models.CharField("empreinte SHA-256", max_length=64, unique=True)
    original_name = models.CharField("nom du fichier reçu", max_length=200, blank=True)
    mail_sender = models.CharField("expéditeur du mail", max_length=300, blank=True)
    mail_subject = models.CharField("objet du mail", max_length=300, blank=True)
    mail_date = models.DateField("date du mail", null=True, blank=True)
    received_at = models.DateTimeField("reçu le", auto_now_add=True)
    text = models.TextField("texte lu", blank=True)

    # -- Reading (rewritten by every reading) --
    # The BL's date: the day the goods came and the empties went, which a
    # re-send printed the next day does not change.
    delivery_date = models.DateField("date de livraison", null=True, blank=True)
    printed_at = models.DateTimeField("imprimé le", null=True, blank=True)
    number = models.CharField("numéro", max_length=40, blank=True)
    # The BL numbers (at most 20, each at most 40 characters): what the bon
    # shares with the seller's invoice, and with the bon it replaces.
    references = models.JSONField("références", default=list, blank=True)
    replaces = models.BooleanField("annule et remplace", default=False)
    printed_total = models.DecimalField("total imprimé", max_digits=12, decimal_places=2, null=True, blank=True)
    remarks = models.TextField("remarques du bon", blank=True)
    checks = models.JSONField("contrôles", default=list, blank=True)
    read_error = models.CharField("erreur de lecture", max_length=300, blank=True)
    read_at = models.DateTimeField("lu le", null=True, blank=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        verbose_name = "bon"
        verbose_name_plural = "bons"

    def __str__(self):
        label = f"Bon n° {self.number}" if self.number else "Bon sans numéro"
        if self.delivery_date:
            label += f" du {self.delivery_date:%d/%m/%Y}"
        return label


class SlipLine(models.Model):
    """One line of a bon's consignes part, as printed: the designation (a
    ticket may cut it - UBA's cuts it to 20 characters), the quantity signed
    as printed (|q| ≤ 99 999), the unit price and the amount when read. Its
    type is NOT stored: returnables.comparison.classify works it out when
    it is drawn, so a motif edited reclassifies it at once."""

    slip = models.ForeignKey(Slip, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveSmallIntegerField("rang")
    designation = models.CharField("désignation", max_length=200)
    quantity = models.IntegerField("quantité")
    unit_amount = models.DecimalField("prix unitaire", max_digits=12, decimal_places=4, null=True, blank=True)
    amount = models.DecimalField("montant", max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "ligne de bon"
        verbose_name_plural = "lignes de bons"

    def __str__(self):
        return f"{self.designation} × {self.quantity}"


class Pickup(models.Model):
    """« Reprise »: the empties taken back one day, counted and photographed
    before the lorry left. All the reprises of one supplier on one day are
    compared as one with that day's bons (returnables/comparison.py)."""

    reference = models.CharField(
        "référence", max_length=16, unique=True, default=new_reference, editable=False
    )
    date = models.DateField("date")
    supplier = models.ForeignKey(
        "invoices.Supplier",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pickups",
        verbose_name="repris par",
    )
    note = models.TextField("note", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-id"]
        verbose_name = "reprise"
        verbose_name_plural = "reprises"

    def __str__(self):
        return f"Reprise du {self.date:%d/%m/%Y}"


class PickupCount(models.Model):
    """How many of one type a reprise handed back. Zero is no row: a count
    is 1 to `MAX_COUNT`, which the database enforces too."""

    pickup = models.ForeignKey(Pickup, on_delete=models.CASCADE, related_name="counts")
    returnable_type = models.ForeignKey(
        ReturnableType, on_delete=models.PROTECT, related_name="counts", verbose_name="type de consigne"
    )
    quantity = models.PositiveIntegerField("nombre", validators=COUNT_VALIDATORS)

    class Meta:
        ordering = ["returnable_type__position", "returnable_type_id"]
        verbose_name = "nombre repris"
        verbose_name_plural = "nombres repris"
        constraints = [
            models.UniqueConstraint(fields=["pickup", "returnable_type"], name="unique_pickup_count_per_type"),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1) & models.Q(quantity__lte=MAX_COUNT),
                name="pickup_count_from_1_to_9999",
            ),
        ]

    def __str__(self):
        return f"{self.returnable_type} {self.quantity}"


class PickupPhoto(models.Model):
    """A photo of the empties, re-encoded on the server (returnables/
    photos.py: upright, at most 2000 px, no EXIF - so no GPS), with its
    thumbnail beside it in the same folder. `taken_at` is the phone's own
    moment when its EXIF said one, else None (the page then says when it was
    sent, `created_at`)."""

    pickup = models.ForeignKey(Pickup, on_delete=models.CASCADE, related_name="photos")
    image = models.FileField("photo", upload_to=PHOTO_FOLDER, max_length=100)
    thumb = models.FileField("vignette", upload_to=PHOTO_FOLDER, max_length=100)
    taken_at = models.DateTimeField("prise le", null=True, blank=True)
    width = models.PositiveIntegerField("largeur")
    height = models.PositiveIntegerField("hauteur")
    created_at = models.DateTimeField("envoyée le", auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "photo de reprise"
        verbose_name_plural = "photos de reprise"

    def __str__(self):
        return self.image.name or "Photo"


# -- Deleting with the files -------------------------------------------------------------------------------------
# A row's files are not deleted with the row: the storage knows nothing of the
# database. So every deletion of a reprise, a photo or a bon goes through
# `delete_with_files`, which collects the (storage, name) pairs BEFORE the
# delete and removes them only once the transaction commits - rolled back, the
# rows and their files both stay (invoices/deletion.py, the same rule).


def files_of(obj) -> list[tuple]:
    """The (storage, name) of every file `obj` holds - a reprise's photos
    and thumbnails, a photo's two files, a bon's PDF - that deleting `obj`
    would leave behind. [] for a row with no file (a type, a format, a
    count, a line)."""
    if isinstance(obj, Pickup):
        return [pair for photo in obj.photos.all() for pair in files_of(photo)]
    if isinstance(obj, PickupPhoto):
        return [(field.storage, field.name) for field in (obj.image, obj.thumb) if field]
    if isinstance(obj, Slip):
        return [(obj.file.storage, obj.file.name)] if obj.file else []
    return []


def delete_files(files) -> None:
    """Delete each (storage, name) pair; one that is already gone, or held
    by another program on Windows, is left: the row is deleted either way,
    and a stray file costs nothing."""
    for storage, name in files:
        try:
            storage.delete(name)
        except OSError:
            continue


def delete_with_files(obj):
    """Delete `obj` (a reprise, a photo, a bon - or any row of this app) and,
    once the transaction commits, the files it held. Returns what
    `Model.delete()` returns. Raises what it raises too: a type or a format
    still in use is a `ProtectedError`, which the page turns into
    « … : désactivez-le plutôt »."""
    with transaction.atomic():
        files = files_of(obj)
        result = obj.delete()
        transaction.on_commit(lambda: delete_files(files))
    return result
