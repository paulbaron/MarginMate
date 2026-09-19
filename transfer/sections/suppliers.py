"""« Enseignes et fournisseurs » (§7.1): every supplier with what files a
document under it - its name, the header its tickets print, the figures it
learned, whether its documents are charges - and the prices a shop's
unnamed ticket lines are known by (`ShopItemPrice`).

Two things are never an import's or a clear's to touch:

* **Metro's firewall state** (`scrape_*`): when AdminMate last signed in
  there, when it was refused, and until when it leaves the site alone.
  Never exported, never written, never reset. A restore that put back an
  older pause, or none, would let the next gather sign in to a site that
  blocks it - METRO was paused until 02/10 when this was written.
* **Code-bound suppliers** (`code_bound`): a reader or a till of their own
  is keyed on their code (`ticket_parser_for`, `workspace.OWN_MODULE`,
  scrapers/metro.py). They are never deleted - by a replace or a clear -
  so never re-created either, and an import never changes their code or
  their parser.

An import writes the archive's state; it is not an act in a supplier's
history: no `SupplierChange` is recorded (§6.6).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import ProtectedError, RestrictedError

from transfer import codec, registry
from transfer.archive import ArchiveError
from transfer.keys import fold
from transfer.sections.base import Section

KEY = "fournisseurs"

SUPPLIER_FIELDS = (
    "code",
    "name",
    "parser_key",
    "is_scrapable",
    "ticket_header",
    "ticket_identifiers",
    "refused_identifiers",
    "expenses_only",
)
#: Every other concrete field of Supplier, and why it stays out of the
#: archive (a guard test holds this: a field added later cannot be forgotten
#: in silence).
NOT_EXPORTED = {
    "id": "pk",
    "scrape_last_login_at": "état du pare-feu de Metro",
    "scrape_last_block_at": "état du pare-feu de Metro",
    "scrape_paused_until": "état du pare-feu de Metro",
    "scrape_pause_reason": "état du pare-feu de Metro",
}
PRICE_FIELDS = ("unit_price_ttc", "label", "valid_from", "created_at")
PRICE_NOT_EXPORTED = {"id": "pk", "supplier": "parent"}

#: What « Fusionner » may fill when this database leaves it blank (§6.1).
FILLABLE = ("ticket_header", "ticket_identifiers", "refused_identifiers", "parser_key")
#: Compared to say « inchangé » or a conflict. The code is the key itself.
COMPARED = tuple(name for name in SUPPLIER_FIELDS if name != "code")
KNOWN = (*SUPPLIER_FIELDS, "item_prices")
LIST_FIELDS = ("ticket_identifiers", "refused_identifiers")

LABELS = {
    "name": "nom",
    "parser_key": "lecteur",
    "is_scrapable": "récupération automatique",
    "ticket_header": "en-tête",
    "ticket_identifiers": "identifiants",
    "refused_identifiers": "identifiants écartés",
    "expenses_only": "nature",
    "item_prices": "prix connus",
}

#: A shop's `ShopItemPrice` rows, under the words the review page uses for
#: them (« Prix connus chez … »): « prix d'articles » read as the prices of
#: articles, which is what the application calls a StockType.
PRICES = "prix connus"

#: What happens to the AI pseudo-supplier, on a row of its own: it is no
#: supplier on the page (`_counted`), so never one of the « fournisseurs ».
AI_ROW = "fiche de l'analyse IA"

KEPT_BOUND = "lecteur propre / caisse réglée dans l'application"
CHARGES_KEPT = (
    "« {name} » : passer en charges (ou en revenir) se fait depuis sa fiche (« changer… »), qui relit ses documents"
)


# -- helpers every lane-A section shares ------------------------------------------------

def said(names, labels=LABELS) -> str:
    """At most three differing fields, in French, for a conflict line."""
    shown = [labels.get(name, name) for name in names]
    return ", ".join(shown[:3]) + (", …" if len(shown) > 3 else "")


def plural(count: int, singular: str, several: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (several or singular + 's')}"


def money(value) -> str:
    return f"{Decimal(value):.2f}".replace(".", ",") + " €"


def day(value) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


def block(ctx, file_code: str) -> None:
    """This file code must resolve to nothing, for every later section: its
    supplier was skipped, and falling back to its name would put its
    documents on another supplier here (the one that name belongs to)."""
    ctx.suppliers.refuse(file_code)


def code_bound(supplier) -> bool:
    """A reader or a till of its own keyed on its code, or the AI
    pseudo-supplier - the rule `supplier_views.delete_refused` uses, plus
    the latter, which has no page to be deleted from."""
    from invoices.parsers import LLM_PARSER_KEY, ticket_parser_for
    from invoices.receipts import has_own_reader

    return (
        ticket_parser_for(supplier.code) is not None
        or has_own_reader(supplier)
        or supplier.parser_key == LLM_PARSER_KEY
    )


def _counted(supplier) -> bool:
    """Whether the counts shown on the page count it - count(), the
    archive's counts and the report's « fournisseurs » alike (`_tally`): not
    the AI pseudo-supplier, which nobody sees as a supplier - exported all
    the same, since the documents it read are filed under it."""
    from invoices.parsers import LLM_PARSER_KEY

    return supplier.parser_key != LLM_PARSER_KEY


def _tally(report, supplier, how: str) -> None:
    """Count what happens to a supplier (`how`: created, updated, deleted or
    unchanged) as count() counts suppliers. Among them, the report said « 30
    inchangés » of the 29 the page and the archive announced. Left
    « inchangé », the AI pseudo-supplier is counted nowhere; changed, on
    `AI_ROW` - never hidden, since the safety archive is chosen from what
    the report counts (safety.sections_at_risk)."""
    if _counted(supplier):
        getattr(report, how)("fournisseurs")
    elif how != "unchanged":
        getattr(report, how)(AI_ROW)


def known_parser(key: str) -> bool:
    """A parser this application has: a key it does not know would file
    the supplier's documents empty, and say nothing."""
    from invoices.parsers import LLM_PARSER_KEY, PARSER_REGISTRY

    return key == "" or key == LLM_PARSER_KEY or key in PARSER_REGISTRY


def _check_lists(record: dict) -> None:
    for name in LIST_FIELDS:
        value = record.get(name)
        if name in record and not (isinstance(value, list) and all(isinstance(item, str) for item in value)):
            raise codec.FieldValueError(f"« {name} » : liste de textes attendue")


# -- item prices --------------------------------------------------------------------------

def _price_key(price) -> tuple[Decimal, date | None]:
    return (Decimal(price.unit_price_ttc).quantize(Decimal("0.01")), price.valid_from)


def _price_text(supplier_name: str, key) -> str:
    price, valid_from = key
    since = f" (depuis le {day(valid_from)})" if valid_from else ""
    return f"Prix {money(price)} de {supplier_name}{since}"


def _file_prices(record: dict, supplier_name: str, report) -> list[tuple[tuple, dict, dict]]:
    """The record's item prices, checked: (key, loaded values, raw record).
    One that does not load, or repeats a key, is skipped with its reason."""
    from invoices.models import ShopItemPrice

    items = record.get("item_prices")
    if items is None:
        return []
    if not isinstance(items, list):
        report.skip(f"Prix connus de {supplier_name} : liste illisible dans l'archive")
        return []
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            report.skip(f"Un prix connu de {supplier_name} est illisible dans l'archive")
            continue
        codec.note_unknown(report, item, PRICE_FIELDS, f"{PRICES} : ")
        try:
            values = {name: codec.load(ShopItemPrice, name, item[name]) for name in PRICE_FIELDS if name in item}
            if values.get("unit_price_ttc") is None:
                raise codec.FieldValueError("« unit_price_ttc » : valeur manquante")
            if not values.get("label"):
                raise codec.FieldValueError("« label » : valeur manquante")
        except codec.FieldValueError as exc:
            report.skip(f"Prix connu de {supplier_name} : {exc}")
            continue
        key = (values["unit_price_ttc"], values.get("valid_from"))
        if key in seen:
            report.skip(f"{_price_text(supplier_name, key)} : en double dans l'archive")
            continue
        seen.add(key)
        result.append((key, values, item))
    return result


def _create_price(supplier, values: dict, report) -> None:
    from invoices.models import ShopItemPrice

    price = ShopItemPrice.objects.create(
        supplier=supplier,
        unit_price_ttc=values["unit_price_ttc"],
        label=values["label"],
        valid_from=values.get("valid_from"),
    )
    if values.get("created_at") is not None:
        # auto_now_add wrote "now" over it; update() does not call pre_save.
        ShopItemPrice.objects.filter(pk=price.pk).update(created_at=values["created_at"])
    report.created(PRICES)


# -- the section ------------------------------------------------------------------------

@registry.register
class SuppliersSection(Section):
    key = KEY

    # -- what this database holds ----------------------------------------------------
    def count(self) -> dict[str, int]:
        from invoices.models import ShopItemPrice, Supplier

        return {
            "fournisseurs": sum(1 for supplier in Supplier.objects.only("parser_key") if _counted(supplier)),
            PRICES: ShopItemPrice.objects.count(),
        }

    def snapshot(self):
        from invoices.models import Supplier

        result = []
        for supplier in Supplier.objects.order_by("code").prefetch_related("item_prices"):
            prices = sorted(
                (codec.record(price, PRICE_FIELDS) for price in supplier.item_prices.all()),
                key=lambda row: (Decimal(row["unit_price_ttc"]), row["valid_from"] or ""),
            )
            result.append({**codec.record(supplier, SUPPLIER_FIELDS), "item_prices": prices})
        return result

    # -- export ------------------------------------------------------------------------
    def export(self, out) -> None:
        from invoices.models import Supplier

        suppliers = list(Supplier.objects.order_by("code").prefetch_related("item_prices"))
        records = []
        prices = 0
        for supplier in suppliers:
            items = sorted(
                supplier.item_prices.all(),
                key=lambda price: (price.unit_price_ttc, price.valid_from or date.min, price.pk),
            )
            prices += len(items)
            records.append(
                {
                    **codec.record(supplier, SUPPLIER_FIELDS),
                    "item_prices": [codec.record(price, PRICE_FIELDS) for price in items],
                }
            )
        out.write(
            {"supplier_names": {supplier.code: supplier.name for supplier in suppliers}, "suppliers": records},
            # Counted as count() counts them: the import tab sets the two
            # side by side, and on identical data they must read the same.
            {"fournisseurs": sum(1 for supplier in suppliers if _counted(supplier)), PRICES: prices},
        )

    # -- import ------------------------------------------------------------------------
    def load(self, src) -> None:
        payload = src.payload()
        records = payload.get("suppliers")
        if not isinstance(records, list):
            raise ArchiveError("Archive refusée : fournisseurs.json n'a pas de liste « suppliers ».")
        self.records = records
        # Filled by apply(): every supplier here a record of the archive
        # stands for. Prune deletes the others (or keeps them, and says why).
        self.claimed: set[int] = set()

    def _match(self, ctx, report) -> list[tuple[dict, object]]:
        """Each usable record with the supplier it stands for here, or None
        (to create). By code first, then - for a code unknown here - by name,
        a supplier here standing for one record only: two records claiming
        one supplier would merge two suppliers of the archive into one."""
        from invoices.models import Supplier

        here = list(Supplier.objects.all())
        by_code = {supplier.code: supplier for supplier in here}
        by_name: dict[str, object] = {}
        for supplier in here:
            by_name.setdefault(fold(supplier.name), supplier)
        claimed: dict[int, str] = {}
        matched: list[list] = []
        seen: set[str] = set()
        for record in self.records:
            if not isinstance(record, dict):
                report.skip("Un fournisseur de l'archive est illisible.")
                continue
            codec.note_unknown(report, record, KNOWN)
            code, name = record.get("code"), record.get("name")
            if not isinstance(code, str) or not code:
                report.skip("Un fournisseur de l'archive n'a pas de code.")
                continue
            if code in seen:
                report.skip(f"Fournisseur de code « {code} » : en double dans l'archive")
                continue
            seen.add(code)
            if not isinstance(name, str) or not fold(name):
                report.skip(f"Fournisseur de code « {code} » : sans nom")
                if code not in by_code:
                    block(ctx, code)
                continue
            supplier = by_code.get(code)
            if supplier is not None:
                claimed[supplier.pk] = code
            matched.append([record, supplier])

        result = []
        for record, supplier in matched:
            code, name = record["code"], record["name"]
            if supplier is None:
                found = by_name.get(fold(name))
                if found is not None and found.pk in claimed:
                    report.skip(
                        f"Fournisseur « {name} » (code {code} dans l'archive) : son nom est ici celui du code "
                        f"{found.code}, qui répond déjà au code {claimed[found.pk]} de l'archive"
                    )
                    block(ctx, code)
                    continue
                if found is not None:
                    claimed[found.pk] = code
                    ctx.suppliers.bind(code, found)
                    report.note(f"« {found.name} » : code {found.code} ici, {code} dans l'archive — rapprochés par le nom")
                    supplier = found
            result.append((record, supplier))
        return result

    def apply(self, ctx, report) -> None:
        replacing = ctx.replacing(self.key)
        for record, supplier in self._match(ctx, report):
            name = record["name"]
            if supplier is not None:
                # In the archive, even if its record is skipped below: prune
                # must never take it for one the archive does not have.
                self.claimed.add(supplier.pk)
            try:
                _check_lists(record)
                if supplier is None:
                    created = self._create(ctx, report, record)
                    if created is not None:
                        self.claimed.add(created.pk)
                elif replacing:
                    self._replace(ctx, report, record, supplier)
                else:
                    self._merge(report, record, supplier)
            except codec.FieldValueError as exc:
                report.skip(f"Fournisseur « {name} » : {exc}")
                if supplier is None:
                    block(ctx, record["code"])

    def _create(self, ctx, report, record):
        from invoices.models import Supplier
        from invoices.receipts import supplier_named

        values = {name: codec.load(Supplier, name, record[name]) for name in SUPPLIER_FIELDS if name in record}
        # A name is unique in this application whatever its case (the rule
        # create_shop keeps): two records of one name are one supplier too many.
        taken = supplier_named(record["name"])
        if taken is not None:
            report.skip(f"Fournisseur « {record['name']} » (code {record['code']}) : nom déjà porté ici par {taken.name}")
            block(ctx, record["code"])
            return None
        supplier = Supplier(code=record["code"])
        parser_key = values.pop("parser_key", "")
        if not known_parser(parser_key):
            report.note(f"Fournisseur « {record['name']} » : lecteur inconnu ici (« {parser_key} ») — laissé vide")
            parser_key = ""
        supplier.parser_key = parser_key
        for name, value in values.items():
            if name != "code":
                setattr(supplier, name, value)
        supplier.save()
        ctx.suppliers.add(supplier)
        _tally(report, supplier, "created")
        for _key, price_values, _item in _file_prices(record, supplier.name, report):
            _create_price(supplier, price_values, report)
        return supplier

    def _merge(self, report, record, supplier) -> None:
        """Add what is missing, fill what is blank here and fillable, keep
        everything else as it is, and say what differs."""
        different = codec.differences(supplier, record, COMPARED)
        filled, conflicts = [], []
        unknown_parser = False
        bound = code_bound(supplier)
        for name in different:
            value = codec.load(type(supplier), name, record[name])
            fillable = name in FILLABLE and codec.is_blank(getattr(supplier, name)) and not codec.is_blank(value)
            if fillable and name == "parser_key" and (bound or not known_parser(value)):
                if not bound:
                    report.conflict(
                        f"Fournisseur « {supplier.name} » : lecteur inconnu ici (« {value} » dans l'archive) — "
                        "gardé tel quel"
                    )
                    unknown_parser = True
                    continue
                fillable = False
            if fillable:
                setattr(supplier, name, value)
                filled.append(name)
            else:
                conflicts.append(name)
        if filled:
            supplier.save(update_fields=filled)

        prices_same = 0
        here = {_price_key(price): price for price in supplier.item_prices.all()}
        for key, values, _item in _file_prices(record, supplier.name, report):
            price = here.get(key)
            if price is None:
                _create_price(supplier, values, report)
            elif price.label == values["label"]:
                prices_same += 1
            else:
                report.conflict(
                    f"{_price_text(supplier.name, key)} : « {price.label} » ici, « {values['label']} » dans "
                    "l'archive — gardé tel quel"
                )
        if prices_same:
            report.unchanged(PRICES, prices_same)

        if conflicts:
            report.conflict(
                f"Fournisseur « {supplier.name} » : différent dans l'archive ({said(conflicts)}) — gardé tel quel"
            )
        if filled:
            _tally(report, supplier, "updated")
        elif not conflicts and not unknown_parser:
            _tally(report, supplier, "unchanged")

    def _replace(self, ctx, report, record, supplier) -> None:
        """The supplier becomes the archive's - but for its code and a
        code-bound one's parser, and a nature its documents here would
        contradict."""
        from invoices import supplier_changes
        from invoices.models import Invoice, SupplierChange
        from invoices.receipts import rename_supplier

        changed: list[str] = []
        bound = code_bound(supplier)
        # Checked first, as a whole: a record that does not load changes nothing.
        different = codec.differences(supplier, record, COMPARED)

        if "name" in different:
            try:
                # rename_supplier, so a supplier of charges' postes and their
                # lines follow its name. It records the rename in the
                # supplier's history; an import records nothing there (§6.6).
                with supplier_changes.collect() as recorded:
                    renamed = rename_supplier(supplier, record["name"])
                SupplierChange.objects.filter(pk__in=[change.pk for change in recorded]).delete()
                if renamed.after != renamed.before:
                    changed.append("name")
            except ValueError as exc:
                report.note(f"Fournisseur « {supplier.name} » : nom « {record['name']} » non repris — {exc}")

        plain = [name for name in ("ticket_header", "ticket_identifiers", "refused_identifiers", "is_scrapable")
                 if name in different]
        if "parser_key" in different:
            value = codec.load(type(supplier), "parser_key", record["parser_key"])
            # Field by field, what a replace does not take is said « À
            # savoir »; « Gardés — encore utilisés » is for whole records.
            if bound:
                there = f"« {value} »" if value else "aucun"
                report.note(
                    f"Fournisseur « {supplier.name} » : garde son lecteur « {supplier.parser_key} » "
                    f"({there} dans l'archive) — {KEPT_BOUND}"
                )
            elif not known_parser(value):
                report.note(f"Fournisseur « {supplier.name} » : lecteur inconnu ici (« {value} » dans l'archive)")
            else:
                plain.append("parser_key")
        if "expenses_only" in different:
            # Flipping the flag alone leaves its documents filed the other
            # way - lines on postes, or goods on a charge - until each is
            # read again, which is what « changer… » on its page does.
            documents_stay = not ctx.replacing("factures") and Invoice.objects.filter(supplier=supplier).exists()
            if documents_stay:
                report.note(CHARGES_KEPT.format(name=supplier.name))
            else:
                plain.append("expenses_only")
        if plain:
            codec.assign(supplier, {name: record[name] for name in plain}, plain)
            supplier.save(update_fields=plain)
            changed += plain

        prices_changed = self._replace_prices(report, record, supplier)
        if changed or prices_changed:
            _tally(report, supplier, "updated")
        else:
            _tally(report, supplier, "unchanged")

    def _replace_prices(self, report, record, supplier) -> bool:
        """Exactly the archive's prices, by key. Only when the archive says
        them: a record without the list leaves them as they are."""
        if "item_prices" not in record:
            return False
        here = {_price_key(price): price for price in supplier.item_prices.all()}
        changed = False
        wanted = set()
        for key, values, _item in _file_prices(record, supplier.name, report):
            wanted.add(key)
            price = here.get(key)
            if price is None:
                _create_price(supplier, values, report)
                changed = True
            elif price.label != values["label"]:
                price.label = values["label"]
                fields = ["label"]
                if values.get("created_at") is not None:
                    price.created_at = values["created_at"]
                    fields.append("created_at")
                price.save(update_fields=fields)
                report.updated(PRICES)
                changed = True
            else:
                report.unchanged(PRICES)
        for key, price in here.items():
            if key not in wanted:
                price.delete()
                report.deleted(PRICES)
                changed = True
        return changed

    def prune(self, ctx, report) -> None:
        from invoices.models import Supplier

        for supplier in Supplier.objects.exclude(pk__in=self.claimed).order_by("name"):
            reason = _still_needed(ctx, supplier)
            if reason:
                report.keep(f"Fournisseur « {supplier.name} » : {reason}")
                continue
            _delete_supplier(ctx, report, supplier)

    # -- clear --------------------------------------------------------------------------
    def clear(self, ctx, report) -> None:
        from invoices.models import ShopItemPrice, Supplier, SupplierChange
        from invoices.parsers import LLM_PARSER_KEY

        # The history of a configuration that no longer exists, and its
        # undo data points at rows being cleared.
        history, _ = SupplierChange.objects.all().delete()
        if history:
            report.note(f"{plural(history, 'changement')} de l'historique des fournisseurs effacé{'s' if history > 1 else ''}")
        kept = []
        for supplier in Supplier.objects.order_by("name"):
            if not code_bound(supplier):
                _delete_supplier(ctx, report, supplier)
                continue
            if supplier.parser_key != LLM_PARSER_KEY:
                kept.append(supplier.name)
            # What it learned goes; what a reader or a till is keyed on -
            # its name, its parser, whether it is fetched - and Metro's
            # firewall state stay as they are.
            reset = {"ticket_header": "", "ticket_identifiers": [], "refused_identifiers": [], "expenses_only": False}
            fields = [name for name, value in reset.items() if getattr(supplier, name) != value]
            for name in fields:
                setattr(supplier, name, reset[name])
            if fields:
                supplier.save(update_fields=fields)
            prices, _ = ShopItemPrice.objects.filter(supplier=supplier).delete()
            if prices:
                report.deleted(PRICES, prices)
            if fields or prices:
                _tally(report, supplier, "updated")
        if kept:
            report.note(
                f"{', '.join(kept)} reste{'nt' if len(kept) > 1 else ''} : lecteur propre ou caisse réglée dans "
                "l'application ; leurs identifiants appris sont remis à zéro"
            )


def _holders(supplier, ctx=None) -> str:
    """What still names a supplier - its documents, its sources, its
    classified products - or "". With `ctx` (an import), which section left
    them there by not being replaced."""
    from inventory.models import Product
    from invoices.models import Invoice, InvoiceType

    def unless(key, label):
        return "" if ctx is None or ctx.replacing(key) else f" ({label} non remplacées)"

    documents = Invoice.objects.filter(supplier=supplier).count()
    if documents:
        many = documents > 1
        return (
            f"{plural(documents, 'document')} y {'sont' if many else 'est'} rangé{'s' if many else ''}"
            + unless("factures", "Factures")
        )
    sources = InvoiceType.objects.filter(supplier=supplier).count()
    if sources:
        verb = "récupèrent" if sources > 1 else "récupère"
        return f"{plural(sources, 'source')} {verb} pour lui" + unless("sources", "Sources")
    products = Product.objects.filter(supplier=supplier, stock_type__isnull=False).count()
    if products:
        return plural(products, "produit classé", "produits classés") + unless("associations", "Associations")
    return ""


def _still_needed(ctx, supplier) -> str:
    """Why a supplier the archive does not have stays, or "". What is left
    once every other section has pruned is what kept data still needs."""
    if code_bound(supplier):
        return KEPT_BOUND
    return _holders(supplier, ctx)


def _delete_supplier(ctx, report, supplier) -> bool:
    """Its products first, then the supplier, in a savepoint of its own - as
    `supplier_delete` does. Its prices, history and payee names go with it;
    the payee names are the bank's, and said in the bank's report. A
    product something still holds keeps it, said."""
    from bank.models import CounterpartyAlias
    from inventory.models import Product

    aliases = CounterpartyAlias.objects.filter(supplier=supplier).count()
    prices = supplier.item_prices.count()
    try:
        with transaction.atomic():
            Product.objects.filter(supplier=supplier).delete()
            supplier.delete()
    except (ProtectedError, RestrictedError):
        reason = _holders(supplier) or "un de ses produits sert encore (inventaire, recette)"
        report.keep(f"Fournisseur « {supplier.name} » : {reason}")
        return False
    _tally(report, supplier, "deleted")
    if prices:
        report.deleted(PRICES, prices)
    if aliases:
        ctx.report("banque").deleted("noms de payeurs appris", aliases)
    return True
