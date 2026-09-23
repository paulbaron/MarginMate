"""Put the money back on the till days already imported.

    python manage.py laddition_backfill_revenue --dry-run
    python manage.py laddition_backfill_revenue
    python manage.py laddition_backfill_revenue --folder /un/dossier

The quantities have been recorded per (till product, day) since the first
import; the amounts have not, because the reader ignored the export's money
columns until now. They are in the .xlsx already downloaded, so this reads
those files - **nothing is downloaded, nothing contacts L'Addition** - and
fills in each day's revenue on the row that already exists for it.

Three things it will not do:

- **create a row.** A (product, day) nobody imported would be money against
  a quantity nobody counted, and every stock figure on it would be a guess.
  Unmatched readings are listed instead, to be imported properly.
- **add a day to itself.** The stored exports cover the same history several
  times over (overlapping windows, different signatures), so a (product,
  day) read again REPLACES what the previous file said rather than adding
  to it - and files that disagree about one are reported, never averaged.
- **touch the quantities.** They are the import's, and they are already
  right.

`--dry-run` first: it says which files, which days, how many rows it would
fill, how many it cannot match, and the revenue per year - before anything
is written.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from recipes.models import PosProduct, PosProductDailyQuantity
from recipes.pos.laddition_xlsx import LadditionExportError, parse_sales_export

#: Rows written per query: SQLite caps the parameters of one statement.
BATCH = 500
#: How many unmatched readings are named before the list is cut short - the
#: point is to make them actionable, not to print a second export.
SHOWN = 20
REVENUE_FIELDS = ["revenue_ttc", "revenue_ht", "revenue_without_rate_ttc", "revenue_read"]


def euros(value) -> str:
    """1234.5 -> « 1 234,50 € »."""
    text = f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} €"


def _day(value: date) -> str:
    return f"{value:%d/%m/%Y}"


class Command(BaseCommand):
    help = (
        "Reprend les recettes (Prix TTC) des exports L'Addition déjà téléchargés et les pose sur "
        "les jours de caisse déjà importés. Ne télécharge rien, ne crée aucune ligne."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--folder",
            default=None,
            help="Dossier des .xlsx à lire (par défaut celui des téléchargements).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Montrer sans rien enregistrer.")

    def handle(self, *args, folder=None, dry_run=False, **options):
        directory = Path(folder or settings.SCRAPE_DOWNLOAD_DIR)
        if not directory.is_dir():
            raise CommandError(f"Dossier introuvable : {directory}")
        # "~$…" is Excel's own lock file, not an export.
        files = sorted(path for path in directory.glob("*.xlsx") if not path.name.startswith("~$"))
        self.stdout.write(f"Dossier : {directory}")
        if not files:
            raise CommandError("Aucun fichier .xlsx dans ce dossier.")
        self.stdout.write(f"{len(files)} fichier(s) .xlsx.")

        money, conflicts = self._read(files)
        if not money:
            self.stdout.write("Aucune recette lue : ces fichiers ne portent pas les colonnes de prix.")
            return
        self._report_conflicts(conflicts)
        self._report_years(money)

        fill, unknown_products, missing_days, unchanged = self._match(money)
        self.stdout.write(f"{len(fill)} ligne(s) (produit, jour) à remplir.")
        if unchanged:
            self.stdout.write(f"{unchanged} ligne(s) déjà à jour.")
        self._report_unmatched(unknown_products, missing_days)

        if dry_run:
            self.stdout.write(self.style.WARNING("(essai : rien n'est enregistré)"))
            return
        written = self._write(fill)
        self.stdout.write(self.style.SUCCESS(f"{written} ligne(s) mises à jour."))

    # -- reading -----------------------------------------------------------------------
    def _read(self, files):
        """{(till name, day): DayMoney} over the whole folder, last reading
        kept, plus the readings two files disagree about."""
        money: dict[tuple[str, date], object] = {}
        conflicts: list[tuple[str, date, Decimal, Decimal]] = []
        for path in files:
            try:
                export = parse_sales_export(str(path))
            except LadditionExportError as exc:
                self.stdout.write(self.style.WARNING(f"{path.name} : illisible - {exc}"))
                continue
            covered = export.days
            window = f"du {_day(covered[0])} au {_day(covered[1])}" if covered else "aucun jour"
            self.stdout.write(
                f"{path.name} : {len(export.entries)} (produit, jour), {window}, "
                f"{euros(export.revenue_ttc)} TTC / {euros(export.revenue_ht)} HT."
            )
            for detail in self._oddities(export):
                self.stdout.write(f"    {detail}")
            for key, day_money in export.money.items():
                before = money.get(key)
                if before is not None and before.revenue_ttc != day_money.revenue_ttc:
                    conflicts.append((key[0], key[1], before.revenue_ttc, day_money.revenue_ttc))
                money[key] = day_money
        return money, conflicts

    @staticmethod
    def _oddities(export) -> list[str]:
        """Everything about this file that is not routine. A column that
        moved, a rate nobody can read or a discount nobody expected has to
        be visible here: downstream it is only ever a margin slightly too
        comfortable."""
        said = []
        if not export.money_columns:
            return ["pas de colonne « Prix TTC » : aucune recette dans ce fichier."]
        if export.lines_without_rate:
            said.append(
                f"{export.lines_without_rate} ligne(s) sans taux lisible : "
                f"{euros(export.revenue_without_rate_ttc)} TTC sans HT (aucun taux supposé)."
            )
        if export.lines_without_amount:
            said.append(f"{export.lines_without_amount} ligne(s) sans montant lisible.")
        if export.days_without_amount:
            said.append(
                f"{export.days_without_amount} (produit, jour) laissé(s) non lu(s) : une de leurs "
                "lignes n'a pas de montant, et leur recette serait trop basse d'un montant inconnu."
            )
        if export.discounted_lines:
            said.append(
                f"{export.discounted_lines} ligne(s) avec remise, {euros(export.discount_ttc)} déduits."
            )
        if export.discounts_not_taken:
            said.append(
                f"{export.discounts_not_taken} ligne(s) dont la remise n'a pas été déduite : "
                "elle rendrait la ligne plus grosse que son propre prix - à vérifier."
            )
        if export.refund_lines:
            said.append(f"{export.refund_lines} ligne(s) de remboursement (quantité négative).")
        if export.unusual_quantity_lines:
            said.append(
                f"{export.unusual_quantity_lines} ligne(s) à une quantité autre que 1 : le montant "
                "lu reste « Prix TTC », celui de la ligne - à vérifier."
            )
        if export.skipped:
            said.append(f"{export.skipped} ligne(s) sans jour/nom/quantité exploitables (dont le total).")
        return said

    def _report_conflicts(self, conflicts) -> None:
        if not conflicts:
            return
        self.stdout.write(
            self.style.WARNING(
                f"{len(conflicts)} (produit, jour) lus différemment selon le fichier - la dernière "
                "lecture est gardée, à vérifier :"
            )
        )
        for name, day, before, after in conflicts[:SHOWN]:
            self.stdout.write(f"  - « {name} » le {_day(day)} : {euros(before)} puis {euros(after)}")
        if len(conflicts) > SHOWN:
            self.stdout.write(f"  … et {len(conflicts) - SHOWN} autre(s).")

    def _report_years(self, money) -> None:
        years: dict[int, list] = defaultdict(lambda: [Decimal("0"), Decimal("0")])
        for (_name, day), day_money in money.items():
            years[day.year][0] += day_money.revenue_ttc
            years[day.year][1] += day_money.revenue_ht
        self.stdout.write("Recettes par année (telles que lues) :")
        for year in sorted(years):
            ttc, ht = years[year]
            self.stdout.write(f"  {year} : {euros(ttc)} TTC / {euros(ht)} HT")

    # -- matching ----------------------------------------------------------------------
    def _match(self, money):
        """What each reading lands on: an existing row, a till product this
        database has never seen, or a product it knows on a day it never
        imported."""
        products = {name: pk for pk, name in PosProduct.objects.values_list("pk", "name")}
        rows = {
            (product_id, sold_on): (pk, revenue_ttc, revenue_ht, without, read)
            for pk, product_id, sold_on, revenue_ttc, revenue_ht, without, read in
            PosProductDailyQuantity.objects.values_list(
                "pk", "product_id", "sold_on", "revenue_ttc", "revenue_ht",
                "revenue_without_rate_ttc", "revenue_read",
            )
        }
        fill: list[tuple[int, object]] = []
        unknown_products: dict[str, list] = defaultdict(lambda: [0, Decimal("0")])
        missing_days: list[tuple[str, date, Decimal]] = []
        unchanged = 0
        for (name, day), day_money in sorted(money.items(), key=lambda item: (item[0][1], item[0][0])):
            product_id = products.get(name)
            if product_id is None:
                unknown_products[name][0] += 1
                unknown_products[name][1] += day_money.revenue_ttc
                continue
            found = rows.get((product_id, day))
            if found is None:
                missing_days.append((name, day, day_money.revenue_ttc))
                continue
            _pk, revenue_ttc, revenue_ht, without, read = found
            if read and (revenue_ttc, revenue_ht, without) == (
                day_money.revenue_ttc, day_money.revenue_ht, day_money.without_rate_ttc
            ):
                unchanged += 1
                continue
            fill.append((found[0], day_money))
        return fill, unknown_products, missing_days, unchanged

    def _report_unmatched(self, unknown_products, missing_days) -> None:
        total = len(unknown_products) + len(missing_days)
        if not total:
            return
        # Said in money as well as in rows: what nothing here carries is
        # revenue the margin pages will never see, and « 12 lignes » does
        # not say whether that is a coffee or a fortnight of takings.
        lost = sum((figures[1] for figures in unknown_products.values()), Decimal("0"))
        lost += sum((amount for _name, _day, amount in missing_days), Decimal("0"))
        self.stdout.write(
            self.style.WARNING(
                f"{len(unknown_products)} produit(s) inconnu(s) ici et {len(missing_days)} jour(s) "
                f"sans ligne enregistrée, {euros(lost)} TTC : sans correspondance, rien n'est créé."
            )
        )
        for name, (days, amount) in sorted(unknown_products.items())[:SHOWN]:
            self.stdout.write(
                f"  - « {name} » : produit inconnu ici ({days} jour(s), {euros(amount)} TTC)"
            )
        for name, day, amount in missing_days[:SHOWN]:
            self.stdout.write(
                f"  - « {name} » le {_day(day)} : aucune ligne enregistrée ({euros(amount)} TTC)"
            )
        if total > SHOWN:
            self.stdout.write(
                "  … relancez l'import L'Addition sur ces dates pour enregistrer les quantités, "
                "puis cette commande."
            )

    # -- writing -----------------------------------------------------------------------
    def _write(self, fill) -> int:
        written = 0
        with transaction.atomic():
            for start in range(0, len(fill), BATCH):
                batch = fill[start:start + BATCH]
                PosProductDailyQuantity.objects.bulk_update(
                    [
                        PosProductDailyQuantity(
                            pk=pk,
                            revenue_ttc=day_money.revenue_ttc,
                            revenue_ht=day_money.revenue_ht,
                            revenue_without_rate_ttc=day_money.without_rate_ttc,
                            revenue_read=True,
                        )
                        for pk, day_money in batch
                    ],
                    REVENUE_FIELDS,
                )
                written += len(batch)
        return written
