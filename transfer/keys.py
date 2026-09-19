"""Natural keys and their resolvers (§5.4): how a record in an archive finds
its row in this database. No primary key ever leaves the database; every
section matches through these, and none re-invents them.

Lookups are dicts built in Python, never `__iexact`: SQLite's case-blind
comparison leaves accented capitals out ("ÉPICERIE" and "épicerie" were two
suppliers), which is why `fold` is the same folding as
`receipts.supplier_named`.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from collections.abc import Iterable

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage

CHUNK = 1024 * 1024


def fold(text: str) -> str:
    return " ".join((text or "").split()).casefold()


# -- files ---------------------------------------------------------------------------

_FILE_SHAS: dict[tuple, str] = {}


def file_sha256(field_file) -> str:
    """The sha256 of a stored file's content, streamed; "" for an empty
    field or a file missing from the disk. Cached per process by name, size
    and modification time, so the invoices section and the bank's keys hash
    each file once."""
    name = field_file if isinstance(field_file, str) else getattr(field_file, "name", "")
    if not name:
        return ""
    storage = default_storage if isinstance(field_file, str) else getattr(field_file, "storage", default_storage)
    try:
        path = storage.path(name)
        stat = os.stat(path)
    except (OSError, NotImplementedError, ValueError, SuspiciousFileOperation):
        # Missing on disk, or a stored name outside media: it names no file.
        return ""
    cache_key = (path, stat.st_size, stat.st_mtime_ns)
    if cache_key not in _FILE_SHAS:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                digest.update(chunk)
        _FILE_SHAS[cache_key] = digest.hexdigest()
    return _FILE_SHAS[cache_key]


# -- suppliers -----------------------------------------------------------------------

class SupplierResolver:
    """Supplier by `code`: an explicit binding made by the fournisseurs
    section wins, then the code, then the folded name (from the archive's
    `supplier_names`) - codes differ between two databases when a shop was
    created on each (LIDL here, LIDL_2 there)."""

    def __init__(self):
        from invoices.models import Supplier

        self._bound: dict[str, object] = {}
        self._by_code: dict[str, object] = {}
        self._by_name: dict[str, object] = {}
        self.names: dict[str, str] = {}
        for supplier in Supplier.objects.all():
            self.add(supplier)

    def remember(self, supplier_names) -> None:
        """The archive's code → name table, from every section file that
        names suppliers."""
        if isinstance(supplier_names, dict):
            for code, name in supplier_names.items():
                if isinstance(code, str) and isinstance(name, str):
                    self.names.setdefault(code, name)

    def add(self, supplier) -> None:
        self._by_code[supplier.code] = supplier
        self._by_name.setdefault(fold(supplier.name), supplier)

    def bind(self, file_code: str, supplier) -> None:
        self._bound[file_code] = supplier
        self.add(supplier)

    def refuse(self, file_code: str) -> None:
        """This file code resolves to nothing for the rest of the run: the
        fournisseurs section skipped its record, and falling back to its
        name would file its documents under another supplier here - the one
        that name belongs to."""
        self._bound[file_code] = None

    def resolve(self, code: str, name: str = ""):
        if not isinstance(code, str):
            return None
        if code in self._bound:
            return self._bound[code]
        if code in self._by_code:
            return self._by_code[code]
        wanted = fold(name or self.names.get(code, ""))
        return self._by_name.get(wanted) if wanted else None


# -- articles, products, recipes -----------------------------------------------------

class ArticleResolver:
    def __init__(self):
        from inventory.models import StockType

        self._by_name = {fold(stock_type.name): stock_type for stock_type in StockType.objects.all()}

    def resolve(self, name: str):
        return self._by_name.get(fold(name)) if isinstance(name, str) else None

    def add(self, stock_type) -> None:
        self._by_name[fold(stock_type.name)] = stock_type


class ProductResolver:
    """(supplier, raw_name) exact, then (supplier, folded raw_name) when
    exactly one product folds that way and the archive does not name that
    one itself. **Never fuzzy**: an import must not merge two products
    ("RICARD 45D 1.5L" into "RICARD 45D 1L" was silently wrong money once
    already).

    The archive's own names (`archive_keys`, `name_archive`) keep apart two
    products differing only by case or spacing. SQLite's case-blind
    comparison is ASCII only, so the app's matcher makes « … FÛT 30L » and
    « … Fût 30L » two products of one supplier; imported into an empty
    database, the first of the pair was created and the second then folded
    onto it - its classification skipped « en double », its invoice line
    moved onto the first. A product here that the archive names exactly is
    that record's, never another's by its folded name (TillProducts' rule
    for the till)."""

    def __init__(self, archive_keys: Iterable[tuple[int, str]] = ()):
        from inventory.models import Product

        self._exact: dict[tuple[int, str], object] = {}
        self._folded: dict[tuple[int, str], list] = defaultdict(list)
        #: (supplier pk here, raw name) of the products the archive names itself.
        self._archive: set[tuple[int, str]] = set(archive_keys)
        for product in Product.objects.all():
            self.add(product)

    def name_archive(self, archive_keys: Iterable[tuple[int, str]]) -> None:
        """What the archive names (archive_product_keys): the import's
        context builds the resolver, the section that reads the archive says
        what it holds."""
        self._archive.update(archive_keys)

    def add(self, product) -> None:
        self._exact[(product.supplier_id, product.raw_name)] = product
        folded = self._folded[(product.supplier_id, fold(product.raw_name))]
        if all(other.pk != product.pk for other in folded):
            folded.append(product)

    def resolve(self, supplier, raw_name: str):
        if supplier is None or not isinstance(raw_name, str):
            return None
        supplier_id = getattr(supplier, "pk", supplier)
        found = self._exact.get((supplier_id, raw_name))
        if found is not None:
            return found
        candidates = [
            product
            for product in self._folded.get((supplier_id, fold(raw_name)), [])
            if (supplier_id, product.raw_name) not in self._archive
        ]
        return candidates[0] if len(candidates) == 1 else None


#: The sections whose records name a product by (supplier code, raw name).
PRODUCT_SECTIONS = ("associations", "factures", "inventaires")


def archive_product_keys(reader, suppliers: SupplierResolver) -> set[tuple[int, str]]:
    """(supplier pk here, raw name) of every product the archive names by
    its key - the associations' products, the invoices' products and lines,
    the stock takes' counted products - whether this run imports those
    sections or not: two products the archive holds apart were two where it
    was made. A key whose supplier is unknown here, or not shaped like a
    key, names nothing (the section reading it says why), and so does a
    section that cannot be read: one this run does not import must not
    refuse the ones it does."""
    from transfer.archive import ArchiveError

    found: set[tuple[int, str]] = set()
    for key in PRODUCT_SECTIONS:
        if key not in reader.sections:
            continue
        try:
            payload = reader.section(key).payload()
        except ArchiveError:
            continue
        names = payload.get("supplier_names")
        names = names if isinstance(names, dict) else {}
        for code, raw_name in _named_products(key, payload):
            name = names.get(code)
            supplier = suppliers.resolve(code, name if isinstance(name, str) else "")
            if supplier is not None:
                found.add((supplier.pk, raw_name))
    return found


def _named_products(key: str, payload: dict) -> list[tuple[str, str]]:
    """The (code, raw name) pairs one section file names, as it writes them."""

    def listed(value) -> list:
        return value if isinstance(value, list) else []

    def as_key(value) -> tuple[str, str] | None:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            code, raw_name = value
            if isinstance(code, str) and code and isinstance(raw_name, str) and raw_name:
                return code, raw_name
        return None

    pairs = []
    if key in ("associations", "factures"):
        pairs += [
            as_key((record.get("supplier"), record.get("raw_name")))
            for record in listed(payload.get("products"))
            if isinstance(record, dict)
        ]
    parents, children = {"factures": ("invoices", "lines"), "inventaires": ("stock_takes", "lines")}.get(key, ("", ""))
    if parents:
        pairs += [
            as_key(line.get("product"))
            for parent in listed(payload.get(parents))
            if isinstance(parent, dict)
            for line in listed(parent.get(children))
            if isinstance(line, dict)
        ]
    return [pair for pair in pairs if pair is not None]


class RecipeResolver:
    def __init__(self):
        from recipes.models import Recipe

        self._by_name = {fold(recipe.name): recipe for recipe in Recipe.objects.all()}

    def resolve(self, name: str):
        return self._by_name.get(fold(name)) if isinstance(name, str) else None

    def add(self, recipe) -> None:
        self._by_name[fold(recipe.name)] = recipe


# -- invoices ------------------------------------------------------------------------

KEY_FIELDS = ("supplier", "number", "sha256", "file_sha256", "occurrence")


def invoice_key(invoice, occurrence: int, file_sha: str | None = None) -> dict:
    """One invoice's key. `occurrence` comes from invoice_keys(), which ranks
    the invoices without a number; `file_sha` defaults to its file's."""
    return {
        "supplier": invoice.supplier.code,
        "number": invoice.invoice_number or "",
        "sha256": invoice.source_sha256 or "",
        "file_sha256": file_sha256(invoice.source_file) if file_sha is None else file_sha,
        "occurrence": occurrence,
    }


def invoice_keys(invoices=None, *, file_shas: dict[int, str] | None = None) -> dict[int, dict]:
    """pk → InvoiceKey for `invoices` (a queryset, instances or pks; None:
    all of them).

    `occurrence` is the rank, by (imported_at, id), among the invoices
    without a number sharing the same fallback value - the stored
    source_sha256, else the file's sha. Ranked over every invoice of the
    database whatever was asked, so the bank's keys and the stock takes'
    agree with the invoices section's. Two real Monoprix tickets have no
    number, no stored sha and byte-identical files: they are (file sha, 0)
    and (file sha, 1), and round-trip as two.

    `file_shas` (pk → sha) are the shas already known - the invoices
    section has them from the file refs it just wrote - so no file is
    hashed twice."""
    from invoices.models import Invoice

    known = dict(file_shas or {})
    rows = list(
        Invoice.objects.order_by("imported_at", "id").values_list(
            "id", "supplier__code", "invoice_number", "source_sha256", "imported_at", "source_file"
        )
    )

    def sha_of(pk, name):
        if pk not in known:
            known[pk] = file_sha256(name or "")
        return known[pk]

    ranks: dict[int, int] = {}
    groups: dict[str, int] = defaultdict(int)
    for pk, _code, number, stored_sha, _imported_at, name in rows:  # already in (imported_at, id) order
        if number:
            continue
        fallback = stored_sha or sha_of(pk, name)
        if fallback:
            ranks[pk] = groups[fallback]
            groups[fallback] += 1

    if invoices is None:
        wanted = None
    else:
        wanted = {getattr(item, "pk", item) for item in invoices}
    return {
        pk: {
            "supplier": code,
            "number": number or "",
            "sha256": stored_sha or "",
            "file_sha256": sha_of(pk, name),
            "occurrence": ranks.get(pk, 0),
        }
        for pk, code, number, stored_sha, _imported_at, name in rows
        if wanted is None or pk in wanted
    }


def _canonical(key: dict) -> tuple:
    return tuple(key.get(name) for name in KEY_FIELDS)


class Ambiguous:
    """Two different invoices answer one key: one by its number, the other by
    its file. The caller skips the record with a reason."""

    def __init__(self, by_number, by_file):
        self.by_number = by_number
        self.by_file = by_file

    def __repr__(self):
        return f"Ambiguous(by_number={self.by_number.pk}, by_file={self.by_file.pk})"


class InvoiceIndex:
    """InvoiceKey → the invoice here, or None, or Ambiguous.

    1. (supplier, number) when the number is not empty - unique by the
       database's own constraint, and 867 of 893 real invoices.
    2. the stored source_sha256 equal to the key's sha256 or file_sha256,
       rank `occurrence` by (imported_at, id) - global across suppliers, like
       the importer's own dedupe.
    3. the file's sha of invoices with no stored sha, rank `occurrence`;
       hashed lazily, on the first miss, once per run.

    Steps 2 and 3 still run when step 1 finds something: a document whose
    number differs between the databases (a stand-in « YYYYMMDD-total »
    that `refresh_document_numbers` rewrote on one side) is found by its
    file, and two different invoices answering is Ambiguous rather than a
    guess."""

    def __init__(self, suppliers: SupplierResolver):
        from invoices.models import Invoice

        self.suppliers = suppliers
        self._bound: dict[tuple, object] = {}
        self._objects: dict[int, object] = {}
        self._order: dict[int, tuple] = {}
        self._numbers: dict[int, str] = {}
        self._by_number: dict[tuple[int, str], int] = {}
        self._by_sha: dict[str, list[int]] = defaultdict(list)
        self._unhashed: dict[int, str] = {}  # pk → file name, invoices with no stored sha
        self._by_file_sha: dict[str, list[int]] | None = None
        for pk, supplier_id, number, stored_sha, imported_at, name in Invoice.objects.values_list(
            "id", "supplier_id", "invoice_number", "source_sha256", "imported_at", "source_file"
        ):
            self._index(pk, supplier_id, number, stored_sha, imported_at, name)
        for ids in self._by_sha.values():
            ids.sort(key=self._order.__getitem__)

    def _index(self, pk, supplier_id, number, stored_sha, imported_at, name) -> None:
        self._order[pk] = (imported_at, pk)
        self._numbers[pk] = number or ""
        if number:
            self._by_number[(supplier_id, number)] = pk
        if stored_sha:
            self._by_sha[stored_sha].append(pk)
        else:
            self._unhashed[pk] = name or ""

    def _invoice(self, pk: int):
        from invoices.models import Invoice

        if pk not in self._objects:
            self._objects[pk] = Invoice.objects.select_related("supplier").get(pk=pk)
        return self._objects[pk]

    def bind(self, key: dict, invoice) -> None:
        """An invoice this run created or matched: every later key equal to
        this one resolves to it, whatever steps 1-3 would say."""
        self._bound[_canonical(key)] = invoice
        self._objects[invoice.pk] = invoice
        if invoice.pk not in self._order:
            self._index(
                invoice.pk, invoice.supplier_id, invoice.invoice_number, invoice.source_sha256,
                invoice.imported_at, invoice.source_file.name if invoice.source_file else "",
            )

    def _rank(self, ids: Iterable[int], occurrence: int) -> int | None:
        ordered = sorted(set(ids), key=self._order.__getitem__)
        return ordered[occurrence] if 0 <= occurrence < len(ordered) else None

    def _by_content(self, key: dict) -> int | None:
        shas = {sha for sha in (key.get("sha256"), key.get("file_sha256")) if isinstance(sha, str) and sha}
        if not shas:
            return None
        occurrence = key.get("occurrence", 0)
        if isinstance(occurrence, bool) or not isinstance(occurrence, int):
            return None
        found = self._rank([pk for sha in shas for pk in self._by_sha.get(sha, [])], occurrence)
        if found is not None:
            return found
        if self._by_file_sha is None:
            self._by_file_sha = defaultdict(list)
            for pk, name in self._unhashed.items():
                sha = file_sha256(name)
                if sha:
                    self._by_file_sha[sha].append(pk)
        return self._rank([pk for sha in shas for pk in self._by_file_sha.get(sha, [])], occurrence)

    def resolve(self, key: dict, report=None):
        if not isinstance(key, dict):
            return None
        canonical = _canonical(key)
        if canonical in self._bound:
            return self._bound[canonical]
        number = key.get("number") or ""
        by_number = None
        if number:
            supplier = self.suppliers.resolve(key.get("supplier"))
            if supplier is not None:
                by_number = self._by_number.get((supplier.pk, number))
        by_file = self._by_content(key)
        if by_number is not None and by_file is not None and by_number != by_file:
            return Ambiguous(self._invoice(by_number), self._invoice(by_file))
        chosen = by_number if by_number is not None else by_file
        if chosen is None:
            return None
        if by_number is None and report is not None and self._numbers.get(chosen, "") != number:
            report.note(
                f"rapproché par son fichier : n° {self._numbers.get(chosen) or '—'} ici, "
                f"n° {number or '—'} dans l'archive"
            )
        return self._invoice(chosen)


def lines_by_ordinal(invoice) -> list:
    """An invoice's lines by ordinal: the rank of the line's id within its
    invoice, 0-based."""
    return list(invoice.lines.order_by("id"))


def line_ordinals(invoice_ids: Iterable[int]) -> dict[int, tuple[int, int]]:
    """line pk → (invoice pk, ordinal), for every line of these invoices."""
    from invoices.models import InvoiceLine

    result: dict[int, tuple[int, int]] = {}
    counters: dict[int, int] = defaultdict(int)
    for pk, invoice_id in InvoiceLine.objects.filter(invoice_id__in=list(invoice_ids)).order_by(
        "invoice_id", "id"
    ).values_list("id", "invoice_id"):
        result[pk] = (invoice_id, counters[invoice_id])
        counters[invoice_id] += 1
    return result
