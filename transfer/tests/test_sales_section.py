"""« Ventes » (§7.8), with the checks of §10.2.

The till's days are built by the till import's own code (`sync_pos_products`
+ `record_sales`, see test_till_links_section.build_till), so the totals and
the sales per recipe an import rebuilds are compared with what the app wrote,
not with a copy of the rebuild.

The sale documents travel whole since the sales invoices (« factures de
vente »): their key, their figures, their lines - one tied to nothing, one
consumed otherwise than invoiced, two rebuilt from a VAT table -, their file
under `ventes/` and the credits of the statement that pay them. Every name,
number, amount and file below is invented.
"""

import contextlib
import copy
import hashlib
import importlib
import json
import sys
import zipfile
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

import transfer
import transfer.sections
from bank.models import BankTransaction
from inventory.models import StockType
from recipes.forms import MANUAL_SALE_SOURCE
from recipes.models import (
    PosDailyPayment,
    PosProduct,
    PosProductDailyQuantity,
    Recipe,
    RecipeSale,
    SaleDocument,
    SaleDocumentLine,
    SaleDocumentPayment,
    legacy_key,
    new_sale_key,
)
from recipes.tests.sale_einvoice_files import (
    BAR_NAME,
    BAR_SIREN,
    CUSTOMER_NAME,
    CUSTOMER_SIREN,
    SALE_CII,
    SALE_CII_MINIMUM,
    SALE_NUMBER,
    factur_x,
)
from tests.factories import make_credit, make_sale_document, make_sale_line, make_sale_payment
from transfer import registry
from transfer.archive import ArchiveError
from transfer.registry import INFO
from transfer.runner import run_clear
from transfer.sections import sales
from transfer.sections.bank import BankSection
from transfer.sections.base import SALE_LINKS, Strategy
from transfer.sections.recipes import RecipesSection
from transfer.sections.sales import LEGACY_NOTE, PAYMENTS, PAYMENTS_NOTE, SALE_UNDONE_NOTE, SalesSection, fingerprint
from transfer.sections.till_links import TillLinksSection, laddition_rows
from transfer.tests.support import FAKES, db_fingerprint, forge, import_archive, media_listing, round_trip
from transfer.tests.test_invoices_section import MediaMixin
from transfer.tests.test_recipes_section import LaneSectionsMixin, tally
from transfer.tests.test_till_links_section import build_till, day, link_of, sales_of

MERGE, REPLACE = Strategy.MERGE, Strategy.REPLACE
EXPORTED = {"ventes", "recettes", "associations", "fournisseurs"}
SALES_REPLACED = {"ventes": REPLACE, "recettes": MERGE, "associations": MERGE, "fournisseurs": MERGE}
#: What the per-day rows are, in words: one row is one till product on one
#: day. Called « jours de vente (caisse) », 15 850 of them read as 43 years
#: of sales (UX review, 19/09) - the real copy holds 662 days.
QUANTITIES, DAYS = "quantités par produit et par jour (caisse)", "jours de caisse"
DOCUMENTS, LINES, FILES, FILE_MB = "bons de vente", "lignes de bons de vente", "fichiers", "Mo de fichiers"
LINKS = SALE_LINKS
TILL = SaleDocument.Counting.TILL
#: Where build_sales keeps its sales invoices' files: an electronic
#: invoice's Factur-X and a MINIMUM one's XML, each written here.
EINVOICE_FILE = "ventes/2026/09/facture-vente-essai.pdf"
MINIMUM_FILE = "ventes/2026/09/facture-vente-minimum.xml"
#: The credit paying the electronic invoice: build_sales' own fingerprint,
#: never one test_full_round_trip's bank builder makes.
SALES_CREDIT = "ventes-credit-1"
#: What this database holds once build_sales has run.
COUNTED = {
    QUANTITIES: 7,
    DAYS: 3,
    "produits caisse": 5,
    "ventes saisies": 2,
    DOCUMENTS: 6,
    FILES: 2,
    FILE_MB: 0,
    LINKS: 1,
}


def moment(hour: int) -> datetime:
    return datetime(2026, 9, 10, hour, 5, 30, 250000, tzinfo=UTC)


def add_document(sold_on, lines, reference="", note="", created=None) -> SaleDocument:
    document = SaleDocument.objects.create(sold_on=sold_on, reference=reference, note=note)
    for target, quantity, price in lines:
        kind = "recipe" if isinstance(target, Recipe) else "stock_type"
        SaleDocumentLine.objects.create(
            document=document, quantity=Decimal(quantity), unit_price_ttc=price, **{kind: target}
        )
    if created is not None:
        SaleDocument.objects.filter(pk=document.pk).update(created_at=created)
    return document


def store_file(name: str, data: bytes) -> str:
    """`data` under exactly this name - what a previous test left there
    goes first -, and its sha256."""
    if default_storage.exists(name):
        default_storage.delete(name)
    default_storage.save(name, ContentFile(data))
    return hashlib.sha256(data).hexdigest()


def stored_sha(name: str) -> str:
    with default_storage.open(name, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def store_factur_x(name: str) -> str:
    """The Factur-X of SALE_CII written under exactly this name; its sha256."""
    path = Path(default_storage.path(name))
    path.parent.mkdir(parents=True, exist_ok=True)
    factur_x(str(path), SALE_CII)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_sales():
    """The till's days (build_till), two sales typed in by hand, a document
    with a recipe line and an article line, two identical documents (a tab
    typed twice on purpose: two, not one) - and the sales invoices: an
    electronic one with its Factur-X, a recipe line stating its HT and rate
    and a line tied to nothing, paid by a credit of the statement; a MINIMUM
    one « Déjà comptée par la caisse », its two lines rebuilt from its VAT
    table; a keg sold off the till, consumed by the litre."""
    build_till()
    mojito, soda, mint = (Recipe.objects.get(name=name) for name in ("Mojito", "Alcool + Soda", "Menthe"))
    for recipe, quantity, hour in ((mojito, 3, 8), (soda, 2, 9)):
        sale = RecipeSale.objects.create(recipe=recipe, sold_on=day(5), quantity=quantity, source=MANUAL_SALE_SOURCE)
        RecipeSale.objects.filter(pk=sale.pk).update(recorded_at=moment(hour))
    add_document(
        day(6),
        [(mojito, "10", Decimal("7.50")), (StockType.objects.get(name="Gin"), "0.7", None)],
        reference="SOIRÉE-1",
        note="Anniversaire",
        created=moment(10),
    )
    add_document(day(7), [(mint, "1", None)], created=moment(11))
    add_document(day(7), [(mint, "1", None)], created=moment(12))

    checks = [{"label": "Total de la facture", "passed": True, "detail": "230,52 €"}]
    invoice = make_sale_document(
        sold_on=day(3),
        reference=SALE_NUMBER,
        customer=CUSTOMER_NAME,
        customer_identifier=CUSTOMER_SIREN,
        stated_total_ttc="230.52",
        stated_total_ht="194.20",
        payable_ttc="230.52",
        einvoice_format="Factur-X",
        einvoice_type_code="380",
        einvoice_issued_on=day(3),
        einvoice_checks=checks,
        seller_name=BAR_NAME,
        seller_siren=BAR_SIREN,
        source_file=EINVOICE_FILE,
        source_sha256=store_factur_x(EINVOICE_FILE),
    )
    make_sale_line(
        invoice,
        recipe=mojito,
        label="Formule cocktail",
        quantity="2",
        unit_price_ht="84.5000",
        total_ht="169.00",
        vat_rate="0.2000",
    )
    make_sale_line(
        invoice, label="Planche apéritive", quantity="6", unit_price_ht="4.2000", total_ht="25.20", vat_rate="0.1000"
    )
    minimum = make_sale_document(
        sold_on=day(4),
        reference="FV-2026-0103",
        customer="Mariage Exemple",
        counting=TILL,
        stated_total_ttc="230.52",
        stated_total_ht="194.20",
        payable_ttc="230.52",
        einvoice_format="CII",
        einvoice_type_code="380",
        einvoice_issued_on=day(4),
        einvoice_checks=checks,
        seller_name=BAR_NAME,
        seller_siren=BAR_SIREN,
        source_file=MINIMUM_FILE,
        source_sha256=store_file(MINIMUM_FILE, SALE_CII_MINIMUM.encode()),
    )
    for percent, rate, base in (("20", "0.2000", "169.00"), ("10", "0.1000", "25.20")):
        make_sale_line(
            minimum,
            label=f"Total au taux de {percent} % (facture sans lignes)",
            quantity="1",
            unit_price_ht=base,
            total_ht=base,
            vat_rate=rate,
            rebuilt=True,
        )
    keg = make_sale_document(sold_on=day(5), note="Fût vendu au comptoir", stated_total_ttc="90.00")
    make_sale_line(
        keg,
        stock_type=StockType.objects.get(name="Rhum"),
        label="Fût de rhum 3 L",
        quantity="1",
        unit_price_ttc="90.00",
        consumed_quantity="3",
    )
    for hour, document in ((13, invoice), (14, minimum), (15, keg)):
        SaleDocument.objects.filter(pk=document.pk).update(created_at=moment(hour))
    credit = make_credit(
        "230.52",
        day(10),
        counterparty="EXEMPLE EVENEMENTS SARL",
        label="VIR SEPA EXEMPLE EVENEMENTS SARL FV-2026-0101",
        fingerprint=SALES_CREDIT,
    )
    link = make_sale_payment(invoice, credit)
    SaleDocumentPayment.objects.filter(pk=link.pk).update(created_at=moment(16))


def till_totals() -> dict[str, tuple]:
    return {
        product.name: (product.total_quantity, product.first_seen, product.last_seen)
        for product in PosProduct.objects.all()
    }


def archived_payload(reader) -> dict:
    """The « ventes » section's JSON as the archive holds it."""
    with zipfile.ZipFile(reader.path) as archive:
        return json.loads(archive.read("ventes.json"))


def record_of(payload: dict, reference: str) -> dict:
    return next(record for record in payload["documents"] if record["reference"] == reference)


def position_of(payload: dict, reference: str) -> int:
    """Its rank in the archive, as a skip names a record it cannot read."""
    return next(n for n, record in enumerate(payload["documents"], start=1) if record["reference"] == reference)


def editing(edit):
    """A forge change: `edit(payload)` in place, the payload returned."""

    def change(payload):
        edit(payload)
        return payload

    return change


def give_legacy_keys() -> None:
    """What recipes 0020 gave every document saved before it: the key its
    content gave it in « Données » (`legacy_key(fingerprint(…),
    occurrence)`, in (created_at, id) order) - here the three typed by hand,
    sold on the 6th and the 7th, whose lines all have one source."""
    occurrences: dict[str, int] = defaultdict(int)
    for document in SaleDocument.objects.filter(sold_on__in=(day(6), day(7))).order_by("created_at", "id"):
        shapes = [
            ("recipe", line.recipe.name, line.quantity, line.unit_price_ttc)
            if line.recipe_id
            else ("article", line.stock_type.name, line.quantity, line.unit_price_ttc)
            for line in document.lines.order_by("id")
        ]
        content = fingerprint(document.reference, document.sold_on, document.note, shapes)
        SaleDocument.objects.filter(pk=document.pk).update(key=legacy_key(content, occurrences[content]))
        occurrences[content] += 1


def written_before_the_keys(payload: dict) -> dict:
    """ventes.json as an archive written before recipes 0020 holds it: the
    documents typed by hand (sold on the 6th and the 7th), carrying what
    they carried then - no key, no new field, no file, no link."""
    payload["documents"] = [
        {
            **{name: record[name] for name in ("reference", "sold_on", "note", "created_at")},
            "lines": [
                {name: line[name] for name in ("recipe", "article", "quantity", "unit_price_ttc")}
                for line in record["lines"]
            ],
        }
        for record in payload["documents"]
        if record["sold_on"] in (day(6).isoformat(), day(7).isoformat())
    ]
    return payload


class SalesRoundTripTests(MediaMixin, LaneSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_sales()

    def test_counts(self):
        """Seven rows - five till products over three days: the rows are
        quantities per product and per day, and the days are counted apart.
        The sale documents' files and their bank links are counted too."""
        self.assertEqual(SalesSection().count(), COUNTED)

    def test_the_archive_counts_as_the_database_does(self):
        """The import tab prints the archive's counts beside this database's:
        the same labels and the same rule, or one side reads as more."""
        self.assertEqual(self.export(EXPORTED).counts("ventes"), COUNTED)

    def after_clear(self):
        self.assertEqual(SalesSection().count(), dict.fromkeys(COUNTED, 0))
        self.assertEqual(RecipeSale.objects.count(), 0)
        # Linked and ignored till products are the links': kept, at zero.
        self.assertEqual(
            till_totals(),
            {name: (0, None, None) for name in ("MOJITO CLASSIQUE", "Mojito HH", "Pinte IPA", "CAFÉ")},
        )
        # The files go with their documents; the credit is the bank's.
        self.assertFalse(default_storage.exists(EINVOICE_FILE))
        self.assertTrue(BankTransaction.objects.filter(fingerprint=SALES_CREDIT).exists())

    def test_round_trip_with_merge(self):
        totals, sales_rows = till_totals(), laddition_rows()
        before, after = round_trip({"ventes"}, MERGE, after_clear=self.after_clear)
        self.assertEqual(set(before), EXPORTED)
        self.assertEqual(after, before)
        # Rebuilt, not copied - and equal to what the till import wrote.
        self.assertEqual(till_totals(), totals)
        self.assertEqual(laddition_rows(), sales_rows)
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 17, "2026-09-02": 8})
        self.assertEqual(PosProduct.objects.get(name="PLANCHE").recipe, None)

    def test_round_trip_with_replace(self):
        before, after = round_trip({"ventes"}, REPLACE, after_clear=self.after_clear)
        self.assertEqual(after, before)

    def test_round_trip_with_the_links(self):
        """Both cleared, both imported: the sales per recipe are rebuilt
        through the links the same run brings back."""
        before, after = round_trip({"ventes", "liens_ventes"}, MERGE)
        self.assertEqual(after, before)
        self.assertEqual(sales_of("Pinte IPA"), {"2026-09-01": 30, "2026-09-03": 25})

    def test_the_sales_per_recipe_follow_the_links_present(self):
        """Without the links in the run, the days come back but sell no
        recipe - the till products arrive « à lier » - until the links do."""
        reader = self.export(EXPORTED | {"liens_ventes"})
        run_clear({"ventes", "liens_ventes"}, preview=False)
        import_archive(reader, {"ventes": MERGE, "recettes": MERGE, "associations": MERGE, "fournisseurs": MERGE})
        self.assertEqual(PosProductDailyQuantity.objects.count(), 7)
        self.assertEqual(laddition_rows(), [])
        self.assertEqual(till_totals()["MOJITO CLASSIQUE"], (20, day(1), day(2)))
        import_archive(reader, MERGE)
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 17, "2026-09-02": 8})

    def test_a_document_with_a_recipe_line_and_an_article_line(self):
        round_trip({"ventes"}, MERGE)
        document = SaleDocument.objects.get(reference="SOIRÉE-1")
        self.assertEqual((document.sold_on, document.note, document.created_at), (day(6), "Anniversaire", moment(10)))
        self.assertEqual(
            [(line.source_name, line.quantity, line.unit_price_ttc) for line in document.lines.order_by("id")],
            [("Mojito", Decimal("10.0000"), Decimal("7.50")), ("Gin", Decimal("0.7000"), None)],
        )
        self.assertEqual(SaleDocument.objects.filter(sold_on=day(7)).count(), 2)

    def test_a_sales_invoice_comes_back_whole(self):
        """Its key, what its electronic invoice states, its file byte for
        byte under the same name, and the credit that pays it."""
        invoice = SaleDocument.objects.get(reference=SALE_NUMBER)
        key, sha = invoice.key, stored_sha(EINVOICE_FILE)
        round_trip({"ventes"}, MERGE)
        document = SaleDocument.objects.get(reference=SALE_NUMBER)
        self.assertEqual(
            (
                document.key,
                document.customer,
                document.customer_identifier,
                document.counting,
                document.stated_total_ttc,
                document.stated_total_ht,
                document.payable_ttc,
                document.einvoice_format,
                document.einvoice_issued_on,
                document.seller_siren,
                document.created_at,
            ),
            (
                key,
                CUSTOMER_NAME,
                CUSTOMER_SIREN,
                SaleDocument.Counting.COUNTED,
                Decimal("230.52"),
                Decimal("194.20"),
                Decimal("230.52"),
                "Factur-X",
                day(3),
                BAR_SIREN,
                moment(13),
            ),
        )
        self.assertEqual((document.source_file.name, document.source_sha256), (EINVOICE_FILE, sha))
        self.assertEqual(stored_sha(EINVOICE_FILE), sha)
        link = SaleDocumentPayment.objects.get(document=document)
        self.assertEqual(
            (link.transaction.fingerprint, link.method, link.created_at), (SALES_CREDIT, "MANUAL", moment(16))
        )

    def test_a_free_line_round_trips(self):
        """A line tied to nothing (« Planche apéritive ») crashed the export
        (`_line_shape` read a None source); it comes back as it was, beside
        a rebuilt line and a line consumed otherwise than invoiced."""
        round_trip({"ventes"}, REPLACE)
        free = SaleDocumentLine.objects.get(label="Planche apéritive")
        self.assertEqual(
            (free.recipe_id, free.stock_type_id, free.quantity, free.total_ht, free.vat_rate),
            (None, None, Decimal("6.0000"), Decimal("25.20"), Decimal("0.1000")),
        )
        self.assertEqual(SaleDocumentLine.objects.filter(rebuilt=True).count(), 2)
        keg = SaleDocumentLine.objects.get(label="Fût de rhum 3 L")
        self.assertEqual((keg.stock_type.name, keg.consumed_quantity), ("Rhum", Decimal("3.0000")))
        self.assertEqual(SaleDocument.objects.get(reference="FV-2026-0103").counting, TILL)

    def test_a_preview_writes_no_file_and_the_confirm_brings_them_back(self):
        reader = self.export(EXPORTED)
        with self.captureOnCommitCallbacks(execute=True):
            run_clear({"ventes"}, preview=False)
        files = media_listing()
        preview = import_archive(reader, MERGE, preview=True)
        self.assertEqual(media_listing(), files)
        self.assertEqual(tally(preview, "ventes", FILES), (2, 0, 0, 0))
        done = import_archive(reader, MERGE)
        self.assertEqual(preview.outcome(), done.outcome())
        self.assertTrue(default_storage.exists(EINVOICE_FILE))
        self.assertTrue(default_storage.exists(MINIMUM_FILE))


class SalesIdempotenceTests(MediaMixin, LaneSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_sales()
        self.reader = self.export(EXPORTED)

    def assert_nothing_happened(self, run):
        report = run.section("ventes")
        self.assertEqual(tally(run, "ventes", "produits caisse"), (0, 0, 0, 5))
        self.assertEqual(tally(run, "ventes", QUANTITIES), (0, 0, 0, 7))
        self.assertEqual(tally(run, "ventes", "ventes saisies"), (0, 0, 0, 2))
        # « inchangés », every one: documents, lines, files and links.
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (0, 0, 0, 6))
        self.assertEqual(tally(run, "ventes", LINES), (0, 0, 0, 9))
        self.assertEqual(tally(run, "ventes", FILES), (0, 0, 0, 2))
        self.assertEqual(tally(run, "ventes", LINKS), (0, 0, 0, 1))
        self.assertEqual((report.conflicts, report.skipped, report.kept, report.notes), ([], [], [], []))
        self.assertEqual(
            run.rebuilt,
            {"mouvements de stock": 0, "statuts de factures": 0, "ventes par recette": 0, "produits caisse": 0},
        )

    def test_merge(self):
        before, files = db_fingerprint(), media_listing()
        self.assert_nothing_happened(import_archive(self.reader, MERGE))
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(media_listing(), files)

    def test_replace(self):
        """Its own export writes nothing - not a line rewritten, not a file,
        not a link: `db_fingerprint` hashes the pks."""
        before, files = db_fingerprint(), media_listing()
        self.assert_nothing_happened(import_archive(self.reader, REPLACE))
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(media_listing(), files)


class SalesMergeAndReplaceTests(MediaMixin, LaneSectionsMixin, TestCase):
    """Since the archive: a day's quantity changed here, a day and a till
    product only here, a day gone from here; the same for a hand-typed sale
    and a document."""

    def setUp(self):
        super().setUp()
        build_sales()
        self.reader = self.export(EXPORTED)
        PosProductDailyQuantity.objects.filter(product__name="MOJITO CLASSIQUE", sold_on=day(2)).update(quantity=9)
        extra = PosProduct.objects.create(name="PLANCHE XL")
        PosProductDailyQuantity.objects.create(product=extra, sold_on=day(4), quantity=1)
        PosProductDailyQuantity.objects.filter(product__name="Pinte IPA", sold_on=day(3)).delete()
        RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE, recipe__name="Mojito").update(quantity=4)
        RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE, recipe__name="Alcool + Soda").delete()
        RecipeSale.objects.create(
            recipe=Recipe.objects.get(name="Menthe"), sold_on=day(5), quantity=1, source=MANUAL_SALE_SOURCE
        )
        SaleDocument.objects.filter(reference="SOIRÉE-1").delete()
        add_document(day(8), [(Recipe.objects.get(name="Menthe"), "2", None)], reference="ICI")

    def test_merge(self):
        run = import_archive(self.reader, MERGE)
        report = run.section("ventes")
        self.assertEqual(tally(run, "ventes", QUANTITIES), (1, 0, 0, 5))
        self.assertEqual(tally(run, "ventes", "ventes saisies"), (1, 0, 0, 0))
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (1, 0, 0, 5))
        self.assertEqual(
            report.conflicts,
            [
                "Produit caisse « MOJITO CLASSIQUE » le 02/09/2026 : 9 ici, 8 dans l'archive — gardé tel quel",
                "Vente saisie de « Mojito » le 05/09/2026 : 4 ici, 3 dans l'archive — gardée telle quelle",
            ],
        )
        # Known by their key, the documents need no word about their content.
        self.assertNotIn(LEGACY_NOTE, report.notes)
        self.assertEqual(till_totals()["Pinte IPA"], (55, day(1), day(3)))
        self.assertEqual(sales_of("Pinte IPA"), {"2026-09-01": 30, "2026-09-03": 25})
        self.assertEqual(till_totals()["PLANCHE XL"], (0, None, None))  # only here: untouched
        self.assertEqual(SaleDocument.objects.count(), 7)

    def test_replace(self):
        run = import_archive(self.reader, SALES_REPLACED)
        self.assertEqual(tally(run, "ventes", QUANTITIES), (1, 1, 1, 5))
        self.assertEqual(tally(run, "ventes", "ventes saisies"), (1, 1, 1, 0))
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (1, 0, 1, 5))
        self.assertEqual(tally(run, "ventes", LINES), (2, 0, 1, 7))
        # Left with no day and no link: pure data, gone.
        self.assertEqual(tally(run, "ventes", "produits caisse")[2], 1)
        self.assertFalse(PosProduct.objects.filter(name="PLANCHE XL").exists())
        self.assertEqual(till_totals()["MOJITO CLASSIQUE"], (20, day(1), day(2)))
        self.assertEqual(sales_of("Mojito"), {"2026-09-01": 17, "2026-09-02": 8})
        self.assertEqual(
            sorted(RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).values_list("recipe__name", "quantity")),
            [("Alcool + Soda", 2), ("Mojito", 3)],
        )
        self.assertFalse(SaleDocument.objects.filter(reference="ICI").exists())
        self.assertEqual(run.affected(), {"ventes"})

    def test_a_preview_changes_nothing_and_says_what_the_confirm_does(self):
        before = db_fingerprint()
        with self.captureOnCommitCallbacks() as callbacks:
            preview = import_archive(self.reader, SALES_REPLACED, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(callbacks, [])
        confirmed = import_archive(self.reader, SALES_REPLACED)
        self.assertEqual(preview.outcome(), confirmed.outcome())
        self.assertNotEqual(db_fingerprint(), before)


class SalesDocumentsTests(MediaMixin, LaneSectionsMixin, TestCase):
    def test_the_same_document_imported_twice_is_not_duplicated(self):
        build_sales()
        reader = self.export(EXPORTED)
        SaleDocument.objects.all().delete()
        for _ in range(2):
            import_archive(reader, MERGE)
        self.assertEqual(SaleDocument.objects.count(), 6)
        self.assertEqual(SaleDocumentLine.objects.count(), 9)
        self.assertEqual(SaleDocumentPayment.objects.count(), 1)

    def test_identical_documents_are_matched_one_for_one(self):
        """Two identical tabs are two documents, each its own key: one here,
        two in the archive, the second comes in."""
        build_sales()
        reader = self.export(EXPORTED)
        SaleDocument.objects.filter(sold_on=day(7)).order_by("-created_at").first().delete()
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (1, 0, 0, 5))
        self.assertEqual(SaleDocument.objects.filter(sold_on=day(7)).count(), 2)


class SalesRefusalTests(MediaMixin, LaneSectionsMixin, TestCase):
    def setUp(self):
        super().setUp()
        build_sales()
        self.reader = self.export(EXPORTED)
        run_clear({"ventes"}, preview=False)

    def edited(self, **changes):
        def edit(payload):
            for name, change in changes.items():
                change(payload[name])
            return payload

        return self.open(forge(self.reader, ventes=edit))

    def test_a_day_of_a_till_product_unknown_here_creates_it_to_link(self):
        def change(rows):
            rows.append(["NOUVEAU COCKTAIL", "2026-09-04", 6])

        run = import_archive(self.edited(daily=change), MERGE)
        product = PosProduct.objects.get(name="NOUVEAU COCKTAIL")
        self.assertEqual((product.recipe, product.ignored), (None, False))
        self.assertEqual(till_totals()["NOUVEAU COCKTAIL"], (6, day(4), day(4)))
        self.assertEqual(tally(run, "ventes", "produits caisse")[0], 2)  # PLANCHE (cleared) and this one

    def test_days_the_fields_refuse(self):
        def change(rows):
            rows[1][2] = True
            rows[2][2] = "8"
            rows[3][1] = "2026-02-30"
            rows.append(["MOJITO CLASSIQUE", "2026-09-01", 12])
            rows.append(["MOJITO CLASSIQUE"])

        run = import_archive(self.edited(daily=change), MERGE)
        self.assertEqual(
            run.section("ventes").skipped,
            [
                "Produit caisse « MOJITO CLASSIQUE » le 01/09/2026 : « quantity » : nombre entier attendu (« True »)",
                "Produit caisse « MOJITO CLASSIQUE » le 02/09/2026 : « quantity » : nombre entier attendu (« 8 »)",
                "Produit caisse « Mojito HH » : « sold_on » : date illisible (« 2026-02-30 »)",
                "Produit caisse « MOJITO CLASSIQUE » le 01/09/2026 : deux fois dans l'archive",
                "vente par jour n° 9 de l'archive : illisible",
            ],
        )
        self.assertEqual(PosProductDailyQuantity.objects.count(), 4)

    def test_a_day_that_nets_negative_is_a_refund_and_comes_back(self):
        """A pint sold one day and taken back the next nets -1 on the day of
        the refund. The archive has to carry it: refused as « nombre positif
        attendu », a restore silently dropped that day, and the next till
        import - which nets the same figure - could not write it either."""

        def change(rows):
            rows[0][2] = -3

        import_archive(self.edited(daily=change), MERGE)

        self.assertEqual(PosProductDailyQuantity.objects.get(product__name="CAFÉ", sold_on=day(1)).quantity, -3)

    def test_a_quantity_past_what_the_sums_hold_is_skipped(self):
        """codec.load reads any int: from 2**63 SQLite refused to store the
        day, and days just under it overflowed the rebuild's sums - the
        preview was a 500 (audit 04/10/2026)."""

        def daily(rows):
            rows[0][2] = 2**63
            rows[1][2] = 4 * 10**18
            rows[2][2] = -(2**31 - 1)

        def manual(sales_rows):
            sales_rows[0]["quantity"] = 10**20

        run = import_archive(self.edited(daily=daily, manual_sales=manual), MERGE)
        bound = "2 147 483 647 au plus, en plus ou en moins"
        self.assertEqual(
            run.section("ventes").skipped,
            [
                f"Produit caisse « CAFÉ » le 01/09/2026 : « quantity » : nombre hors limites (« {2**63} ») : {bound}",
                (
                    f"Produit caisse « MOJITO CLASSIQUE » le 01/09/2026 : « quantity » : nombre hors limites "
                    f"(« {4 * 10**18} ») : {bound}"
                ),
                f"Vente saisie de « Alcool + Soda » : « quantity » : nombre hors limites (« {10**20} ») : {bound}",
            ],
        )
        self.assertEqual(
            PosProductDailyQuantity.objects.get(product__name="MOJITO CLASSIQUE", sold_on=day(2)).quantity,
            -(2**31 - 1),
        )

    def test_a_sale_typed_in_for_a_recipe_unknown_here_is_skipped(self):
        def change(sales_rows):
            sales_rows[0]["recipe"] = "Mojito fraise"

        run = import_archive(self.edited(manual_sales=change), MERGE)
        self.assertEqual(
            run.section("ventes").skipped, ["Vente saisie du 05/09/2026 : recette inconnue « Mojito fraise »"]
        )
        self.assertEqual(RecipeSale.objects.filter(source=MANUAL_SALE_SOURCE).count(), 1)

    def test_a_document_whose_line_cannot_be_found_is_skipped_whole(self):
        def change(documents):
            documents[0]["lines"][1]["article"] = "Gin rose"
            documents[1]["lines"][0]["article"] = "Menthe"

        run = import_archive(self.edited(documents=change), MERGE)
        self.assertEqual(
            run.section("ventes").skipped,
            [
                "Bon de vente du 06/09/2026 (SOIRÉE-1) : article inconnu « Gin rose »",
                "bon de vente n° 2 de l'archive : ligne n° 1 : une recette ou un article, pas les deux",
            ],
        )
        # The second identical tab and the three sales invoices.
        self.assertEqual(SaleDocument.objects.count(), 4)
        self.assertEqual(SaleDocumentLine.objects.count(), 6)

    def test_the_columns_of_the_days_are_checked(self):
        reader = self.open(
            forge(
                self.reader,
                ventes=lambda payload: {**payload, "daily_columns": ["sold_on", "till_product", "quantity"]},
            )
        )
        with self.assertRaisesMessage(ArchiveError, "« daily_columns » doit être"):
            import_archive(reader, MERGE)
        reader = self.open(forge(self.reader, ventes=lambda payload: {**payload, "daily": {"MOJITO": 1}}))
        with self.assertRaisesMessage(ArchiveError, "« daily » n'est pas une liste"):
            import_archive(reader, MERGE)


class SalesClearTests(MediaMixin, LaneSectionsMixin, TestCase):
    def test_clear_keeps_linked_till_products_at_zero(self):
        build_sales()
        run = run_clear({"ventes"}, preview=False)
        self.assertEqual(PosProductDailyQuantity.objects.count(), 0)
        self.assertEqual(RecipeSale.objects.count(), 0)
        self.assertEqual(SaleDocument.objects.count(), 0)
        self.assertFalse(PosProduct.objects.filter(name="PLANCHE").exists())
        self.assertEqual(link_of("MOJITO CLASSIQUE"), ("Mojito", False))
        self.assertEqual(link_of("CAFÉ"), (None, True))
        self.assertEqual(till_totals()["Pinte IPA"], (0, None, None))
        self.assertEqual(tally(run, "ventes", QUANTITIES), (0, 0, 7, 0))
        self.assertEqual(tally(run, "ventes", "ventes saisies"), (0, 0, 2, 0))
        self.assertEqual(tally(run, "ventes", "ventes par recette"), (0, 0, 4, 0))
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (0, 0, 6, 0))
        self.assertEqual(tally(run, "ventes", "produits caisse"), (0, 0, 1, 0))

    def test_clear_counts_the_lines_the_files_and_the_links_and_deletes_the_files_on_commit(self):
        """The prune and the clear count the same four rows (transfer
        critique 8); the bank's credit stays - it is « Banque »'s."""
        build_sales()
        with self.captureOnCommitCallbacks(execute=True):
            run = run_clear({"ventes"}, preview=False)
        self.assertEqual(tally(run, "ventes", LINES), (0, 0, 9, 0))
        self.assertEqual(tally(run, "ventes", FILES), (0, 0, 2, 0))
        self.assertEqual(tally(run, "ventes", LINKS), (0, 0, 1, 0))
        self.assertFalse(default_storage.exists(EINVOICE_FILE))
        self.assertFalse(default_storage.exists(MINIMUM_FILE))
        self.assertFalse(SaleDocumentPayment.objects.exists())
        self.assertTrue(BankTransaction.objects.filter(fingerprint=SALES_CREDIT).exists())

    def test_the_effacer_tab_says_the_files_and_links_go_before(self):
        self.assertIn("fichiers des factures de vente", INFO["ventes"].clear_note)
        self.assertIn("règlements bancaires", INFO["ventes"].clear_note)
        self.assertIn("la sauvegarde", INFO["ventes"].clear_note)


def pay(n: int, method: str, amount: str, payments: int = 1) -> PosDailyPayment:
    """A till day's payments by one method (invented amounts)."""
    return PosDailyPayment.objects.create(sold_on=day(n), method=method, amount=Decimal(amount), payments=payments)


def payments_held() -> list[tuple]:
    return list(PosDailyPayment.objects.values_list("sold_on", "method", "amount"))


class SalesPaymentsTests(MediaMixin, LaneSectionsMixin, TestCase):
    """The till's means of payment per day are NOT in the archive: like the
    day's money, they are re-read from the exports on disk
    (`manage.py laddition_backfill_payments`). So a clear deletes them and
    says how to bring them back, a « Remplacer » deletes those of the days
    it leaves with no sales, and nothing else touches them."""

    def setUp(self):
        super().setUp()
        build_sales()  # till days 1, 2 and 3

    def test_the_page_says_the_amounts_do_not_travel(self):
        self.assertIn("ne voyagent pas", INFO["ventes"].description)
        self.assertIn("laddition_backfill_payments", INFO["ventes"].description)

    def test_the_archive_does_not_carry_them_and_the_counts_do_not_name_them(self):
        """count() is what the import tab sets beside the archive's counts:
        a row the archive cannot carry would read as always missing."""
        pay(1, PosDailyPayment.CARD, "120.00", 9)
        reader = self.export(EXPORTED)
        self.assertNotIn(PAYMENTS, SalesSection().count())
        self.assertNotIn(PAYMENTS, reader.counts("ventes"))
        self.assertEqual(set(archived_payload(reader)) & {"payments", "paiements"}, set())

    def test_clear_deletes_every_payment_and_says_how_to_bring_them_back(self):
        pay(1, PosDailyPayment.CARD, "120.00", 9)
        pay(1, PosDailyPayment.CASH, "35.50", 4)
        pay(9, PosDailyPayment.CARD, "12.00")  # a day with no sales stored
        run = run_clear({"ventes"}, preview=False)
        self.assertEqual(PosDailyPayment.objects.count(), 0)
        self.assertEqual(tally(run, "ventes", PAYMENTS), (0, 0, 3, 0))
        self.assertIn(PAYMENTS_NOTE, run.section("ventes").notes)

    def test_a_preview_of_the_clear_deletes_nothing_and_announces_it(self):
        pay(1, PosDailyPayment.CARD, "120.00", 9)
        before = db_fingerprint()
        preview = run_clear({"ventes"}, preview=True)
        self.assertEqual(db_fingerprint(), before)
        self.assertEqual(tally(preview, "ventes", PAYMENTS), (0, 0, 1, 0))

    def test_a_clear_with_no_payments_says_nothing_about_them(self):
        run = run_clear({"ventes"}, preview=False)
        self.assertEqual(tally(run, "ventes", PAYMENTS), (0, 0, 0, 0))
        self.assertNotIn(PAYMENTS_NOTE, run.section("ventes").notes)

    def test_replace_deletes_the_payments_of_the_days_it_leaves_without_sales(self):
        """Day 3 is gone from the archive: « Remplacer » prunes its sales, and
        its payments go with them - money for a day « Ventes » does not hold
        is a figure the sales pages contradict. Day 1 keeps its sales, so it
        keeps its payments: the archive has none to put in their place."""
        PosProductDailyQuantity.objects.filter(sold_on=day(3)).delete()
        reader = self.export(EXPORTED)
        PosProductDailyQuantity.objects.create(
            product=PosProduct.objects.get(name="Pinte IPA"), sold_on=day(3), quantity=25
        )
        pay(1, PosDailyPayment.CARD, "120.00", 9)
        pay(3, PosDailyPayment.CARD, "80.00", 6)
        pay(3, PosDailyPayment.CASH, "14.00", 2)
        run = import_archive(reader, SALES_REPLACED)
        self.assertEqual(payments_held(), [(day(1), PosDailyPayment.CARD, Decimal("120.00"))])
        self.assertEqual(tally(run, "ventes", PAYMENTS), (0, 0, 2, 0))
        self.assertIn(PAYMENTS_NOTE, run.section("ventes").notes)

    def test_merge_and_replace_of_the_same_data_leave_the_payments_alone(self):
        pay(1, PosDailyPayment.CARD, "120.00", 9)
        pay(2, PosDailyPayment.CHEQUE, "50.00")
        reader = self.export(EXPORTED)
        for strategy in (MERGE, SALES_REPLACED):
            with self.subTest(strategy=strategy):
                before = db_fingerprint()
                run = import_archive(reader, strategy)
                self.assertEqual(db_fingerprint(), before)
                self.assertEqual(tally(run, "ventes", PAYMENTS), (0, 0, 0, 0))
                self.assertEqual(run.section("ventes").notes, [])


def till_coverage():
    """How far the till's sales are imported without a gap (auto_sales)."""
    from invoices.models import GatherCoverage

    row = GatherCoverage.objects.filter(code="ventes-laddition").first()
    return row.searched_until if row is not None else None


def cover_till(until) -> None:
    from invoices.models import GatherCoverage

    GatherCoverage.objects.update_or_create(code="ventes-laddition", defaults={"searched_until": until})


class SalesCoverageTests(MediaMixin, LaneSectionsMixin, TestCase):
    """Days « Ventes » deletes are no longer imported: the till's coverage
    (`ventes-laddition`, where the next automatic import starts) goes back
    to the day before the first of them - never forward -, or the automatic
    imports would never fetch them again."""

    #: A tick shortly after the invented days (12:00 in Paris).
    NOW = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)

    def setUp(self):
        super().setUp()
        build_sales()  # till days 1, 2 and 3

    def period(self):
        from recipes import auto_sales

        return auto_sales.period_for("laddition", self.NOW)

    def older_archive(self):
        """An archive without day 3, day 3 imported here since."""
        PosProductDailyQuantity.objects.filter(sold_on=day(3)).delete()
        reader = self.export(EXPORTED)
        PosProductDailyQuantity.objects.create(
            product=PosProduct.objects.get(name="Pinte IPA"), sold_on=day(3), quantity=25
        )
        return reader

    def test_replace_lowers_it_to_the_day_before_the_first_day_it_deletes(self):
        reader = self.older_archive()
        cover_till(day(9))
        import_archive(reader, SALES_REPLACED)
        self.assertEqual(till_coverage(), day(2))
        period = self.period()
        self.assertFalse(period.up_to_date)
        self.assertEqual(period.start, day(2) - timedelta(days=3))

    def test_clear_lowers_it_before_the_first_day_it_held(self):
        cover_till(day(9))
        run_clear({"ventes"}, preview=False)
        self.assertEqual(till_coverage(), day(1) - timedelta(days=1))
        self.assertFalse(self.period().up_to_date)

    def test_a_coverage_already_lower_is_never_raised(self):
        cover_till(day(1) - timedelta(days=10))
        run_clear({"ventes"}, preview=False)
        self.assertEqual(till_coverage(), day(1) - timedelta(days=10))

    def test_one_never_recorded_is_not_rebuilt_past_the_days_deleted(self):
        """Missing, the coverage would be read from the successful imports'
        history - which still reaches the days just deleted."""
        from recipes.models import SalesImportJob

        SalesImportJob.objects.create(
            status=SalesImportJob.Status.SUCCESS,
            range_start=day(1),
            range_end=day(3),
            finished_at=self.NOW,
        )
        run_clear({"ventes"}, preview=False)
        self.assertEqual(till_coverage(), day(1) - timedelta(days=1))

    def test_a_preview_leaves_it_alone(self):
        reader = self.older_archive()
        cover_till(day(9))
        import_archive(reader, SALES_REPLACED, preview=True)
        run_clear({"ventes"}, preview=True)
        self.assertEqual(till_coverage(), day(9))

    def test_merge_and_a_replace_deleting_nothing_leave_it_alone(self):
        cover_till(day(9))
        reader = self.export(EXPORTED)
        for strategy in (MERGE, SALES_REPLACED):
            with self.subTest(strategy=strategy):
                import_archive(reader, strategy)
                self.assertEqual(till_coverage(), day(9))


class SaleInvoiceArchiveTests(MediaMixin, LaneSectionsMixin, TestCase):
    """A sale document travels by its key: random for a new one, its
    content's for one saved before recipes 0020 and for an older archive's
    records, so an archive written before the sales invoices still finds its
    documents. What it carries since - its figures, lines, file - comes back
    as it was, and what an archive does not say is never blanked."""

    def setUp(self):
        super().setUp()
        build_sales()
        self.invoice = SaleDocument.objects.get(reference=SALE_NUMBER)

    def forged(self, reader, edit):
        return self.open(forge(reader, ventes=editing(edit)))

    def test_the_key_travels_and_finds_its_document(self):
        keys = set(SaleDocument.objects.values_list("key", flat=True))
        reader = self.export(EXPORTED)
        self.assertEqual({record["key"] for record in archived_payload(reader)["documents"]}, keys)
        round_trip({"ventes"}, MERGE)
        self.assertEqual(set(SaleDocument.objects.values_list("key", flat=True)), keys)
        # Its number changed here: the same document, by its key - a
        # conflict, never a second copy.
        SaleDocument.objects.filter(reference=SALE_NUMBER).update(reference="FV-CHANGEE")
        run = import_archive(reader, MERGE)
        self.assertEqual(SaleDocument.objects.count(), 6)
        self.assertEqual(
            run.section("ventes").conflicts,
            ["Bon de vente du 03/09/2026 (FV-2026-0101) : différent dans l'archive (numéro) — gardé tel quel"],
        )

    def test_an_archive_written_before_the_keys_finds_its_documents_by_their_content(self):
        """The migration gave each document the key its content gave it in
        « Données »: an older archive's records, key-less, derive the same
        one - the two identical tabs included, one for one."""
        give_legacy_keys()
        reader = self.forged(self.export(EXPORTED), written_before_the_keys)
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (0, 0, 0, 3))
        self.assertEqual(SaleDocument.objects.count(), 6)
        self.assertEqual(run.section("ventes").skipped, [])
        self.assertNotIn(LEGACY_NOTE, run.section("ventes").notes)
        # Replaced, found and kept the same - nothing written for them; the
        # sales invoices it does not name go.
        lines = sorted(SaleDocumentLine.objects.filter(document__sold_on__in=(day(6), day(7))).values_list("pk"))
        run = import_archive(reader, SALES_REPLACED)
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (0, 0, 3, 3))
        self.assertEqual(sorted(SaleDocumentLine.objects.values_list("pk")), lines)

    def test_a_legacy_document_typed_again_since_is_found_by_its_content(self):
        """Deleted and typed again after the update, it has a random key:
        the older archive's record is looked for by its content too, or
        « Fusionner » made it twice (transfer critique 17)."""
        give_legacy_keys()
        reader = self.forged(self.export(EXPORTED), written_before_the_keys)
        SaleDocument.objects.filter(reference="SOIRÉE-1").update(key=new_sale_key())
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (0, 0, 0, 3))
        self.assertEqual(SaleDocument.objects.filter(reference="SOIRÉE-1").count(), 1)

    def test_an_older_archive_says_a_document_created_came_by_its_content(self):
        give_legacy_keys()
        reader = self.forged(self.export(EXPORTED), written_before_the_keys)
        SaleDocument.objects.filter(sold_on=day(7)).order_by("-created_at").first().delete()
        run = import_archive(reader, MERGE)
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (1, 0, 0, 2))
        self.assertIn(LEGACY_NOTE, run.section("ventes").notes)
        created = SaleDocument.objects.filter(sold_on=day(7)).order_by("-created_at").first()
        self.assertEqual(created.created_at, moment(12))

    def test_an_older_archive_keeps_both_twins_when_the_earlier_one_was_deleted(self):
        """The EARLIER of two identical documents deleted here: the survivor
        keeps its key legacy(c, 1) but is ranked (c, 0) by content. The
        record keyed (c, 1) finds it by its key; the record keyed (c, 0)
        must not take it first by content - it creates the deleted twin
        again, under « Fusionner » and « Remplacer » alike."""
        run = self.earlier_twin_deleted_then_imported(MERGE)
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (1, 0, 0, 2))
        self.assertIn(LEGACY_NOTE, run.section("ventes").notes)

    def test_replace_from_an_older_archive_keeps_both_twins_too(self):
        self.earlier_twin_deleted_then_imported(SALES_REPLACED)

    def earlier_twin_deleted_then_imported(self, strategy):
        give_legacy_keys()
        reader = self.forged(self.export(EXPORTED), written_before_the_keys)
        SaleDocument.objects.filter(sold_on=day(7)).order_by("created_at").first().delete()
        run = import_archive(reader, strategy)
        self.assertEqual(run.section("ventes").skipped, [])
        self.assertEqual(SaleDocument.objects.filter(sold_on=day(7)).count(), 2)
        return run

    def test_a_tie_put_back_by_an_older_archive_clears_a_consumed_quantity_it_does_not_say(self):
        """A line re-tied here to an article, its consumed quantity in that
        article's unit (30 litres), then « Remplacer » from an archive
        written before the consumed quantities: the recipe comes back, and
        the 30 - measured in the old tie's unit, which the archive never
        said - is cleared and said, as the page clears it on a tie changed."""
        give_legacy_keys()
        reader = self.forged(self.export(EXPORTED), written_before_the_keys)
        line = SaleDocument.objects.get(reference="SOIRÉE-1").lines.order_by("id").first()
        SaleDocumentLine.objects.filter(pk=line.pk).update(
            recipe=None, stock_type=StockType.objects.get(name="Gin"), consumed_quantity=Decimal("30")
        )
        run = import_archive(reader, SALES_REPLACED)
        line.refresh_from_db()
        self.assertEqual(line.recipe.name, "Mojito")
        self.assertIsNone(line.stock_type)
        self.assertIsNone(line.consumed_quantity)
        self.assertIn(
            "Bon de vente du 06/09/2026 (SOIRÉE-1) : quantité consommée remise à la quantité facturée "
            "(nouvelle correspondance) : ligne n° 1",
            run.section("ventes").notes,
        )

    def test_what_the_reader_stores_comes_back(self):
        """« Lire la facture » stores an electronic invoice's own dates as it
        states them (an issue date of 0001-01-01 beside a plausible delivery
        date, one more than a year ahead) and a type code a document wrote
        before the reader cut it to its column: a safety archive of an
        « Effacer » or a « Remplacer » brings the document back - its lines,
        its file and its links -, never skips it."""
        far = timezone.localdate() + timedelta(days=800)
        SaleDocument.objects.filter(pk=self.invoice.pk).update(
            einvoice_issued_on=date(1, 1, 1), einvoice_delivered_on=far, einvoice_type_code="380EXEMPLE12"
        )
        lines = self.invoice.lines.count()
        links = self.invoice.bank_payments.count()
        for strategy in (MERGE, SALES_REPLACED):
            with self.subTest(strategy=strategy):
                reader = self.export(EXPORTED)
                run_clear({"ventes"}, preview=False)
                run = import_archive(reader, strategy)
                self.assertEqual(run.section("ventes").skipped, [])
                document = SaleDocument.objects.get(reference=SALE_NUMBER)
                self.assertEqual(document.einvoice_issued_on, date(1, 1, 1))
                self.assertEqual(document.einvoice_delivered_on, far)
                self.assertEqual(document.einvoice_type_code, "380EXEMPLE")
                self.assertEqual(document.lines.count(), lines)
                self.assertEqual(document.bank_payments.count(), links)
                self.assertTrue(document.source_file)
                self.assertTrue(any("380EXEMPLE12" in note for note in run.section("ventes").notes))
                SaleDocument.objects.filter(pk=document.pk).update(einvoice_type_code="380EXEMPLE12")

    def test_two_records_with_one_key_make_one_document(self):
        """The second was created under a key the first had just taken: the
        unique index failed the WHOLE run (transfer critique 5)."""
        reader = self.export(EXPORTED)
        run_clear({"ventes"}, preview=False)

        def twice(payload):
            twin = copy.deepcopy(record_of(payload, "SOIRÉE-1"))
            twin["note"] = "Copie"
            payload["documents"].append(twin)

        run = import_archive(self.forged(reader, twice), MERGE)
        self.assertIn("Bon de vente du 06/09/2026 (SOIRÉE-1) : en double dans l'archive", run.section("ventes").skipped)
        self.assertEqual(
            list(SaleDocument.objects.filter(reference="SOIRÉE-1").values_list("note")), [("Anniversaire",)]
        )

    def test_a_key_less_record_deriving_a_key_already_read_is_one_too(self):
        give_legacy_keys()
        reader = self.export(EXPORTED)
        run_clear({"ventes"}, preview=False)

        def twice(payload):
            twin = copy.deepcopy(record_of(payload, "SOIRÉE-1"))
            del twin["key"]
            payload["documents"].append(twin)

        run = import_archive(self.forged(reader, twice), MERGE)
        self.assertIn("Bon de vente du 06/09/2026 (SOIRÉE-1) : en double dans l'archive", run.section("ventes").skipped)
        self.assertEqual(SaleDocument.objects.filter(reference="SOIRÉE-1").count(), 1)

    def test_an_unreadable_key_skips_the_record(self):
        reader = self.export(EXPORTED)
        run_clear({"ventes"}, preview=False)
        position = position_of(archived_payload(reader), SALE_NUMBER)
        for key in ("ABCDEF0123456789", "0123", None, 12):
            with self.subTest(key=key):
                run = import_archive(
                    self.forged(reader, lambda p, key=key: record_of(p, SALE_NUMBER).update(key=key)), MERGE
                )
                self.assertIn(f"bon de vente n° {position} de l'archive : clé illisible", run.section("ventes").skipped)
                self.assertFalse(SaleDocument.objects.filter(reference=SALE_NUMBER).exists())

    def test_a_record_without_a_key_holding_a_line_tied_to_nothing_is_skipped(self):
        """No archive written before the keys can carry a free line: only a
        hand edit makes one, and it has no content key."""
        reader = self.export(EXPORTED)
        run_clear({"ventes"}, preview=False)
        position = position_of(archived_payload(reader), SALE_NUMBER)
        run = import_archive(self.forged(reader, lambda payload: record_of(payload, SALE_NUMBER).pop("key")), MERGE)
        self.assertIn(
            f"bon de vente n° {position} de l'archive : ligne n° 2 : une recette ou un article",
            run.section("ventes").skipped,
        )
        self.assertFalse(SaleDocument.objects.filter(reference=SALE_NUMBER).exists())

    def test_a_field_the_archive_does_not_say_is_kept_under_replace(self):
        """Read as blank, an older archive's « Remplacer » wiped what was
        typed since: the customer, the counting."""
        reader = self.export(EXPORTED)
        SaleDocument.objects.filter(pk=self.invoice.pk).update(counting=SaleDocument.Counting.DEPOSIT)

        def unsaid(payload):
            record = record_of(payload, SALE_NUMBER)
            del record["customer"], record["counting"]
            record["note"] = "Mariage du 12/09"

        run = import_archive(self.forged(reader, unsaid), SALES_REPLACED)
        self.invoice.refresh_from_db()
        self.assertEqual(
            (self.invoice.note, self.invoice.customer, self.invoice.counting),
            ("Mariage du 12/09", CUSTOMER_NAME, SaleDocument.Counting.DEPOSIT),
        )
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (0, 1, 0, 5))

    def test_a_lines_label_the_archive_does_not_say_is_kept_under_replace(self):
        reader = self.export(EXPORTED)

        def unlabelled(payload):
            line = record_of(payload, SALE_NUMBER)["lines"][0]
            del line["label"]
            line["quantity"] = "3.0000"

        run = import_archive(self.forged(reader, unlabelled), SALES_REPLACED)
        self.assertEqual(
            list(self.invoice.lines.order_by("id").values_list("label", "quantity", "recipe__name")),
            [("Formule cocktail", Decimal("3.0000"), "Mojito"), ("Planche apéritive", Decimal("6.0000"), None)],
        )
        self.assertEqual(tally(run, "ventes", LINES), (0, 1, 0, 8))

    def test_lines_replaced_by_rank(self):
        """Under « Remplacer » a line changed is updated in place, one too
        many deleted, one more created - the others untouched."""
        reader = self.export(EXPORTED)
        first = self.invoice.lines.order_by("id").first()
        make_sale_line(self.invoice, label="Ligne ajoutée ici", quantity="1", unit_price_ttc="5.00")

        def change(payload):
            record_of(payload, SALE_NUMBER)["lines"][0]["total_ht"] = "170.00"

        run = import_archive(self.forged(reader, change), SALES_REPLACED)
        lines = list(self.invoice.lines.order_by("id"))
        self.assertEqual(lines[0].pk, first.pk)
        self.assertEqual([line.total_ht for line in lines], [Decimal("170.00"), Decimal("25.20")])
        self.assertEqual(tally(run, "ventes", LINES), (0, 1, 1, 8))

    def test_a_replace_never_writes_a_line_its_constraints_refuse(self):
        """A rebuilt line here paired with a tied line of the archive: the
        tie assigned, the line would be both - the CHECK failed the whole
        run. The document is kept as it is, and said."""
        reader = self.export(EXPORTED)

        def tied(payload):
            line = record_of(payload, "FV-2026-0103")["lines"][0]
            line["recipe"] = "Mojito"
            del line["rebuilt"]

        run = import_archive(self.forged(reader, tied), SALES_REPLACED)
        self.assertIn(
            "Bon de vente du 04/09/2026 (FV-2026-0103) : ligne n° 1 : une ligne reconstituée ne se relie pas "
            "— gardé tel quel",
            run.section("ventes").skipped,
        )
        self.assertEqual(SaleDocumentLine.objects.filter(rebuilt=True, recipe__isnull=False).count(), 0)

    def test_a_file_missing_from_the_disk_at_export_comes_in_without_it(self):
        """As the invoices do: imported without, said - and its sha kept, so
        the same file read again is known."""
        sha = self.invoice.source_sha256
        default_storage.delete(EINVOICE_FILE)
        reader = self.export(EXPORTED)
        self.assertEqual(reader.counts("ventes")[FILES], 1)
        run_clear({"ventes"}, preview=False)
        run = import_archive(reader, MERGE)
        document = SaleDocument.objects.get(reference=SALE_NUMBER)
        self.assertFalse(document.source_file)
        self.assertEqual(document.source_sha256, sha)
        self.assertIn(
            "Bon de vente du 03/09/2026 (FV-2026-0101) : fichier absent du disque à l'export, importé sans.",
            run.section("ventes").notes,
        )

    def test_a_file_outside_the_sales_folder_is_refused(self):
        """The archive takes names under every folder documents live in: an
        invoice's or a slip's would be taken over by a sale document."""
        reader = self.export(EXPORTED)
        position = position_of(archived_payload(reader), SALE_NUMBER)
        for name in ("consignes/bons/2026/09/bon-essai.pdf", "invoices/2026/09/facture-essai.pdf"):
            with self.subTest(name=name):
                run_clear({"ventes"}, preview=False)

                def moved(payload, name=name):
                    record_of(payload, SALE_NUMBER)["file"]["name"] = name

                run = import_archive(self.forged(reader, moved), MERGE)
                self.assertIn(
                    f"bon de vente n° {position} de l'archive : fichier hors du dossier des ventes (« {name} »)",
                    run.section("ventes").skipped,
                )
                self.assertFalse(SaleDocument.objects.filter(reference=SALE_NUMBER).exists())

    def test_a_file_without_its_sha_skips_the_record_never_the_run(self):
        """The archive takes a ref that leaves its sha out (`has_file` reads
        the declared one), but a sale document holds its file's sha - the
        one-file rule, the adoption: read as missing, the KeyError failed the
        WHOLE import. Skipped, and its document here kept from the prune."""
        reader = self.export(EXPORTED)
        position = position_of(archived_payload(reader), SALE_NUMBER)
        forged = self.forged(reader, lambda payload: record_of(payload, SALE_NUMBER)["file"].pop("sha256"))
        skipped = f"bon de vente n° {position} de l'archive : fichier illisible"
        run = import_archive(forged, SALES_REPLACED)
        self.assertIn(skipped, run.section("ventes").skipped)
        self.assertTrue(SaleDocument.objects.filter(pk=self.invoice.pk).exists())
        run_clear({"ventes"}, preview=False)
        for strategy in (MERGE, SALES_REPLACED):
            with self.subTest(strategy=strategy):
                run = import_archive(forged, strategy)
                self.assertIn(skipped, run.section("ventes").skipped)
                self.assertFalse(SaleDocument.objects.filter(reference=SALE_NUMBER).exists())

    def test_a_file_another_document_holds_is_skipped(self):
        reader = self.export(EXPORTED)

        def twin(payload):
            copy_of = copy.deepcopy(record_of(payload, SALE_NUMBER))
            copy_of.update(key="0123456789abcdef", reference="FV-2026-0199", payments=[])
            payload["documents"].append(copy_of)

        run = import_archive(self.forged(reader, twin), MERGE)
        self.assertIn(
            "Bon de vente du 03/09/2026 (FV-2026-0199) : fichier déjà celui du bon de vente du 03/09/2026 "
            "(FV-2026-0101), gardé tel quel",
            run.section("ventes").skipped,
        )
        self.assertFalse(SaleDocument.objects.filter(reference="FV-2026-0199").exists())

    def test_a_document_deleted_and_read_again_comes_back_under_replace(self):
        """Deleted, then the same file read again: a new key. Restoring
        yesterday's archive skipped its record (« fichier déjà … ») and the
        prune deleted the copy: the invoice in neither (transfer critique 4).
        The record takes the copy over: one document, its file kept."""
        reader = self.export(EXPORTED)
        key, name, sha = self.invoice.key, self.invoice.source_file.name, self.invoice.source_sha256
        self.invoice.delete()
        again = make_sale_document(
            sold_on=day(3), reference=SALE_NUMBER, einvoice_format="Factur-X", source_file=name, source_sha256=sha
        )
        make_sale_line(again, label="Formule cocktail", quantity="2", total_ht="169.00", vat_rate="0.2000")
        run = import_archive(reader, SALES_REPLACED)
        document = SaleDocument.objects.get(reference=SALE_NUMBER)
        self.assertEqual((document.pk, document.key), (again.pk, key))
        self.assertEqual((document.source_file.name, document.source_sha256), (name, sha))
        self.assertTrue(default_storage.exists(name))
        self.assertEqual(document.customer, CUSTOMER_NAME)
        self.assertEqual(document.lines.count(), 2)
        self.assertEqual(SaleDocumentPayment.objects.filter(document=document).count(), 1)
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (0, 1, 0, 5))

    def test_two_files_swapped_under_replace_meet_no_constraint(self):
        """Each document's sha written as its file is stored met
        `saledocument_one_per_file` half way, and the WHOLE import failed:
        the shas are written after the loop, in two steps."""
        reader = self.export(EXPORTED)
        minimum = SaleDocument.objects.get(reference="FV-2026-0103")
        mine = (self.invoice.source_file.name, self.invoice.source_sha256)
        theirs = (minimum.source_file.name, minimum.source_sha256)
        SaleDocument.objects.filter(pk=self.invoice.pk).update(source_sha256="")
        SaleDocument.objects.filter(pk=minimum.pk).update(source_file=mine[0], source_sha256=mine[1])
        SaleDocument.objects.filter(pk=self.invoice.pk).update(source_file=theirs[0], source_sha256=theirs[1])
        run = import_archive(reader, SALES_REPLACED)
        self.assertEqual(run.section("ventes").skipped, [])
        self.invoice.refresh_from_db()
        minimum.refresh_from_db()
        self.assertEqual((self.invoice.source_file.name, self.invoice.source_sha256), mine)
        self.assertEqual((minimum.source_file.name, minimum.source_sha256), theirs)
        self.assertEqual(tally(run, "ventes", FILES), (0, 2, 0, 0))

    def test_merge_fills_a_blank_customer_and_a_lost_file_beside_a_conflict(self):
        """Filled even beside a conflict, as the invoices do (transfer
        critique 7); nothing else of it moves."""
        reader = self.export(EXPORTED)
        sha = self.invoice.source_sha256
        SaleDocument.objects.filter(pk=self.invoice.pk).update(customer="", counting=TILL)
        default_storage.delete(EINVOICE_FILE)
        run = import_archive(reader, MERGE)
        self.invoice.refresh_from_db()
        self.assertEqual((self.invoice.customer, self.invoice.counting), (CUSTOMER_NAME, TILL))
        self.assertEqual(stored_sha(EINVOICE_FILE), sha)
        report = run.section("ventes")
        self.assertEqual(
            report.conflicts,
            ["Bon de vente du 03/09/2026 (FV-2026-0101) : différent dans l'archive (compte) — gardé tel quel"],
        )
        self.assertIn("Bon de vente du 03/09/2026 (FV-2026-0101) : fichier restauré", report.notes)
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (0, 1, 0, 5))
        self.assertEqual(tally(run, "ventes", FILES), (1, 0, 0, 1))

    def test_replace_removes_a_file_said_null_and_deletes_it_on_commit(self):
        reader = self.forged(self.export(EXPORTED), lambda payload: record_of(payload, SALE_NUMBER).update(file=None))
        with self.captureOnCommitCallbacks(execute=True):
            run = import_archive(reader, SALES_REPLACED)
        self.invoice.refresh_from_db()
        self.assertFalse(self.invoice.source_file)
        self.assertEqual(self.invoice.source_sha256, "")
        self.assertFalse(default_storage.exists(EINVOICE_FILE))
        self.assertEqual(tally(run, "ventes", FILES), (0, 0, 1, 1))

    def test_values_out_of_bounds_are_skipped_with_their_reason(self):
        """The codec reads any ISO date and any nine characters: a
        9999-12-31 from an archive made every page a 500 once, and Achats'
        guard reads the seller's SIREN (transfer critique 12)."""
        reader = self.export(EXPORTED)
        run_clear({"ventes"}, preview=False)
        today = timezone.localdate()

        def bounded(payload):
            records = payload["documents"]
            records[0]["sold_on"] = "9999-12-31"
            records[1]["einvoice_checks"] = [1]
            records[2]["adjustment_vat_rate"] = "1.5000"
            record_of(payload, SALE_NUMBER)["seller_siren"] = "ABCDEFGHI"
            record_of(payload, "FV-2026-0103")["source_sha256"] = "a" * 63
            # Informational, stored by « Lire la facture » as stated: never a
            # reason to skip the document (its own date is `sold_on`).
            records[5]["einvoice_delivered_on"] = "2999-01-01"

        run = import_archive(self.forged(reader, bounded), MERGE)
        self.assertEqual(
            run.section("ventes").skipped,
            [
                (
                    "bon de vente n° 1 de l'archive : « date de vente » : date hors limites (« 9999-12-31 ») : entre "
                    f"le 01/01/2000 et le {today:%d/%m/%Y}"
                ),
                "bon de vente n° 2 de l'archive : contrôles illisibles",
                "bon de vente n° 3 de l'archive : « taux des frais et remises » : taux hors 0 à 100 %",
                "bon de vente n° 4 de l'archive : SIREN du vendeur illisible (« ABCDEFGHI »)",
                "bon de vente n° 5 de l'archive : empreinte du fichier illisible",
            ],
        )
        self.assertEqual(list(SaleDocument.objects.values_list("einvoice_delivered_on", flat=True)), [date(2999, 1, 1)])

    def test_a_line_the_constraints_refuse_skips_the_record(self):
        reader = self.export(EXPORTED)
        run_clear({"ventes"}, preview=False)
        position = position_of(archived_payload(reader), SALE_NUMBER)
        cases = {
            "une recette, un article ou un libellé": {"label": ""},
            "un montant HT sans son taux": {"vat_rate": None},
            "taux hors 0 à 100 %": {"vat_rate": "2.0000"},
            "une quantité consommée sans recette ni article": {"consumed_quantity": "3.0000"},
            "une ligne reconstituée ne se relie pas": {"rebuilt": True, "recipe": "Mojito"},
        }
        for reason, change in cases.items():
            with self.subTest(reason=reason):

                def edit(payload, change=change):
                    record_of(payload, SALE_NUMBER)["lines"][1].update(change)

                run = import_archive(self.forged(reader, edit), MERGE)
                self.assertIn(
                    f"bon de vente n° {position} de l'archive : ligne n° 2 : {reason}", run.section("ventes").skipped
                )
                self.assertFalse(SaleDocument.objects.filter(reference=SALE_NUMBER).exists())

    def test_documents_left_out_of_an_archive_prune_nothing(self):
        """Read as an empty list, « Remplacer » deleted every sale invoice,
        its file and its links (transfer critique 13)."""
        reader = self.forged(self.export(EXPORTED), lambda payload: payload.pop("documents"))
        run = import_archive(reader, SALES_REPLACED)
        self.assertEqual(SaleDocument.objects.count(), 6)
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (0, 0, 0, 0))
        self.assertEqual(SaleDocumentPayment.objects.count(), 1)

    def test_links_that_are_no_list_refuse_the_archive(self):
        """The bank's own rule: a « Remplacer » reading them as empty would
        unlink everything."""
        reader = self.export(EXPORTED)
        for payments in (None, "aucun", [1]):
            with self.subTest(payments=payments):
                forged = self.forged(
                    reader, lambda p, payments=payments: record_of(p, SALE_NUMBER).update(payments=payments)
                )
                with self.assertRaisesMessage(
                    ArchiveError,
                    "Archive refusée : dans ventes.json, les règlements d'un bon de vente ne sont pas une liste.",
                ):
                    import_archive(forged, MERGE)


class SaleLinksArchiveTests(MediaMixin, LaneSectionsMixin, TestCase):
    """The credits of the statement that pay a document, nested in its
    record by their fingerprint: merged like the bank's own links - never
    against a decision the credit holds here -, replaced to be exactly the
    archive's, and only on a document this run applied."""

    def setUp(self):
        super().setUp()
        build_sales()
        self.invoice = SaleDocument.objects.get(reference=SALE_NUMBER)
        self.credit = BankTransaction.objects.get(fingerprint=SALES_CREDIT)
        self.reader = self.export(EXPORTED)

    def links(self) -> list[tuple]:
        return sorted(SaleDocumentPayment.objects.values_list("document_id", "transaction_id", "method"))

    def forged(self, edit):
        return self.open(forge(self.reader, ventes=editing(edit)))

    def test_merge_brings_a_link_back_with_its_moment(self):
        SaleDocumentPayment.objects.all().delete()
        run = import_archive(self.reader, MERGE)
        link = SaleDocumentPayment.objects.get()
        self.assertEqual(
            (link.document_id, link.transaction_id, link.method, link.created_at),
            (self.invoice.pk, self.credit.pk, "MANUAL", moment(16)),
        )
        self.assertEqual(tally(run, "ventes", LINKS), (1, 0, 0, 0))
        # A merge never runs the bank's automatic pass: nothing else linked.
        self.assertFalse(BankTransaction.objects.get(pk=self.credit.pk).settled_by_hand)

    def test_replace_makes_a_documents_links_the_archives(self):
        other = make_credit("50.00", day(11), fingerprint="ventes-credit-essai-2")
        make_sale_payment(self.invoice, other)
        SaleDocumentPayment.objects.filter(transaction=self.credit).update(method="AUTO")
        run = import_archive(self.reader, SALES_REPLACED)
        self.assertEqual(self.links(), [(self.invoice.pk, self.credit.pk, "MANUAL")])
        self.assertEqual(tally(run, "ventes", LINKS), (0, 1, 1, 0))

    def test_merge_keeps_a_credit_paying_here_a_document_the_archive_does_not_give_it(self):
        keg = SaleDocument.objects.get(sold_on=day(5))
        SaleDocumentPayment.objects.all().delete()
        make_sale_payment(keg, self.credit)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(self.links(), [(keg.pk, self.credit.pk, "MANUAL")])
        self.assertEqual(
            run.section("ventes").conflicts,
            [
                (
                    "Entrée du 10/09/2026 (230,52 €) : règle ici le bon de vente du 05/09/2026, dans l'archive le "
                    "bon de vente du 03/09/2026 (FV-2026-0101) — gardée telle quelle"
                )
            ],
        )

    def test_merge_keeps_a_link_undone_by_hand(self):
        """`settled_by_hand` and no link: the person unlinked it here. Made
        again, the invoice would read as paid by a credit they said does
        not pay it."""
        SaleDocumentPayment.objects.all().delete()
        BankTransaction.objects.filter(pk=self.credit.pk).update(settled_by_hand=True)
        run = import_archive(self.reader, MERGE)
        self.assertEqual(self.links(), [])
        self.assertEqual(
            run.section("ventes").conflicts,
            [
                (
                    "Entrée du 10/09/2026 : réglée à la main ici sans régler le bon de vente du 03/09/2026 "
                    "(FV-2026-0101), qu'elle règle dans l'archive — gardée telle quelle"
                )
            ],
        )
        self.assertIn(SALE_UNDONE_NOTE, run.section("ventes").notes)
        # « Remplacer » is the archive's word.
        import_archive(self.reader, SALES_REPLACED)
        self.assertEqual(self.links(), [(self.invoice.pk, self.credit.pk, "MANUAL")])

    def test_merge_says_a_link_of_another_method(self):
        SaleDocumentPayment.objects.update(method="AUTO")
        run = import_archive(self.reader, MERGE)
        self.assertEqual(
            run.section("ventes").conflicts,
            [
                (
                    "Entrée du 10/09/2026 (230,52 €) : le lien vers le bon de vente du 03/09/2026 (FV-2026-0101) "
                    "est « Automatique » ici, « À la main » dans l'archive — gardé tel quel"
                )
            ],
        )
        self.assertEqual(self.links(), [(self.invoice.pk, self.credit.pk, "AUTO")])

    def test_a_credit_absent_here_is_skipped(self):
        SaleDocumentPayment.objects.all().delete()
        reader = self.forged(
            lambda payload: record_of(payload, SALE_NUMBER)["payments"][0].update(transaction="absente")
        )
        run = import_archive(reader, MERGE)
        self.assertIn(
            "Bon de vente du 03/09/2026 (FV-2026-0101) : entrée absente (lien du 10/09/2026)",
            run.section("ventes").skipped,
        )
        self.assertEqual(self.links(), [])

    def test_a_debit_pays_no_sale(self):
        make_credit("-230.52", day(11), fingerprint="ventes-debit-essai", kind=BankTransaction.Kind.DEBIT)
        SaleDocumentPayment.objects.all().delete()
        reader = self.forged(
            lambda payload: record_of(payload, SALE_NUMBER)["payments"][0].update(transaction="ventes-debit-essai")
        )
        run = import_archive(reader, MERGE)
        self.assertIn(
            "Bon de vente du 03/09/2026 (FV-2026-0101) : l'opération du 11/09/2026 n'est pas une entrée d'argent",
            run.section("ventes").skipped,
        )
        self.assertEqual(self.links(), [])

    def test_a_link_twice_in_a_record_and_an_unknown_method_are_skipped(self):
        SaleDocumentPayment.objects.all().delete()

        def twice(payload):
            payments = record_of(payload, SALE_NUMBER)["payments"]
            payments.append(dict(payments[0]))
            record_of(payload, "FV-2026-0103")["payments"] = [{"transaction": SALES_CREDIT, "method": "PERDU"}]

        run = import_archive(self.forged(twice), MERGE)
        skipped = run.section("ventes").skipped
        self.assertIn(
            "Bon de vente du 03/09/2026 (FV-2026-0101) : règlement de l'entrée du 10/09/2026 en double dans l'archive",
            skipped,
        )
        self.assertIn(
            "Bon de vente du 04/09/2026 (FV-2026-0103) : règlement de l'entrée du 10/09/2026 : « method » : valeur "
            "inconnue (« PERDU »)",
            skipped,
        )
        self.assertEqual(self.links(), [(self.invoice.pk, self.credit.pk, "MANUAL")])

    def test_a_record_kept_as_it_is_takes_no_link_under_replace(self):
        other = make_credit("50.00", day(11), fingerprint="ventes-credit-essai-2")
        make_sale_payment(self.invoice, other)
        reader = self.forged(lambda payload: record_of(payload, SALE_NUMBER)["lines"][0].update(recipe="Mojito fraise"))
        run = import_archive(reader, SALES_REPLACED)
        self.assertIn(
            "Bon de vente du 03/09/2026 (FV-2026-0101) : recette inconnue « Mojito fraise » — gardé tel quel",
            run.section("ventes").skipped,
        )
        self.assertEqual(
            self.links(), [(self.invoice.pk, self.credit.pk, "MANUAL"), (self.invoice.pk, other.pk, "MANUAL")]
        )
        self.assertTrue(SaleDocument.objects.filter(pk=self.invoice.pk).exists())

    def test_a_document_kept_whole_by_a_conflict_takes_none(self):
        SaleDocumentPayment.objects.all().delete()
        SaleDocument.objects.filter(pk=self.invoice.pk).update(note="Changée ici")
        run = import_archive(self.reader, MERGE)
        self.assertEqual(self.links(), [])
        self.assertEqual(tally(run, "ventes", LINKS), (0, 0, 0, 0))

    def test_a_document_the_archive_does_not_name_goes_with_its_links(self):
        stray = make_sale_document(sold_on=day(9), reference="ICI")
        make_sale_line(stray, label="Vestiaire", quantity="1", unit_price_ttc="2.00")
        make_sale_payment(stray, make_credit("2.00", day(9), fingerprint="ventes-credit-ici"))
        run = import_archive(self.reader, SALES_REPLACED)
        self.assertFalse(SaleDocument.objects.filter(reference="ICI").exists())
        self.assertEqual(tally(run, "ventes", DOCUMENTS), (0, 0, 1, 6))
        self.assertEqual(tally(run, "ventes", LINES)[2], 1)
        self.assertEqual(tally(run, "ventes", LINKS), (0, 0, 1, 1))


class SaleLinksWithTheBankTests(MediaMixin, LaneSectionsMixin, TestCase):
    """« Banque » real beside « Ventes », as an owner restores both."""

    def setUp(self):
        super().setUp()
        swap = registry.swap(
            {
                **FAKES,
                "recettes": RecipesSection,
                "liens_ventes": TillLinksSection,
                "ventes": SalesSection,
                "banque": BankSection,
            }
        )
        swap.__enter__()
        self.addCleanup(swap.__exit__, None, None, None)
        build_sales()
        self.reader = self.export(EXPORTED | {"banque"})
        # A credit imported after the archive was taken, which the archive's
        # link is made to name.
        self.later = make_credit("230.52", day(12), fingerprint="ventes-credit-apres-export")
        SaleDocumentPayment.objects.all().delete()
        self.forged = self.open(
            forge(
                self.reader,
                ventes=editing(
                    lambda payload: record_of(payload, SALE_NUMBER)["payments"][0].update(
                        transaction=self.later.fingerprint
                    )
                ),
            )
        )

    def test_banque_replaced_in_the_same_run_links_only_to_its_own_lines(self):
        """Its prune deletes every line banque.json does not name: a link
        made to one would rest on a line the same run removes."""
        run = import_archive(self.forged, {**SALES_REPLACED, "banque": REPLACE})
        self.assertIn(
            "Bon de vente du 03/09/2026 (FV-2026-0101) : entrée absente (lien du 10/09/2026)",
            run.section("ventes").skipped,
        )
        self.assertFalse(SaleDocumentPayment.objects.exists())
        self.assertFalse(BankTransaction.objects.filter(fingerprint=self.later.fingerprint).exists())

    def test_banque_merged_keeps_every_line_and_takes_the_link(self):
        import_archive(self.forged, {**SALES_REPLACED, "banque": MERGE})
        self.assertEqual(
            list(SaleDocumentPayment.objects.values_list("document__reference", "transaction__fingerprint")),
            [(SALE_NUMBER, self.later.fingerprint)],
        )

    def test_both_cleared_and_imported_together_the_link_comes_back(self):
        before = SalesSection().snapshot()
        SaleDocumentPayment.objects.all().delete()
        make_sale_payment(
            SaleDocument.objects.get(reference=SALE_NUMBER), BankTransaction.objects.get(fingerprint=SALES_CREDIT)
        )
        reader = self.export(EXPORTED | {"banque"})
        run_clear({"ventes", "banque"}, preview=False)
        import_archive(reader, REPLACE)
        self.assertEqual(
            list(SaleDocumentPayment.objects.values_list("document__reference", "transaction__fingerprint")),
            [(SALE_NUMBER, SALES_CREDIT)],
        )
        self.assertEqual(len(SalesSection().snapshot()["documents"]), len(before["documents"]))


class ContractTests(SimpleTestCase):
    def test_every_field_of_a_sale_document_is_exported_or_said_why_not(self):
        """A field added to one of these models later cannot be left out of
        the archive in silence: the round trip would not see it, its
        snapshot being made of the same records."""
        self.assertEqual(set(sales.EXPORTED), {SaleDocument, SaleDocumentLine, SaleDocumentPayment})
        self.assertEqual(set(sales.NOT_EXPORTED), set(sales.EXPORTED))
        for model, exported in sales.EXPORTED.items():
            with self.subTest(model=model.__name__):
                concrete = {field.name for field in model._meta.concrete_fields}
                self.assertEqual(concrete, set(exported) | set(sales.NOT_EXPORTED[model]))
                self.assertFalse(set(exported) & set(sales.NOT_EXPORTED[model]))

    def test_what_a_record_says_is_what_the_section_reads(self):
        self.assertEqual(sales.DOCUMENT_KEYS, (*sales.DOCUMENT_FIELDS, "created_at", "file", "lines", "payments"))
        self.assertNotIn("source_sha256", sales.COMPARED)
        self.assertNotIn("key", sales.COMPARED)
        self.assertLessEqual(set(sales.FILLABLE), set(sales.COMPARED))
        self.assertEqual(sales.FOLDER, "ventes/")
        self.assertIs(sales.LINKS, SALE_LINKS)


class ImportOrderTests(SimpleTestCase):
    def test_the_sections_import_without_a_cycle(self):
        """« Ventes » and « Banque » share the sale links' label and the
        helpers of their prunes through sections/base.py, never through each
        other: registry.load imports « Ventes » first, and a « Banque »
        importing it while it imported « Banque » would be an ImportError on
        a partly initialised module - « Données » down. Every section
        imported afresh, in the registry's own order: gone from sys.modules
        AND from its package's attributes, or `from transfer.sections import
        bank` hands back the module already loaded and hides the cycle."""
        self.assertLess(registry.SECTION_MODULES.index("sales"), registry.SECTION_MODULES.index("bank"))
        names = [name for name in sys.modules if name == "transfer.registry" or name.startswith("transfer.sections.")]
        sections = vars(transfer.sections)
        before = dict(sections)

        def put_back():
            # The fresh imports set the package's attributes to themselves.
            for attribute in set(sections) - set(before):
                del sections[attribute]
            sections.update(before)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(sys.modules))
            stack.enter_context(mock.patch.object(transfer, "registry", registry))
            stack.callback(put_back)
            for name in names:
                del sys.modules[name]
                if name.startswith("transfer.sections."):
                    sections.pop(name.rsplit(".", 1)[1], None)
            fresh = importlib.import_module("transfer.registry")
            with self.assertNoLogs("transfer.registry", level="ERROR"):
                fresh.load_sections()
            self.assertEqual(set(fresh.registered()), set(fresh.INFO))
            self.assertIsNot(fresh, registry)
