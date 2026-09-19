"""« Factures et tickets » (§7.6): every document with its lines, what was
read on it and checked, its two files (the PDF or photo, and the review
preview), and the identity of every product its lines use.

**Rows are written, nothing is read again.** Going back through
`import_receipt` / `import_document` would run the OCR, learn identifiers
and record a « premier document » for every supplier whose first document
came back - thirty amber « À voir » on the suppliers' tab. An import sets
the rows as the archive has them and leaves the derived data to the
rebuild: each line's PURCHASE movement (`dirty.lines`), and an invoice's
status only where the archive's classification of a product disagrees
with this database's (`dirty.status_products`) - the status is otherwise
copied, since it also carries a charge's checks and ERROR, which no rule
rebuilds.

**Files** are compared by size, then by sha256 only when the sizes are
equal (§6.4), and written under the same relative name when it is free,
an available one otherwise - never over another document's file. A file
replaced or removed goes on commit, and only if no row still names it.

**A stock take's trail is never broken.** A line a count was priced from
(`StockTakeLineSource`, PROTECT) is updated in place, never deleted: an
invoice whose replacement would remove one - or, lines being paired by
rank, turn it into another product's or another name's line - is kept as
it is, and said - the rule of `replace_invoice_lines` and `delete_invoice`.
But not for a count this same run deletes (an « Inventaires » « Remplacer »
whose archive does not name it, `stock_takes.named_takes`): the invoices
apply before that prune, and a line only such a count holds goes in this
section's prune, after it.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.utils import timezone

from transfer import codec, keys, registry
from transfer.archive import ArchiveError
from transfer.sections.base import FileRefused, Section
from transfer.sections.suppliers import day, plural, said

KEY = "factures"

INVOICE_FIELDS = (
    "invoice_number",
    "invoice_date",
    "status",
    "error_message",
    "supplier_doubt",
    "imported_at",
    "reconciliation_adjustment",
    "printed_total_ttc",
    "ocr_text",
    "source_text",
    "ocr_confidence",
    "parse_checks",
    "vat_breakdown",
    "vat_table_typed",
    "reviewed_at",
    "source_sha256",
)
FILE_FIELDS = ("source_file", "preview_image")
LINE_FIELDS = (
    "raw_name",
    "quantity",
    "colisage",
    "total_volume",
    "unit_cost_ht",
    "total_ht",
    "taxes",
    "discount",
    "vat_rate",
    "category",
    "read_as",
    "printed_ttc",
    "discount_ttc",
)
PRODUCT_FIELDS = ("raw_name", "ean", "is_expense")
#: Every concrete field of Invoice and InvoiceLine is in INVOICE_FIELDS,
#: FILE_FIELDS or LINE_FIELDS, or here with why - a guard test holds it, so
#: a field added later cannot be left out of the archive in silence.
NOT_EXPORTED = {"id": "pk", "supplier": "by key", "product": "by key", "invoice": "parent"}

#: What « inchangé » compares: never when it was imported (§6.4) - merging
#: an export of the same data taken a minute later must change nothing.
COMPARED = tuple(name for name in INVOICE_FIELDS if name != "imported_at")
KNOWN = ("key", "supplier", *INVOICE_FIELDS, *FILE_FIELDS, "lines")
LINE_KNOWN = ("product", *LINE_FIELDS)
#: `created_at` is written only on a product this import creates (§4.3,
#: §6.4): never compared, so an archive taken later is still « inchangé ».
PRODUCT_KNOWN = ("supplier", *PRODUCT_FIELDS, "created_at", "classified")

BATCH = 500
SIZE_CACHE_SECONDS = 60

LABELS = {
    "supplier": "fournisseur",
    "invoice_number": "numéro",
    "invoice_date": "date",
    "status": "statut",
    "error_message": "erreur",
    "supplier_doubt": "fournisseur à confirmer",
    "reconciliation_adjustment": "ajustement",
    "printed_total_ttc": "total imprimé",
    "ocr_text": "texte lu",
    "source_text": "texte du document",
    "ocr_confidence": "confiance de la lecture",
    "parse_checks": "contrôles",
    "vat_breakdown": "table de TVA",
    "vat_table_typed": "table de TVA saisie",
    "reviewed_at": "vérification",
    "source_sha256": "empreinte",
    "lines": "lignes",
    "source_file": "fichier",
    "preview_image": "photo",
}
FILE_LABELS = {"source_file": "fichier", "preview_image": "photo"}
RESTORED = {
    ("source_file",): "fichier restauré",
    ("preview_image",): "photo restaurée",
    ("source_file", "preview_image"): "fichier et photo restaurés",
}

#: Worded the same in a preview and in the report of what was done: a
#: preview and its confirm on the same data have one outcome, word for word
#: (RunReport.outcome), which is what tells a stale preview apart. Both
#: sections, and in ONE import: a payment names its document, so « Banque »
#: imported alone from the backup restored none of them; imported after the
#: invoices in a second run, it cannot tell a line whose payment went with
#: its invoice from one a person unlinked, and reports it as a conflict
#: (bank.UNDONE_NOTE) instead of linking it again.
BANK_NOTE = {
    False: "1 paiement de la banque est supprimé avec sa facture ; réimportez ensemble « Factures et tickets » "
    "et « Banque » de la sauvegarde pour le retrouver",
    True: "{count} paiements de la banque sont supprimés avec leurs factures ; réimportez ensemble « Factures et "
    "tickets » et « Banque » de la sauvegarde pour les retrouver",
}


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
class _Doc:
    """One record of the archive, checked - nothing written yet."""

    record: dict
    key: dict
    supplier: object
    label: str
    values: dict
    refs: dict
    # [(product key, loaded values, raw line record)], or None: not said.
    lines: list | None
    # The invoice here its key answers to, or None: to create.
    found: object = None


def describe(supplier_name: str, number: str, invoice_date) -> str:
    if number:
        return f"Facture {supplier_name} n° {number}"
    if invoice_date:
        return f"Document {supplier_name} du {day(invoice_date)}"
    return f"Document {supplier_name} sans numéro ni date"


def describe_invoice(invoice) -> str:
    return describe(invoice.supplier.name, invoice.invoice_number, invoice.invoice_date)


def _chunks(items: list, size: int = BATCH):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _canonical(key: dict) -> tuple:
    return tuple(key.get(name) for name in keys.KEY_FIELDS)


def _clean_key(key) -> dict | None:
    """An InvoiceKey as the archive gives it, checked field by field."""
    if not isinstance(key, dict) or not isinstance(key.get("supplier"), str) or not key["supplier"]:
        return None
    clean = {"supplier": key["supplier"]}
    for name in ("number", "sha256", "file_sha256"):
        value = key.get(name, "")
        if not isinstance(value, str):
            return None
        clean[name] = value
    occurrence = key.get("occurrence", 0)
    if isinstance(occurrence, bool) or not isinstance(occurrence, int) or occurrence < 0:
        return None
    clean["occurrence"] = occurrence
    return clean


def _readable_checks(checks) -> bool:
    """Checks as the application writes them ({"label", "passed",
    "detail"}) - what the review queue and page read of each one. Stored as
    anything else, one record took « À vérifier » down: `failed_checks`
    asks every check `.get("passed")`."""
    return isinstance(checks, list) and all(
        isinstance(check, dict)
        and isinstance(check.get("label"), str)
        and isinstance(check.get("passed"), bool)
        and isinstance(check.get("detail", ""), str)
        for check in checks
    )


def _readable_vat_table(rows) -> bool:
    """Rows as the review page writes them and `receipts.vat_table` reads
    them: [rate, base, tax], three numbers written as text. A row it cannot
    read it leaves out without a word, and the checks built on the table
    would then be answered against part of it."""
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, list) or len(row) != 3 or not all(isinstance(value, str) for value in row):
            return False
        try:
            if not all(Decimal(value).is_finite() for value in row):
                return False
        except (ArithmeticError, ValueError):
            return False
    return True


def _stored(name: str) -> bool:
    """Whether storage holds this name - False for one it refuses."""
    try:
        return bool(name) and default_storage.exists(name)
    except (SuspiciousFileOperation, OSError, ValueError):
        return False


# -- sizes, for count() ------------------------------------------------------------------

_SIZES: dict = {"token": None, "at": 0.0, "bytes": 0}


def _bytes_of(names: frozenset[str]) -> int:
    """Their total size on disk, a missing one counting nothing. Drawn on
    every visit of the page: 1 520 stat calls, kept a minute."""
    token = hash(names)
    now = time.monotonic()
    if _SIZES["token"] == token and now - _SIZES["at"] < SIZE_CACHE_SECONDS:
        return _SIZES["bytes"]
    total = 0
    for name in names:
        try:
            total += os.path.getsize(default_storage.path(name))
        except (OSError, SuspiciousFileOperation, NotImplementedError, ValueError):
            continue
    _SIZES.update(token=token, at=now, bytes=total)
    return total


def _named_files() -> set[str]:
    from invoices.models import Invoice

    names = set()
    for source, preview in Invoice.objects.values_list("source_file", "preview_image"):
        names.update(name for name in (source, preview) if name)
    return names


def _orphan_files(named: set[str]) -> int:
    """Files under media/invoices and media/receipts no document names - left
    as they are by a clear, as by everything else."""
    from django.conf import settings

    count = 0
    root = os.fspath(settings.MEDIA_ROOT)
    for folder in ("invoices", "receipts"):
        for directory, _dirs, files in os.walk(os.path.join(root, folder)):
            for filename in files:
                name = os.path.relpath(os.path.join(directory, filename), root).replace(os.sep, "/")
                if name not in named:
                    count += 1
    return count


@registry.register
class InvoicesSection(Section):
    key = KEY

    # -- what this database holds ----------------------------------------------------
    def count(self) -> dict[str, int]:
        from invoices.models import Invoice, InvoiceLine

        names = frozenset(_named_files())
        return {
            "documents": Invoice.objects.count(),
            "lignes": InvoiceLine.objects.count(),
            "fichiers": len(names),
            "Mo de fichiers": round(_bytes_of(names) / 1_000_000),
        }

    def snapshot(self):
        from inventory.models import MovementKind, Product, StockMovement
        from invoices.models import Invoice, InvoiceLine

        invoice_keys = keys.invoice_keys()
        movements = {
            line_id: (stock_type, str(quantity), str(unit_cost))
            for line_id, stock_type, quantity, unit_cost in StockMovement.objects.filter(
                kind=MovementKind.PURCHASE, invoice_line__isnull=False
            ).values_list("invoice_line_id", "stock_type__name", "quantity", "unit_cost_ht")
        }
        lines = defaultdict(list)
        used = set()
        for line in InvoiceLine.objects.select_related("product__supplier").order_by("invoice_id", "id"):
            used.add(line.product_id)
            lines[line.invoice_id].append(
                {
                    "product": [line.product.supplier.code, line.product.raw_name],
                    **codec.record(line, LINE_FIELDS),
                    "movement": movements.get(line.pk),
                }
            )
        documents = []
        for invoice in Invoice.objects.select_related("supplier"):
            files = {}
            for name in FILE_FIELDS:
                stored = getattr(invoice, name)
                files[name] = (stored.name, keys.file_sha256(stored)) if stored else None
            documents.append(
                {
                    "key": invoice_keys[invoice.pk],
                    "supplier": invoice.supplier.code,
                    **codec.record(invoice, INVOICE_FIELDS),
                    **files,
                    "lines": lines.get(invoice.pk, []),
                }
            )
        documents.sort(key=lambda document: tuple(str(part) for part in _canonical(document["key"])))
        products = sorted(
            (
                product.supplier.code,
                product.raw_name,
                product.ean,
                product.is_expense,
                product.stock_type_id is not None,
            )
            for product in Product.objects.filter(pk__in=list(used)).select_related("supplier")
        )
        return {"invoices": documents, "products": products}

    # -- export ------------------------------------------------------------------------
    def export(self, out) -> None:
        from inventory.models import Product
        from invoices.models import Invoice, InvoiceLine

        invoices = list(Invoice.objects.select_related("supplier").order_by("imported_at", "id"))
        refs: dict[int, tuple] = {}
        file_shas: dict[int, str] = {}
        stored: dict[str, int] = {}
        for invoice in invoices:
            source = out.add_file(invoice.source_file)
            preview = out.add_file(invoice.preview_image)
            refs[invoice.pk] = (source, preview)
            # The key's file sha comes from the ref just written: no file is
            # hashed twice (keys.invoice_keys).
            file_shas[invoice.pk] = source.get("sha256", "") if source else ""
            for ref in (source, preview):
                if ref and not ref.get("missing"):
                    stored[ref["name"]] = ref["size"]
        invoice_keys = keys.invoice_keys(file_shas=file_shas)

        lines_of = defaultdict(list)
        for line in InvoiceLine.objects.order_by("invoice_id", "id").iterator(chunk_size=2000):
            lines_of[line.invoice_id].append(line)
        used = {line.product_id for lines in lines_of.values() for line in lines}
        products = {product.pk: product for product in Product.objects.select_related("supplier") if product.pk in used}

        records = []
        line_count = 0
        for invoice in invoices:
            source, preview = refs[invoice.pk]
            lines = lines_of.get(invoice.pk, [])
            line_count += len(lines)
            records.append(
                {
                    "key": invoice_keys[invoice.pk],
                    "supplier": invoice.supplier.code,
                    **codec.record(invoice, INVOICE_FIELDS),
                    "source_file": source,
                    "preview_image": preview,
                    "lines": [
                        {
                            "product": [products[line.product_id].supplier.code, products[line.product_id].raw_name],
                            **codec.record(line, LINE_FIELDS),
                        }
                        for line in lines
                    ],
                }
            )
        product_records = sorted(
            (
                {
                    "supplier": product.supplier.code,
                    **codec.record(product, (*PRODUCT_FIELDS, "created_at")),
                    "classified": product.stock_type_id is not None,
                }
                for product in products.values()
            ),
            key=lambda record: (record["supplier"], record["raw_name"]),
        )
        names = {invoice.supplier.code: invoice.supplier.name for invoice in invoices}
        names.update({product.supplier.code: product.supplier.name for product in products.values()})
        out.write(
            {"supplier_names": names, "products": product_records, "invoices": records},
            {
                "documents": len(records),
                "lignes": line_count,
                "fichiers": len(stored),
                "Mo de fichiers": round(sum(stored.values()) / 1_000_000),
            },
        )

    # -- import ------------------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        records = payload.get("invoices")
        products = payload.get("products", [])
        if not isinstance(records, list):
            raise ArchiveError("Archive refusée : factures.json n'a pas de liste « invoices ».")
        if not isinstance(products, list):
            raise ArchiveError("Archive refusée : factures.json n'a pas de liste « products ».")
        self.records = records
        self.product_records = products
        # Every invoice here a record of the archive answered to - even one
        # whose record is skipped - and every one this run created: prune
        # removes only the others.
        self.named: set[int] = set()
        # (invoice, rank, line): lines a replace removes, held for now by a
        # count this run's « Inventaires » prune deletes - removed by prune.
        self.released_lines: list[tuple] = []

    def apply(self, ctx, report) -> None:
        self._ctx, self._report = ctx, report
        self._products = ctx.products()
        self._file_products = self._load_products()
        self._expense_fixed: set[int] = set()
        self._released = self._released_takes()
        claimed: set[int] = set()
        matched, new = [], []
        for record in self.records:
            doc = self._parse(record)
            if doc is None:
                continue
            found = doc.found
            if found is None:
                new.append(doc)
            elif found.pk in claimed:
                report.skip(f"{doc.label} : un autre document de l'archive répond déjà au même document ici")
            else:
                claimed.add(found.pk)
                matched.append((doc, found))
        # Documents found here first: a replace may free a number a new
        # document of the archive then takes.
        self._update(matched)
        self._create(new)

    # .. checking a record ..........................................................
    def _load_products(self) -> dict[tuple[str, str], dict]:
        from inventory.models import Product

        result = {}
        for record in self.product_records:
            if not isinstance(record, dict):
                self._report.skip("Un produit de l'archive est illisible.")
                continue
            codec.note_unknown(self._report, record, PRODUCT_KNOWN, "produits : ")
            code, raw_name = record.get("supplier"), record.get("raw_name")
            if not isinstance(code, str) or not isinstance(raw_name, str) or not raw_name:
                self._report.skip("Un produit de l'archive n'a pas de fournisseur ou de nom.")
                continue
            try:
                values = {
                    name: codec.load(Product, name, record[name])
                    for name in (*PRODUCT_FIELDS, "created_at")
                    if name in record
                }
                classified = record.get("classified")
                if classified is not None and not isinstance(classified, bool):
                    raise codec.FieldValueError(f"« classified » : oui ou non attendu (« {classified} »)")
            except codec.FieldValueError as exc:
                self._report.skip(f"Produit « {raw_name} » : {exc}")
                continue
            values["classified"] = classified
            result[(code, raw_name)] = values
        return result

    def _parse(self, record) -> _Doc | None:
        """The record, checked whole: a document with one bad value or one
        line that cannot be resolved is skipped with its reason, never
        created in part (§6.3)."""
        from invoices.models import Invoice, InvoiceLine

        ctx, report = self._ctx, self._report
        if not isinstance(record, dict):
            report.skip("Un document de l'archive est illisible.")
            return None
        codec.note_unknown(report, record, KNOWN)
        code = record.get("supplier")
        supplier = ctx.suppliers.resolve(code) if isinstance(code, str) else None
        number = record.get("invoice_number") if isinstance(record.get("invoice_number"), str) else ""
        label = describe(supplier.name if supplier is not None else str(code), number, None)
        try:
            invoice_date = codec.load(Invoice, "invoice_date", record.get("invoice_date"))
            label = describe(supplier.name if supplier is not None else str(code), number, invoice_date)
        except codec.FieldValueError:
            pass

        key = _clean_key(record.get("key"))
        if key is None:
            report.skip(f"{label} : clé illisible dans l'archive")
            return None
        # Resolved before anything else is checked: a document the archive
        # has, even skipped, is one prune must not remove.
        found = ctx.invoices.resolve(key, report=report)
        if isinstance(found, keys.Ambiguous):
            self.named.update({found.by_number.pk, found.by_file.pk})
            report.skip(
                f"{label} : deux documents différents répondent à cette facture (n° "
                f"{found.by_number.invoice_number} et fichier de {describe_invoice(found.by_file)})"
            )
            return None
        if found is not None:
            self.named.add(found.pk)
        if supplier is None:
            report.skip(f"{label} : fournisseur inconnu « {code} »")
            return None

        try:
            values = {name: codec.load(Invoice, name, record[name]) for name in INVOICE_FIELDS if name in record}
            if "parse_checks" in values and not _readable_checks(values["parse_checks"]):
                raise codec.FieldValueError("contrôles illisibles")
            if "vat_breakdown" in values and not _readable_vat_table(values["vat_breakdown"]):
                raise codec.FieldValueError("table de TVA illisible")
            refs = {name: self._ref(record, name) for name in FILE_FIELDS}
            lines = self._lines(record, InvoiceLine)
        except (codec.FieldValueError, FileRefused) as exc:
            report.skip(f"{label} : {exc}")
            return None
        return _Doc(
            record=record, key=key, supplier=supplier, label=label, values=values, refs=refs, lines=lines, found=found
        )

    def _ref(self, record: dict, name: str):
        if name not in record:
            return NOT_SAID
        ref = record[name]
        if ref is None:
            return None
        if not isinstance(ref, dict) or not isinstance(ref.get("name"), str):
            raise codec.FieldValueError(f"« {name} » : fichier illisible dans l'archive")
        if ref.get("missing"):
            return _Missing(ref["name"])
        self._ctx.check_file(ref, ref["name"])
        return ref

    def _lines(self, record: dict, line_model) -> list | None:
        if "lines" not in record:
            return None
        items = record["lines"]
        if not isinstance(items, list):
            raise codec.FieldValueError("« lines » : liste attendue")
        lines = []
        for ordinal, item in enumerate(items):
            where = f"ligne n°{ordinal + 1}"
            if not isinstance(item, dict):
                raise codec.FieldValueError(f"{where} illisible")
            codec.note_unknown(self._report, item, LINE_KNOWN, "lignes : ")
            product_key = item.get("product")
            if (
                not isinstance(product_key, (list, tuple))
                or len(product_key) != 2
                or not all(isinstance(part, str) for part in product_key)
            ):
                raise codec.FieldValueError(f"{where} : produit illisible")
            product_key = (product_key[0], product_key[1])
            try:
                values = {name: codec.load(line_model, name, item[name]) for name in LINE_FIELDS if name in item}
            except codec.FieldValueError as exc:
                raise codec.FieldValueError(f"{where} : {exc}") from None
            supplier = self._ctx.suppliers.resolve(product_key[0])
            if supplier is None:
                raise codec.FieldValueError(f"{where} : fournisseur inconnu « {product_key[0]} »")
            if self._products.resolve(supplier, product_key[1]) is None and product_key not in self._file_products:
                raise codec.FieldValueError(f"{where} : produit inconnu « {product_key[1]} »")
            lines.append((product_key, values, item))
        return lines

    # .. products ....................................................................
    def _product(self, product_key: tuple[str, str], create: bool = True):
        """The product a line of the archive names: found here - never
        fuzzily - or created as the archive has it, unclassified: a
        classification only ever comes from the associations."""
        from inventory.models import Product

        supplier = self._ctx.suppliers.resolve(product_key[0])
        product = self._products.resolve(supplier, product_key[1])
        if product is not None or not create:
            return product
        values = self._file_products[product_key]
        product = Product.objects.create(
            supplier=supplier,
            raw_name=product_key[1],
            ean=values.get("ean") or "",
            is_expense=bool(values.get("is_expense", False)),
        )
        if values.get("created_at") is not None:
            # auto_now_add wrote "now" over it on insert.
            product.created_at = values["created_at"]
            Product.objects.filter(pk=product.pk).update(created_at=product.created_at)
        self._products.add(product)
        self._report.created("produits")
        return product

    def _settle_products(self, doc: _Doc, products) -> None:
        """What the archive says of the products this document's lines use:
        an unclassified one takes its nature (poste de charge or not), and
        one classified here but not there - or the reverse - has its
        invoices' statuses worked out again, since the status copied from
        the archive was right for the archive's classification."""
        from inventory.models import Product

        for (product_key, _values, _item), product in zip(doc.lines or [], products):
            values = self._file_products.get(product_key, {})
            if (
                product.pk not in self._expense_fixed
                and product.stock_type_id is None
                and values.get("is_expense") is not None
                and product.is_expense != values["is_expense"]
            ):
                product.is_expense = values["is_expense"]
                Product.objects.filter(pk=product.pk).update(is_expense=product.is_expense)
                self._expense_fixed.add(product.pk)
            classified = values.get("classified")
            if classified is not None and classified != (product.stock_type_id is not None):
                self._ctx.dirty.status_products.add(product.pk)

    # .. files ......................................................................
    def _file_state(self, ref, stored) -> str:
        """same | fill | differs, for one file of a document found here."""
        if ref is NOT_SAID or isinstance(ref, _Missing):
            return "same"
        name = stored.name if stored else ""
        if ref is None:
            return "differs" if name else "same"
        if not _stored(name):
            return "fill"
        return "same" if self._ctx.file_matches(ref, name) else "differs"

    def _store(self, doc: _Doc, names) -> dict[str, str | None]:
        """Store these files of the document; None for a field the archive
        leaves empty. A refusal half way removes what was already stored:
        nothing would name it."""
        stored: dict[str, str | None] = {}
        try:
            for name in names:
                ref = doc.refs[name]
                stored[name] = None if ref is None else self._ctx.save_file(ref, ref["name"])
        except FileRefused:
            if not self._ctx.preview:
                for written in stored.values():
                    if written and written in self._ctx.stored_files:
                        default_storage.delete(written)
                        self._ctx.stored_files.remove(written)
            raise
        return stored

    # .. documents found here ........................................................
    def _update(self, matched: list) -> None:
        from invoices.models import InvoiceLine

        here_lines = defaultdict(list)
        for chunk in _chunks([invoice.pk for _doc, invoice in matched]):
            for line in InvoiceLine.objects.filter(invoice_id__in=chunk).order_by("invoice_id", "id"):
                here_lines[line.invoice_id].append(line)
        replacing = self._ctx.replacing(self.key)
        for doc, invoice in matched:
            lines = here_lines.get(invoice.pk, [])
            try:
                if replacing:
                    self._replace(doc, invoice, lines)
                else:
                    self._merge(doc, invoice, lines)
            except FileRefused as exc:
                self._report.skip(f"{doc.label} : {exc}")
                continue
            self._ctx.invoices.bind(doc.key, invoice)

    def _differences(self, doc: _Doc, invoice, lines) -> tuple[list[str], list[str], list[bool]]:
        """What differs (field names), which files may be filled, and per
        line of the archive whether it differs from the line of the same
        rank here."""
        different = []
        if doc.supplier.pk != invoice.supplier_id:
            different.append("supplier")
        different += codec.differences(invoice, doc.record, COMPARED)
        line_changes: list[bool] = []
        if doc.lines is not None:
            for ordinal, (product_key, _values, item) in enumerate(doc.lines):
                if ordinal >= len(lines):
                    line_changes.append(True)
                    continue
                line = lines[ordinal]
                product = self._product(product_key, create=False)
                line_changes.append(
                    product is None
                    or product.pk != line.product_id
                    or bool(codec.differences(line, item, LINE_FIELDS))
                )
            if len(doc.lines) != len(lines) or any(line_changes):
                different.append("lines")
        fills = []
        for name in FILE_FIELDS:
            state = self._file_state(doc.refs[name], getattr(invoice, name))
            if state == "fill":
                fills.append(name)
            elif state == "differs":
                different.append(name)
        return different, fills, line_changes

    def _unchanged(self, invoice, lines) -> None:
        report = self._report
        report.unchanged("documents")
        if lines:
            report.unchanged("lignes", len(lines))
        self._untouched_files(invoice, ())

    def _untouched_files(self, invoice, written) -> None:
        """The document's files this run did not write, « inchangés »."""
        files = sum(1 for name in FILE_FIELDS if name not in written and getattr(invoice, name))
        if files:
            self._report.unchanged("fichiers", files)

    def _merge(self, doc: _Doc, invoice, lines) -> None:
        """Keep the document as it is and say what differs - but give it
        back a file the archive has and this database lost or never had."""
        report = self._report
        different, fills, _line_changes = self._differences(doc, invoice, lines)
        if fills:
            stored = self._store(doc, fills)
            for name, value in stored.items():
                setattr(invoice, name, value)
            invoice.save(update_fields=fills)
            report.updated("documents")
            report.created("fichiers", len(fills))
            self._untouched_files(invoice, fills)
            if not different and lines:
                report.unchanged("lignes", len(lines))
            report.note(f"{doc.label} : {RESTORED[tuple(fills)]}")
        if different:
            report.conflict(f"{doc.label} : différente dans l'archive ({said(different, LABELS)}) — gardée telle quelle")
        elif not fills:
            self._unchanged(invoice, lines)

    def _released_takes(self) -> set[int]:
        """The counts here this run's « Inventaires » prune deletes: under
        « Remplacer », every one the archive's counts do not name. None when
        the counts are merged or not in the run - they all stay."""
        from inventory.models import StockTake
        from transfer.sections.stock_takes import StockTakesSection, named_takes

        key = StockTakesSection.key
        if not self._ctx.replacing(key):
            return set()
        named = named_takes(self._ctx.reader.section(key).payload().get("stock_takes"))
        return set(StockTake.objects.exclude(pk__in=named).values_list("pk", flat=True))

    def _trail_kept(self, doc: _Doc, invoice, lines) -> str:
        """Why this document stays as it is, or "": a line a stock take was
        priced from would be removed, or rewritten in place into another
        purchase - another product, or another name at the same rank, which
        is how the stock takes' own import tells a source's line
        (sections/stock_takes.py). Lines are paired by rank: here [A, C]
        with C priced, the archive [A, B, C] (B taken out after the export),
        C's row became B's and the count of C was priced from a purchase of
        B, with nothing said. The earliest count is named, as
        `delete_invoice` does.

        Not a count this same run deletes (`_released`): the invoices apply
        before the counts prune, and a document kept for such a count was
        said kept beside the count said deleted - a whole « Remplacer »
        restore left the invoice short of the archive's line."""
        from inventory.models import StockTakeLineSource

        if doc.lines is None:
            return ""
        priced = sorted(
            (
                (line_id, taken_at)
                for line_id, take_id, taken_at in StockTakeLineSource.objects.filter(invoice_line__in=lines).values_list(
                    "invoice_line_id", "stock_take_line__stock_take_id", "stock_take_line__stock_take__taken_at"
                )
                if take_id not in self._released
            ),
            key=lambda row: row[1],
        )
        ordinals = {line.pk: ordinal for ordinal, line in enumerate(lines)}
        for line_id, taken_at in priced:
            ordinal = ordinals[line_id]
            why = (
                f"{describe_invoice(invoice)} : sa ligne n°{ordinal + 1} a valorisé l'inventaire du "
                f"{day(timezone.localtime(taken_at))}"
            )
            if ordinal >= len(doc.lines):
                return why
            line = lines[ordinal]
            product_key, values, _item = doc.lines[ordinal]
            product = self._product(product_key, create=False)
            raw_name = values.get("raw_name", line.raw_name)
            if product is None or product.pk != line.product_id or keys.fold(raw_name) != keys.fold(line.raw_name):
                return f"{why} ; l'archive met « {raw_name} » à sa place"
        return ""

    def _replace(self, doc: _Doc, invoice, lines) -> None:
        """The document becomes the archive's: its fields, its supplier (a
        document found by its file may have moved), its files, and its lines
        paired by rank and updated in place - never a line a stock take was
        priced from into another purchase (`_trail_kept`)."""
        from inventory.models import StockMovement, StockTakeLineSource
        from invoices.deletion import remove_orphan_products
        from invoices.models import Invoice, InvoiceLine

        report = self._report
        different, fills, line_changes = self._differences(doc, invoice, lines)
        if not different and not fills:
            self._unchanged(invoice, lines)
            return

        extra = lines[len(doc.lines) :] if doc.lines is not None else []
        trail = self._trail_kept(doc, invoice, lines)
        if trail:
            report.keep(trail)
            return
        number = doc.values.get("invoice_number", invoice.invoice_number)
        if (
            number
            and (doc.supplier.pk, number) != (invoice.supplier_id, invoice.invoice_number)
            and Invoice.objects.filter(supplier=doc.supplier, invoice_number=number).exclude(pk=invoice.pk).exists()
        ):
            report.keep(f"{describe_invoice(invoice)} : n° {number} déjà porté ici par un autre document de {doc.supplier.name}")
            return

        files = [name for name in FILE_FIELDS if name in fills or name in different]
        stored = self._store(doc, files)
        codec.assign(invoice, doc.record, INVOICE_FIELDS)
        invoice.supplier = doc.supplier
        for name, value in stored.items():
            old = getattr(invoice, name).name if getattr(invoice, name) else ""
            setattr(invoice, name, value)
            if old and old != value:
                self._ctx.delete_file_on_commit(old)
                if value is None:
                    report.deleted("fichiers")
            if value is not None:
                (report.created if name in fills else report.updated)("fichiers")
        # Its other files stay as they are: counted, or a restore reads
        # 1 518 files for the 1 520 the page announced (verifier, 19/09).
        self._untouched_files(invoice, stored)
        invoice.save()
        report.updated("documents")

        if doc.lines is None:
            self._ctx.dirty.lines.update(line.pk for line in lines)
            return
        previous = {line.pk: line.product_id for line in lines}
        products = [self._product(product_key) for product_key, _values, _item in doc.lines]
        new_lines = []
        for ordinal, ((_product_key, values, item), product) in enumerate(zip(doc.lines, products)):
            if ordinal >= len(lines):
                new_lines.append(InvoiceLine(invoice=invoice, product=product, **values))
                continue
            line = lines[ordinal]
            if not line_changes[ordinal]:
                report.unchanged("lignes")
                continue
            codec.assign(line, item, LINE_FIELDS)
            line.product = product
            line.save()
            report.updated("lignes")
        if extra:
            # A line to remove that a count was priced from got here only if
            # that count is one this run deletes (`_trail_kept`). It still
            # holds the line (PROTECT) until the counts' prune, which runs
            # before this section's: the line goes there (`prune`).
            held = set(
                StockTakeLineSource.objects.filter(invoice_line__in=extra).values_list("invoice_line_id", flat=True)
            )
            self.released_lines += [
                (invoice, len(doc.lines) + offset, line) for offset, line in enumerate(extra) if line.pk in held
            ]
            extra = [line for line in extra if line.pk not in held]
        if extra:
            # Deleted explicitly: StockMovement.invoice_line is SET_NULL, and a
            # purchase with no line behind it would stay in the stock.
            StockMovement.objects.filter(invoice_line__in=extra).delete()
            InvoiceLine.objects.filter(pk__in=[line.pk for line in extra]).delete()
            report.deleted("lignes", len(extra))
        if new_lines:
            InvoiceLine.objects.bulk_create(new_lines, batch_size=BATCH)
            report.created("lignes", len(new_lines))
        self._settle_products(doc, products)
        self._ctx.dirty.lines.update(line.pk for line in lines[: len(doc.lines)])
        self._ctx.dirty.lines.update(line.pk for line in new_lines)
        removed = remove_orphan_products(set(previous.values()))
        if removed:
            report.deleted("produits", removed)

    # .. documents new here ..........................................................
    def _create(self, new: list) -> None:
        from invoices.models import Invoice, InvoiceLine

        report = self._report
        taken = set(Invoice.objects.exclude(invoice_number="").values_list("supplier_id", "invoice_number"))
        seen: set[tuple] = set()
        rows = []
        for doc in new:
            canonical = _canonical(doc.key)
            if canonical in seen:
                report.skip(f"{doc.label} : en double dans l'archive")
                continue
            number = doc.values.get("invoice_number", "")
            if number and (doc.supplier.pk, number) in taken:
                report.skip(f"{doc.label} : n° déjà porté ici par un autre document de {doc.supplier.name}")
                continue
            try:
                stored = self._store(doc, [name for name in FILE_FIELDS if isinstance(doc.refs[name], dict)])
            except FileRefused as exc:
                report.skip(f"{doc.label} : {exc}")
                continue
            seen.add(canonical)
            if number:
                taken.add((doc.supplier.pk, number))
            for name in FILE_FIELDS:
                if isinstance(doc.refs[name], _Missing):
                    report.note(
                        f"{doc.label} : {FILE_LABELS[name]} absent{'e' if name == 'preview_image' else ''} du "
                        f"disque à l'export (« {doc.refs[name].name} ») — importé{'e' if number else ''} sans"
                    )
            invoice = Invoice(supplier=doc.supplier, **doc.values)
            for name, value in stored.items():
                setattr(invoice, name, value)
            products = [self._product(product_key) for product_key, _values, _item in doc.lines or []]
            rows.append((doc, invoice, products))
            report.created("documents")
            if stored:
                report.created("fichiers", len(stored))
        if not rows:
            return

        Invoice.objects.bulk_create([invoice for _doc, invoice, _products in rows], batch_size=BATCH)
        restored = []
        for doc, invoice, _products in rows:
            if doc.values.get("imported_at") is not None:
                # auto_now_add wrote "now" over it on insert (bulk_create
                # included); bulk_update does not call pre_save. The ordering
                # uses it, and so does every key's occurrence.
                invoice.imported_at = doc.values["imported_at"]
                restored.append(invoice)
        if restored:
            Invoice.objects.bulk_update(restored, ["imported_at"], batch_size=BATCH)
        lines = []
        for doc, invoice, products in rows:
            self.named.add(invoice.pk)
            self._ctx.invoices.bind(doc.key, invoice)
            for (_product_key, values, _item), product in zip(doc.lines or [], products):
                lines.append(InvoiceLine(invoice=invoice, product=product, **values))
            self._settle_products(doc, products)
        if lines:
            InvoiceLine.objects.bulk_create(lines, batch_size=BATCH)
            report.created("lignes", len(lines))
            self._ctx.dirty.lines.update(line.pk for line in lines)

    # .. prune ......................................................................
    def prune(self, ctx, report) -> None:
        from invoices.models import Invoice

        self._remove_released_lines(report)
        ids = [pk for pk in Invoice.objects.order_by("id").values_list("id", flat=True) if pk not in self.named]
        if ids:
            _remove(ctx, report, ids, set())

    def _remove_released_lines(self, report) -> None:
        """The lines `_replace` left to this prune: the counts that held them
        are gone by now. One a count of the archive was re-pointed at since -
        only a hand-edited archive names a line its invoice does not have -
        stays, said, rather than break that count's trail."""
        from inventory.models import StockMovement, StockTakeLineSource
        from invoices.deletion import remove_orphan_products
        from invoices.models import InvoiceLine

        if not self.released_lines:
            return
        held: dict[int, object] = {}
        for line_id, taken_at in StockTakeLineSource.objects.filter(
            invoice_line_id__in=[line.pk for _invoice, _rank, line in self.released_lines]
        ).values_list("invoice_line_id", "stock_take_line__stock_take__taken_at"):
            held[line_id] = min(taken_at, held.get(line_id, taken_at))
        gone = []
        for invoice, rank, line in self.released_lines:
            if line.pk in held:
                report.keep(
                    f"{describe_invoice(invoice)} : sa ligne n°{rank + 1}, que l'archive n'a pas, a valorisé "
                    f"l'inventaire du {day(timezone.localtime(held[line.pk]))}"
                )
            else:
                gone.append(line)
        if not gone:
            return
        # Its purchase first: StockMovement.invoice_line is SET_NULL.
        StockMovement.objects.filter(invoice_line__in=gone).delete()
        InvoiceLine.objects.filter(pk__in=[line.pk for line in gone]).delete()
        report.deleted("lignes", len(gone))
        removed = remove_orphan_products({line.product_id for line in gone})
        if removed:
            report.deleted("produits", removed)

    # -- clear --------------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        from inventory.models import Product
        from invoices.models import Invoice

        orphans = _orphan_files(_named_files())
        ids = list(Invoice.objects.order_by("id").values_list("id", flat=True))
        # Every product, not only those of the documents: an unclassified
        # product no line uses is debris of a document gone. A classified
        # one stays - it is the associations'.
        _remove(ctx, report, ids, set(Product.objects.values_list("id", flat=True)))
        report.note("Les historiques de récupération et d'import de tickets restent.")
        if orphans:
            report.note(
                f"{plural(orphans, 'fichier')} que plus rien ne cite dans media/ "
                f"reste{'nt' if orphans > 1 else ''} tel{'s' if orphans > 1 else ''} quel{'s' if orphans > 1 else ''}."
            )


def _remove(ctx, report, ids: list[int], products: set[int]) -> None:
    """`delete_invoice`, in bulk: refused for a document a stock take was
    priced from (kept, said); otherwise its lines' movements explicitly
    (SET_NULL would leave them in the stock), then the document - its lines,
    its bank payment and its history links follow - then the products only
    it had created, and its files on commit."""
    from bank.models import InvoicePayment
    from inventory.models import StockMovement, StockTakeLineSource
    from invoices.deletion import remove_orphan_products
    from invoices.models import Invoice, InvoiceLine

    blocked: dict[int, set] = defaultdict(set)
    for chunk in _chunks(ids):
        for invoice_id, taken_at in StockTakeLineSource.objects.filter(invoice_line__invoice_id__in=chunk).values_list(
            "invoice_line__invoice_id", "stock_take_line__stock_take__taken_at"
        ):
            blocked[invoice_id].add(taken_at)
    for chunk in _chunks(sorted(blocked)):
        for invoice in Invoice.objects.filter(pk__in=chunk).select_related("supplier"):
            dates = ", ".join(day(timezone.localtime(moment)) for moment in sorted(blocked[invoice.pk]))
            report.keep(f"{describe_invoice(invoice)} : a servi à valoriser l'inventaire du {dates}")

    doomed = [pk for pk in ids if pk not in blocked]
    names: list[str] = []
    products = set(products)
    lines = payments = 0
    for chunk in _chunks(doomed):
        for source, preview in Invoice.objects.filter(pk__in=chunk).values_list("source_file", "preview_image"):
            names.extend(name for name in (source, preview) if name)
        for product_id in InvoiceLine.objects.filter(invoice_id__in=chunk).values_list("product_id", flat=True):
            products.add(product_id)
            lines += 1
        StockMovement.objects.filter(invoice_line__invoice_id__in=chunk).delete()
        payments += InvoicePayment.objects.filter(invoice_id__in=chunk).count()
        Invoice.objects.filter(pk__in=chunk).delete()
    removed = remove_orphan_products(products) if products else 0
    for name in names:
        ctx.delete_file_on_commit(name)

    if doomed:
        report.deleted("documents", len(doomed))
    if lines:
        report.deleted("lignes", lines)
    if names:
        report.deleted("fichiers", len(names))
    if removed:
        report.deleted("produits", removed)
    if payments:
        bank = ctx.report("banque")
        bank.deleted("paiements", payments)
        bank.note(BANK_NOTE[payments > 1].format(count=payments))
