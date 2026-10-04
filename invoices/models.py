import re
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Value
from django.db.models.functions import Concat
from django.utils import timezone

from common import JobLogMixin, group_thousands

#: Which attachment of an e-mail is the invoice, by default. Since the
#: electronic invoicing reform an invoice arrives as a Factur-X PDF **or as
#: the XML on its own** - a « plateforme agréée », or a supplier, may simply
#: forward it - and both are the legal invoice. A source still filtering on
#: `\.pdf$` alone would leave its supplier's invoices in the mailbox without
#: a word, which is the silent loss this codebase exists to avoid. A pattern
#: someone has tuned by hand is theirs and is left alone
#: (migration 0032).
INVOICE_ATTACHMENT_PATTERN = r"(?i)\.(pdf|xml)$"


class Supplier(models.Model):
    """A vendor invoices come from. ``parser_key`` points at an entry in the
    parser registry (invoices/parsers/registry.py); blank, its PDFs are filed
    empty to be typed in - and its tickets read like any shop's (see
    parsers.is_ticket_shop).
    """

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=255)
    parser_key = models.CharField(max_length=32, blank=True)
    is_scrapable = models.BooleanField(
        default=False, help_text="Si « Récupérer les nouvelles factures » sait aller les chercher tout seul."
    )
    # What a shop no reader was configured for prints at the top of its
    # tickets: an import finds it there (receipts.detect_parser), before the
    # configured shops - a person said so.
    ticket_header = models.CharField(
        "texte d'en-tête des tickets",
        max_length=100,
        blank=True,
        help_text="Imprimé en haut de ses tickets (nom, rue…) : un ticket qui le porte est rangé chez ce fournisseur.",
    )
    # What its documents print that names it whatever their layout - SIREN,
    # phone, web site (invoices/identifiers.py) - learned from the ones a
    # person filed or checked under it (receipts.learn_identifiers). Found on
    # a document no header was recognised on, and on no other supplier's
    # list, it files the document here.
    ticket_identifiers = models.JSONField("identifiants lus sur ses documents", default=list, blank=True)
    # A supplier whose documents are charges, not goods: a phone
    # subscription, the rent, the water. There is no product behind a rent,
    # so its documents are filed as one line per VAT rate (importing.
    # expense_lines) on a product of its own that never reaches the stock
    # pages - and what they cost is shown on "Produits" in a fold apart.
    expenses_only = models.BooleanField(
        "factures de charges",
        default=False,
        help_text="Abonnement, loyer, eau… : une ligne par taux de TVA, aucun produit à classer.",
    )
    # A site that protects itself (Metro's firewall): when AdminMate last
    # signed in there - noted before the password is sent, so a run that
    # dies still counts - and until when it leaves the site alone after a
    # refusal (scrapers/metro.metro_pause). Kept here, not read from the
    # gathers' history: that history was deleted once, by a development
    # session, and every script calling the scraper must obey it too.
    scrape_last_login_at = models.DateTimeField(null=True, blank=True)
    scrape_last_block_at = models.DateTimeField(null=True, blank=True)
    scrape_paused_until = models.DateTimeField(null=True, blank=True)
    scrape_pause_reason = models.TextField(blank=True)
    # Figures a person set aside on its page ("Retirer"): never learned
    # again - UBA relearns at every gather, through its own reader.
    refused_identifiers = models.JSONField("identifiants écartés à la main", default=list, blank=True)
    # Figures a person TYPED on its page rather than letting them be learned:
    # a subset of `ticket_identifiers`, and the whole of what tells the two
    # apart. It is an assertion - « ce n° SIREN est le sien », said by
    # somebody who has the invoice in front of them - so `still_naming`
    # never takes one back: learning may only forget what it learned itself.
    # Without this, a SIREN typed for a supplier whose stored documents do
    # not print it yet was dropped again at the very next import, under a
    # reason about documents the person never claimed it was on.
    typed_identifiers = models.JSONField("identifiants saisis à la main", default=list, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SupplierChange(models.Model):
    """What changed in what recognises a supplier - its name, its header, the
    figures it learned, an invoice type moved to or from it - when, why, and
    from which document; shown on its page, and undone from there
    (supplier_changes).

    On 18/09 the figures a supplier had learned went without a word, as a
    side effect of a correction validated on one of its documents: nothing
    said so, nothing kept it. A change nobody asked for is `needs_review`
    until someone has seen it."""

    class Kind(models.TextChoices):
        CREATED = "CREATED", "Création"
        RENAMED = "RENAMED", "Nom"
        HEADER = "HEADER", "En-tête"
        IDENTIFIERS = "IDENTIFIERS", "Identifiants"
        CHARGES = "CHARGES", "Nature"
        TYPES = "TYPES", "Types de factures"
        FIRST_DOCUMENT = "FIRST_DOCUMENT", "Premier document"

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="changes")
    # The other side of a type moved (TYPES): the supplier it came from or went to.
    other_supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    summary = models.TextField()
    cause = models.CharField(max_length=255, blank=True)
    invoice = models.ForeignKey("Invoice", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    by_person = models.BooleanField(default=False)
    needs_review = models.BooleanField(default=False)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Ties the two sides of one type moved (TYPES) together: undone as one.
    operation = models.UUIDField(null=True, blank=True, db_index=True)
    # Before and after, and what moved: what an undo needs.
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    undone_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.supplier} · {self.get_kind_display()} · {self.summary[:60]}"


class InvoiceType(models.Model):
    """One recognizable "kind" of invoice this app knows how to gather and
    parse - e.g. "UBA - Factures". Separate from Supplier because a single
    supplier could in principle send more than one distinguishable invoice
    format (each needing its own matching rule and/or parser), and because
    the matching rule itself lives in a separate, source-kind-specific
    model (see EmailInvoiceSource) - a future website-based source would
    need an unrelated shape (login/selectors, not regex patterns).
    """

    class SourceKind(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        WEBSITE = "WEBSITE", "Site web"  # see WebsiteInvoiceSource

    name = models.CharField(max_length=255)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="invoice_types")
    # Same convention as Supplier.parser_key (see its docstring) - kept
    # separate rather than reusing Supplier.parser_key so one supplier can
    # have two invoice types needing two different parsers.
    parser_key = models.CharField(max_length=32, blank=True)
    source_kind = models.CharField(max_length=10, choices=SourceKind.choices, default=SourceKind.EMAIL)
    is_active = models.BooleanField(
        default=True, help_text="Inclure ce type dans « Récupérer les nouvelles factures »."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class EmailInvoiceSource(models.Model):
    """How to recognize an InvoiceType's emails in the shared invoice
    mailbox (see settings.INVOICE_EMAIL_ADDRESS) and which attachment to
    treat as the invoice. All patterns are real regexes (not IMAP's own
    crude substring search - see invoices/scrapers/generic_email.py for
    why), tested against the From header, the Subject, the decoded text
    body, and each attachment's filename respectively. subject_pattern and
    body_pattern blank means "match anything"; sender_pattern is required
    since matching every email in the inbox would defeat the point.
    """

    invoice_type = models.OneToOneField(InvoiceType, on_delete=models.CASCADE, related_name="email_source")
    sender_pattern = models.CharField(max_length=500, help_text="Expression régulière testée sur l'expéditeur.")
    subject_pattern = models.CharField(
        max_length=500, blank=True, help_text="Expression régulière testée sur l'objet (vide = tous)."
    )
    body_pattern = models.CharField(
        max_length=500, blank=True, help_text="Expression régulière testée sur le contenu (vide = tous)."
    )
    attachment_pattern = models.CharField(
        max_length=200,
        blank=True,
        default=INVOICE_ATTACHMENT_PATTERN,
        help_text="Expression régulière testée sur le nom de la pièce jointe (PDF ou XML : une facture électronique peut arriver seule).",
    )

    def __str__(self):
        return f"Source email de {self.invoice_type}"

    def clean(self):
        # The motif guard, as the gather will compile them (security audit
        # 04/10/2026): a bare re.compile accepted a pattern whose matching
        # hangs on one email anybody can send.
        from returnables.patterns import PatternError, check_invoice_mail_pattern

        errors = {}
        for field_name in ("sender_pattern", "subject_pattern", "body_pattern", "attachment_pattern"):
            value = getattr(self, field_name)
            if not value:
                continue
            try:
                check_invoice_mail_pattern(value, field_label="Expression régulière invalide")
            except PatternError as exc:
                errors[field_name] = exc.message
        if errors:
            raise ValidationError(errors)


ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: The .env variables the application reads for itself (config/settings.py,
#: invoices/apps.py), by family so that one added to a family later is
#: covered: Metro's sign-in, the mailbox, the till, the AI, Django's own. A
#: gather types what a portal's variables hold into the portal's page, so a
#: portal naming one of these would hand Metro's password to any site - and
#: sign in to Metro outside its firewall's pause. One list, for the source
#: form and the « Données » import (transfer/sections/sources.py); a test
#: holds it against every name settings.py reads.
APP_ENV_PREFIXES = (
    "DJANGO_",
    "METRO_",
    "UBA_EMAIL_",
    "INVOICE_EMAIL_",
    "INVOICE_IMAP_",
    "LADDITION_",
    "ANTHROPIC_",
    "SCRAPER_",
    "PRODUCT_FUZZY_",
    "RUN_MAIN",
    # The timesheet signatures (staff/signing.py): the key's passphrase, the
    # private folder, the site's address - and the mail server's password.
    "MARGINMATE_",
    "EMAIL_",
    "DEFAULT_FROM_EMAIL",
)
APP_ENV_REFUSED = (
    "« {name} » est une variable de l'application elle-même (Metro, la boîte mail, la caisse, l'IA) : "
    "jamais celle d'un portail"
)


def portal_host(url) -> str:
    """The host a customer portal's password is bound to: its login page's
    host, lower-case - "" for an address that is not https (a password is
    never sent to one). The « Identifiants » page records it with the
    password typed (accounts/vault.py `bindings`), and the scraper types that
    password on that host's site only (scrapers/website.py)."""
    from urllib.parse import urlsplit

    from django.conf import settings

    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return ""
    if not host:
        return ""
    if host in getattr(settings, "PORTAL_PLAIN_HTTP_HOSTS", ()):
        # The test settings' local portal, on whatever port the test chose:
        # no production setting names one.
        return host if parts.scheme in ("http", "https") else ""
    if parts.scheme == "https" and port in (None, 443):
        # Another port is another server: https://site:8443 is not the site.
        return host
    return ""


def app_env_name(name) -> bool:
    """Whether the application reads this .env variable for itself: never
    one a portal may type into its page."""
    return isinstance(name, str) and name.startswith(APP_ENV_PREFIXES)


#: A portal's addresses (WebsiteInvoiceSource.clean): an address without a
#: `portal_host` is one no stored password is ever bound to, so a source
#: naming one could never sign in - or would sign in in clear.
LOGIN_URL_NOT_HTTPS = "Une adresse en https:// : un mot de passe n'est jamais tapé sur une page qui n'est pas chiffrée."
INVOICES_URL_NOT_HTTPS = (
    "Une adresse en https:// : une fois connecté, le navigateur ne va jamais sur une page qui n'est pas chiffrée."
)
SAME_NAME_TWICE = (
    "Un autre nom que celui de l'identifiant : sous un même nom, le mot de passe serait affiché comme un identifiant."
)


class WebsiteInvoiceSource(models.Model):
    """How to fetch an InvoiceType's invoices from a supplier's customer
    portal - the rent's, the water's, the phone's - with no code of its own
    (invoices/scrapers/website.py reads these settings).

    Credentials are never stored here, only the NAMES of the .env variables
    holding them (FREEBOX_LOGIN / FREEBOX_PASSWORD): the database is copied,
    backed up and shown on screen, a .env file is not. Every CSS selector is
    optional - left blank, the scraper finds the login form, the link to the
    invoices and the download links itself; one is only needed where a
    site's page defeats that.
    """

    invoice_type = models.OneToOneField(InvoiceType, on_delete=models.CASCADE, related_name="website_source")
    login_url = models.URLField(max_length=500, help_text="La page où l'on se connecte.")
    username_env = models.CharField(
        max_length=64, help_text="Nom de la variable du fichier .env qui contient l'identifiant (ex. FREEBOX_LOGIN)."
    )
    password_env = models.CharField(
        max_length=64,
        help_text="Nom de la variable du fichier .env qui contient le mot de passe (ex. FREEBOX_PASSWORD).",
    )
    invoices_url = models.URLField(
        max_length=500,
        blank=True,
        help_text="La page qui liste les factures, une fois connecté. Vide : le lien « Factures » de la page est suivi.",
    )
    navigation = models.TextField(
        blank=True,
        help_text="Liens à suivre dans l'ordre après la connexion, un texte par ligne (ex. « Conso et factures »). "
        "Vide : le premier lien qui parle de factures.",
    )
    username_selector = models.CharField(
        max_length=300, blank=True, help_text="Sélecteur CSS du champ identifiant (vide : trouvé seul)."
    )
    password_selector = models.CharField(
        max_length=300, blank=True, help_text="Sélecteur CSS du champ mot de passe (vide : trouvé seul)."
    )
    submit_selector = models.CharField(
        max_length=300, blank=True, help_text="Sélecteur CSS du bouton de connexion (vide : trouvé seul)."
    )
    link_selector = models.CharField(
        max_length=300,
        blank=True,
        help_text="Sélecteur CSS des liens ou boutons qui téléchargent une facture (vide : ceux qui mènent à un PDF "
        "ou disent « Télécharger »).",
    )
    next_selector = models.CharField(
        max_length=300,
        blank=True,
        help_text="Sélecteur CSS du bouton « page suivante » (vide : « Suivant », « Voir plus »… s'il y en a un).",
    )
    show_browser = models.BooleanField(
        default=False,
        help_text="Ouvrir la fenêtre du navigateur : pour un site qui demande un code reçu par SMS ou un captcha, "
        "à saisir vous-même pendant la récupération.",
    )

    def __str__(self):
        return f"Site web de {self.invoice_type}"

    def clean(self):
        """The rules the source form and the « Données » import both apply
        (transfer/sections/sources.py runs full_clean): the gather types
        what « Identifiants » holds under these two names into the page at
        `login_url`, so both the page and the names decide where a stored
        password goes."""
        errors = {}
        for field_name in ("username_env", "password_env"):
            value = getattr(self, field_name)
            if value and not ENV_NAME_RE.match(value):
                errors[field_name] = (
                    "Le nom d'une variable du fichier .env : majuscules, chiffres et _ (ex. FREEBOX_LOGIN) - "
                    "jamais l'identifiant ou le mot de passe lui-même."
                )
            elif app_env_name(value):
                errors[field_name] = APP_ENV_REFUSED.format(name=value)
        # Blank is the field's own « required » error, said already.
        if self.login_url and not portal_host(self.login_url):
            errors["login_url"] = LOGIN_URL_NOT_HTTPS
        if self.invoices_url and not portal_host(self.invoices_url):
            errors["invoices_url"] = INVOICES_URL_NOT_HTTPS
        if "username_env" not in errors and "password_env" not in errors:
            if self.username_env and self.username_env == self.password_env:
                errors["password_env"] = SAME_NAME_TWICE
            else:
                errors.update(self._names_taken())
        if errors:
            raise ValidationError(errors)

    def _names_taken(self) -> dict[str, str]:
        """This source's names, held against every other portal's (by field).

        - A name that is a password elsewhere is never a login here, nor the
          reverse: « Identifiants » shows a login in clear, and one name
          holds one value.
        - A name another site's source uses is refused: the store binds a
          password to the site it was typed for, and two sites sharing one
          name would have one site's password typed into the other's page
          (or the owner told to type it again for each in turn). One site's
          sources share their names - one account, typed once.

        Every other row counts, a paused source's or a mailbox source's
        unused portal row included: switched back on, it would sign in with
        those names again. This source itself - by its row and by its type,
        the row an import is about to rewrite - never counts. Without a site
        of its own (an address refused, or one the form could not read) the
        names are not held against other sites: the address is what to
        correct first, and nothing is saved meanwhile."""
        names = [name for name in (self.username_env, self.password_env) if name]
        if not names:
            return {}
        others = WebsiteInvoiceSource.objects.filter(
            Q(username_env__in=names) | Q(password_env__in=names)
        ).select_related("invoice_type")
        if self.pk is not None:
            others = others.exclude(pk=self.pk)
        if self.invoice_type_id is not None:
            others = others.exclude(invoice_type_id=self.invoice_type_id)
        host = portal_host(self.login_url)
        errors: dict[str, str] = {}
        for other in others.order_by("invoice_type__name", "pk"):
            named = f"« {other.invoice_type.name} »"
            if other.password_env == self.username_env:
                errors.setdefault(
                    "username_env",
                    f"Ce nom est celui du mot de passe de {named} : un identifiant s'affiche en clair sur la page "
                    "Identifiants, un mot de passe jamais.",
                )
            if other.username_env == self.password_env:
                errors.setdefault(
                    "password_env",
                    f"Ce nom est celui de l'identifiant de {named} : un mot de passe ne se range pas sous le nom "
                    "d'un identifiant, affiché en clair sur la page Identifiants.",
                )
            other_host = portal_host(other.login_url)
            if not host or other_host == host:
                continue
            site = f"{named} (site {other_host or other.login_url})"
            same_login = other.username_env == self.username_env
            same_password = other.password_env == self.password_env
            if same_login and same_password:
                errors.setdefault(
                    "password_env", f"Ces noms sont déjà ceux de {site} : son mot de passe serait tapé sur ce site."
                )
            elif same_password:
                errors.setdefault(
                    "password_env",
                    f"Ce nom est déjà celui du mot de passe de {site} : son mot de passe serait tapé sur ce site.",
                )
            elif same_login:
                errors.setdefault(
                    "username_env",
                    f"Ce nom est déjà celui de l'identifiant de {site} : son identifiant serait tapé sur ce site.",
                )
        return errors


class Invoice(models.Model):
    class Status(models.TextChoices):
        IMPORTED = "IMPORTED", "Importée"
        NEEDS_REVIEW = "NEEDS_REVIEW", "À vérifier"
        COMPLETE = "COMPLETE", "Complète"
        ERROR = "ERROR", "Erreur"

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="invoices")
    invoice_number = models.CharField(max_length=100, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    source_file = models.FileField(upload_to="invoices/%Y/%m/", blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IMPORTED)
    error_message = models.TextField(blank=True)
    # Why this document may be another supplier's than the one it is filed
    # under: an invoice type fetched it for its supplier, and it prints the
    # company number or header of another one (receipts.type_supplier_doubt).
    # Until a person validates it or moves it, it waits to be fixed and
    # teaches nobody anything. Its own field, not a check: reading the
    # document again, or as a charge, rewrites the checks, and the doubt
    # went with them.
    supplier_doubt = models.TextField(blank=True, default="")
    imported_at = models.DateTimeField(auto_now_add=True)
    # The gap between the supplier's own printed grand total (Montant HT +
    # Droits) and the sum of what we could actually attribute to individual
    # lines - e.g. UBA prints several separate duty categories (ACCISE,
    # REGIE, VIG. SECU, ...) as one invoice-level total, but only some of
    # them show up in a per-product column, so summing lines alone slightly
    # understates the true cost. Added into total_ht below purely so the
    # invoice's own total reconciles to the penny with what was actually
    # billed - never attributed to any individual product's own price,
    # since there's no reliable way to know which product it belongs to.
    reconciliation_adjustment = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # The VAT rate that adjustment carries, when the DOCUMENT states it -
    # which an EN 16931 invoice does, in BT-96/BT-103. Null everywhere else,
    # and then adjustment_ttc goes on deducing it from the lines, which is
    # all a supplier's PDF or a till receipt ever offers. Duty on alcohol is
    # 20 % on an invoice whose food is at 5,5 %: deduced there, the invoice
    # is filed below what the bank debits and no check notices.
    adjustment_vat_rate = models.DecimalField(max_digits=5, decimal_places=4, null=True, blank=True)
    # A photographed receipt's printed total - what was paid. See total_ttc.
    printed_total_ttc = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Everything below is only populated for photographed till receipts (see
    # invoices/parsers/receipt_base.py). A digital PDF needs none of it: if
    # the layout matched, the numbers are the numbers.
    #
    # A photo is different. OCR can misread a digit and produce a perfectly
    # well-formed wrong price, so a receipt parser checks its own arithmetic
    # against the totals the ticket itself prints and stores the verdict
    # here. `parse_checks` is a list of {"label", "passed", "detail"} - see
    # parsers.base.ParseCheck - and the review screen shows it beside the
    # photo so a human can see *why* a receipt was flagged.
    ocr_text = models.TextField(blank=True)
    # What a digital document carries as text, kept for what it says about
    # its sender (invoices/identifiers.py) - a receipt keeps `ocr_text`
    # instead, and only that one makes a document a receipt.
    source_text = models.TextField(blank=True)
    ocr_confidence = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    parse_checks = models.JSONField(default=list, blank=True)
    # The VAT table the document prints, as [[rate, base HT, tax], ...] with
    # the rate as a fraction. Stored because it is one of the three things a
    # check compares - the lines, the printed total, this - and the only one
    # a person could not correct: every check that mentioned it was a
    # warning nobody could answer. Typed on the review screen like the rest.
    vat_breakdown = models.JSONField(default=list, blank=True)
    # Whether that table is one a person saved from the review page - even
    # empty. An empty table used to mean "never stored" only, and was read
    # again from the photo (receipts.vat_table): a table emptied on purpose
    # came back as the reading's, with checks against figures nobody typed
    # (invoice 842, 19/09).
    vat_table_typed = models.BooleanField(
        default=False,
        help_text="La table de TVA est celle qu'une personne a enregistrée, même vide : elle n'est plus relue.",
    )
    preview_image = models.ImageField(upload_to="receipts/%Y/%m/", blank=True, null=True)
    # Set when a person has actually looked at the photo and accepted the
    # lines. Distinct from status=COMPLETE, which only means every product
    # was recognised - a receipt can be COMPLETE and still misread.
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # SHA-256 of the file exactly as it was uploaded. A folder of receipt
    # photos gets scanned again and again as new ones land in it; this is
    # what lets the ones already imported be skipped before any OCR runs,
    # rather than each costing seconds to recognise only to be refused as a
    # duplicate afterwards.
    source_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    # Which EN 16931 document this was read from - "Factur-X" (the XML came
    # attached to a PDF), "CII" or "UBL" (the XML arrived on its own) - and
    # blank for every other document, which is what makes it a question the
    # database can answer. Set by invoices/einvoice.py's import path and by
    # nothing else, it means: **the figures below are the invoice's own data,
    # not a reading**. Everything else here infers - OCR reads a photograph,
    # a regex finds an amount in a column - and `parse_checks` exists to
    # catch the inferences that are wrong. There is nothing to catch here, so
    # this document is not a receipt (`is_receipt`), never joins the queue
    # where receipts are re-typed, and says so on every page that lists it.
    einvoice_format = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ["-invoice_date", "-imported_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["supplier", "invoice_number"],
                condition=~Q(invoice_number=""),
                name="unique_supplier_invoice_number",
            ),
        ]

    def __str__(self):
        return f"{self.supplier} {self.invoice_number or self.pk} ({self.invoice_date})"

    @property
    def total_ht(self):
        """Added up in Python from the lines, like total_ttc: the invoice list
        prefetches them, so showing both totals costs two queries for the
        whole page rather than one per invoice per total."""
        return self.total_ht_of(self.lines.all())

    def total_ht_of(self, lines) -> Decimal:
        """`total_ht` from `lines`, this invoice's lines already in hand - the
        documents list prefetches them into a plain list
        (workspace._documents), and a related manager per row per total was a
        tenth of that page."""
        return sum((line.total_ht for line in lines), start=Decimal("0")) + self.reconciliation_adjustment

    @property
    def total_ttc(self):
        """Summed per line, not total_ht times one rate: a single invoice
        mixes 20% spirits with 5.5% food, and any blended rate would be
        wrong for both.

        The reconciliation adjustment takes VAT too. It is duty the lines
        don't carry (or a receipt's HT rounding), and duty is part of the VAT
        base: added flat, every UBA total fell five or six cents short of
        what the bank actually debited for it.

        A receipt whose every line kept its printed amount is added up from
        those, never from HT (ten pitas at 0,70 came to 7,01), and its
        adjustment is left out: in HT it puts back cents the division by
        (1 + rate) rounded away, which the printed amounts never lost. When
        those amounts are within the parser's tolerance of the printed total,
        the printed total is the answer - it is what was paid, and a cent the
        OCR misread must not make the bank payment unmatchable.

        An **electronic invoice states its own total** (BT-112), and its
        lines are stated in HT: 169,00 at 20% and 25,20 at 5,5% work back out
        to 229,386 where the invoice says 229,39, and a document filed a
        fraction of a cent from what the bank will pay is exactly the drift
        reading the XML exists to remove. Within a cent a line of what the
        lines make - past that, somebody has edited them, and what they now
        say wins.

        **A printed total does the same job**, and not only an XML one. A
        French supplier rounds its VAT once per rate, on that rate's HT
        subtotal; this sums each line's own product and rounds at the end.
        The two are the same arithmetic - exactly, `sum(ht * (1 + rate))`
        is `sum(ht)` plus each bracket's `base * rate` - so all that divides
        them is WHERE the cent is rounded, which can move the total by at
        most a cent per bracket. That cent is not cosmetic: `bank.matching`
        needs `candidate.total == due` to the cent, so an invoice a cent
        from its debit can never be matched automatically. Metro invoices
        had been reconciled by hand, or filed a cent away from what they
        will be debited, for no other reason; and Franprix tickets whose
        own printed total was right sat a cent under it because only some
        of their lines kept a printed amount, which is what the branch
        above asks for.

        The slack is per BRACKET, never per line: `CENTS * len(lines)` on a
        long wholesaler's invoice would swallow most of a euro, and a gap
        that size is a row that was not read - a Metro invoice was tens of
        euros short of its printed total because an own-brand row was
        dropped.
        Rescued, it would have filed the right total over wrong lines and
        hidden the fault entirely. Past a cent a bracket the lines stand,
        wrong and visible.
        """
        return self.total_ttc_of(list(self.lines.all()))

    def total_ttc_of(self, lines: list) -> Decimal:
        """`total_ttc` from `lines`, this invoice's lines already in hand (see
        total_ht_of)."""
        from .parsers.receipt_base import CENTS, RECONCILIATION_TOLERANCE

        if lines and all(line.printed_ttc is not None for line in lines):
            printed = sum((line.total_ttc for line in lines), start=Decimal("0"))
            paid = self.printed_total_ttc
            if paid is not None and abs(paid - printed) <= RECONCILIATION_TOLERANCE:
                return paid
            return printed
        # Otherwise all from HT: the adjustment covers every line's rounding,
        # so mixing in printed amounts would count some of it twice.
        total = sum(
            (line.total_ht * (Decimal("1") + line.vat_rate) for line in lines),
            start=Decimal("0"),
        ) + self.adjustment_ttc_of(lines)
        if self.printed_total_ttc is not None:
            if self.einvoice_format:
                slack = max(RECONCILIATION_TOLERANCE, CENTS * len(lines))
            else:
                slack = CENTS * max(1, len({line.vat_rate for line in lines}))
            if abs(self.printed_total_ttc - total) <= slack:
                return self.printed_total_ttc
        return total

    @property
    def adjustment_ttc(self):
        """The reconciliation adjustment with its VAT on top - duty is part
        of the VAT base, and added flat every UBA total fell five or six
        cents short of what the bank debited. One definition, since the
        correction page checks the lines against the total with it
        (receipts.lines_check).

        **What the document states beats what the lines suggest.** An
        electronic invoice carries the charge's own rate (BT-96/BT-103) and
        `adjustment_vat_rate` holds it; deducing it there put 100,00 € of
        duty at 5,5 % because the lines beside it were soft drinks, and filed
        a 1 175,00 € invoice at 1 160,50 € - unmatchable against the bank,
        understated in « Marges », and with not one failing check, since the
        checks all work on figures the document states and those balance."""
        return self.adjustment_ttc_of(self.lines.all())

    def adjustment_ttc_of(self, lines) -> Decimal:
        """`adjustment_ttc` from `lines` (see total_ht_of) - read only when
        the document states no rate, so a queryset passed here costs no
        query otherwise."""
        rate = self.adjustment_vat_rate
        if rate is None:
            rate = self._adjustment_vat_rate(list(lines))
        return self.reconciliation_adjustment * (Decimal("1") + rate)

    @staticmethod
    def _adjustment_vat_rate(lines) -> Decimal:
        """The rate of the goods the adjustment belongs with, where nothing
        states one: the lines that carry duty when some do, otherwise all of
        them - by largest HT share when their rates are mixed."""
        weights = {}
        for line in [line for line in lines if line.taxes] or lines:
            weights[line.vat_rate] = weights.get(line.vat_rate, Decimal("0")) + abs(line.total_ht)
        return max(weights, key=weights.get) if weights else Decimal("0")

    @property
    def lines_total_ht(self):
        """What the lines alone come to, before the reconciliation
        adjustment - the two are shown side by side when they differ."""
        return sum((line.total_ht for line in self.lines.all()), start=Decimal("0"))

    @property
    def needs_review_count(self):
        """How many of its lines wait for a stock item - never a charge's
        charge items, which are not products (see Product.needs_review)."""
        return self.lines.filter(product__stock_type__isnull=True, product__is_expense=False).count()

    @property
    def is_einvoice(self) -> bool:
        """Read from an EN 16931 document's own data (see
        `einvoice_format`) rather than from a reading of a page."""
        return bool(self.einvoice_format)

    @property
    def is_receipt(self):
        """A photographed till receipt rather than a digital invoice.

        A check is what makes a document a receipt - which is why an
        electronic invoice, whose checks are about the SUPPLIER's arithmetic
        and not about any reading, is never one: as a receipt it would be
        re-typed from a photo it has not got, read again through OCR, and
        have its own exact figures replaced on validation
        (receipts.recheck_after_review).
        """
        return (bool(self.parse_checks) or bool(self.ocr_text)) and not self.is_einvoice

    @property
    def waiting_check(self) -> bool:
        """Whether it is in the queue of documents to check - the same rule
        as `receipts.pending_receipts`, since a row that says "À vérifier"
        over an empty queue is a row nobody can act on. A charge is never in
        it: there is nothing to type on a rent. Nor is an electronic
        invoice: there is nothing to type on exact data either."""
        return (
            bool(self.parse_checks)
            and self.reviewed_at is None
            and not self.supplier.expenses_only
            and not self.is_einvoice
        )

    @property
    def review_state(self) -> dict:
        """What this document's state is called, wherever it is listed: the
        pill's class (which colours it) and its label."""
        if self.supplier_doubt:
            # Before its total or its lines: whose it is comes first.
            return {"css": self.Status.NEEDS_REVIEW, "label": "Fournisseur à confirmer"}
        if self.waiting_check:
            return {"css": self.Status.NEEDS_REVIEW, "label": "À vérifier"}
        if self.supplier.expenses_only:
            # Before the electronic invoice below: a rent or a subscription
            # is a charge whichever format it arrived in, and it is filed,
            # shown and settled as one everywhere else (charge_state).
            return (
                {"css": self.Status.NEEDS_REVIEW, "label": "Total à vérifier"}
                if self.status == self.Status.NEEDS_REVIEW
                else {"css": self.Status.COMPLETE, "label": "Charge"}
            )
        if self.is_einvoice:
            # Its figures are the invoice's own, so what is left to say is
            # whether anything stops it being used: its own totals not
            # holding, or no date (out of every valuation and of the bank
            # match). Both are in « Documents à corriger »
            # (workspace.DOCUMENT_TO_FIX), which is where the rest of that
            # list waits - never in the ticket queue.
            if self.error_message:
                return {"css": self.Status.NEEDS_REVIEW, "label": "À corriger"}
            if self.status == self.Status.NEEDS_REVIEW:
                return {"css": self.status, "label": "Produits à classer"}
            return {"css": self.Status.COMPLETE, "label": "Facture électronique"}
        if self.parse_checks:
            return {"css": self.Status.COMPLETE, "label": "Vérifié"}
        if self.status == self.Status.NEEDS_REVIEW:
            # What it says comes before what it is waiting on: a supplier's
            # PDF whose lines do not add up to its own printed total holds
            # itself here saying so (invoices/parsers/metro.py), and
            # "Produits à classer" would name the wrong problem - the same
            # distinction the electronic-invoice branch above draws.
            if self.error_message:
                return {"css": self.status, "label": "À corriger"}
            return {"css": self.status, "label": "Produits à classer"}
        return {"css": self.status, "label": self.get_status_display()}

    @property
    def document_text(self):
        """What the document says, however it was read."""
        return self.ocr_text or self.source_text

    @property
    def failed_checks(self):
        return [check for check in self.parse_checks if not check.get("passed")]

    @property
    def ocr_confidence_percent(self):
        """69, not 0.69 - the stored value is a fraction, and the review
        screen rendered it straight through `floatformat:0`, which turned
        every receipt's confidence into "1%"."""
        if self.ocr_confidence is None:
            return None
        return self.ocr_confidence * Decimal("100")

    @property
    def receipt_verified(self):
        """The parse proved itself against the ticket's own printed totals.

        Not the same as "correct" - it is the strongest statement the
        machine can make on its own, and it is what decides whether a
        receipt needs a human to look at the photo.
        """
        return bool(self.parse_checks) and not self.failed_checks

    @property
    def needs_receipt_review(self):
        return self.is_receipt and self.reviewed_at is None and not self.receipt_verified


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, related_name="invoice_lines")
    raw_name = models.CharField(max_length=255)
    # A count, or a measure when the document sells by it: 0,82 m² of
    # plywood, 2,5 m of cable. To the thousandth.
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    # The supplier's own "packs per line" multiplier (Metro's "Colisage"),
    # already folded into `quantity` (quantity = colisage * qty bought) -
    # shown separately in the review queue since whether a pack size was
    # already applied to `quantity` or still needs to be applied by hand via
    # the stock_equivalent factor isn't always obvious from quantity alone.
    colisage = models.IntegerField(default=1)
    total_volume = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    unit_cost_ht = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    total_ht = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    taxes = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=4, default=0)
    category = models.CharField(max_length=255, blank=True)
    # The product name exactly as OCR read it off a receipt photo, kept when
    # the review screen corrects `raw_name`. Each reading is one more name
    # its product is known by (inventory.matching.known_readings): one till
    # label comes back as BAGUETTE BLANC, BLAND, BLAVD or AGUETTE BLANC, and
    # the next ticket is matched against all of them. Blank for digital
    # invoices, typed lines and "Article divers" - a price is not a name.
    read_as = models.CharField(max_length=255, blank=True)
    # The amount tax included as the receipt printed it (or as typed on the
    # review screen, which is in TTC). Needed because HT to the cent does not
    # always convert back: 7,00 at 5.5% is 6,64 HT, which is 7,01. Null for
    # digital invoices, which are in HT, and for receipt lines a promotion was
    # folded into before promotions were kept apart (`discount_ttc`);
    # `total_ttc` then works it out from HT.
    printed_ttc = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # The line's share of a promotion, tax included, off `printed_ttc`: a
    # ticket prints the loaf at 0,49 and "3 pour 2 - 0,49" in a block of its
    # own, and the review screen shows both as printed. `total_ht` is what the
    # line cost after it.
    discount_ttc = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # This line is not a product: it is a charge the document prints for the
    # whole order - delivery above all - and what it costs belongs on the
    # goods it delivered. The line STAYS a line, with its own amount and its
    # own rate, so every total, every check and the bank match are the
    # document's own figures; only its COST moves, onto the other lines'
    # `spread_ht` (see importing.spread_charges).
    is_spread_charge = models.BooleanField(
        "frais à répartir",
        default=False,
        help_text="Cette ligne n'est pas un produit : son montant est réparti sur les autres lignes de la facture.",
    )
    # This line's share of those charges, HT - and **not** part of
    # `total_ht`, which stays what the supplier priced the goods at. Folded
    # in there instead it would move money between VAT brackets (Metro
    # delivers at 20 % invoices whose food is at 5,5 %) and the invoice's own
    # TTC would stop being the one the bank debits. `cost_ht` is the two
    # together, and that is what prices a unit.
    spread_ht = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        from common import plain_number

        return f"{self.raw_name} x{plain_number(self.quantity)}"

    @property
    def total_ttc(self):
        """What the line cost, tax included: as printed, less its share of a
        promotion - or worked out from HT when nothing was printed."""
        if self.printed_ttc is not None:
            return self.printed_ttc - self.discount_ttc
        return self.total_ht * (Decimal("1") + self.vat_rate)

    @property
    def cost_ht(self):
        """What this line really cost: what the supplier priced it at, plus
        its share of the charges the invoice spreads over its lines.

        **This is what prices a unit** - the stock movement's `unit_cost_ht`
        and the FIFO valuation both divide it by the count. `total_ht` is
        what the DOCUMENT says and is what every total, check and bank match
        is made of; this is what a bottle actually costs once the delivery
        that brought it is paid for.

        A spreading line costs nothing itself: its money is already on the
        lines it was shared over, and counted here too it would be paid for
        twice. So the lines' costs still add up to `lines_total_ht`, exactly
        - nothing is created, it only moves.
        """
        if self.is_spread_charge:
            return Decimal("0")
        return self.total_ht + self.spread_ht

    @property
    def vat_percent(self):
        """20 rather than 0.200 - the stored rate is a fraction, and every
        page that showed it raw made people read it as a currency amount."""
        return self.vat_rate * Decimal("100")


class ShopItemPrice(models.Model):
    """What a shop's unnamed receipt line actually was, keyed by its price.

    Some tills print no product names at all - every line on a Sabbh Oriental
    ticket reads "Article divers", and the only thing telling a lemon from a
    bunch of mint is what it cost. This is the operator's own answer to that,
    built up one price at a time from the review screen.

    **Keyed on the unit price, not the line total.** 0,70 EUR shows up as
    3pcs/2,10, 6pcs/4,20, 7pcs/4,90 and 11pcs/7,70 across five receipts:
    keying on the total would need a new entry for every quantity ever
    bought, while the unit price needs one per product. The price is stored
    tax-inclusive because that is what the receipt prints and what the
    operator reads off the photo.

    `valid_from` exists because shop prices move. An entry with no date is
    the standing answer; a dated one takes over from that date on, so a
    price change is recorded rather than overwriting what older invoices
    were priced with.
    """

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="item_prices")
    unit_price_ttc = models.DecimalField(
        max_digits=10, decimal_places=2, help_text="Prix unitaire TTC tel qu'imprimé sur le ticket."
    )
    label = models.CharField(max_length=255, help_text="Le produit correspondant à ce prix.")
    valid_from = models.DateField(
        null=True,
        blank=True,
        help_text="À partir de quand ce prix s'applique. Vide = depuis toujours.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["supplier", "unit_price_ttc", "-valid_from"]
        constraints = [
            # Two constraints rather than one: SQLite treats NULLs as
            # distinct, so a single UniqueConstraint over a nullable
            # valid_from would let unlimited undated duplicates through.
            models.UniqueConstraint(
                fields=["supplier", "unit_price_ttc", "valid_from"],
                condition=~Q(valid_from=None),
                name="unique_shop_item_price_dated",
            ),
            models.UniqueConstraint(
                fields=["supplier", "unit_price_ttc"],
                condition=Q(valid_from=None),
                name="unique_shop_item_price_undated",
            ),
        ]

    def __str__(self):
        return f"{self.supplier} {group_thousands(self.unit_price_ttc)} EUR -> {self.label}"


def label_for_unit_price(supplier, unit_price_ttc, on_date=None):
    """The product name recorded for `unit_price_ttc`, or "" if none is.

    Returns the most recent entry that had already taken effect on
    `on_date`; undated entries are the fallback. Never guesses at a nearby
    price - a 0,70 mapping must not answer for a 0,75 line, because that is
    how one product's costs quietly become another's.
    """
    candidates = supplier.item_prices.filter(unit_price_ttc=unit_price_ttc)
    if on_date is not None:
        candidates = candidates.filter(Q(valid_from=None) | Q(valid_from__lte=on_date))
    # ordering puts dated entries (most recent first) ahead of undated ones.
    best = candidates.order_by(models.F("valid_from").desc(nulls_last=True)).first()
    return best.label if best else ""


class ScrapeJob(JobLogMixin):
    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Terminé"
        FAILED = "FAILED", "Échoué"
        CANCELLED = "CANCELLED", "Annulé"

    class Kind(models.TextChoices):
        GATHER = "GATHER", "Récupération"
        TEST = "TEST", "Test de motif"

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.GATHER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Set by the "Annuler" button (see views.cancel_gather) while the job is
    # still PENDING/RUNNING - the background thread can't be killed outright
    # (no safe way to force-stop a plain Python thread), so this is checked
    # cooperatively between the job's own work units (before each source,
    # and between IMAP batches within one email scan - see tasks.py) and
    # the thread stops itself and sets status to CANCELLED once it notices.
    cancel_requested = models.BooleanField(default=False)
    log = models.TextField(blank=True)
    # {"METRO": {"label": "Metro", "found": 3, "imported": 2}, "type-4": {...}}
    progress = models.JSONField(default=dict, blank=True)
    # Only populated for kind=TEST - [{"sender", "subject", "date", "attachments": [...]}, ...],
    # the matches a pattern test found, shown inline instead of imported. See
    # views.invoice_type_form / tasks.test_email_pattern_task.
    test_matches = models.JSONField(default=list, blank=True)
    invoices_found = models.IntegerField(default=0)
    invoices_created = models.IntegerField(default=0)
    range_start = models.DateField(null=True, blank=True)
    range_end = models.DateField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def append_log(self, message: str):
        # Timestamped so a slow run can actually be diagnosed after the fact
        # (which specific step took how long) instead of just knowing the
        # whole thing felt slow.
        elapsed = (timezone.now() - self.started_at).total_seconds()
        line = f"[+{elapsed:6.1f}s] {message}"
        self.log = f"{self.log}{line}\n" if self.log else f"{line}\n"
        self.last_heartbeat = timezone.now()
        self.save(update_fields=["log", "last_heartbeat"])

    def update_progress(self, supplier_code: str, label: str = "", **counts):
        entry = self.progress.setdefault(supplier_code, {"label": label, "found": 0, "imported": 0})
        if label:
            entry["label"] = label
        entry.update(counts)
        self.save(update_fields=["progress"])

    @property
    def failed_sources(self) -> list[dict]:
        """The sources of this run that failed, each on its own line of the
        progress table (its "error"): one source failing no longer stops the
        others, so a run can end with some in error and still be "Terminé"."""
        return [entry for entry in (self.progress or {}).values() if entry.get("error")]

    @property
    def slips_only(self) -> bool:
        """A gather of returnables slips only - every source of its progress
        is a format's « bons-<pk> » (tasks._gather_slips), as when the
        Consignes page asks for it. Its status card then counts « Bons
        trouvés / Nouveaux » (_gather_status.html, which the polling view
        renders with the job alone), and Achats never offers its period
        again (workspace._import_card). A job with no source yet is not:
        the task puts a gather of slips' formats on its progress before
        anything can stop it (tasks._name_slip_sources)."""
        codes = list((self.progress or {}).keys())
        return bool(codes) and all(str(code).startswith("bons-") for code in codes)


class ReceiptBatch(JobLogMixin):
    """A batch of receipt photos imported in the background - typically a
    whole folder. See invoices/receipt_batches.py.

    `results` has one entry per file, in upload order:

        {"name": "Franprix 13.06EUR.pdf", "stored": "receipt_batches/12/0003.pdf",
         "status": "pending" | "ok" | "duplicate" | "unrecognised" | "error"
                   | "ignored" | "cancelled",
         "message": "...", "invoice_id": 42, "shop": "Franprix",
         "total": "13.06", "date": "2026-07-15", "verified": true}

    One JSON list rather than a table: it only exists to be shown on the
    batch page. The batch's thread and the requests choosing a shop both
    change it, one entry at a time (receipt_batches.RESULTS_LOCK).
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Terminé"
        FAILED = "FAILED", "Échoué"
        CANCELLED = "CANCELLED", "Annulé"

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Checked between files: a thread can't be stopped safely from outside,
    # so the job stops itself (same as ScrapeJob.cancel_requested).
    cancel_requested = models.BooleanField(default=False)
    log = models.TextField(blank=True)
    results = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    #: The login that sent it (its username, the address it logs in with;
    #: the logins live in the accounts database, no key reaches them):
    #: « Ajouter des factures » lists a login's own imports, and an employee
    #: who may only add follows those alone (invoices/views.py). Blank for
    #: an import from before (02/10/2026).
    sent_by = models.CharField("envoyé par", max_length=150, blank=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    #: A receipt takes seconds, and a running batch beats every 15 s
    #: (receipt_batches._Heartbeat): quiet for this long, it is dead - the
    #: dev server restarted, most likely. Ten minutes, the default, is how
    #: long a dead import used to keep showing "En cours".
    STALE_AFTER = timedelta(seconds=90)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Lot de tickets du {self.started_at:%d/%m/%Y %H:%M}"

    def append_log(self, message: str):
        elapsed = (timezone.now() - self.started_at).total_seconds()
        line = f"[+{elapsed:6.1f}s] {message}\n"
        self.log = f"{self.log}{line}"
        # Appended in the database: the batch's thread and a request choosing
        # a shop both write here, each from its own copy of the log.
        # Not a heartbeat: the reaper writes here too, and the line saying a
        # batch is dead must not make it look alive (can_resume). A running
        # batch beats on its own - after every file, and from its _Heartbeat.
        ReceiptBatch.objects.filter(pk=self.pk).update(log=Concat("log", Value(line), output_field=models.TextField()))

    def _count(self, *statuses) -> int:
        return sum(1 for entry in self.results if entry.get("status") in statuses)

    @property
    def to_read(self) -> int:
        """Files that are receipts to recognise - everything but the ignored."""
        return len(self.results) - self._count("ignored")

    @property
    def read(self) -> int:
        return self.to_read - self._count("pending")

    @property
    def progress_percent(self) -> int:
        return round(100 * self.read / self.to_read) if self.to_read else 100

    @property
    def imported_count(self) -> int:
        return self._count("ok")

    @property
    def verified_count(self) -> int:
        return sum(1 for entry in self.results if entry.get("status") == "ok" and entry.get("verified"))

    @property
    def duplicate_count(self) -> int:
        return self._count("duplicate")

    @property
    def failed_count(self) -> int:
        """Files that went nowhere: broken, or no shop recognised and the file
        no longer kept to name it (awaiting_shop_count is the rest)."""
        return self._count("error") + sum(
            1 for entry in self.results if entry["status"] == "unrecognised" and not entry.get("kept")
        )

    @property
    def pending_count(self) -> int:
        return self._count("pending")

    @property
    def awaiting_shop_count(self) -> int:
        """Files no shop was recognised on, kept for the operator to name it."""
        return sum(1 for entry in self.results if entry["status"] == "unrecognised" and entry.get("kept"))

    @property
    def can_resume(self) -> bool:
        """Files left unread by a run that died. Not while it may still be
        running: a batch heard from within STALE_AFTER could be one whose
        machine has just woken up, and resuming it would read files twice."""
        if self.is_active or not self.pending_count:
            return False
        since = self.last_heartbeat or self.started_at
        return timezone.now() - since > self.STALE_AFTER

    @property
    def ignored_count(self) -> int:
        return self._count("ignored")
