"""« Ventes » (§7.8): the till's quantities per product and per day - the raw
data -, the sales typed in by hand, and the sale documents - « factures de
vente » on screen, « bons de vente » here - whole: their figures, their
lines, their file and the bank credits that pay them.

What is derived from them is rebuilt, never copied: each till product's
total and first/last day (`recipes.sales.recount_pos_products`, proven equal
to what the till import writes), and each recipe's « laddition » sales,
summed through the links this database has when the import ends
(`resync_recipe_from_daily_quantities`). Without « Liens recettes ↔ ventes »
in the same run, the till products this section creates arrive « à lier »
and sell no recipe until they are linked - the page recommends the links
for that reason.

The per-day table is the big one (15 850 rows on 19/09), so it travels as
compact rows (`daily_columns`) and is written in bulk.

**The money does not travel.** The archive carries the quantities only
(`DAILY_COLUMNS`): neither a day's takings (`revenue_*` on the per-day rows,
which a restore leaves « non lu ») nor the till's means of payment per day
(`PosDailyPayment`). Both are re-read from the exports already on disk -
`manage.py laddition_backfill_revenue`, then `laddition_backfill_payments`,
contacting nothing. So a clear deletes the payments, and a « Remplacer »
deletes those of every day it leaves with no till sales, and both say how to
bring them back (`PAYMENTS_NOTE`) - or, in a tenant whose till the server
does not import (a hosted bar, which runs no command on the server), that
this is « à configurer » (`payments_note`); nothing else here touches them.

**The till's coverage goes back with the days deleted** (`lower_till_coverage`):
the automatic sales imports start from `ventes-<key>`, « imported without a
gap up to », so a clear or a « Remplacer » that deletes till days lowers it
to the day before the first of them - never forward -, and the next
automatic import fetches them again. Left as they were, those days stayed
empty for good under a page saying they were imported.

**A sale document is its `key`** (recipes.models: 16 hex characters, random
for a new one). An archive written before recipes 0019 carries none: each
of its records is keyed by its content - `legacy_key(fingerprint(…),
occurrence)`, the key the migration gave every document of the database the
archive came from -, so an older archive still finds its documents. One not
found by that key is looked for by its content among the documents no
record has matched yet: a legacy document deleted and typed again since has
a random key, and « Fusionner » made it twice. A field an archive does not
say is never blanked (`NOT_SAID`): « Remplacer » of an older archive keeps a
document's customer, its counting and its lines' consumed quantities. A
record checked whole - the constraints of `SaleDocumentLine` included - is
skipped with its reason, never written in part: one row refused by SQLite
failed the WHOLE import.

**Its file** lives under `ventes/` (`archive.STORAGE_FOLDERS`; written
STORED in the zip, `archive.STORED_FOLDERS`) and is checked as the invoices'
are, in the preview too. A document's `source_sha256` is the sha of the file
it stores, written once every file is placed, in two steps
(`_write_shas`): files swapped between two documents, or a file moving to a
document while its old holder waits for the prune, never meet
`saledocument_one_per_file`. Under « Remplacer », a record whose file a
document here holds that no record names - deleted and read again since,
under a new key - takes that document over (« adopted ») rather than
leaving it to the prune: the invoice was in neither.

**Its bank links** (`SaleDocumentPayment`) are nested in each record by the
credit's fingerprint. « Ventes » applies after « Banque », so both ends are
there. Merged, a link is never made against a decision the credit holds here
(another document it pays, a link undone by hand); replaced, a document's
links become exactly the archive's - and with « Banque » replaced in the
same run, only to lines banque.json names, since its prune deletes the
others. An import runs no automatic pass of the bank: it writes rows, it
reads nothing again.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.db.models import Min, Prefetch
from django.utils import timezone

from bank.models import BankTransaction
from common import format_money
from invoices.forms import EARLIEST_DOCUMENT_DATE
from recipes.forms import MANUAL_SALE_SOURCE
from recipes.integration import TILL_TO_CONFIGURE, till_allowed
from recipes.models import (
    SALE_FILES_FOLDER,
    PosDailyPayment,
    PosProduct,
    PosProductDailyQuantity,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
    SaleDocumentPayment,
    legacy_key,
)
from transfer import codec, keys, registry
from transfer.archive import ArchiveError
from transfer.keys import fold
from transfer.sections.base import SALE_LINKS, FileRefused, Section, delete_ids, restore_moments, stored_bytes
from transfer.sections.recipes import Skip
from transfer.sections.till_links import (
    DESCRIPTIVE,
    TillProducts,
    laddition_rows,
    merge_descriptive,
    till_names,
)

DAILY_COLUMNS = ["till_product", "sold_on", "quantity"]
LISTS = ("till_products", "daily", "manual_sales", "documents")
TILL_KEYS = ("name", *DESCRIPTIVE)
MANUAL_KEYS = ("recipe", "sold_on", "quantity", "recorded_at")
#: What an archive written before the sales invoices carries of a document -
#: and what its content fingerprint (`fingerprint`, unchanged) hashes.
LEGACY_FIELDS = ("reference", "sold_on", "note")
DOCUMENT_FIELDS = (
    "key",
    *LEGACY_FIELDS,
    "customer",
    "customer_identifier",
    "counting",
    "stated_total_ttc",
    "stated_total_ht",
    "prepaid_ttc",
    "payable_ttc",
    "adjustment_ht",
    "adjustment_vat_rate",
    "einvoice_format",
    "einvoice_type_code",
    "einvoice_issued_on",
    "einvoice_delivered_on",
    "einvoice_preceding_number",
    "einvoice_checks",
    "seller_name",
    "seller_siren",
    "source_sha256",
)
DOCUMENT_KEYS = (*DOCUMENT_FIELDS, "created_at", "file", "lines", "payments")
#: Compared between an archive and this database, and what « Remplacer »
#: assigns (with the moment, never compared): every field but the key and
#: the file's sha - which follows the file stored (`_write_shas`), never the
#: record's figure.
COMPARED = tuple(name for name in DOCUMENT_FIELDS if name not in ("key", "source_sha256"))
#: Filled by « Fusionner » when blank here, even beside a conflict.
FILLABLE = ("customer", "customer_identifier", "note")
LINE_FIELDS = (
    "label",
    "quantity",
    "unit_price_ttc",
    "unit_price_ht",
    "total_ht",
    "vat_rate",
    "consumed_quantity",
    "rebuilt",
)
LINE_KEYS = ("recipe", "article", *LINE_FIELDS)
#: Every archive has carried a line's quantity and price: read as ever, a
#: price left out is none. The other fields are « not said » when absent -
#: an old line's label or rate is kept under « Remplacer ».
LEGACY_LINE_FIELDS = ("quantity", "unit_price_ttc")
PAYMENT_FIELDS = ("method", "created_at")
PAYMENT_KEYS = ("transaction", *PAYMENT_FIELDS)
#: Where this section's files live (archive.STORAGE_FOLDERS has it).
FOLDER = SALE_FILES_FOLDER

#: Every concrete field of the sale documents' three models is exported or
#: said why not (ContractTests), so a field added later cannot be left out
#: of the archive in silence - the round trip would not see it, its snapshot
#: being made of the same records. The till's models of this section are
#: older than this guard, and PosProduct's fields are shared with « Liens »
#: (`till_links.DESCRIPTIVE`): a follow-up.
EXPORTED = {
    # `source_file` travels as the record's « file » (a file ref).
    SaleDocument: (*DOCUMENT_FIELDS, "created_at", "source_file"),
    SaleDocumentLine: LINE_FIELDS,
    SaleDocumentPayment: PAYMENT_FIELDS,
}
NOT_EXPORTED = {
    SaleDocument: {"id": "pk"},
    SaleDocumentLine: {"id": "pk", "document": "parent", "recipe": "by name", "stock_type": "by name"},
    SaleDocumentPayment: {"id": "pk", "document": "parent", "transaction": "by its fingerprint"},
}

#: A per-day row is one till product on one day, so its label says so, and
#: the days are counted apart: called « jours de vente (caisse) », the 15 850
#: rows of 19/09 (211 products over 662 days) read as 43 years of sales, or
#: as a clear wiping 15 850 days (UX review, 19/09).
QUANTITIES, TILL_DAYS = "quantités par produit et par jour (caisse)", "jours de caisse"
#: « bons de vente » and their lines keep the labels every archive written so
#: far says: the Importer tab compares an archive's counts with count(),
#: label by label.
PRODUCTS, MANUAL, DOCUMENTS = "produits caisse", "ventes saisies", "bons de vente"
LINES, RECIPE_SALES = "lignes de bons de vente", "ventes par recette"
#: The invoices' labels: safety.estimated_bytes reads « Mo de fichiers ».
FILES, FILE_MB = "fichiers", "Mo de fichiers"
LINKS = SALE_LINKS
#: One row is one means of payment on one day, so the label says so - the
#: lesson of QUANTITIES. Never in count(): the archive does not carry them,
#: and the import tab compares count() with the archive's counts.
PAYMENTS = "totaux par jour et par moyen de paiement (caisse)"
PAYMENTS_NOTE = (
    "Les moyens de paiement de la caisse ne sont pas dans les archives : "
    "« manage.py laddition_backfill_payments » les relit des exports déjà téléchargés, pour les jours "
    "dont les ventes sont enregistrées."
)
#: PAYMENTS_NOTE where that command is not this tenant's to run: it reads the
#: exports of the till the server imports, the owner's (recipes/integration.py),
#: and a hosted bar runs no command on the server.
PAYMENTS_NOTE_TO_CONFIGURE = (
    f"Les moyens de paiement de la caisse ne sont pas dans les archives, et {TILL_TO_CONFIGURE}."
)
#: Said only when key-less records - an archive written before the sales
#: invoices - created documents while this database had some.
LEGACY_NOTE = (
    "Bons de vente d'une archive d'avant les factures de vente : retrouvés par leur contenu, un bon modifié avant la "
    "mise à jour arrive comme un nouveau bon."
)
#: The notes about a document's file, participles: a preview and its
#: confirm read the same.
MISSING_FILE_NOTE = "{document} : fichier absent du disque à l'export, importé sans."
MISSING_FILE_KEPT_NOTE = "{document} : fichier absent du disque à l'export, celui d'ici gardé."
RESTORED_NOTE = "{document} : fichier restauré"
#: Said once a run, beside the conflicts « réglée à la main ici sans régler
#: … »: the same credit is left by a person's « Délier » and by the document
#: deleted then brought back by another run, and only the person knows which.
SALE_UNDONE_NOTE = (
    "Une entrée réglée à la main ici sans une facture de vente qu'elle règle dans l'archive est gardée telle quelle : "
    "ce lien a pu être défait à la main. Refaites-le sur la page de la facture si besoin, ou importez « Ventes » en "
    "« Remplacer »."
)

KEY_SHAPE = re.compile(r"[0-9a-f]{16}")
SHA_SHAPE = re.compile(r"[0-9a-f]{64}")
#: Nine ASCII digits: Achats' guard reads it against the bar's own invoices.
SIREN_SHAPE = re.compile(r"[0-9]{9}")
#: How a conflict names a field: the model's own words.
FIELD_LABELS = {
    **{name: str(SaleDocument._meta.get_field(name).verbose_name) for name in DOCUMENT_FIELDS},
    "lines": "lignes",
    "file": "fichier",
}
#: A line's fields as a line created with nothing said of them has them.
LINE_DEFAULTS = {"label": "", "total_ht": None, "vat_rate": None, "consumed_quantity": None, "rebuilt": False}


def payments_note() -> str:
    """What a run that deleted the till's payments says, for the bound tenant."""
    return PAYMENTS_NOTE if till_allowed() else PAYMENTS_NOTE_TO_CONFIGURE


#: Rows written or deleted per query: SQLite caps a statement's parameters.
BATCH = 500


def _day(value) -> str:
    return f"{value:%d/%m/%Y}"


def _text(value, places: int) -> str | None:
    """A Decimal as the fingerprint writes it: at the field's places, so the
    archive's « 2.0000 » and the database's Decimal("2") are one quantity."""
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal(1).scaleb(-places)))


def fingerprint(reference: str, sold_on, note: str, lines) -> str:
    """A sale document's content key: its reference, day, note and its lines
    in order, each (« recipe » or « article », folded name, quantity, unit
    price). What « Données » knew a document by before recipes 0019, and
    still the key of a record with none of its own (`legacy_key`): left as
    it is, or an older archive no longer finds its documents."""
    canonical = {
        "reference": reference or "",
        "sold_on": sold_on.isoformat(),
        "note": note or "",
        "lines": [[kind, fold(name), _text(quantity, 4), _text(price, 2)] for kind, name, quantity, price in lines],
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _title(sold_on, reference: str) -> str:
    """How a message names a document: « Bon de vente du 06/09/2026
    (SOIRÉE-1) »."""
    return f"Bon de vente du {_day(sold_on)}" + (f" ({reference})" if reference else "")


def _lower(title: str) -> str:
    """« bon de vente du … », inside a sentence."""
    return title[:1].lower() + title[1:]


def _named(document) -> str:
    """How a sentence names a document here: « le bon de vente du … »."""
    return "le " + _lower(_title(document.sold_on, document.reference))


def _euros(amount) -> str:
    return format_money(amount).replace(".", ",") + " €"


def _entry(line, amount: bool = True) -> str:
    """How a message names a credit: « Entrée du 10/09/2026 (230,52 €) »."""
    said = f"Entrée du {_day(line.operation_date)}"
    return f"{said} ({_euros(line.amount)})" if amount else said


def _fields(names) -> str:
    """At most three differing fields, in French, for a conflict line."""
    shown = [FIELD_LABELS.get(name, name) for name in names]
    return ", ".join(shown[:3]) + (", …" if len(shown) > 3 else "")


def _linked_on(item: dict) -> str:
    """« (lien du 10/09/2026) »: when a link of the archive was made, as far
    as it reads - the credit it names being nowhere here."""
    try:
        moment = codec.load(SaleDocumentPayment, "created_at", item.get("created_at"))
    except codec.FieldValueError:
        return ""
    return f" (lien du {timezone.localtime(moment):%d/%m/%Y})"


def _readable_checks(checks) -> bool:
    """An electronic invoice's checks as the application writes them
    ({"label", "passed", "detail"}): what the document's page reads of each."""
    return isinstance(checks, list) and all(
        isinstance(check, dict)
        and isinstance(check.get("label"), str)
        and isinstance(check.get("passed"), bool)
        and isinstance(check.get("detail", ""), str)
        for check in checks
    )


def _check_day(name: str, value, last) -> None:
    """A day the codec read, within the first day any document is dated and
    `last`. The codec reads any ISO date: a 9999-12-31 from an archive made
    every page reading it a 500 once (`bank._check_day`'s rule)."""
    if value is not None and not EARLIEST_DOCUMENT_DATE <= value <= last:
        raise codec.FieldValueError(
            f"« {FIELD_LABELS[name]} » : date hors limites (« {value.isoformat()} ») : entre le "
            f"{_day(EARLIEST_DOCUMENT_DATE)} et le {_day(last)}"
        )


def _line_problem(tied: bool, values: dict) -> str:
    """Why a line holding these values cannot be stored - the constraints of
    `SaleDocumentLine`, said, never an IntegrityError failing the whole run
    -, or "". `values` are the line's fields as they will be."""
    rate = values.get("vat_rate")
    if not tied and not (values.get("label") or "").strip():
        return "une recette, un article ou un libellé"
    if values.get("total_ht") is not None and rate is None:
        return "un montant HT sans son taux"
    if rate is not None and not Decimal("0") <= rate <= Decimal("1"):
        return "taux hors 0 à 100 %"
    if not tied and values.get("consumed_quantity") is not None:
        return "une quantité consommée sans recette ni article"
    if tied and values.get("rebuilt"):
        return "une ligne reconstituée ne se relie pas"
    return ""


def _stored(name: str) -> bool:
    """Whether storage holds this name - False for one it refuses."""
    try:
        return bool(name) and default_storage.exists(name)
    except (SuspiciousFileOperation, OSError, ValueError):
        return False


def _documents(*, payments: bool = False):
    """This database's documents in (created_at, id) order - the order the
    export writes them, so two identical documents are occurrence 0 and 1
    on both sides - with their lines (and, for the export and the snapshot,
    the credits paying them): two queries, three."""
    documents = SaleDocument.objects.prefetch_related(
        Prefetch("lines", queryset=SaleDocumentLine.objects.select_related("recipe", "stock_type").order_by("id"))
    )
    if payments:
        documents = documents.prefetch_related(
            Prefetch("bank_payments", queryset=SaleDocumentPayment.objects.select_related("transaction").order_by("id"))
        )
    return documents.order_by("created_at", "id")


def _line_record(line: SaleDocumentLine) -> dict:
    return {
        "recipe": line.recipe.name if line.recipe_id else None,
        "article": line.stock_type.name if line.stock_type_id else None,
        **codec.record(line, LINE_FIELDS),
    }


def _line_shape(line: SaleDocumentLine) -> tuple | None:
    """What the content fingerprint reads of a line: one source, or None -
    a line tied to nothing has no content key (only a document saved since
    recipes 0019 can hold one)."""
    if line.recipe_id and not line.stock_type_id:
        return ("recipe", line.recipe.name, line.quantity, line.unit_price_ttc)
    if line.stock_type_id and not line.recipe_id:
        return ("article", line.stock_type.name, line.quantity, line.unit_price_ttc)
    return None


def _contents(documents) -> dict[tuple[str, int], SaleDocument]:
    """(fingerprint, occurrence) → document, for every document here whose
    lines each have one source - the only kind an archive written before
    recipes 0019 can name -, in the order given ((created_at, id))."""
    found: dict[tuple[str, int], SaleDocument] = {}
    seen: dict[str, int] = defaultdict(int)
    for document in documents:
        shapes = [_line_shape(line) for line in document.lines.all()]
        if any(shape is None for shape in shapes):
            continue
        content = fingerprint(document.reference, document.sold_on, document.note, shapes)
        found[(content, seen[content])] = document
        seen[content] += 1
    return found


def _pk(row) -> int | None:
    return row.pk if row is not None else None


#: Said of a line whose tie an import changed back and whose consumed
#: quantity, measured in the old tie's unit, the record does not say.
CONSUMED_CLEARED_NOTE = (
    "{title} : quantité consommée remise à la quantité facturée (nouvelle correspondance) : ligne n° {number}"
)


def _clears_consumed(line: _Line, here, target: dict) -> bool:
    """Whether writing `line` over `here` changes its tie while the record
    says no consumed quantity (an archive written before them): the one
    kept here is measured in the OLD tie's unit - 30 litres of a keg read
    as 30 servings of a recipe -, so it is cleared, as the page clears it
    on a tie changed (CONSUMED_CLEARED)."""
    if here is None or "consumed_quantity" in line.data or here.consumed_quantity is None:
        return False
    return here.recipe_id != _pk(target["recipe"]) or here.stock_type_id != _pk(target["stock_type"])


def _target(line: _Line, recipes, articles) -> dict:
    """The recipe or the article here a line of the archive names, by its
    folded name - never fuzzily; Skip when it names one this database has
    not got."""
    if line.recipe is not None:
        recipe = recipes.resolve(line.recipe)
        if recipe is None:
            raise Skip(f"recette inconnue « {line.recipe} »")
        return {"recipe": recipe, "stock_type": None}
    if line.article is not None:
        stock_type = articles.resolve(line.article)
        if stock_type is None:
            raise Skip(f"article inconnu « {line.article} »")
        return {"recipe": None, "stock_type": stock_type}
    return {"recipe": None, "stock_type": None}


def _bank_fingerprints(ctx) -> set[str]:
    """The lines « Banque »'s file names: with « Banque » replaced in the
    same run, its prune deletes every other one (CLAUDE.md « A keep in apply
    must not rest on data the same run's prune removes »). Its load checked
    the list."""
    transactions = ctx.reader.section("banque").payload().get("transactions")
    return {record.get("fingerprint") for record in transactions or [] if isinstance(record, dict)}


def _batches(items: list):
    for start in range(0, len(items), BATCH):
        yield items[start : start + BATCH]


def _delete(model, pks: list[int]) -> int:
    deleted = 0
    for batch in _batches(pks):
        _total, per_model = model.objects.filter(pk__in=batch).delete()
        deleted += per_model.get(model._meta.label, 0)
    return deleted


def _say_documents_deleted(ctx, report, documents: int, per_model: dict, names: list[str]) -> None:
    """What deleting documents took, counted alike by the prune and the
    clear (transfer critique 8): the documents, their lines and their bank
    links - from the counts the delete returned -, and their files, each
    deleted on commit if no row names it any more."""
    for what, count in (
        (DOCUMENTS, documents),
        (LINES, per_model.get(SaleDocumentLine._meta.label, 0)),
        (FILES, len(names)),
        (LINKS, per_model.get(SaleDocumentPayment._meta.label, 0)),
    ):
        if count:
            report.deleted(what, count)
    for name in names:
        ctx.delete_file_on_commit(name)


class _NotSaid:
    """A field the record does not have: never compared, never written."""

    def __repr__(self):
        return "NOT_SAID"


NOT_SAID = _NotSaid()


@dataclass(frozen=True)
class _Missing:
    """A file the other database named but its disk lacked at export."""

    name: str


@dataclass
class _Line:
    """One line of a record, checked."""

    recipe: str | None
    article: str | None
    #: What is compared and assigned, as the archive says it: the legacy
    #: fields always, the others when said.
    data: dict
    #: The same, loaded.
    values: dict

    @property
    def shape(self) -> tuple:
        """What the content fingerprint reads of it (a line of a record with
        no key has one source)."""
        if self.recipe is not None:
            return ("recipe", self.recipe, self.values["quantity"], self.values["unit_price_ttc"])
        return ("article", self.article, self.values["quantity"], self.values["unit_price_ttc"])


@dataclass
class _Doc:
    """One record of the archive, checked - nothing written yet."""

    position: int
    key: str
    #: (fingerprint, occurrence) for a record with no key of its own (an
    #: archive written before recipes 0019), else None.
    content: tuple[str, int] | None
    title: str
    #: What is compared and assigned, as the archive says it: the legacy
    #: fields always (a reference or a note left out is ""), the others and
    #: the moment when said.
    data: dict
    #: Every field said, loaded - the file's sha included.
    values: dict
    stamp: object
    lines: list[_Line] | None  # None: not said
    ref: Any  # NOT_SAID, None (no file), _Missing or a checked ref (a dict)
    payments: list | None  # None: not said
    #: Each line's recipe and article here; None when one is unknown (why:
    #: `unknown`).
    targets: list[dict] | None = None
    unknown: str = ""
    found: SaleDocument | None = None
    adopted: bool = False
    #: What it applies to once known: found here, adopted or created.
    document: SaleDocument | None = None


@dataclass
class _Plan:
    """What a record does to the document here it answers to."""

    doc: _Doc
    document: SaleDocument
    here_lines: list
    different: list[str]
    line_changes: list[bool]
    file_state: str  # same | fill | differs
    kind: str = "unchanged"  # unchanged | merge | replace | kept
    fills: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    reason: str = ""  # why kept
    store: bool = False  # the record's file stored for it
    remove: bool = False  # its file removed: the record says it has none
    stored: str = ""  # the name that file was stored under
    written: bool = False  # by this run: removed if the plan gives way
    sha: str = ""  # the sha it ends with

    @property
    def changes_sha(self) -> bool:
        return self.sha != self.document.source_sha256

    @property
    def final_title(self) -> str:
        return self.doc.title if self.kind == "replace" else _title(self.document.sold_on, self.document.reference)


#: Said when the till's coverage went back (a participle: the preview and the
#: confirm read the same).
COVERAGE_NOTE = (
    "Import automatique des ventes : ramené au {day:%d/%m/%Y}, les jours supprimés sont importés à nouveau ensuite."
)


def lower_till_coverage(first_deleted, report=None) -> None:
    """Days deleted here are no longer imported: each sales source's
    coverage (`ventes-<key>`, where the next automatic import starts) goes
    back to the day before the first of them - never forward. A source with
    no row is given one only where its history (the successful imports,
    which a clear keeps) would rebuild it past those days. Inside the run's
    transaction: a preview rolls it back with the rest."""
    if first_deleted is None:
        return
    from invoices.models import GatherCoverage
    from recipes import auto_sales, sales_sources

    until = first_deleted - timedelta(days=1)
    lowered = False
    for key in sales_sources.SOURCES:
        code = auto_sales.coverage_code(key)
        row = GatherCoverage.objects.filter(code=code).first()
        if row is None:
            known = auto_sales.covered_until(key)
            if known is not None and known > until:
                GatherCoverage.objects.create(code=code, searched_until=until)
                lowered = True
        elif row.searched_until is not None and row.searched_until > until:
            row.searched_until = until
            row.save(update_fields=["searched_until", "updated_at"])
            lowered = True
    if lowered and report is not None:
        report.note(COVERAGE_NOTE.format(day=until))


@registry.register
class SalesSection(Section):
    key = "ventes"

    # -- what this database holds ---------------------------------------------------
    def count(self) -> dict[str, int]:
        names = frozenset(
            SaleDocument.objects.exclude(source_file="").exclude(source_file=None).values_list("source_file", flat=True)
        )
        return {
            QUANTITIES: PosProductDailyQuantity.objects.count(),
            TILL_DAYS: PosProductDailyQuantity.objects.values("sold_on").distinct().count(),
            PRODUCTS: PosProductDailyQuantity.objects.values("product_id").distinct().count(),
            MANUAL: RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).count(),
            DOCUMENTS: SaleDocument.objects.count(),
            FILES: len(names),
            FILE_MB: round(stored_bytes(names, kind=self.key) / 1_000_000),
            LINKS: SaleDocumentPayment.objects.count(),
        }

    def snapshot(self):
        payload = self._payload()
        return {
            "till_products": sorted(
                (
                    product.name,
                    product.category,
                    product.typology,
                    product.total_quantity,
                    codec.dump(product, "first_seen"),
                    codec.dump(product, "last_seen"),
                )
                for product in PosProduct.objects.filter(daily_quantities__isnull=False).distinct()
            ),
            "daily": sorted(tuple(row) for row in payload["daily"]),
            "manual_sales": sorted(
                (sale["recipe"], sale["sold_on"], sale["quantity"], sale["recorded_at"])
                for sale in payload["manual_sales"]
            ),
            # The file by its name and content: a ref needs the archive's writer.
            "documents": sorted(
                json.dumps(
                    {
                        **codec.record(document, (*DOCUMENT_FIELDS, "created_at")),
                        "lines": [_line_record(line) for line in document.lines.all()],
                        "file": [document.source_file.name, keys.file_sha256(document.source_file)]
                        if document.source_file
                        else None,
                        "payments": sorted(
                            [payment.transaction.fingerprint, payment.method, codec.dump(payment, "created_at")]
                            for payment in document.bank_payments.all()
                        ),
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                )
                for document in _documents(payments=True)
            ),
            # Derived: the till's sales per recipe, rebuilt through the links.
            "laddition": laddition_rows(),
        }

    # -- export ------------------------------------------------------------------------
    def _payload(self) -> dict:
        till_products = [
            {"name": name, "category": category, "typology": typology}
            for name, category, typology in PosProduct.objects.filter(
                pk__in=PosProductDailyQuantity.objects.values("product_id")
            )
            .order_by("name")
            .values_list("name", "category", "typology")
        ]
        daily = [
            [name, sold_on.isoformat(), quantity]
            for name, sold_on, quantity in PosProductDailyQuantity.objects.order_by(
                "product__name", "sold_on"
            ).values_list("product__name", "sold_on", "quantity")
        ]
        manual_sales = [
            {"recipe": sale.recipe.name, **codec.record(sale, ("sold_on", "quantity", "recorded_at"))}
            for sale in RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE)
            .select_related("recipe")
            .order_by("recipe__name", "sold_on")
        ]
        return {
            "till_products": till_products,
            "daily_columns": list(DAILY_COLUMNS),
            "daily": daily,
            "manual_sales": manual_sales,
        }

    def export(self, out) -> None:
        payload = self._payload()
        documents, stored, links = [], {}, 0
        for document in _documents(payments=True):
            ref = out.add_file(document.source_file)
            if ref and not ref.get("missing"):
                stored[ref["name"]] = ref["size"]
            # Always said, an empty list included: absent, the links read as
            # an archive written before them - « not said ».
            payments = [
                {"transaction": payment.transaction.fingerprint, **codec.record(payment, PAYMENT_FIELDS)}
                for payment in document.bank_payments.all()
            ]
            links += len(payments)
            documents.append(
                {
                    **codec.record(document, (*DOCUMENT_FIELDS, "created_at")),
                    "file": ref,
                    "lines": [_line_record(line) for line in document.lines.all()],
                    "payments": payments,
                }
            )
        payload["documents"] = documents
        out.write(
            payload,
            {
                # The same rules as count(): the import tab prints both side by
                # side. Files are the ones stored, as « Factures et tickets »
                # counts them: one gone from the disk is in count(), not here,
                # and the manifest's note says why.
                QUANTITIES: len(payload["daily"]),
                TILL_DAYS: len({sold_on for _name, sold_on, _quantity in payload["daily"]}),
                PRODUCTS: len(payload["till_products"]),
                MANUAL: len(payload["manual_sales"]),
                DOCUMENTS: len(documents),
                FILES: len(stored),
                FILE_MB: round(sum(stored.values()) / 1_000_000),
                LINKS: links,
            },
        )

    # -- import ------------------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        if payload.get("daily_columns") != DAILY_COLUMNS:
            raise ArchiveError(
                "Archive refusée : dans ventes.json, « daily_columns » doit être "
                f"{json.dumps(DAILY_COLUMNS, ensure_ascii=False)}."
            )
        for name in LISTS:
            if not isinstance(payload.get(name, []), list):
                raise ArchiveError(f"Archive refusée : dans ventes.json, « {name} » n'est pas une liste.")
        # The documents left out are not said - None, never an empty list,
        # which « Remplacer » read as « delete every sale invoice here », its
        # file and its links (transfer critique 13).
        documents = payload.get("documents")
        for record in documents or []:
            if not isinstance(record, dict) or "payments" not in record:
                continue
            payments = record["payments"]
            # The bank's own rule: read as empty, a « Remplacer » would unlink
            # everything.
            if not isinstance(payments, list) or not all(isinstance(item, dict) for item in payments):
                raise ArchiveError(
                    "Archive refusée : dans ventes.json, les règlements d'un bon de vente ne sont pas une liste."
                )
        self.payload = payload
        self.till_products, self.daily, self.manual_sales = (payload.get(name, []) for name in LISTS[:3])
        self.documents: list | None = documents
        # Before any apply (the runner loads every section first): a credit
        # and a document here before the run are ones a person may have
        # unlinked (`_merge_links`).
        self._lines_before: set[int] = set(BankTransaction.objects.values_list("pk", flat=True))
        self._documents_before: set[int] = set(SaleDocument.objects.values_list("pk", flat=True))

    def apply(self, ctx, report) -> None:
        codec.note_unknown(report, self.payload, (*LISTS, "daily_columns", "supplier_names"))
        self._ctx, self._report = ctx, report
        self.replacing = ctx.replacing(self.key)
        names = till_names(self.till_products) | {
            row[0] for row in self.daily if isinstance(row, list) and row and isinstance(row[0], str)
        }
        self.tills = TillProducts(names)
        #: Till products whose days changed: their totals are recounted, and
        #: the recipes they are linked to rebuilt.
        self.touched: set[int] = set()
        #: The documents the prune leaves alone, by pk: every one a record
        #: matched (by key or by content), adopted or created, and every one
        #: whose key a record carries that was skipped after its key could
        #: be read - the prune leaves this database's copy of it alone.
        self.kept: set[int] = set()
        #: The records this run applied to a document: their links are made.
        self._applied: list[_Doc] = []
        #: Every record answering to a document here, applied or kept whole:
        #: what the archive says of the credits (`_merge_links`).
        self._parsed: list[_Doc] = []
        self._till_products(report)
        self._daily(report)
        self._manual_sales(ctx, report)
        self._documents(ctx, report)
        self._links(ctx, report)
        self._settle(ctx)

    def _new_product(self, name: str, **values) -> PosProduct:
        """A till product this database has never seen: « à lier », as the
        till import would leave it, with no total until its days are counted."""
        product = PosProduct.objects.create(name=name, **values)
        self.tills.add(product)
        return product

    def _till_products(self, report) -> None:
        seen: set[int] = set()
        for position, record in enumerate(self.till_products, start=1):
            if not isinstance(record, dict) or not isinstance(record.get("name"), str) or not record["name"].strip():
                report.skip(f"produit caisse n° {position} de l'archive : illisible")
                continue
            name = record["name"]
            codec.note_unknown(report, record, TILL_KEYS, "produits caisse : ")
            try:
                codec.load(PosProduct, "name", name)
                values = {
                    field: codec.load(PosProduct, field, record[field]) for field in DESCRIPTIVE if field in record
                }
            except codec.FieldValueError as exc:
                report.skip(f"Produit caisse « {name} » : {exc}")
                continue
            product = self.tills.resolve(name)
            if product is None:
                seen.add(self._new_product(name, **values).pk)
                report.created(PRODUCTS)
                continue
            if product.pk in seen:
                report.skip(f"Produit caisse « {name} » : deux fois dans l'archive, la seconde est ignorée")
                continue
            seen.add(product.pk)
            changed, differing = merge_descriptive(product, values, self.replacing)
            if changed:
                product.save(update_fields=changed)
                report.updated(PRODUCTS)
            if differing:
                report.conflict(
                    f"Produit caisse « {product.name} » : différent dans l'archive ({', '.join(differing)}) "
                    "— gardé tel quel"
                )
            elif not changed:
                report.unchanged(PRODUCTS)

    def _daily(self, report) -> None:
        existing = {
            (product_id, sold_on): (pk, quantity)
            for pk, product_id, sold_on, quantity in PosProductDailyQuantity.objects.values_list(
                "pk", "product_id", "sold_on", "quantity"
            )
        }
        #: Every (till product, day) the file holds, skipped or not: prune
        #: leaves those rows as they are.
        self.daily_keys: set[tuple[int, object]] = set()
        creates: list[PosProductDailyQuantity] = []
        updates: list[PosProductDailyQuantity] = []
        for position, row in enumerate(self.daily, start=1):
            if (
                not isinstance(row, list)
                or len(row) != len(DAILY_COLUMNS)
                or not isinstance(row[0], str)
                or not row[0].strip()
            ):
                report.skip(f"vente par jour n° {position} de l'archive : illisible")
                continue
            name, sold_on, quantity = row
            try:
                sold_on = codec.load(PosProductDailyQuantity, "sold_on", sold_on)
            except codec.FieldValueError as exc:
                report.skip(f"Produit caisse « {name} » : {exc}")
                continue
            product = self.tills.resolve(name)
            if product is not None:
                if (product.pk, sold_on) in self.daily_keys:
                    report.skip(f"Produit caisse « {name} » le {_day(sold_on)} : deux fois dans l'archive")
                    continue
                self.daily_keys.add((product.pk, sold_on))
            try:
                quantity = codec.load(PosProductDailyQuantity, "quantity", quantity)
                codec.check_count("quantity", quantity)
                if product is None:
                    codec.load(PosProduct, "name", name)
            except codec.FieldValueError as exc:
                report.skip(f"Produit caisse « {name} » le {_day(sold_on)} : {exc}")
                continue
            if product is None:
                # Named by a day but not in the file's list of till products.
                product = self._new_product(name)
                report.created(PRODUCTS)
                self.daily_keys.add((product.pk, sold_on))
            found = existing.get((product.pk, sold_on))
            if found is None:
                creates.append(PosProductDailyQuantity(product=product, sold_on=sold_on, quantity=quantity))
                self.touched.add(product.pk)
            elif found[1] == quantity:
                report.unchanged(QUANTITIES)
            elif self.replacing:
                updates.append(PosProductDailyQuantity(pk=found[0], quantity=quantity))
                self.touched.add(product.pk)
            else:
                report.conflict(
                    f"Produit caisse « {product.name} » le {_day(sold_on)} : {found[1]} ici, {quantity} dans "
                    "l'archive — gardé tel quel"
                )
        if creates:
            PosProductDailyQuantity.objects.bulk_create(creates, batch_size=BATCH)
            report.created(QUANTITIES, len(creates))
        if updates:
            PosProductDailyQuantity.objects.bulk_update(updates, ["quantity"], batch_size=BATCH)
            report.updated(QUANTITIES, len(updates))

    def _manual_sales(self, ctx, report) -> None:
        recipes = ctx.recipes()
        existing = {
            (recipe_id, sold_on): (pk, quantity)
            for pk, recipe_id, sold_on, quantity in RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).values_list(
                "pk", "recipe_id", "sold_on", "quantity"
            )
        }
        self.manual_keys: set[tuple[int, object]] = set()
        for position, record in enumerate(self.manual_sales, start=1):
            if not isinstance(record, dict) or not isinstance(record.get("recipe"), str):
                report.skip(f"vente saisie n° {position} de l'archive : illisible")
                continue
            codec.note_unknown(report, record, MANUAL_KEYS, "ventes saisies : ")
            recipe_name = record["recipe"]
            try:
                sold_on = codec.load(RecipeSale, "sold_on", record.get("sold_on"))
                quantity = codec.load(RecipeSale, "quantity", record.get("quantity"))
                codec.check_count("quantity", quantity)
                stamp = (
                    codec.load(RecipeSale, "recorded_at", record["recorded_at"])
                    if record.get("recorded_at") is not None
                    else None
                )
            except codec.FieldValueError as exc:
                report.skip(f"Vente saisie de « {recipe_name} » : {exc}")
                continue
            recipe = recipes.resolve(recipe_name)
            if recipe is None:
                report.skip(f"Vente saisie du {_day(sold_on)} : recette inconnue « {recipe_name} »")
                continue
            key = (recipe.pk, sold_on)
            if key in self.manual_keys:
                report.skip(f"Vente saisie de « {recipe.name} » le {_day(sold_on)} : deux fois dans l'archive")
                continue
            self.manual_keys.add(key)
            found = existing.get(key)
            if found is None:
                sale = RecipeSale.objects.create(
                    recipe=recipe, sold_on=sold_on, quantity=quantity, source=MANUAL_SALE_SOURCE
                )
                if stamp is not None:
                    # auto_now_add stamped it "now"; the archive's is the real one.
                    RecipeSale.objects.filter(pk=sale.pk).update(recorded_at=stamp)
                report.created(MANUAL)
            elif found[1] == quantity:
                report.unchanged(MANUAL)
            elif self.replacing:
                RecipeSale.objects.filter(pk=found[0]).update(quantity=quantity)
                report.updated(MANUAL)
            else:
                report.conflict(
                    f"Vente saisie de « {recipe.name} » le {_day(sold_on)} : {found[1]} ici, {quantity} dans "
                    "l'archive — gardée telle quelle"
                )

    # .. documents ...................................................................
    def _documents(self, ctx, report) -> None:
        """Every record checked first, in the archive's order (`_read`);
        under « Remplacer », the adoptions; then the documents found here
        (`_plan`), whose files are settled together (`_settle_files`) before
        any is written; then the documents new here; then the shas."""
        if self.documents is None:
            return
        self._today = timezone.localdate()
        here = list(_documents())
        self._by_key = {document.key: document for document in here}
        self._by_content = _contents(here)
        self._claimed: set[int] = set()
        docs, skipped = self._read(ctx)
        if self.replacing:
            self._adopt(docs, here)
        for doc in docs:
            if doc.found is None and doc.unknown:
                skipped.append((doc.position, f"{doc.title} : {doc.unknown}"))
        # In the archive's order, whichever step found them.
        for _position, text in sorted(skipped, key=lambda item: item[0]):
            report.skip(text)

        plans = [self._plan(doc, doc.found) for doc in docs if doc.found is not None]
        taken = self._settle_files(plans, here)
        shas: dict[int, str] = {}
        for plan in plans:
            self._write(plan)
            if plan.kind in ("merge", "replace") and plan.changes_sha:
                shas[plan.document.pk] = plan.sha
        moments = []
        created_by_content = 0
        for doc in docs:
            if doc.found is not None or doc.unknown:
                continue
            document = self._create(doc, taken)
            if document is None:
                continue
            moments.append((document, doc.stamp))
            if doc.content is not None:
                created_by_content += 1
            sha = self._created_sha(doc)
            if sha:
                shas[document.pk] = sha
        restore_moments(moments, "created_at")
        self._write_shas(shas, here)
        if created_by_content and self._documents_before:
            report.note(LEGACY_NOTE)

    def _read(self, ctx) -> tuple[list[_Doc], list[tuple[int, str]]]:
        """Every record parsed, keyed, matched to the document here it
        answers to - by its key, else, for a record with none, by its
        content among the documents no record has matched yet - and its
        lines resolved. Returns the records that can go on, and the skips
        said so far with their record's rank.

        **Keys first, across every record; content after.** A survivor of
        two identical legacy documents, the earlier deleted here, keeps its
        key legacy(c, 1) while its content now ranks it (c, 0): matched
        record by record, the archive's (c, 0) took it by content before
        the (c, 1) record could claim it by its key, and the deleted twin
        was never created again. Keys are unique here and in the archive
        (`seen_keys`), so no two records claim one document by key."""
        recipes, articles = ctx.recipes(), ctx.articles()
        docs: list[_Doc] = []
        skipped: list[tuple[int, str]] = []
        seen_keys: set[str] = set()
        occurrences: dict[str, int] = defaultdict(int)
        for position, record in enumerate(self.documents or [], start=1):
            try:
                doc = self._parse(position, record, occurrences)
            except Skip as exc:
                skipped.append((position, f"bon de vente n° {position} de l'archive : {exc}"))
                continue
            # Created twice, the second failed the WHOLE run on the unique
            # index (transfer critique 5).
            if doc.key in seen_keys:
                skipped.append((position, f"{doc.title} : en double dans l'archive"))
                continue
            seen_keys.add(doc.key)
            found = self._by_key.get(doc.key)
            if found is not None:
                self._claim(found)
                doc.found = doc.document = found
            if doc.lines is not None:
                try:
                    doc.targets = [_target(line, recipes, articles) for line in doc.lines]
                except Skip as exc:
                    doc.unknown = str(exc)
            docs.append(doc)
        for doc in docs:
            if doc.found is not None or doc.content is None:
                continue
            candidate = self._by_content.get(doc.content)
            if candidate is not None and candidate.pk not in self._claimed:
                self._claim(candidate)
                doc.found = doc.document = candidate
        return docs, skipped

    def _claim(self, document: SaleDocument) -> None:
        self._claimed.add(document.pk)
        self.kept.add(document.pk)

    def _protect(self, key: str, content) -> None:
        """A record skipped after its key could be read: the document here
        it answers to stays - the prune leaves this database's copy alone."""
        document = self._by_key.get(key)
        if document is None and content is not None:
            candidate = self._by_content.get(content)
            if candidate is not None and candidate.pk not in self._claimed:
                document = candidate
        if document is not None:
            self.kept.add(document.pk)

    def _parse(self, position: int, record, occurrences: dict[str, int]) -> _Doc:
        """The record checked whole, nothing resolved or written: its key,
        its fields (the legacy ones as ever, the others only when said, each
        within the bounds the codec does not apply), its lines, its file and
        its links. Skip with the reason; a key read before the failure keeps
        its document here from the prune."""
        if not isinstance(record, dict):
            raise Skip("illisible")
        codec.note_unknown(self._report, record, DOCUMENT_KEYS, "bons de vente : ")
        record = self._type_code_cut(record)
        key = None
        if "key" in record:
            key = record["key"]
            if not isinstance(key, str) or not KEY_SHAPE.fullmatch(key):
                raise Skip("clé illisible")
        content = None
        try:
            values = {
                "reference": codec.load(SaleDocument, "reference", record.get("reference", "")),
                "sold_on": codec.load(SaleDocument, "sold_on", record.get("sold_on")),
                "note": codec.load(SaleDocument, "note", record.get("note", "")),
            }
            _check_day("sold_on", values["sold_on"], self._today)
            stamp = (
                codec.load(SaleDocument, "created_at", record["created_at"])
                if record.get("created_at") is not None
                else None
            )
            lines = self._lines(record, keyless=key is None)
        except (Skip, codec.FieldValueError) as exc:
            if key is not None:
                self._protect(key, None)
            raise Skip(str(exc)) from None
        if key is None:
            # An archive written before recipes 0019: the key its content
            # gave it there, which the migration gave its document here.
            print_ = fingerprint(
                values["reference"], values["sold_on"], values["note"], [line.shape for line in lines or []]
            )
            content = (print_, occurrences[print_])
            occurrences[print_] += 1
            key = legacy_key(*content)
        try:
            values.update(self._fields(record))
            ref = self._ref(record)
        except (Skip, codec.FieldValueError, FileRefused) as exc:
            self._protect(key, content)
            raise Skip(str(exc)) from None
        data = {
            "reference": record.get("reference", ""),
            "sold_on": record.get("sold_on"),
            "note": record.get("note", ""),
            **{name: record[name] for name in COMPARED if name not in LEGACY_FIELDS and name in record},
        }
        if stamp is not None:
            data["created_at"] = record["created_at"]
        return _Doc(
            position=position,
            key=key,
            content=content,
            title=_title(values["sold_on"], values["reference"]),
            data=data,
            values=values,
            stamp=stamp,
            lines=lines,
            ref=ref,
            payments=record.get("payments"),
        )

    def _type_code_cut(self, record: dict) -> dict:
        """`record` with a type code wider than its column cut the way the
        reader cuts it (recipes.sale_files.document_from_reading), and said:
        the codec refuses a value wider than its field, and a document
        stored before that cut would otherwise never come back. Cut here,
        before anything reads the record, so what is compared and written
        is the cut code."""
        code = record.get("einvoice_type_code")
        width = SaleDocument._meta.get_field("einvoice_type_code").max_length
        if not isinstance(code, str) or len(code) <= width:
            return record
        self._report.note(f"bons de vente : code de type « {code} » coupé à {width} caractères")
        return {**record, "einvoice_type_code": code[:width]}

    def _fields(self, record: dict) -> dict:
        """The fields since the sales invoices that the record says, loaded,
        and held to what the codec does not check (transfer critique 12).

        **What « Lire la facture » stores is taken as it is.** The electronic
        invoice's own dates (`einvoice_issued_on`, `einvoice_delivered_on`)
        are stored as the invoice states them, a 0001-01-01 beside a real
        delivery date included: informational, read by no window, they are
        never a reason to skip the document - skipped, a safety archive lost
        the invoice, its lines, its file and its links for good. A type code
        wider than its column came cut (`_type_code_cut`)."""
        values = {
            name: codec.load(SaleDocument, name, record[name])
            for name in DOCUMENT_FIELDS
            if name not in ("key", *LEGACY_FIELDS) and name in record
        }
        siren = values.get("seller_siren")
        if siren and not SIREN_SHAPE.fullmatch(siren):
            raise codec.FieldValueError(f"SIREN du vendeur illisible (« {siren} »)")
        sha = values.get("source_sha256")
        if sha and not SHA_SHAPE.fullmatch(sha):
            raise codec.FieldValueError("empreinte du fichier illisible")
        if "einvoice_checks" in values and not _readable_checks(values["einvoice_checks"]):
            raise codec.FieldValueError("contrôles illisibles")
        rate = values.get("adjustment_vat_rate")
        if rate is not None and not Decimal("0") <= rate <= Decimal("1"):
            raise codec.FieldValueError(f"« {FIELD_LABELS['adjustment_vat_rate']} » : taux hors 0 à 100 %")
        return values

    def _lines(self, record: dict, *, keyless: bool) -> list[_Line] | None:
        if "lines" not in record:
            return None
        items = record["lines"]
        if not isinstance(items, list):
            raise Skip("lignes illisibles")
        return [self._line(number, item, keyless=keyless) for number, item in enumerate(items, start=1)]

    def _line(self, number: int, item, *, keyless: bool) -> _Line:
        if not isinstance(item, dict):
            raise Skip(f"ligne n° {number} illisible")
        codec.note_unknown(self._report, item, LINE_KEYS, "lignes de bons de vente : ")
        recipe, article = item.get("recipe"), item.get("article")
        for name, what in ((recipe, "recette"), (article, "article")):
            if name is not None and not isinstance(name, str):
                raise Skip(f"ligne n° {number} : {what} illisible")
        if recipe is not None and article is not None:
            raise Skip(f"ligne n° {number} : une recette ou un article, pas les deux")
        tied = recipe is not None or article is not None
        if keyless and not tied:
            # No archive written before the sales invoices carries one: only
            # a hand edit makes it, and it has no content key.
            raise Skip(f"ligne n° {number} : une recette ou un article")
        data = {name: item.get(name) for name in LEGACY_LINE_FIELDS}
        data.update({name: item[name] for name in LINE_FIELDS if name not in LEGACY_LINE_FIELDS and name in item})
        try:
            values = {name: codec.load(SaleDocumentLine, name, value) for name, value in data.items()}
        except codec.FieldValueError as exc:
            raise Skip(f"ligne n° {number} : {exc}") from None
        problem = _line_problem(tied, values)
        if problem:
            raise Skip(f"ligne n° {number} : {problem}")
        return _Line(recipe=recipe, article=article, data=data, values=values)

    def _ref(self, record: dict):
        """The record's file: NOT_SAID, None (none), a _Missing or a ref
        checked against the archive - in the preview too."""
        if "file" not in record:
            return NOT_SAID
        ref = record["file"]
        if ref is None:
            return None
        if not isinstance(ref, dict) or not isinstance(ref.get("name"), str):
            raise codec.FieldValueError("fichier illisible")
        if ref.get("missing"):
            return _Missing(ref["name"])
        if not isinstance(ref.get("sha256"), str):
            # Only a hand edit leaves it out, and the archive takes such a
            # ref (`has_file` reads the declared sha): but a document holds
            # its file's sha (the one-file rule, the adoption), and read as
            # missing it was a KeyError failing the whole run.
            raise codec.FieldValueError("fichier illisible")
        if not ref["name"].startswith(FOLDER):
            # Accepted by the archive (invoices/, receipts/, consignes/), but
            # not this section's: a sale document would take an invoice's or
            # a slip's file over.
            raise FileRefused(f"fichier hors du dossier des ventes (« {ref['name'][:80]} »)")
        self._ctx.check_file(ref, ref["name"])
        return ref

    def _adopt(self, docs: list[_Doc], here: list[SaleDocument]) -> None:
        """Under « Remplacer », a record found nowhere here whose file - its
        ref's sha, else its own figure - a document here holds that no
        record names (one the prune would delete) takes that document over:
        its key becomes the record's, and the record replaces it as one
        found here. Deleted, the same file read again under a new key, then
        yesterday's archive restored: the record was skipped (« fichier
        déjà … ») and the prune deleted the copy - the invoice in neither
        (transfer critique 4)."""
        holders = {
            document.source_sha256: document
            for document in here
            if document.source_sha256 and document.pk not in self.kept
        }
        for doc in docs:
            if doc.found is not None:
                continue
            sha = doc.ref["sha256"] if isinstance(doc.ref, dict) else doc.values.get("source_sha256", "")
            holder = holders.pop(sha, None) if sha else None
            if holder is not None:
                self._claim(holder)
                doc.found = doc.document = holder
                doc.adopted = True

    def _file_state(self, ref, stored) -> str:
        """same | fill | differs, for the file of a document found here."""
        if ref is NOT_SAID or isinstance(ref, _Missing):
            return "same"
        name = stored.name if stored else ""
        if ref is None:
            return "differs" if name else "same"
        if not _stored(name):
            return "fill"
        return "same" if self._ctx.file_matches(ref, name) else "differs"

    def _plan(self, doc: _Doc, document: SaleDocument) -> _Plan:
        """What the record does to the document here: compared on what it
        says - the fields, the lines by rank, the file -, nothing written."""
        here_lines = list(document.lines.all())
        different = codec.differences(document, doc.data, [name for name in COMPARED if name in doc.data])
        line_changes: list[bool] = []
        if doc.lines is not None:
            for rank, line in enumerate(doc.lines):
                if rank >= len(here_lines) or doc.targets is None:
                    line_changes.append(True)
                    continue
                here, target = here_lines[rank], doc.targets[rank]
                line_changes.append(
                    here.recipe_id != _pk(target["recipe"])
                    or here.stock_type_id != _pk(target["stock_type"])
                    or bool(codec.differences(here, line.data, list(line.data)))
                )
            if len(doc.lines) != len(here_lines) or any(line_changes):
                different.append("lines")
        plan = _Plan(
            doc=doc,
            document=document,
            here_lines=here_lines,
            different=different,
            line_changes=line_changes,
            file_state=self._file_state(doc.ref, document.source_file),
            sha=document.source_sha256,
        )
        if self.replacing:
            self._plan_replace(plan)
        else:
            self._plan_merge(plan)
        return plan

    def _plan_merge(self, plan: _Plan) -> None:
        """« Fusionner » adds what is missing and never changes a value: a
        FILLABLE field blank here takes the archive's, and a file this
        database has not got or lost from the disk is restored - even beside
        a conflict, as the invoices do (transfer critique 7)."""
        doc, document = plan.doc, plan.document
        plan.fills = [
            name
            for name in plan.different
            if name in FILLABLE and codec.is_blank(getattr(document, name)) and not codec.is_blank(doc.values.get(name))
        ]
        plan.conflicts = [name for name in plan.different if name not in plan.fills]
        if plan.file_state == "differs":
            plan.conflicts.append("file")
        elif plan.file_state == "fill":
            refused = self._store_file(plan)
            if refused:
                self._report.skip(f"{doc.title} : {refused}")
        plan.kind = "merge" if plan.fills or plan.store or plan.conflicts else "unchanged"

    def _plan_replace(self, plan: _Plan) -> None:
        """« Remplacer » makes the document the archive's - unless nothing
        differs, then nothing is written: its own export changes nothing
        (`db_fingerprint` hashes the pks). A line naming what this database
        has not got, or a line its constraints would refuse once assigned,
        keeps the whole document as it is."""
        doc = plan.doc
        if not plan.different and plan.file_state == "same" and not doc.adopted:
            plan.kind = "unchanged"
            return
        if doc.unknown:
            plan.kind, plan.reason = "kept", doc.unknown
            return
        problem = self._lines_problem(plan)
        if problem:
            plan.kind, plan.reason = "kept", problem
            return
        plan.kind = "replace"
        if plan.file_state in ("fill", "differs") and isinstance(doc.ref, dict):
            refused = self._store_file(plan)
            if refused:
                plan.kind, plan.reason = "kept", refused
        elif plan.file_state == "differs":
            # The archive says it has no file.
            plan.remove, plan.sha = True, ""

    def _lines_problem(self, plan: _Plan) -> str:
        """Why one of the document's lines, the archive's assigned over the
        one of its rank here, could not be stored; "" when none."""
        doc = plan.doc
        for rank, (line, target) in enumerate(zip(doc.lines or [], doc.targets or [], strict=False)):
            here = plan.here_lines[rank] if rank < len(plan.here_lines) else None
            if here is not None and not plan.line_changes[rank]:
                continue
            values = {
                name: line.values[name] if name in line.values else getattr(here, name, default)
                for name, default in LINE_DEFAULTS.items()
            }
            if _clears_consumed(line, here, target):
                values["consumed_quantity"] = None
            problem = _line_problem(target["recipe"] is not None or target["stock_type"] is not None, values)
            if problem:
                return f"ligne n° {rank + 1} : {problem}"
        return ""

    def _store_file(self, plan: _Plan) -> str:
        """The record's file stored for the document (written by the confirm
        only); "" when it is, else why not - the document then keeps its
        own."""
        before = len(self._ctx.stored_files)
        try:
            plan.stored = self._ctx.save_file(plan.doc.ref, plan.doc.ref["name"])
        except FileRefused as exc:
            return str(exc)
        plan.written = len(self._ctx.stored_files) > before
        plan.store, plan.sha = True, plan.doc.ref["sha256"]
        return ""

    def _unstore(self, plan: _Plan) -> None:
        """A file this run stored for a plan that gave way: nothing names it."""
        if plan.written and not self._ctx.preview and plan.stored in self._ctx.stored_files:
            default_storage.delete(plan.stored)
            self._ctx.stored_files.remove(plan.stored)
        plan.store = plan.written = False
        plan.stored, plan.sha = "", plan.document.source_sha256

    def _settle_files(self, plans: list[_Plan], here: list[SaleDocument]) -> dict[str, str]:
        """Which document ends with which file - a sha the database holds
        once (`saledocument_one_per_file`). A document here no record names
        keeps its own (under « Remplacer », only one the prune keeps: the
        others give theirs up); so does a plan that keeps its file. A plan
        moving to a file another document ends with gives way - under
        « Fusionner » that file is not filled, under « Remplacer » the
        document is kept as it is - until no two want one, so a document
        giving way, keeping its own file, can make another give way in turn.
        Returns sha → the document that ends with it, as a sentence names it,
        for the documents created after."""
        fixed = {
            document.source_sha256: _lower(_title(document.sold_on, document.reference))
            for document in here
            if document.source_sha256
            and document.pk not in self._claimed
            and (not self.replacing or document.pk in self.kept)
        }
        while True:
            taken = dict(fixed)
            for plan in plans:
                if not plan.changes_sha and plan.sha:
                    taken[plan.sha] = _lower(plan.final_title)
            losers = []
            for plan in plans:
                if plan.changes_sha and plan.sha:
                    if plan.sha in taken:
                        losers.append((plan, taken[plan.sha]))
                    else:
                        taken[plan.sha] = _lower(plan.final_title)
            if not losers:
                return taken
            for plan, holder in losers:
                self._unstore(plan)
                if plan.kind == "merge":
                    plan.conflicts.append("file")
                else:
                    plan.kind, plan.reason = "kept", f"fichier déjà celui du {holder}"

    def _write(self, plan: _Plan) -> None:
        if plan.kind == "unchanged":
            self._unchanged(plan.document, plan.here_lines)
            self._applied.append(plan.doc)
        elif plan.kind == "kept":
            self._report.skip(f"{plan.doc.title} : {plan.reason} — gardé tel quel")
        elif plan.kind == "merge":
            self._merge(plan)
        else:
            self._replace(plan)
        self._parsed.append(plan.doc)

    def _unchanged(self, document: SaleDocument, here_lines: list) -> None:
        """« inchangés », every row: the document, its lines and its file."""
        self._report.unchanged(DOCUMENTS)
        if here_lines:
            self._report.unchanged(LINES, len(here_lines))
        if document.source_file:
            self._report.unchanged(FILES)

    def _merge(self, plan: _Plan) -> None:
        """Kept as it is, and what differs said - but given back a blank
        here and a file it lost. Counted as the invoices count it: updated
        when filled, its kept file and, merged without a conflict, its lines
        « inchangés »; kept whole by a conflict, it takes no link."""
        report, doc, document = self._report, plan.doc, plan.document
        changed = codec.assign(document, doc.data, plan.fills) if plan.fills else []
        if plan.store:
            document.source_file = plan.stored
            changed.append("source_file")
        if changed:
            document.save(update_fields=changed)
            report.updated(DOCUMENTS)
            if plan.store:
                report.created(FILES)
                report.note(RESTORED_NOTE.format(document=doc.title))
            elif document.source_file:
                report.unchanged(FILES)
            if not plan.conflicts and plan.here_lines:
                report.unchanged(LINES, len(plan.here_lines))
        if plan.conflicts:
            report.conflict(f"{doc.title} : différent dans l'archive ({_fields(plan.conflicts)}) — gardé tel quel")
            return
        if not changed:
            self._unchanged(document, plan.here_lines)
        self._applied.append(doc)

    def _replace(self, plan: _Plan) -> None:
        """The document becomes the archive's: every field it says, the
        moment included (never compared), its key when adopted, its file,
        and its lines paired by rank and updated in place."""
        report, doc, document = self._report, plan.doc, plan.document
        names = [name for name in COMPARED if name in doc.data]
        if "created_at" in doc.data:
            names.append("created_at")
        changed = codec.assign(document, doc.data, names)
        if doc.adopted:
            document.key = doc.key
            changed.append("key")
        old = document.source_file.name if document.source_file else ""
        if plan.store:
            document.source_file = plan.stored
            changed.append("source_file")
        elif plan.remove:
            document.source_file = None
            changed.append("source_file")
        if changed:
            document.save(update_fields=list(dict.fromkeys(changed)))
        report.updated(DOCUMENTS)
        if plan.store:
            (report.created if plan.file_state == "fill" else report.updated)(FILES)
            if old and old != plan.stored:
                self._ctx.delete_file_on_commit(old)
        elif plan.remove:
            report.deleted(FILES)
            self._ctx.delete_file_on_commit(old)
        elif document.source_file:
            report.unchanged(FILES)
        if isinstance(doc.ref, _Missing):
            note = MISSING_FILE_KEPT_NOTE if document.source_file else MISSING_FILE_NOTE
            report.note(note.format(document=doc.title))
        self._replace_lines(plan)
        self._applied.append(doc)

    def _replace_lines(self, plan: _Plan) -> None:
        """Paired by rank: a line unchanged stays as it is, a changed one is
        updated in place with what the archive says of it (an old line's
        label or rate, not said, kept), one too many here goes, one more is
        created. Lines not said are kept."""
        doc, document, report = plan.doc, plan.document, self._report
        if doc.lines is None:
            return
        created = []
        for rank, (line, target) in enumerate(zip(doc.lines, doc.targets or [], strict=False)):
            if rank >= len(plan.here_lines):
                created.append(SaleDocumentLine(document=document, **target, **line.values))
                continue
            if not plan.line_changes[rank]:
                report.unchanged(LINES)
                continue
            here = plan.here_lines[rank]
            clears = _clears_consumed(line, here, target)
            codec.assign(here, line.data, list(line.data))
            if clears:
                here.consumed_quantity = None
                report.note(CONSUMED_CLEARED_NOTE.format(title=doc.title, number=rank + 1))
            here.recipe, here.stock_type = target["recipe"], target["stock_type"]
            here.save()
            report.updated(LINES)
        extra = plan.here_lines[len(doc.lines) :]
        if extra:
            SaleDocumentLine.objects.filter(pk__in=[line.pk for line in extra]).delete()
            report.deleted(LINES, len(extra))
        if created:
            SaleDocumentLine.objects.bulk_create(created)
            report.created(LINES, len(created))

    @staticmethod
    def _created_sha(doc: _Doc) -> str:
        """The sha of the file a new document stores: its ref's - or, for a
        file missing from the disk at export, the record's own figure, so
        the same file read again is known (transfer critique 6)."""
        if isinstance(doc.ref, dict):
            return doc.ref["sha256"]
        if isinstance(doc.ref, _Missing):
            return doc.values.get("source_sha256", "")
        return ""

    def _create(self, doc: _Doc, taken: dict[str, str]) -> SaleDocument | None:
        """A document new here, as the archive has it, under its key - but
        never a second holder of a file another document ends with."""
        report = self._report
        sha = self._created_sha(doc)
        if sha and sha in taken:
            report.skip(f"{doc.title} : fichier déjà celui du {taken[sha]}, gardé tel quel")
            return None
        stored = None
        if isinstance(doc.ref, dict):
            try:
                stored = self._ctx.save_file(doc.ref, doc.ref["name"])
            except FileRefused as exc:
                report.skip(f"{doc.title} : {exc}")
                return None
        values = {name: value for name, value in doc.values.items() if name != "source_sha256"}
        # Its sha is set with the others, after every file is placed.
        document = SaleDocument.objects.create(key=doc.key, source_file=stored, **values)
        lines = [
            SaleDocumentLine(document=document, **target, **line.values)
            for line, target in zip(doc.lines or [], doc.targets or [], strict=False)
        ]
        if lines:
            SaleDocumentLine.objects.bulk_create(lines)
        report.created(DOCUMENTS)
        if lines:
            report.created(LINES, len(lines))
        if stored:
            report.created(FILES)
        if isinstance(doc.ref, _Missing):
            report.note(MISSING_FILE_NOTE.format(document=doc.title))
        if sha:
            taken[sha] = _lower(doc.title)
        self._claim(document)
        doc.document = document
        self._applied.append(doc)
        self._parsed.append(doc)
        return document

    def _write_shas(self, shas: dict[int, str], here: list[SaleDocument]) -> None:
        """Each document's sha is its stored file's, written once every file
        is placed, in two steps inside the section's transaction: every
        document whose sha changes - and, under « Remplacer », every one the
        prune deletes that holds a sha a document wants - is given "", then
        the new shas are set. Written as each file was stored, a swap of two
        files between two documents met `saledocument_one_per_file` half way
        and failed the WHOLE import (transfer critique 4)."""
        if not shas:
            return
        blank = set(shas)
        if self.replacing:
            wanted = {sha for sha in shas.values() if sha}
            blank |= {
                document.pk for document in here if document.pk not in self.kept and document.source_sha256 in wanted
            }
        for batch in _batches(sorted(blank)):
            SaleDocument.objects.filter(pk__in=batch).update(source_sha256="")
        rows = [SaleDocument(pk=pk, source_sha256=sha) for pk, sha in shas.items() if sha]
        if rows:
            SaleDocument.objects.bulk_update(rows, ["source_sha256"], batch_size=BATCH)

    # .. bank links ..................................................................
    def _links(self, ctx, report) -> None:
        """The credits paying each document, nested in its record by their
        fingerprint - made only on a document this run applied, never on a
        record skipped nor on a document kept whole by a conflict
        (transfer critique 14)."""
        said = [doc for doc in self._applied if doc.payments is not None]
        if not said:
            return
        # What the archive says of each credit, every record answering to a
        # document here counted, applied or kept whole.
        given: dict[str, set[int]] = defaultdict(set)
        for doc in self._parsed:
            for item in doc.payments or []:
                if isinstance(item.get("transaction"), str) and doc.document is not None:
                    given[item["transaction"]].add(doc.document.pk)
        credits = self._credits(ctx, said)
        wanted = [(doc, self._wanted(doc, credits)) for doc in said]
        if self.replacing:
            self._replace_links(report, wanted)
        else:
            self._merge_links(report, wanted, given)

    def _credits(self, ctx, said: list[_Doc]) -> dict[str, BankTransaction]:
        """The lines the records name, by fingerprint, read at once - with
        « Banque » replaced in the same run, only those banque.json names."""
        names = sorted(
            {
                item["transaction"]
                for doc in said
                for item in doc.payments or []
                if isinstance(item.get("transaction"), str)
            }
        )
        lines = {}
        for batch in _batches(names):
            for line in BankTransaction.objects.filter(fingerprint__in=batch).only(
                "pk", "fingerprint", "settled_by_hand", "operation_date", "amount"
            ):
                lines[line.fingerprint] = line
        if ctx.replacing("banque"):
            named = _bank_fingerprints(ctx)
            lines = {fingerprint_: line for fingerprint_, line in lines.items() if fingerprint_ in named}
        return lines

    def _wanted(self, doc: _Doc, credits: dict[str, BankTransaction]) -> list[tuple]:
        """The record's links that can be made here: (credit, method, moment),
        each other one skipped with its reason."""
        report, found, seen = self._report, [], set()
        for item in doc.payments or []:
            codec.note_unknown(report, item, PAYMENT_KEYS, "règlements des bons de vente : ")
            name = item.get("transaction")
            if not isinstance(name, str) or not name:
                report.skip(f"{doc.title} : règlement illisible")
                continue
            line = credits.get(name)
            if line is None:
                report.skip(f"{doc.title} : entrée absente{_linked_on(item)}")
                continue
            if line.amount <= 0:
                report.skip(f"{doc.title} : l'opération du {_day(line.operation_date)} n'est pas une entrée d'argent")
                continue
            try:
                method = codec.load(SaleDocumentPayment, "method", item.get("method"))
                moment = (
                    codec.load(SaleDocumentPayment, "created_at", item["created_at"])
                    if item.get("created_at") is not None
                    else None
                )
            except codec.FieldValueError as exc:
                report.skip(f"{doc.title} : règlement de l'entrée du {_day(line.operation_date)} : {exc}")
                continue
            if line.pk in seen:
                report.skip(
                    f"{doc.title} : règlement de l'entrée du {_day(line.operation_date)} en double dans l'archive"
                )
                continue
            seen.add(line.pk)
            found.append((line, method, moment))
        return found

    def _merge_links(self, report, wanted, given: dict[str, set[int]]) -> None:
        """A link the document here has not got is added, unless the credit
        says otherwise here (the mirror of the bank's own `_merge_payments`,
        by the pair): it pays another document the archive does not give
        it, or a person settled it by hand without this document - while
        both were here before the run - which is how « Délier » leaves it."""
        current = list(SaleDocumentPayment.objects.select_related("document"))
        by_pair = {(payment.transaction_id, payment.document_id): payment for payment in current}
        by_credit: dict[int, list] = defaultdict(list)
        for payment in current:
            by_credit[payment.transaction_id].append(payment)
        created, undone = [], False
        for doc, links in wanted:
            document = doc.document
            for line, method, moment in links:
                held = by_pair.get((line.pk, document.pk))
                if held is not None:
                    if held.method == method:
                        report.unchanged(LINKS)
                    else:
                        report.conflict(
                            f"{_entry(line)} : le lien vers {_named(document)} est « {held.get_method_display()} » "
                            f"ici, « {SaleDocumentPayment.Method(method).label} » dans l'archive — gardé tel quel"
                        )
                    continue
                others = [
                    payment
                    for payment in by_credit.get(line.pk, [])
                    if payment.document_id != document.pk and payment.document_id not in given.get(line.fingerprint, ())
                ]
                if others:
                    here = ", ".join(_named(payment.document) for payment in others)
                    report.conflict(
                        f"{_entry(line)} : règle ici {here}, dans l'archive {_named(document)} — gardée telle quelle"
                    )
                    continue
                if line.settled_by_hand and line.pk in self._lines_before and document.pk in self._documents_before:
                    report.conflict(
                        f"{_entry(line, amount=False)} : réglée à la main ici sans régler {_named(document)}, qu'elle "
                        "règle dans l'archive — gardée telle quelle"
                    )
                    undone = True
                    continue
                payment = SaleDocumentPayment(transaction=line, document=document, method=method)
                by_pair[(line.pk, document.pk)] = payment
                created.append((payment, moment))
        if undone:
            report.note(SALE_UNDONE_NOTE)
        self._create_links(report, created)

    def _replace_links(self, report, wanted) -> None:
        """The documents this run applied whose record says its links: their
        links become exactly the archive's, keyed by the pair - others
        deleted, a method changed updated, the missing ones created."""
        documents = sorted({doc.document.pk for doc, _links in wanted})
        existing = {}
        for batch in _batches(documents):
            for payment in SaleDocumentPayment.objects.filter(document_id__in=batch):
                existing[(payment.transaction_id, payment.document_id)] = payment
        pairs = {
            (line.pk, doc.document.pk): (line, doc.document, method, moment)
            for doc, links in wanted
            for line, method, moment in links
        }
        doomed = [payment.pk for pair, payment in existing.items() if pair not in pairs]
        if doomed:
            report.deleted(LINKS, delete_ids(SaleDocumentPayment, doomed))
        changed, created = [], []
        for pair, (line, document, method, moment) in pairs.items():
            payment = existing.get(pair)
            if payment is None:
                created.append((SaleDocumentPayment(transaction=line, document=document, method=method), moment))
            elif payment.method != method:
                payment.method = method
                changed.append(payment)
            else:
                report.unchanged(LINKS)
        if changed:
            SaleDocumentPayment.objects.bulk_update(changed, ["method"])
            report.updated(LINKS, len(changed))
        self._create_links(report, created)

    @staticmethod
    def _create_links(report, created) -> None:
        if not created:
            return
        SaleDocumentPayment.objects.bulk_create([payment for payment, _moment in created])
        restore_moments(created, "created_at")
        report.created(LINKS, len(created))

    def _settle(self, ctx) -> None:
        """What changed days make stale: the till products' totals, and the
        sales of the recipes they are linked to - through the links this
        database has now (« Liens » applied before, in the same run)."""
        touched = sorted(self.touched)
        ctx.dirty.pos_products.update(touched)
        for batch in _batches(touched):
            ctx.dirty.recipes.update(
                PosProduct.objects.filter(pk__in=batch, recipe__isnull=False).values_list("recipe_id", flat=True)
            )

    def prune(self, ctx, report) -> None:
        """What the file does not have goes: days, hand-typed sales,
        documents - with their lines, files and bank links -, and the till
        products left with no day and no link - pure data. A linked or
        ignored one stays: that is the links'. An archive saying nothing of
        the documents prunes none of them."""
        stale = [
            (pk, product_id, sold_on)
            for pk, product_id, sold_on in PosProductDailyQuantity.objects.values_list("pk", "product_id", "sold_on")
            if (product_id, sold_on) not in self.daily_keys
        ]
        days = _delete(PosProductDailyQuantity, [pk for pk, _product, _sold_on in stale])
        if days:
            report.deleted(QUANTITIES, days)
            self.touched.update(product_id for _pk, product_id, _sold_on in stale)
            lower_till_coverage(min(sold_on for _pk, _product, sold_on in stale), report)

        manual = _delete(
            RecipeSale,
            [
                pk
                for pk, recipe_id, sold_on in RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).values_list(
                    "pk", "recipe_id", "sold_on"
                )
                if (recipe_id, sold_on) not in self.manual_keys
            ],
        )
        if manual:
            report.deleted(MANUAL, manual)

        if self.documents is not None:
            doomed = [
                pk for pk in SaleDocument.objects.order_by("pk").values_list("pk", flat=True) if pk not in self.kept
            ]
            if doomed:
                names = []
                for batch in _batches(doomed):
                    names += [
                        name
                        for name in SaleDocument.objects.filter(pk__in=batch).values_list("source_file", flat=True)
                        if name
                    ]
                deleted: dict[str, int] = {}
                documents = delete_ids(SaleDocument, doomed, deleted)
                _say_documents_deleted(ctx, report, documents, deleted, names)

        orphans = list(
            PosProduct.objects.filter(recipe__isnull=True, ignored=False, daily_quantities__isnull=True).values_list(
                "pk", flat=True
            )
        )
        products = _delete(PosProduct, orphans)
        if products:
            report.deleted(PRODUCTS, products)
        self.touched.difference_update(orphans)

        # A day « Ventes » no longer holds keeps no payments: money for a
        # day with no sales is a figure the sales pages contradict (the
        # backfill's own rule). A day still here keeps its payments - the
        # archive has none to replace them with.
        with_sales = set(PosProductDailyQuantity.objects.values_list("sold_on", flat=True).distinct())
        payments = _delete(
            PosDailyPayment,
            [pk for pk, sold_on in PosDailyPayment.objects.values_list("pk", "sold_on") if sold_on not in with_sales],
        )
        if payments:
            report.deleted(PAYMENTS, payments)
            report.note(payments_note())
        self._settle(ctx)

    # -- clear ---------------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        """Every day, every sale per recipe (typed in or the till's), every
        document with its lines, file and bank links (the credits stay: they
        are « Banque »'s), every day's payments. The till products with no
        link go; a linked or ignored one is the links' and stays, at zero and
        with no day. The till's coverage goes back before the first day it
        held."""
        first = PosProductDailyQuantity.objects.aggregate(first=Min("sold_on"))["first"]
        days, _ = PosProductDailyQuantity.objects.all().delete()
        lower_till_coverage(first, report)
        payments, _ = PosDailyPayment.objects.all().delete()
        if payments:
            report.note(payments_note())
        manual = RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).count()
        _total, per_model = RecipeSale.objects.all().delete()
        till_sales = per_model.get(RecipeSale._meta.label, 0) - manual
        names = [name for name in SaleDocument.objects.values_list("source_file", flat=True) if name]
        _total, documents = SaleDocument.objects.all().delete()
        _total, per_model = PosProduct.objects.filter(recipe__isnull=True, ignored=False).delete()
        products = per_model.get(PosProduct._meta.label, 0)
        PosProduct.objects.exclude(total_quantity=0, first_seen=None, last_seen=None).update(
            total_quantity=0, first_seen=None, last_seen=None
        )
        for what, n in (
            (QUANTITIES, days),
            (PRODUCTS, products),
            (MANUAL, manual),
            (RECIPE_SALES, till_sales),
        ):
            if n:
                report.deleted(what, n)
        _say_documents_deleted(ctx, report, documents.get(SaleDocument._meta.label, 0), documents, names)
        if payments:
            report.deleted(PAYMENTS, payments)
