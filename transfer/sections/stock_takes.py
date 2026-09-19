"""« Inventaires » (§7.9): every count with its frozen value, the invoice
lines that priced it, and the losses and corrections written down.

A count's value is **not** derived data. It was priced once, from the
purchases known that day, and frozen (StockTake) so that a later correction
of an invoice cannot rewrite what a past count was worth. So it is copied as
it is - value, shortfall and every FIFO slice - and never recomputed, even
when an invoice line here now says another price.

Its sources are re-pointed at this database's invoice lines by key (the
invoice's natural key and the line's rank in it), and each is checked
against the line's name: a count whose line cannot be found, or is no
longer the product it was priced from, is refused whole rather than
imported with a hole in it or revalued in silence (§6.3).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.db.models import Prefetch
from django.utils import timezone

from inventory.models import (
    MovementKind,
    StockMovement,
    StockTake,
    StockTakeLine,
    StockTakeLineSource,
)
from transfer import codec, keys, registry
from transfer.archive import ArchiveError
from transfer.keys import fold
from transfer.sections.base import Section

LINE_FIELDS = ("counted_quantity", "unit", "value_ht", "has_shortfall", "shortfall_quantity")
LINE_REQUIRED = ("counted_quantity", "unit", "value_ht")
SOURCE_FIELDS = ("quantity_used", "unit_cost_ht")
MOVEMENT_FIELDS = ("kind", "quantity", "unit_cost_ht", "note", "occurred_on", "created_at")
TAKE_KNOWN = ("taken_at", "note", "created_at", "lines")
LINE_KNOWN = ("product", "article", *LINE_FIELDS, "sources")
SOURCE_KNOWN = ("invoice", "line", "raw_name", *SOURCE_FIELDS)
MOVEMENT_KNOWN = ("article", *MOVEMENT_FIELDS)
WRITTEN_DOWN = (MovementKind.LOSS, MovementKind.CORRECTION)

TAKES = "inventaires"
LINES = "lignes comptées"
MOVEMENTS = "pertes et corrections"


class _Skip(Exception):
    """A count (or a loss) that cannot be imported whole: the message says
    why, in French."""


def day(moment) -> str:
    if not moment:
        return "?"
    try:
        return f"{timezone.localtime(moment):%d/%m/%Y}"
    except (OverflowError, ValueError, OSError):
        # Saying the date must never be what fails: a moment off the
        # calendar (a hand-edited archive) has no local time here, and this
        # is how a refusal names the count it is about (codec.load refuses
        # such a moment, so nothing stored reaches this).
        return "?"


def describe_invoice(invoice) -> str:
    if invoice.invoice_number:
        return f"la facture {invoice.supplier.name} n° {invoice.invoice_number}"
    if invoice.invoice_date:
        return f"le document {invoice.supplier.name} du {invoice.invoice_date:%d/%m/%Y}"
    return f"le document {invoice.supplier.name} sans numéro ni date"


def _when(record) -> str:
    """A loss's date as the pages write it, for a message about a record
    that may be unreadable."""
    for name in ("occurred_on", "created_at"):
        value = record.get(name)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).strftime("%d/%m/%Y")
            except ValueError:
                return value
    return "?"


def _number(value) -> str:
    return "?" if value is None else str(value)


def _source_row(line_pk, values) -> tuple:
    return (line_pk, _number(values.get("quantity_used")), _number(values.get("unit_cost_ht")))


def movement_print(article_name, values) -> str:
    """A written-down movement has no key of its own: its whole content is
    the key, creation time included, so two identical losses typed a minute
    apart stay two."""
    created_at = values.get("created_at")
    occurred_on = values.get("occurred_on")
    content = [
        fold(article_name),
        values.get("kind"),
        _number(values.get("quantity")),
        _number(values.get("unit_cost_ht")),
        values.get("note") or "",
        occurred_on.isoformat() if occurred_on else None,
        created_at.astimezone(dt_timezone.utc).isoformat() if created_at else None,
    ]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode("utf-8")).hexdigest()


def _here_print(movement) -> str:
    return movement_print(
        movement.stock_type.name,
        {
            "kind": movement.kind,
            "quantity": codec.load(StockMovement, "quantity", codec.dump(movement, "quantity")),
            "unit_cost_ht": codec.load(StockMovement, "unit_cost_ht", codec.dump(movement, "unit_cost_ht")),
            "note": movement.note,
            "occurred_on": movement.occurred_on,
            "created_at": movement.created_at,
        },
    )


def pair_takes(records, here: dict):
    """Each record of the archive's counts with its moment, the count here it
    answers to or None, and why it cannot be read ("" when it can).

    A count is keyed by its moment, then by its rank among the counts of
    that moment (by id here, by order in the file): two counts taken at the
    same instant stay two. `here`: moment → the counts here at that moment,
    by id. The one rule `apply` pairs by, and `named_takes` too."""
    seen: Counter = Counter()
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            yield record, None, None, "Inventaire illisible dans l'archive : ignoré"
            continue
        try:
            taken_at = codec.load(StockTake, "taken_at", record.get("taken_at"))
        except codec.FieldValueError as exc:
            yield record, None, None, f"Inventaire sans date lisible : {exc}"
            continue
        occurrence = seen[taken_at]
        seen[taken_at] += 1
        takes = here.get(taken_at, [])
        yield record, taken_at, takes[occurrence] if occurrence < len(takes) else None, ""


def named_takes(records) -> set[int]:
    """The counts here the archive's counts answer to - named, even by a
    record the import then skips. Under « Remplacer », this section's prune
    deletes every other count. The invoices apply before it and prune after
    it: a document they keep for a count is kept for one that stays
    (sections/invoices.py `_trail_kept`) - asked here, so the two never
    disagree on which counts go."""
    here: dict = defaultdict(list)
    for take in StockTake.objects.order_by("taken_at", "id").only("id", "taken_at"):
        here[take.taken_at].append(take)
    return {take.pk for _record, _taken_at, take, _why in pair_takes(records, here) if take is not None}


def _takes_with_lines():
    return StockTake.objects.order_by("taken_at", "id").prefetch_related(
        Prefetch(
            "lines",
            queryset=StockTakeLine.objects.order_by("id").select_related("product__supplier", "stock_type"),
        ),
        Prefetch(
            "lines__sources",
            queryset=StockTakeLineSource.objects.order_by("id").select_related("invoice_line"),
        ),
    )


@registry.register
class StockTakesSection(Section):
    key = "inventaires"

    # -- what this database holds ----------------------------------------------------
    def count(self) -> dict[str, int]:
        return {
            TAKES: StockTake.objects.count(),
            LINES: StockTakeLine.objects.count(),
            MOVEMENTS: StockMovement.objects.filter(kind__in=WRITTEN_DOWN).count(),
        }

    def snapshot(self):
        takes = list(_takes_with_lines())
        invoice_ids = {source.invoice_line.invoice_id for take in takes for line in take.lines.all()
                       for source in line.sources.all()}
        invoice_keys = keys.invoice_keys(invoice_ids) if invoice_ids else {}
        ordinals = keys.line_ordinals(invoice_ids)

        def line_key(line):
            if line.product_id:
                return ("produit", line.product.supplier.code, line.product.raw_name)
            return ("article", line.stock_type.name)

        snapshot_takes = sorted(
            (
                codec.dump(take, "taken_at"),
                take.note,
                codec.dump(take, "created_at"),
                tuple(sorted(
                    (
                        line_key(line),
                        *(codec.dump(line, name) for name in LINE_FIELDS),
                        tuple(sorted(
                            (
                                tuple(sorted(invoice_keys[source.invoice_line.invoice_id].items())),
                                ordinals[source.invoice_line_id][1],
                                source.invoice_line.raw_name,
                                *(codec.dump(source, name) for name in SOURCE_FIELDS),
                            )
                            for source in line.sources.all()
                        )),
                    )
                    for line in take.lines.all()
                )),
            )
            for take in takes
        )
        # An undated loss dumps None beside a dated one's text: ordered by
        # repr, never by comparing the two.
        movements = sorted(
            (
                (movement.stock_type.name, *(codec.dump(movement, name) for name in MOVEMENT_FIELDS))
                for movement in StockMovement.objects.filter(kind__in=WRITTEN_DOWN).select_related("stock_type")
            ),
            key=repr,
        )
        return {"stock_takes": snapshot_takes, "movements": movements}

    # -- export ----------------------------------------------------------------------
    def export(self, out) -> None:
        takes = list(_takes_with_lines())
        invoice_ids = {source.invoice_line.invoice_id for take in takes for line in take.lines.all()
                       for source in line.sources.all()}
        invoice_keys = keys.invoice_keys(invoice_ids) if invoice_ids else {}
        ordinals = keys.line_ordinals(invoice_ids)
        supplier_names: dict[str, str] = {}
        if invoice_ids:
            from invoices.models import Invoice

            supplier_names.update(
                Invoice.objects.filter(pk__in=invoice_ids).values_list("supplier__code", "supplier__name")
            )
        records = []
        line_count = 0
        for take in takes:
            lines = []
            for line in take.lines.all():
                line_count += 1
                if line.product_id:
                    supplier_names[line.product.supplier.code] = line.product.supplier.name
                lines.append({
                    "product": [line.product.supplier.code, line.product.raw_name] if line.product_id else None,
                    "article": line.stock_type.name if line.stock_type_id else None,
                    **codec.record(line, LINE_FIELDS),
                    "sources": [
                        {
                            "invoice": invoice_keys[source.invoice_line.invoice_id],
                            "line": ordinals[source.invoice_line_id][1],
                            "raw_name": source.invoice_line.raw_name,
                            **codec.record(source, SOURCE_FIELDS),
                        }
                        for source in line.sources.all()
                    ],
                })
            records.append({**codec.record(take, ("taken_at", "note", "created_at")), "lines": lines})
        movements = [
            {"article": movement.stock_type.name, **codec.record(movement, MOVEMENT_FIELDS)}
            for movement in StockMovement.objects.filter(kind__in=WRITTEN_DOWN)
            .select_related("stock_type")
            .order_by("created_at", "id")
        ]
        out.write(
            {"supplier_names": supplier_names, "stock_takes": records, "movements": movements},
            {TAKES: len(records), LINES: line_count, MOVEMENTS: len(movements)},
        )

    # -- import ----------------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        for name in ("stock_takes", "movements"):
            if not isinstance(payload.get(name), list):
                raise ArchiveError(f"Archive refusée : inventaires.json n'a pas de liste « {name} ».")
        if "supplier_names" in payload and not isinstance(payload["supplier_names"], dict):
            raise ArchiveError("Archive refusée : inventaires.json a un « supplier_names » illisible.")
        self.takes = payload["stock_takes"]
        self.movements = payload["movements"]

    def apply(self, ctx, report) -> None:
        self._ctx = ctx
        self._report = report
        # A count of a product this database lacks is never put on its twin
        # (a name differing only by an accented capital) by its folded name:
        # the archive names both, so they are two - the context's resolver
        # knows what the archive names.
        self._products = ctx.products()
        self._articles = ctx.articles()
        self._invoice_lines: dict[int, list] = {}
        #: Counts here a record of the file names - applied or not: prune keeps them.
        self._matched: set[int] = set()
        #: Written-down movements here the file has.
        self._matched_movements: set[int] = set()
        self._apply_takes(ctx.replacing(self.key))
        self._apply_movements()

    def _apply_takes(self, replacing: bool) -> None:
        report = self._report
        here: dict = defaultdict(list)
        for take in _takes_with_lines():
            here[take.taken_at].append(take)
        created = []
        for record, taken_at, existing, unreadable in pair_takes(self.takes, here):
            if isinstance(record, dict):
                codec.note_unknown(report, record, TAKE_KNOWN, "inventaires.")
            if unreadable:
                report.skip(unreadable)
                continue
            label = f"Inventaire du {day(taken_at)}"
            if existing is not None:
                self._matched.add(existing.pk)
            try:
                parsed = self._parse_take(record)
            except (_Skip, codec.FieldValueError) as exc:
                report.skip(f"{label} : {exc}")
                continue

            if existing is None:
                take = StockTake.objects.create(taken_at=taken_at, note=parsed["note"] or "")
                for line in parsed["lines"]:
                    self._create_line(take, line)
                if parsed["created_at"] is not None:
                    take.created_at = parsed["created_at"]
                    created.append(take)
                self._matched.add(take.pk)
                report.created(TAKES)
                if parsed["lines"]:
                    report.created(LINES, len(parsed["lines"]))
                continue

            differences = self._differences(existing, record, parsed)
            if not differences:
                report.unchanged(TAKES)
                report.unchanged(LINES, len(parsed["lines"]))
            elif replacing:
                self._replace(existing, record, parsed)
            else:
                report.conflict(f"{label} : différent dans l'archive ({', '.join(differences)}) — gardé tel quel")
        # auto_now_add wrote "now" on insert; the archive's date is the true one.
        StockTake.objects.bulk_update(created, ["created_at"])

    def _parse_take(self, record) -> dict:
        """Everything a count needs, resolved here - or _Skip: a count is
        imported whole or not at all."""
        note = codec.load(StockTake, "note", record["note"]) if "note" in record else None
        created_at = codec.load(StockTake, "created_at", record["created_at"]) if record.get("created_at") else None
        lines = record.get("lines")
        if not isinstance(lines, list):
            raise _Skip("ses lignes sont illisibles")
        parsed = []
        keys_seen = set()
        for line in lines:
            if not isinstance(line, dict):
                raise _Skip("une de ses lignes est illisible")
            codec.note_unknown(self._report, line, LINE_KNOWN, "inventaires.lignes.")
            product, article = self._line_target(line)
            key = ("produit", product.pk) if product is not None else ("article", article.pk)
            if key in keys_seen:
                raise _Skip(f"« {(product or article)} » est compté deux fois")
            keys_seen.add(key)
            for name in LINE_REQUIRED:
                if line.get(name) is None:
                    raise _Skip(f"une ligne sans « {name} » ({product or article})")
            values = {name: codec.load(StockTakeLine, name, line[name]) for name in LINE_FIELDS if name in line}
            sources = line.get("sources", [])
            if not isinstance(sources, list):
                raise _Skip(f"les sources de « {product or article} » sont illisibles")
            parsed.append({
                "key": key,
                "product": product,
                "article": article,
                "record": line,
                "values": values,
                "sources": [self._source(source) for source in sources],
            })
        return {"note": note, "created_at": created_at, "lines": parsed}

    def _line_target(self, line):
        product_key, article_name = line.get("product"), line.get("article")
        if product_key is not None and article_name is not None:
            raise _Skip("une ligne compte à la fois un produit et un article")
        if product_key is None and article_name is None:
            raise _Skip("une ligne ne compte ni produit ni article")
        if product_key is not None:
            if (
                not isinstance(product_key, list)
                or len(product_key) != 2
                or not all(isinstance(part, str) for part in product_key)
            ):
                raise _Skip("une ligne nomme un produit illisible")
            code, raw_name = product_key
            supplier = self._ctx.suppliers.resolve(code)
            product = self._products.resolve(supplier, raw_name) if supplier is not None else None
            if product is None:
                supplier_name = supplier.name if supplier is not None else (self._ctx.suppliers.names.get(code) or code)
                raise _Skip(f"produit inconnu « {raw_name} » ({supplier_name})")
            return product, None
        article = self._articles.resolve(article_name)
        if article is None:
            raise _Skip(f"article inconnu « {article_name} »")
        return None, article

    def _source(self, source) -> tuple:
        """(invoice line here, loaded values) for one FIFO slice."""
        if not isinstance(source, dict):
            raise _Skip("une de ses sources est illisible")
        codec.note_unknown(self._report, source, SOURCE_KNOWN, "inventaires.sources.")
        key = source.get("invoice")
        found = self._ctx.invoices.resolve(key, report=self._report) if isinstance(key, dict) else None
        if isinstance(found, keys.Ambiguous):
            raise _Skip(
                f"deux documents différents répondent à {describe_invoice(found.by_number)} "
                f"(par son numéro, et {describe_invoice(found.by_file)} par son fichier)"
            )
        if found is None:
            raise _Skip(f"facture absente : {self._describe_key(key)}")
        ordinal = source.get("line")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise _Skip(f"une source de {describe_invoice(found)} n'a pas de rang de ligne lisible")
        lines = self._lines_of(found)
        if ordinal >= len(lines):
            raise _Skip(f"{describe_invoice(found)} n'a pas de ligne n°{ordinal + 1}")
        line = lines[ordinal]
        raw_name = source.get("raw_name")
        if not isinstance(raw_name, str) or fold(raw_name) != fold(line.raw_name):
            raise _Skip(
                f"la ligne n°{ordinal + 1} de {describe_invoice(found)} n'est plus « {raw_name} » : "
                "il ne sera pas revalorisé en silence"
            )
        for name in SOURCE_FIELDS:
            if source.get(name) is None:
                raise _Skip(f"une source de {describe_invoice(found)} sans « {name} »")
        return line, {name: codec.load(StockTakeLineSource, name, source[name]) for name in SOURCE_FIELDS}

    def _describe_key(self, key) -> str:
        if not isinstance(key, dict):
            return "illisible"
        code = key.get("supplier")
        name = (self._ctx.suppliers.names.get(code) or code) if isinstance(code, str) else "?"
        number = key.get("number")
        return f"{name} n° {number}" if number else f"un document {name} sans numéro"

    def _lines_of(self, invoice) -> list:
        if invoice.pk not in self._invoice_lines:
            self._invoice_lines[invoice.pk] = keys.lines_by_ordinal(invoice)
        return self._invoice_lines[invoice.pk]

    def _create_line(self, take, line) -> StockTakeLine:
        row = StockTakeLine(stock_take=take, product=line["product"], stock_type=line["article"], **line["values"])
        row.save()
        StockTakeLineSource.objects.bulk_create(
            StockTakeLineSource(stock_take_line=row, invoice_line=invoice_line, **values)
            for invoice_line, values in line["sources"]
        )
        return row

    @staticmethod
    def _sources_here(line) -> list:
        return sorted(
            _source_row(source.invoice_line_id, {name: getattr(source, name) for name in SOURCE_FIELDS})
            for source in line.sources.all()
        )

    @staticmethod
    def _sources_in_file(line) -> list:
        return sorted(_source_row(invoice_line.pk, values) for invoice_line, values in line["sources"])

    def _differences(self, take, record, parsed) -> list[str]:
        """What differs, in the words of the report (note, lignes, sources);
        a field the file leaves out is not said, so never a difference."""
        different = []
        if "note" in record and codec.differences(take, record, ("note",)):
            different.append("note")
        here = {self._key_of(line): line for line in take.lines.all()}
        wanted = {line["key"]: line for line in parsed["lines"]}
        if set(here) != set(wanted) or any(
            codec.differences(here[key], wanted[key]["record"], LINE_FIELDS) for key in wanted
        ):
            different.append("lignes")
        if any(
            key in here and self._sources_here(here[key]) != self._sources_in_file(wanted[key]) for key in wanted
        ):
            different.append("sources")
        return different

    @staticmethod
    def _key_of(line) -> tuple:
        return ("produit", line.product_id) if line.product_id else ("article", line.stock_type_id)

    def _replace(self, take, record, parsed) -> None:
        """The count becomes the file's: its note, and its lines by key -
        updated, created or deleted - each line's sources replaced whole."""
        report = self._report
        if "note" in record and codec.assign(take, record, ("note",)):
            take.save(update_fields=["note"])
        here = {self._key_of(line): line for line in take.lines.all()}
        wanted = {line["key"]: line for line in parsed["lines"]}
        gone = [line.pk for key, line in here.items() if key not in wanted]
        if gone:
            StockTakeLine.objects.filter(pk__in=gone).delete()
            report.deleted(LINES, len(gone))
        for key, line in wanted.items():
            row = here.get(key)
            if row is None:
                self._create_line(take, line)
                report.created(LINES)
                continue
            changed = codec.assign(row, line["record"], LINE_FIELDS)
            if changed:
                row.save(update_fields=changed)
            if self._sources_here(row) != self._sources_in_file(line):
                row.sources.all().delete()
                StockTakeLineSource.objects.bulk_create(
                    StockTakeLineSource(stock_take_line=row, invoice_line=invoice_line, **values)
                    for invoice_line, values in line["sources"]
                )
                changed = True
            if changed:
                report.updated(LINES)
            else:
                report.unchanged(LINES)
        report.updated(TAKES)

    def _apply_movements(self) -> None:
        report = self._report
        here: dict[str, list[int]] = defaultdict(list)
        for movement in StockMovement.objects.filter(kind__in=WRITTEN_DOWN).select_related("stock_type").order_by("id"):
            here[_here_print(movement)].append(movement.pk)
        created = []
        for record in self.movements:
            if not isinstance(record, dict):
                report.skip("Perte ou correction illisible dans l'archive : ignorée")
                continue
            codec.note_unknown(report, record, MOVEMENT_KNOWN, "pertes.")
            try:
                article, values = self._parse_movement(record)
            except (_Skip, codec.FieldValueError) as exc:
                report.skip(f"Perte ou correction du {_when(record)} : {exc}")
                continue
            fingerprint = movement_print(article.name, values)
            if here[fingerprint]:
                self._matched_movements.add(here[fingerprint].pop(0))
                report.unchanged(MOVEMENTS)
                continue
            created_at = values.pop("created_at", None)
            movement = StockMovement.objects.create(stock_type=article, **values)
            if created_at is not None:
                movement.created_at = created_at
                created.append(movement)
            self._matched_movements.add(movement.pk)
            report.created(MOVEMENTS)
        StockMovement.objects.bulk_update(created, ["created_at"])

    def _parse_movement(self, record):
        name = record.get("article")
        article = self._articles.resolve(name) if isinstance(name, str) else None
        if article is None:
            raise _Skip(f"article inconnu « {name} »")
        if record.get("kind") not in WRITTEN_DOWN:
            raise _Skip("seules les pertes et les corrections se copient : un achat vient de sa facture")
        if record.get("quantity") is None:
            raise _Skip("sans quantité")
        values = {name: codec.load(StockMovement, name, record[name]) for name in MOVEMENT_FIELDS if name in record}
        # What the file leaves out takes the model's default - and the
        # fingerprint is computed on that, as it will be stored.
        if values.get("note") is None:
            values["note"] = ""
        if values.get("unit_cost_ht") is None:
            values["unit_cost_ht"] = Decimal("0.0000")
        values.setdefault("occurred_on", None)
        return article, values

    def prune(self, ctx, report) -> None:
        """Counts and losses the file does not have go. Runs before the
        invoices' prune (reverse order), which is what releases the invoice
        lines a removed count was priced from."""
        gone = StockTake.objects.exclude(pk__in=self._matched)
        lines = StockTakeLine.objects.filter(stock_take__in=gone).count()
        _count, per_model = gone.delete()
        takes = per_model.get(StockTake._meta.label, 0)
        if takes:
            report.deleted(TAKES, takes)
        if lines:
            report.deleted(LINES, lines)
        _count, per_model = (
            StockMovement.objects.filter(kind__in=WRITTEN_DOWN).exclude(pk__in=self._matched_movements).delete()
        )
        movements = per_model.get(StockMovement._meta.label, 0)
        if movements:
            report.deleted(MOVEMENTS, movements)

    # -- clear -----------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        lines = StockTakeLine.objects.count()
        _count, per_model = StockTake.objects.all().delete()
        takes = per_model.get(StockTake._meta.label, 0)
        if takes:
            report.deleted(TAKES, takes)
        if lines:
            report.deleted(LINES, lines)
        _count, per_model = StockMovement.objects.filter(kind__in=WRITTEN_DOWN).delete()
        movements = per_model.get(StockMovement._meta.label, 0)
        if movements:
            report.deleted(MOVEMENTS, movements)
