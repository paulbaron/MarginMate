"""What changed in what recognises a supplier, and why (SupplierChange).

A change is recorded where it is made (receipts.set_identifiers, the
supplier pages), with the cause of the act it came from: a view or a gather
says what it is doing around the call - `with cause("validation du ticket
n° 12", invoice=ticket, by_person=True):` - and whatever changes inside is
recorded under it. With no cause set, it is "automatique". `collect()`
hands the changes of an act back, for a message or a job log to say them.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

AUTOMATIC = "automatique"


@dataclass(frozen=True)
class _Cause:
    text: str
    invoice_id: int | None
    by_person: bool


_cause: ContextVar[_Cause | None] = ContextVar("supplier_change_cause", default=None)
_collected: ContextVar[list | None] = ContextVar("supplier_change_collected", default=None)


@contextmanager
def cause(text: str, invoice=None, by_person: bool = False):
    """What the changes made inside are recorded as coming from."""
    token = _cause.set(_Cause(text, getattr(invoice, "pk", None), by_person))
    try:
        yield
    finally:
        _cause.reset(token)


@contextmanager
def collect():
    """The changes recorded inside, as a list filled as they are."""
    rows: list = []
    token = _collected.set(rows)
    try:
        yield rows
    finally:
        _collected.reset(token)


def by_a_person() -> bool:
    current = _cause.get()
    return bool(current and current.by_person)


def record(supplier, kind, summary: str, *, data=None, other_supplier=None, needs_review=False, operation=None,
           invoice=None, by_person=None):
    """One change of `supplier`, under the current cause."""
    from .models import SupplierChange

    current = _cause.get()
    change = SupplierChange.objects.create(
        supplier=supplier,
        kind=kind,
        summary=summary,
        cause=(current.text if current else AUTOMATIC)[:255],
        invoice_id=invoice.pk if invoice is not None else (current.invoice_id if current else None),
        by_person=by_person if by_person is not None else bool(current and current.by_person),
        needs_review=needs_review,
        other_supplier=other_supplier,
        operation=operation,
        data=data or {},
    )
    rows = _collected.get()
    if rows is not None:
        rows.append(change)
    return change


def why_to_see(change) -> str:
    """Why `change` asks to be seen, and how to answer it - said wherever it
    is listed (the « Enseignes et fournisseurs » tab, the supplier's page).

    The tab lit an amber number and a row said « À voir », and nothing said
    what was to be seen or why: the owner could not tell (19/09). Two kinds
    ask today - a first document (receipts._record_first_document) and a
    figure lost without anyone asking (receipts.set_identifiers)."""
    from .models import SupplierChange

    name = change.supplier.name
    data = change.data or {}
    if change.kind == SupplierChange.Kind.FIRST_DOCUMENT:
        if not data.get("learned"):
            # Recognised by its header, or printing nothing to learn: there
            # is nothing to take back, only where it is filed to confirm.
            return (
                "C'est son premier document : rien d'autre ne garantit cette lecture. Il ne lui a rien appris. "
                f"S'il est bien de {name}, cliquez « Vu » ; sinon, changez le document de fournisseur."
            )
        return (
            "C'est son premier document : rien d'autre ne garantit cette lecture. Ce qu'il lui a appris rangera "
            f"désormais chez {name} tout document qui l'imprime. S'il est bien de {name}, cliquez « Vu » ; sinon, "
            "« Retirer » l'identifiant sur sa fiche ou changez le document de fournisseur."
        )
    if change.kind == SupplierChange.Kind.IDENTIFIERS:
        return (
            "Un identifiant lui a été retiré sans que personne ne l'ait demandé. S'il le reconnaît encore, "
            "« Annuler » sur sa fiche le lui rend ; sinon, cliquez « Vu »."
        )
    return "Fait automatiquement : vérifiez-le sur sa fiche, puis cliquez « Vu »."
