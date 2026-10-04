"""The ways the till's sales come in, and which a bar has.

Two kinds, one writer (recipes.tasks.store_reading):

- **a fetcher** signs in to a till's back office with the bar's own account
  and downloads its export - today L'Addition alone (`LADDITION`). Its card
  on « Ventes » is drawn only where its account is ready: the till may be
  used in this espace (`integration.till_allowed`) and every credential has
  a value a connector would sign in with (`accounts.vault.ready` - typed on
  « Identifiants », or the server's .env in the owner's espace only), read
  in ONE reading of the store. Nowhere else does a bar see a form that
  could only fail;
- **a file** uploaded on « Ventes »: L'Addition's own « Lignes de ventes »
  export (`LADDITION_CHOICE`, read by its parser, no account needed), or
  any till's CSV or .xlsx read by a format the bar describes
  (`TillFormat`, recipes/pos/till_file.py).

Which till a bar has is derived, never a setting: the credentials and the
formats it has typed say it, and one bar may use both (fetching, and old
history files).

A future API connector (Zelty, Lightspeed…) is a `Fetcher` entry here, an
account on « Identifiants » (accounts/credentials.py) and a reader returning
the same `ParsedExport` - then `store_reading`, like the others. Every row it
writes is the till's (`sales.TILL_SOURCE`): the connector's name lives in the
job's log, never in the rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .till_file import TillFileError, check_suffix


@dataclass(frozen=True)
class Fetcher:
    """A till whose export is fetched with the bar's own account."""

    key: str
    label: str
    #: The names its credentials are kept under (« Identifiants », else the
    #: owner's .env).
    credentials: tuple[str, ...]

    def ready(self) -> bool:
        """Whether this espace may use it and every credential has a value -
        one reading of the store; a store that cannot be read is not ready."""
        from accounts import vault

        from ..integration import till_allowed

        return till_allowed() and vault.ready(*self.credentials)


LADDITION = Fetcher("laddition", "L'Addition", ("LADDITION_EMAIL", "LADDITION_PASSWORD"))
FETCHERS = (LADDITION,)

#: The upload's choice reading L'Addition's own export, by its parser.
LADDITION_CHOICE = "laddition"
LADDITION_CHOICE_LABEL = "Export L'Addition « Lignes de ventes » (.xlsx)"

FORMAT_UNKNOWN = "Format inconnu."
FORMAT_GONE = "Le format choisi n'existe plus : choisissez-en un autre."
LADDITION_XLSX = "L'export « Lignes de ventes » de L'Addition est un fichier .xlsx."


@dataclass(frozen=True)
class UploadChoice:
    """One entry of the upload's « Format » select."""

    value: str
    label: str
    #: The format reads no day: the day is given with the upload.
    needs_day: bool = False


def upload_choices() -> list[UploadChoice]:
    """L'Addition's export first, then every format of this espace, by name."""
    from ..models import TillFormat

    choices = [UploadChoice(LADDITION_CHOICE, LADDITION_CHOICE_LABEL)]
    for fmt in TillFormat.objects.order_by("name"):
        choices.append(UploadChoice(str(fmt.pk), f"{fmt.name} ({fmt.get_kind_display()})", not fmt.day_column.strip()))
    return choices


@dataclass(frozen=True)
class Choice:
    """An upload's choice, resolved: L'Addition's export, or a format and its
    compiled layout."""

    label: str
    fmt: object = None
    layout: object = None

    @property
    def laddition(self) -> bool:
        return self.fmt is None


def resolve(value: str, file_name: str) -> Choice:
    """The choice posted with an upload, checked - TillFileError in French:
    a file no reader takes, an unknown format, a format the check refuses
    now. Called by the page before anything is kept, and by the job again
    (the format may have been edited or deleted in between)."""
    from common import is_id

    from ..models import TillFormat
    from .till_file import FormatError, check_format

    check_suffix(file_name)
    if value == LADDITION_CHOICE:
        if not str(file_name).lower().endswith(".xlsx"):
            raise TillFileError(LADDITION_XLSX)
        return Choice(LADDITION_CHOICE_LABEL)
    if not is_id(value):
        raise TillFileError(FORMAT_UNKNOWN)
    fmt = TillFormat.objects.filter(pk=value).first()
    if fmt is None:
        raise TillFileError(FORMAT_GONE)
    try:
        layout = check_format(fmt)
    except FormatError as error:
        raise TillFileError(f"Le format « {fmt.name} » est à corriger : {error.message}") from None
    return Choice(fmt.name, fmt, layout)


def read_upload(path, choice: Choice, *, file_name: str, day: date | None = None):
    """The uploaded file read with its choice: a `ParsedExport`, or
    TillFileError in French. L'Addition's export through its own parser,
    under the reader's bounds for an upload."""
    from . import till_file
    from .laddition_xlsx import LadditionExportError, parse_sales_export

    if choice.laddition:
        if day is not None:
            raise TillFileError(till_file.DAY_GIVEN_TWICE)
        try:
            return parse_sales_export(path, untrusted=True)
        except LadditionExportError as refusal:
            raise TillFileError(str(refusal)) from None
    return till_file.read(path, choice.layout, file_name=file_name, day=day).export
