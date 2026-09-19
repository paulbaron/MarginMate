"""A supplier's own page (« fiche fournisseur »), and what is done from it:
what names it kept, set aside or given back, every change seen and undone.

Every action is a POST from the page that shows what it will do, answers
with a message saying what it did, and is recorded in the supplier's
history (SupplierChange) under the person's name, with its undo. A value the
page did not offer is refused with a message, never an error page.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlencode

from django.contrib import messages
from django.db import transaction
from django.db.models import Max, Min, ProtectedError, RestrictedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from . import supplier_changes
from .models import Invoice, InvoiceType, Supplier, SupplierChange
from .parsers import LLM_PARSER_KEY, ticket_parser_for

#: Achats' « Enseignes et fournisseurs » tab, where every supplier is listed.
SUPPLIERS = "invoices:supplier_list"
HISTORY_SHOWN = 30


def fiche_url(supplier) -> str:
    return reverse("invoices:supplier_detail", args=[supplier.pk])


def _local_return(request) -> str:
    """Where `?retour=` asks to go back to - a path of this site only."""
    target = request.POST.get("retour") or request.GET.get("retour") or ""
    if target.startswith("/") and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return ""


def _supplier(request, pk):
    """The supplier, or where to go instead: the AI pseudo-supplier has no
    page - nothing names it."""
    supplier = get_object_or_404(Supplier, pk=pk)
    if supplier.parser_key == LLM_PARSER_KEY:
        messages.info(request, "Ce fournisseur n'a pas de fiche.")
        return None, redirect(SUPPLIERS)
    return supplier, None


def supplier_list(request):
    """"Achats", on the « Enseignes et fournisseurs » tab: who each document
    is filed under. It was the foot of the Sources tab, under the sources of
    invoices - two things the owner asked to tell apart (19/09)."""
    from .workspace import render_purchases

    return render_purchases(request, "fournisseurs")


def supplier_detail(request, pk):
    from .forms import CHANNELS
    from .receipts import has_own_reader, identifier_report, supplier_notices
    from .workspace import OWN_MODULE

    supplier, away = _supplier(request, pk)
    if away is not None:
        return away
    documents = Invoice.objects.filter(supplier=supplier)
    count = documents.count()
    span = documents.aggregate(first=Min("invoice_date"), last=Max("invoice_date"))
    is_till = ticket_parser_for(supplier.code) is not None
    own_reader = has_own_reader(supplier)
    changes = list(supplier.changes.select_related("supplier", "invoice", "other_supplier")[:HISTORY_SHOWN])
    for change in changes:
        change.undo_label = _undo_label(change)
        change.can_undo = change.undone_at is None and bool(change.undo_label)
        change.to_see = change.needs_review and change.reviewed_at is None and change.undone_at is None
    invoice_types = list(InvoiceType.objects.filter(supplier=supplier).order_by("name"))
    for invoice_type in invoice_types:
        invoice_type.channel = CHANNELS.get(invoice_type.source_kind, invoice_type.get_source_kind_display())
    return render(
        request,
        "invoices/supplier_detail.html",
        {
            "supplier": supplier,
            "is_till": is_till,
            "own_reader": own_reader,
            "report": identifier_report(supplier),
            "notices": supplier_notices(supplier),
            "document_count": count,
            "first_date": span["first"],
            "last_date": span["last"],
            "invoice_types": invoice_types,
            # Metro: fetched by the gather's own module, with no source.
            "own_module": Supplier.objects.filter(OWN_MODULE, pk=supplier.pk).exists(),
            "delete_refused": delete_refused(supplier),
            "changes": changes,
            "fiche": fiche_url(supplier),
        },
    )


def _undo_label(change) -> str:
    """What a change's undo button says - none for a change that has no
    undo of its own. The dots: it opens a page that says what it will do."""
    data = change.data or {}
    kind = SupplierChange.Kind
    if data.get("undoes"):
        # An undo has none of its own: that is the change done again.
        return ""
    if change.kind == kind.CREATED:
        return "Annuler la création…"
    if change.kind == kind.RENAMED:
        return f"Rétablir « {data.get('before', '')} »"
    if change.kind == kind.HEADER:
        return f"Rétablir « {data['before']} »" if data.get("before") else "Retirer cet en-tête"
    if change.kind == kind.TYPES:
        came_from = change.supplier if data.get("from") == change.supplier_id else change.other_supplier
        return f"Rendre cette source à {came_from.name}" if came_from is not None else ""
    return "Annuler" if change.kind in UNDO else ""


def record_type_moved(invoice_type, before, after, undoes=None) -> None:
    """An invoice type now fetching for `after` instead of `before`, in both
    histories as one operation - either one gives it back."""
    import uuid

    operation = uuid.uuid4()
    data = {"type": invoice_type.pk, "from": before.pk, "to": after.pk}
    if undoes is not None:
        data["undoes"] = undoes.pk
    back = "Rendu : " if undoes is not None else ""
    supplier_changes.record(
        before, SupplierChange.Kind.TYPES,
        f"{back}« {invoice_type.name} » récupère désormais pour {after.name}.",
        data=data, other_supplier=after, operation=operation,
    )
    supplier_changes.record(
        after, SupplierChange.Kind.TYPES,
        f"{back}« {invoice_type.name} » récupère désormais pour lui (avant : {before.name}).",
        data=data, other_supplier=before, operation=operation,
    )


def delete_refused(supplier) -> str:
    """Why `supplier` cannot be deleted - empty when it can: nothing filed
    under it, nothing fetching for it, no till or reader of its own."""
    from .receipts import has_own_reader

    if ticket_parser_for(supplier.code) is not None:
        return "sa caisse est réglée dans l'application"
    if has_own_reader(supplier):
        return "il a son propre lecteur"
    count = Invoice.objects.filter(supplier=supplier).count()
    if count:
        return f"{count} document{'s' if count > 1 else ''} y {'sont' if count > 1 else 'est'} rangé{'s' if count > 1 else ''}"
    types = InvoiceType.objects.filter(supplier=supplier).count()
    if types:
        return f"{types} source{'s' if types > 1 else ''} {'récupèrent' if types > 1 else 'récupère'} pour lui"
    return ""


def supplier_create(request):
    """A supplier before any document of its own - so that a type can fetch
    for it, or an import be filed under it, from the first one. Where its
    invoices come from decides the next page: the type form, filled in."""
    from .forms import SupplierCreateForm
    from .receipts import create_shop

    retour = _local_return(request)
    if request.method == "POST":
        form = SupplierCreateForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                with transaction.atomic(), supplier_changes.cause("création à la main", by_person=True):
                    supplier = create_shop(data["name"], data["header"], expenses_only=data["nature"] == "charges")
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                _say_created(request, supplier)
                fiche = fiche_url(supplier)
                if data["arrivee"] in ("EMAIL", "WEBSITE"):
                    query = urlencode({"fournisseur": supplier.pk, "source": data["arrivee"], "retour": fiche}, safe="/")
                    return redirect(reverse("invoices:invoice_type_create") + "?" + query)
                if retour:
                    return redirect(retour + ("&" if "?" in retour else "?") + f"fournisseur={supplier.pk}")
                return redirect(fiche)
    else:
        form = SupplierCreateForm()
    return render(
        request,
        "invoices/supplier_create.html",
        {
            "form": form,
            "taken": getattr(form, "taken", None),
            "retour": retour,
            "back": retour or reverse(SUPPLIERS),
        },
    )


def _say_created(request, supplier) -> None:
    from .receipts import describe_tickets, tickets_printing

    if not supplier.ticket_header:
        messages.success(
            request,
            f"{supplier.name} est créé. Rien ne le reconnaît encore : une source qui récupère pour lui, "
            "ou l'import où vous le choisissez, lui apprendra ce que ses documents impriment.",
        )
        return
    from .receipt_batches import requeue_everywhere

    requeued = requeue_everywhere()
    messages.success(
        request,
        f"{supplier.name} est créé : les documents qui portent « {supplier.ticket_header} » y seront rangés"
        + (f" ; {requeued} fichier(s) sans enseigne des derniers imports sont relus." if requeued else "."),
    )
    elsewhere = [ticket for ticket in tickets_printing(supplier.ticket_header) if ticket.supplier_id != supplier.pk]
    if elsewhere:
        messages.warning(
            request,
            f"« {supplier.ticket_header} » est aussi imprimé sur {describe_tickets(elsewhere[:5])} : s'ils sont de "
            f"{supplier.name}, rangez-les chez lui depuis leur page (« Changer de fournisseur »).",
        )


def _edit_version(supplier) -> str:
    """What the modification page was drawn from: its name, its header, the
    last change recorded - saving applies only if it is still that."""
    latest = supplier.changes.order_by("-pk").values_list("pk", flat=True).first()
    return hashlib.sha256(f"{supplier.name}\x00{supplier.ticket_header}\x00{latest}".encode("utf-8")).hexdigest()


def _edit_plan(supplier, name, header, header_editable) -> dict:
    """What saving `name` and `header` would do, as sentences, and what
    refuses it - worked out by the code that saves, nothing written."""
    from .identifiers import describe
    from .receipts import _stored_texts, check_header, prints_header, rename_supplier, tickets_printing

    said, refused = [], []
    renaming = name != supplier.name
    if renaming:
        try:
            renamed = rename_supplier(supplier, name, dry_run=True)
        except ValueError as exc:
            refused.append(str(exc))
        else:
            sentence = f"Nom : « {renamed.before} » → « {renamed.after} »."
            if renamed.lines:
                sentence += (
                    f" {renamed.lines} ligne{'s' if renamed.lines > 1 else ''} de charge « {renamed.before} »"
                    f" et son poste prennent le nouveau nom."
                )
            said.append(sentence + " Son code interne ne change pas ; le rapprochement bancaire garde les libellés déjà appris.")
    changing_header = header_editable and header != supplier.ticket_header
    if changing_header and header:
        try:
            check_header(header, shop=supplier)
        except ValueError as exc:
            refused.append(str(exc))
        else:
            own = _stored_texts(Invoice.objects.filter(supplier=supplier))
            printing = sum(1 for text in own if prints_header(text, header))
            elsewhere = [ticket for ticket in tickets_printing(header) if ticket.supplier_id != supplier.pk]
            sentence = f"En-tête « {header} »"
            if supplier.ticket_header:
                sentence += f" (au lieu de « {supplier.ticket_header} »)"
            sentence += f" : imprimé sur {printing} de ses {len(own)} documents, " if own else " : "
            sentence += (
                f"et sur {len(elsewhere)} document(s) d'autres fournisseurs, qui y restent."
                if elsewhere
                else "sur aucun document d'un autre fournisseur."
            )
            said.append(
                sentence + " Un en-tête n'ajoute que les prochains documents : aucun document rangé ne bouge. "
                "Les fichiers sans enseigne des derniers imports seront relus."
            )
    elif changing_header:
        still = [describe(identifier) for identifier in supplier.ticket_identifiers or ()]
        said.append(
            f"En-tête « {supplier.ticket_header} » retiré : ses prochains documents seront reconnus par "
            + (", ".join(still) if still else "rien : ils vous seront demandés à l'import")
            + "."
        )
    return {"said": said, "refused": refused, "changes": bool(said or refused), "ok": not refused}


def supplier_edit(request, pk):
    """Its name and header, changed after a look at what that does (« Vérifier
    les changements »): nothing is saved before, and the save applies only
    if the supplier is still as it was checked. One with no documents saves
    in one step - there is nothing for a change to move."""
    from .receipts import header_choices, offerable_headers, rename_supplier, set_shop_header

    supplier, away = _supplier(request, pk)
    if away is not None:
        return away
    fiche = fiche_url(supplier)
    header_editable = ticket_parser_for(supplier.code) is None and not _own_reader(supplier)
    documents = Invoice.objects.filter(supplier=supplier)
    count = documents.count()
    version = _edit_version(supplier)
    step, stale, plan = "modifier", False, None
    if request.method == "POST":
        name = " ".join(request.POST.get("name", "").split())
        header = " ".join(request.POST.get("header", "").split()) if header_editable else supplier.ticket_header
        action = request.POST.get("action", "")
        stale = request.POST.get("version") != version
        if action in ("verifier", "enregistrer"):
            plan = _edit_plan(supplier, name, header, header_editable)
            step = "verifier"
            if action == "enregistrer" and not stale and plan["ok"] and plan["changes"]:
                header_changed = header_editable and header != supplier.ticket_header
                try:
                    with transaction.atomic(), supplier_changes.cause(
                        f"modification de la fiche de {supplier.name}", by_person=True
                    ):
                        if name != supplier.name:
                            rename_supplier(supplier, name)
                        if header_changed:
                            set_shop_header(supplier, header)
                except ValueError as exc:
                    messages.error(request, str(exc))
                    supplier.refresh_from_db()
                else:
                    messages.success(request, "Enregistré : " + " ".join(plan["said"]))
                    if header_changed and header:
                        from .receipt_batches import requeue_everywhere

                        requeue_everywhere()
                    return redirect(fiche)
            elif action == "enregistrer" and not plan["changes"]:
                return redirect(fiche)
    else:
        name, header = supplier.name, supplier.ticket_header
    latest = documents.exclude(ocr_text="", source_text="").order_by("-invoice_date", "-pk").first()
    chips = (
        offerable_headers(header_choices(latest.document_text), shop=supplier)
        if header_editable and latest is not None and latest.document_text
        else []
    )
    posted = {"name": name, "version": version}
    if header_editable:
        posted["header"] = header
    return render(
        request,
        "invoices/supplier_edit.html",
        {
            "supplier": supplier,
            "fiche": fiche,
            "step": step,
            "stale": stale and request.method == "POST",
            "plan": plan,
            "name": name,
            "header": header,
            "header_editable": header_editable,
            "chips": chips,
            "document_count": count,
            "version": version,
            "posted": posted,
        },
    )


def _own_reader(supplier) -> bool:
    from .receipts import has_own_reader

    return has_own_reader(supplier)


def supplier_delete(request, pk):
    """A supplier nothing rests on - no document, no type fetching for it -
    deleted after saying what goes with it: its unused products, the prices
    and payee names it was known by, its history."""
    from bank.models import CounterpartyAlias
    from inventory.models import Product

    from .models import ShopItemPrice

    supplier, away = _supplier(request, pk)
    if away is not None:
        return away
    fiche = fiche_url(supplier)
    refused = delete_refused(supplier)
    if request.method == "POST" and request.POST.get("confirme") == "1":
        if refused:
            messages.error(request, f"{supplier.name} n'est pas supprimé : {refused}.")
            return redirect(fiche)
        name = supplier.name
        try:
            with transaction.atomic():
                Product.objects.filter(supplier=supplier).delete()
                supplier.delete()
        except (ProtectedError, RestrictedError):
            messages.error(request, f"{name} n'est pas supprimé : un de ses produits sert encore (inventaire, recette).")
            return redirect(fiche)
        messages.success(request, f"{name} est supprimé.")
        return redirect(SUPPLIERS)
    return render(
        request,
        "invoices/supplier_delete.html",
        {
            "supplier": supplier,
            "fiche": fiche,
            "refused": refused,
            "prices": ShopItemPrice.objects.filter(supplier=supplier).count(),
            "aliases": CounterpartyAlias.objects.filter(supplier=supplier).count(),
            "products": Product.objects.filter(supplier=supplier).count(),
            "history": supplier.changes.count(),
        },
    )


ACTIONS = {"retenir": "Retenir", "retirer": "Retirer", "ne_plus_ecarter": "Ne plus l'écarter"}


def supplier_identifiers(request, pk):
    """« Retenir » a figure its documents print, « Retirer » one it holds -
    set aside for good, never learned again - or « Ne plus l'écarter »."""
    from .identifiers import describe
    from .receipts import _stored_texts, _without_header, identifier_report, identifiers_naming, set_identifiers

    supplier, away = _supplier(request, pk)
    if away is not None:
        return away
    fiche = fiche_url(supplier)
    if request.method != "POST":
        return redirect(fiche)
    action = request.POST.get("action", "")
    identifier = request.POST.get("identifier", "")
    report = identifier_report(supplier)
    known = set(supplier.ticket_identifiers or ())
    refused = set(supplier.refused_identifiers or ())
    keepable = {row["identifier"] for row in report["printed"] if row["can_keep"]}
    label = describe(identifier) if identifier else ""
    with supplier_changes.cause(f"« {ACTIONS.get(action, action)} » sur la fiche de {supplier.name}", by_person=True):
        if action == "retenir" and identifier in keepable:
            set_identifiers(supplier, known | {identifier}, asked=True)
            messages.success(request, f"{supplier.name} sera reconnu par {label}.")
        elif action == "retirer" and identifier in known:
            change = set_identifiers(supplier, known - {identifier}, reasons={identifier: "retiré à la main"}, asked=True)
            supplier.refused_identifiers = sorted(refused | {identifier})
            supplier.save(update_fields=["refused_identifiers"])
            if change is not None:
                change.data["refused_added"] = [identifier]
                change.save(update_fields=["data"])
            messages.success(
                request, f"{label} ne reconnaît plus {supplier.name}, et ne lui sera plus appris : « Ne plus l'écarter » le rend."
            )
        elif action == "ne_plus_ecarter" and identifier in refused:
            supplier.refused_identifiers = sorted(refused - {identifier})
            supplier.save(update_fields=["refused_identifiers"])
            own = _stored_texts(Invoice.objects.filter(supplier=supplier))
            others = _stored_texts(Invoice.objects.exclude(supplier=supplier))
            back = identifiers_naming(own, others, {identifier}, headerless_texts=_without_header(supplier, own))
            change = set_identifiers(supplier, known | back, asked=True) if back else None
            if change is None:
                change = supplier_changes.record(
                    supplier, SupplierChange.Kind.IDENTIFIERS,
                    f"{label} n'est plus écarté : il pourra être appris de nouveau.",
                )
            change.data["refused_removed"] = [identifier]
            change.save(update_fields=["data"])
            messages.success(
                request,
                f"{label} n'est plus écarté"
                + (f" et reconnaît de nouveau {supplier.name}." if back else " : ses documents ne l'impriment pas assez pour le reconnaître."),
            )
        else:
            messages.error(
                request, "Cette action ne vaut plus pour cet identifiant (la fiche a changé) : voici la fiche à jour."
            )
    return redirect(fiche)


def _undo_identifiers(request, supplier, change) -> bool:
    """What a change gave is taken back; what it took is given back only if
    it still names the supplier - printed on none of another's documents,
    on one of its own at least - otherwise the reason is said."""
    from .identifiers import describe
    from .receipts import _stored_texts, _why_lost, set_identifiers, still_naming

    data = change.data or {}
    known = set(supplier.ticket_identifiers or ())
    refused = set(supplier.refused_identifiers or ())
    new_refused = (refused - set(data.get("refused_added", ()))) | set(data.get("refused_removed", ()))
    gained = set(data.get("gained", ())) & known
    lost = set(data.get("lost", ())) - known - new_refused
    own = _stored_texts(Invoice.objects.filter(supplier=supplier))
    others = _stored_texts(Invoice.objects.exclude(supplier=supplier))
    back = still_naming(lost, own, others)
    for identifier, reason in _why_lost(supplier, lost - back).items():
        messages.warning(request, f"{describe(identifier)} ne revient pas : {reason}.")
    if new_refused != refused:
        supplier.refused_identifiers = sorted(new_refused)
        supplier.save(update_fields=["refused_identifiers"])
    recorded = set_identifiers(
        supplier, (known - gained) | back, reasons={identifier: "annulé" for identifier in gained}, asked=True
    )
    if recorded is None and new_refused != refused:
        # Only what is set aside changed: said all the same, or the history
        # showed the change undone and nothing that undid it.
        again = sorted(new_refused - refused)
        freed = sorted(refused - new_refused)
        supplier_changes.record(
            supplier, SupplierChange.Kind.IDENTIFIERS,
            "Annulation : "
            + "; ".join(
                part
                for part in (
                    ", ".join(describe(identifier) for identifier in again) + " de nouveau écarté" if again else "",
                    ", ".join(describe(identifier) for identifier in freed) + " n'est plus écarté" if freed else "",
                )
                if part
            )
            + ".",
            data={"refused_added": again, "refused_removed": freed},
        )
    done = bool(gained or back or new_refused != refused)
    if done:
        messages.success(request, "Changement annulé.")
    return done


def _undo_rename(request, supplier, change) -> bool:
    from .receipts import rename_supplier

    data = change.data or {}
    if supplier.name != data.get("after"):
        messages.error(request, f"Son nom a changé depuis (« {supplier.name} ») : rien n'est rétabli.")
        return False
    try:
        rename_supplier(supplier, data.get("before", ""))
    except ValueError as exc:
        messages.error(request, f"Nom non rétabli : {exc}")
        return False
    messages.success(request, f"Nom « {supplier.name} » rétabli.")
    return True


def _undo_header(request, supplier, change) -> bool:
    """The header it had, given back through the checks any header goes
    through - documents filed since by the one undone stay where they are
    (a header only adds)."""
    from .receipts import set_shop_header

    data = change.data or {}
    if supplier.ticket_header != data.get("after", ""):
        messages.error(request, f"Son en-tête a changé depuis (« {supplier.ticket_header} ») : rien n'est rétabli.")
        return False
    before = data.get("before", "")
    try:
        set_shop_header(supplier, before)
    except ValueError as exc:
        messages.error(request, f"En-tête non rétabli : {exc}")
        return False
    if before:
        from .receipt_batches import requeue_everywhere

        requeue_everywhere()
        messages.success(request, f"En-tête « {before} » rétabli.")
    else:
        messages.success(request, "En-tête retiré.")
    return True


def _undo_types(request, supplier, change) -> bool:
    """The type given back to the supplier it fetched for - refused if it
    has moved again since, or if either supplier is gone."""
    data = change.data or {}
    invoice_type = InvoiceType.objects.filter(pk=data.get("type")).select_related("supplier").first()
    came_from = Supplier.objects.filter(pk=data.get("from")).first()
    went_to = Supplier.objects.filter(pk=data.get("to")).first()
    if invoice_type is None or came_from is None or went_to is None:
        messages.error(request, "Cette source ou l'un des deux fournisseurs n'existe plus : rien n'est rendu.")
        return False
    if invoice_type.supplier_id != went_to.pk:
        messages.error(
            request,
            f"« {invoice_type.name} » n'est plus chez {went_to.name} "
            f"(elle récupère pour {invoice_type.supplier.name}) : rien n'est rendu.",
        )
        return False
    invoice_type.supplier = came_from
    invoice_type.save(update_fields=["supplier"])
    record_type_moved(invoice_type, went_to, came_from, undoes=change)
    messages.success(request, f"« {invoice_type.name} » récupère de nouveau pour {came_from.name}.")
    return True


UNDO = {
    SupplierChange.Kind.TYPES: _undo_types,
    SupplierChange.Kind.IDENTIFIERS: _undo_identifiers,
    SupplierChange.Kind.RENAMED: _undo_rename,
    SupplierChange.Kind.HEADER: _undo_header,
}


def supplier_change_undo(request, pk, change_pk):
    supplier = get_object_or_404(Supplier, pk=pk)
    change = get_object_or_404(SupplierChange, pk=change_pk, supplier=supplier)
    back = _local_return(request) or fiche_url(supplier) + "#historique"
    if request.method != "POST":
        return redirect(back)
    if change.undone_at is not None:
        messages.info(request, "Ce changement est déjà annulé.")
        return redirect(back)
    if (change.data or {}).get("undoes"):
        messages.error(
            request, "Ce changement en annulait un autre : il ne s'annule pas lui-même. Refaites le changement voulu."
        )
        return redirect(back)
    if change.kind == SupplierChange.Kind.CREATED:
        # A creation is undone by deleting it, from the page that says
        # whether it can be and what goes with it.
        return redirect("invoices:supplier_delete", pk=supplier.pk)
    undo = UNDO.get(change.kind)
    if undo is None:
        messages.error(request, f"« {change.get_kind_display()} » ne s'annule pas depuis l'historique.")
        return redirect(back)
    with supplier_changes.cause(f"annulation : {change.summary[:120]}", by_person=True):
        with supplier_changes.collect() as recorded:
            undone = undo(request, supplier, change)
        # What the undo recorded says what it undoes: offered an undo of its
        # own, it redid the change in one click, with nothing shown first.
        for made in recorded:
            made.data = {**(made.data or {}), "undoes": change.pk}
            made.save(update_fields=["data"])
        if undone:
            # Both sides of one operation (a type moved) are undone together.
            if change.operation:
                SupplierChange.objects.filter(operation=change.operation).update(undone_at=timezone.now())
            else:
                change.undone_at = timezone.now()
                change.save(update_fields=["undone_at"])
    return redirect(back)


def supplier_change_seen(request, pk, change_pk):
    """« Vu », from the history, the supplier's page or the « Enseignes et
    fournisseurs » tab (`retour`): it answers with what it marked, like every
    action here - the tab's « à voir » going away is not a word."""
    supplier = get_object_or_404(Supplier, pk=pk)
    change = get_object_or_404(SupplierChange, pk=change_pk, supplier=supplier)
    if request.method == "POST" and change.reviewed_at is None:
        change.reviewed_at = timezone.now()
        change.save(update_fields=["reviewed_at"])
        messages.success(request, f"Vu : {change.get_kind_display().lower()} de {supplier.name}.")
    return redirect(_local_return(request) or fiche_url(supplier) + "#historique")
