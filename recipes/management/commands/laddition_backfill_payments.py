"""Put the till's means of payment on the till days already imported.

    python manage.py laddition_backfill_payments --dry-run
    python manage.py laddition_backfill_payments
    python manage.py laddition_backfill_payments --folder /un/dossier

The export's « SalesDocument » sheet - one row per ticket, and how it was
paid - was not read until now, and the .xlsx already downloaded carry it.
This reads those files - **nothing is downloaded, nothing contacts
L'Addition** - and stores each day's payments per means of payment
(PosDailyPayment), what the bank's « Entrées d'argent » page compares with
what arrived on the account.

Three things it will not do:

- **fill a day « Ventes » does not hold.** Payments for a day with no till
  sales stored would be money the sales pages contradict, the same reason
  laddition_backfill_revenue creates no row. Those days are counted and
  said, to be imported properly first.
- **add a day to itself.** The stored exports cover the same history several
  times over, so a day read again REPLACES what the previous file said -
  every method of it - and files that disagree about a day are reported,
  never averaged.
- **touch the sales.** Quantities and the day's takings are the import's.

`--dry-run` first: it says which files, which days, how many it would fill,
how many it leaves out, and the payments per year and per method - before
anything is written.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from recipes.management.commands.laddition_backfill_revenue import SHOWN, euros
from recipes.models import PosDailyPayment, PosProductDailyQuantity
from recipes.payments import by_method, changed_days, day_total, oddities, replace_days
from recipes.pos.laddition_xlsx import (
    DayPayment,
    LadditionExportError,
    PaymentsSheetMissing,
    parse_payments_export,
)


def _day(value: date) -> str:
    return f"{value:%d/%m/%Y}"


def _said(payments: dict) -> str:
    """{method: DayPayment} as « Carte 7,50 €, Espèces 2,00 € »."""
    if not payments:
        return "aucun paiement"
    return ", ".join(
        f"{PosDailyPayment.label_for(method)} {euros(payment.amount)}" for method, payment in by_method(payments)
    )


class Command(BaseCommand):
    help = (
        "Reprend les moyens de paiement (feuille des tickets) des exports L'Addition déjà téléchargés "
        "et les pose sur les jours de caisse déjà importés. Ne télécharge rien."
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

        readings, conflicts = self._read(files)
        if not readings:
            self.stdout.write(
                "Aucun paiement lu : ces fichiers ne portent pas la feuille des tickets (SalesDocument)."
            )
            return
        self._report_conflicts(conflicts)
        self._report_years(readings)
        self._report_methods(readings)

        fill, unchanged, without_sales, nothing_paid = self._match(readings)
        self.stdout.write(f"{len(fill)} jour(s) de caisse à remplir.")
        if unchanged:
            self.stdout.write(f"{unchanged} jour(s) déjà à jour.")
        self._report_without_sales(without_sales)
        if nothing_paid:
            # Said, neutrally: the file's reading stays visible, and nothing
            # sends the owner to download what no import can bring.
            self.stdout.write(
                f"{nothing_paid} jour(s) lus sans rien de payé (tickets à 0 €) et sans ventes enregistrées : "
                "rien à y écrire."
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("(essai : rien n'est enregistré)"))
            return
        written = replace_days(readings, fill)
        self.stdout.write(
            self.style.SUCCESS(
                f"{written.days_written} jour(s) mis à jour ({written.rows_created} total(aux) par moyen "
                "de paiement)."
            )
        )

    # -- reading -----------------------------------------------------------------------
    def _read(self, files):
        """{day: {method: DayPayment}} over the whole folder, last reading of
        a day kept whole, plus the days two files disagree about."""
        readings: dict[date, dict] = {}
        conflicts: list[tuple[date, dict, dict]] = []
        for path in files:
            try:
                export = parse_payments_export(str(path))
            except PaymentsSheetMissing:
                # An older export: not damage, just no payments in it.
                self.stdout.write(f"{path.name} : pas de feuille des tickets (SalesDocument), aucun paiement.")
                continue
            except LadditionExportError as exc:
                self.stdout.write(self.style.WARNING(f"{path.name} : illisible - {exc}"))
                continue
            days = export.payment_days
            window = f"du {_day(min(days))} au {_day(max(days))}" if days else "aucun jour"
            self.stdout.write(
                f"{path.name} : {export.tickets} ticket(s), {window}, {euros(export.payments_total)} payés."
            )
            for detail in oddities(export):
                self.stdout.write(f"    {detail}")
            for day, payments in export.payments_by_day().items():
                before = readings.get(day)
                if before is not None and self._amounts(before) != self._amounts(payments):
                    conflicts.append((day, before, payments))
                readings[day] = payments
        return readings, conflicts

    @staticmethod
    def _amounts(payments: dict) -> dict:
        return {method: payment.amount for method, payment in payments.items()}

    def _report_conflicts(self, conflicts) -> None:
        if not conflicts:
            return
        self.stdout.write(
            self.style.WARNING(
                f"{len(conflicts)} jour(s) lus différemment selon le fichier - la dernière lecture est "
                "gardée, à vérifier :"
            )
        )
        for day, before, after in conflicts[:SHOWN]:
            self.stdout.write(f"  - le {_day(day)} : {_said(before)} puis {_said(after)}")
        if len(conflicts) > SHOWN:
            self.stdout.write(f"  … et {len(conflicts) - SHOWN} autre(s).")

    def _report_years(self, readings) -> None:
        """Per year, what was paid and how - the figure to check by hand
        against what the owner knows the bar took (it stays out of this
        repository, which is public)."""
        years: dict[int, dict] = defaultdict(dict)
        for day, payments in readings.items():
            for method, payment in payments.items():
                total = years[day.year].setdefault(method, DayPayment())
                total.amount += payment.amount
                total.count += payment.count
        self.stdout.write("Paiements par année (tels que lus) :")
        for year in sorted(years):
            self.stdout.write(f"  {year} : {euros(day_total(years[year]))} ({_said(years[year])})")

    def _report_methods(self, readings) -> None:
        methods: dict[str, list] = defaultdict(lambda: [Decimal("0"), 0])
        for payments in readings.values():
            for method, payment in payments.items():
                methods[method][0] += payment.amount
                methods[method][1] += payment.count
        self.stdout.write("Par moyen de paiement (tels que lus) :")
        for method, (amount, count) in by_method(methods):
            # UNREAD and UNPAID count tickets, not payments.
            unit = "ticket(s)" if method in (PosDailyPayment.UNREAD, PosDailyPayment.UNPAID) else "paiement(s)"
            self.stdout.write(f"  {PosDailyPayment.label_for(method)} : {euros(amount)} en {count} {unit}")

    # -- matching ----------------------------------------------------------------------
    def _match(self, readings):
        """(days to write, days already as read, [(day, amount)] of the days
        « Ventes » does not hold, how many of those paid nothing at all).

        A day whose reading paid nothing - every ticket comped, `{}` - is
        not « left aside »: there is nothing to write on it, and when the
        lines sheet holds no row for it no import can ever give it a day in
        « Ventes ». Listed with the others it drew, on every run, the advice
        to import again what no import brings."""
        with_sales = set(PosProductDailyQuantity.objects.values_list("sold_on", flat=True).distinct())
        missing = [day for day in sorted(readings) if day not in with_sales]
        without_sales = [(day, day_total(readings[day])) for day in missing if readings[day]]
        fill, unchanged = changed_days(readings, [day for day in readings if day in with_sales])
        return fill, unchanged, without_sales, len(missing) - len(without_sales)

    def _report_without_sales(self, without_sales) -> None:
        if not without_sales:
            return
        # Said in money as well as in days: « 3 jours » does not say whether
        # that is a quiet Monday or a fortnight of takings.
        amount = sum((total for _day_, total in without_sales), Decimal("0"))
        self.stdout.write(
            self.style.WARNING(
                f"{len(without_sales)} jour(s) sans ventes enregistrées ici, {euros(amount)} payés : laissés "
                "de côté - des paiements sans les ventes du jour seraient un chiffre que les pages de ventes "
                "contrediraient."
            )
        )
        for day, total in without_sales[:SHOWN]:
            self.stdout.write(f"  - le {_day(day)} : {euros(total)}")
        if len(without_sales) > SHOWN:
            self.stdout.write(f"  … et {len(without_sales) - SHOWN} autre(s).")
        self.stdout.write(
            "  Relancez l'import L'Addition sur ces dates pour enregistrer les ventes, puis cette commande."
        )
