"""The « Données » page (§3, §5.7): three tabs - Exporter, Importer,
Effacer - each with the same picker.

The server is authoritative about the selection: the page's script ticks
what a section requires (or, to clear, what requires it), but a selection
posted without it - no JavaScript, a hand-made request - is refused with
the closure ticked, never completed in silence. Every write is a POST; an
import and a clear are two steps, the preview first, and the confirm
refuses a selection other than the one previewed - and a preview other than
the one on the page it was clicked from (SHOWN_PREVIEW).

One tenant per bar: every folder is the bound tenant's own (safety,
staging), the session's entries are keyed by the tenant (`session_key`), and
the page names no folder of the server - a hosted bar can neither read a
server path nor stop the server to put a database copy back, which the page
asks the administrator to do instead.

**Importing and clearing are the owner's, his password confirmed**
(security review of 01/10/2026; `_refused`): an import replaces the
espace's invoices, bank, prices and portals - a portal decides where a
stored password is typed - and a clear deletes them. Every POST that
stages an archive (sent, or a backup), previews, confirms or cancels a
staged one, or previews or confirms a clear, requires
`accounts.tenancy.is_owner` (a member: 403, `OWNER_ONLY`) and
`accounts.sudo.confirmed` (else the confirmation page, back to the tab it
was posted from - what was posted is not replayed), and keeps the
confirmation alive. The export and every tab stay open - to the owner:
since 02/10/2026 an employee opens no page of « Données » at all
(accounts/access.py names no area for it), so `_refused` is the owner's
password check, and its member branch the second lock behind the gate.
"""

from __future__ import annotations

import importlib
import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from django.contrib import messages
from django.http import FileResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode

from accounts import sudo
from accounts.tenancy import is_owner, tenant_key
from transfer import registry, safety, staging
from transfer.archive import ArchiveError, DeleteOnClose, manifest_counts, shown_moment
from transfer.registry import GROUP_LABELS, INFO
from transfer.report import RunReport
from transfer.runner import (
    Busy,
    NotAsPreviewed,
    busy_reason,
    run_clear,
    run_export,
    run_import,
)
from transfer.safety import SafetyError
from transfer.sections.base import Group, Strategy

logger = logging.getLogger(__name__)

PREVIEW_MAX_AGE = timedelta(minutes=30)
#: The session's entries - read and written through `session_key`: the
#: session is the login's, and one login may work for two tenants (and a
#: membership may move): unkeyed, A's pending « Effacer » and
#: A's report were drawn on B's page.
SESSION_REPORT = "transfer.report"
SESSION_CLEAR = "transfer.clear"
#: Where the backups are, as the page says it - never a path.
BACKUPS_PLACE = "les sauvegardes de votre espace"
#: The field each confirm form posts: the fingerprint of the preview it shows
#: (RunReport.fingerprint). The preview stored is the last one made, perhaps
#: in another tab: « Importer » clicked under « 0 à supprimer » ran a newer
#: preview that deleted a ticket and its photo (review, 19/09).
SHOWN_PREVIEW = "apercu"

EMPTY = "Cochez au moins une partie."
CHANGED = "La sélection a changé depuis l'aperçu : voici le nouvel aperçu."
#: Not CHANGED: a page left open past the 30 minutes, confirmed with the same
#: boxes, was told its selection had changed (review, 19/09).
EXPIRED = "L'aperçu avait plus de 30 minutes : voici le nouvel aperçu, confirmez de nouveau."
#: The confirm was not the previewed run (runner.NotAsPreviewed): undone.
MOVED_IMPORT = "La base a changé depuis l'aperçu : rien n'a été importé. Voici le nouvel aperçu."
MOVED_CLEAR = "La base a changé depuis l'aperçu : rien n'a été effacé. Voici le nouvel aperçu."
#: The confirm came from a page whose form names another preview than the one
#: stored - another tab has previewed since.
OTHER_TAB_IMPORT = (
    "Un autre onglet a prévisualisé depuis : rien n'a été importé. Voici l'aperçu à jour, à confirmer de nouveau."
)
OTHER_TAB_CLEAR = (
    "Un autre onglet a prévisualisé depuis : rien n'a été effacé. Voici l'aperçu à jour, à confirmer de nouveau."
)
#: The form names no preview at all: it was drawn before this version - the
#: owner's « Effacer » did nothing on 20/09 and was told « la base a changé »,
#: so they went looking for a change nobody had made. The page a redirect
#: draws carries the field, so confirming again from it works.
OLD_PAGE_IMPORT = (
    "Rien n'a été importé : la page datait d'avant une mise à jour de l'application. "
    "Vérifiez l'aperçu à jour et confirmez de nouveau."
)
OLD_PAGE_CLEAR = (
    "Rien n'a été effacé : la page datait d'avant une mise à jour de l'application. "
    "Vérifiez l'aperçu à jour et confirmez de nouveau."
)
GONE = "Cette archive n'est plus en attente : envoyez-la de nouveau."
#: A member's import or clear (`_refused`).
OWNER_ONLY = "Seul le propriétaire de l'espace peut importer ou effacer des données."
TYPE_TO_CONFIRM = "Tapez EFFACER pour confirmer."
#: Installed by the migrations into every database: a database holding only
#: these (and no invoice) is new, and « Remplacer » is what gives them the
#: archive's settings. A new hosted espace holds only some of them - not the
#: original bar's UBA, SABBH and WINGSENG (invoices.seeds) - and is new too.
#: Not OTHER, the AI reading's « Autre (analyse IA) »: invoices/0038 removed
#: it where nothing named it, and kept it as a supplier of the bar's own
#: where something did.
SEEDED_SUPPLIERS = {"METRO", "UBA", "FRANPRIX", "MONOPRIX", "SABBH", "WINGSENG"}
#: The sections whose rows the migrations install into every database too
#: (the suppliers, the UBA mailbox search, the bank's format and recognition
#: rules, the returnable types and the UBA slip format): merged into a new
#: database, an archive's edited copy of one of them is a conflict and the
#: installed one stays - only « Remplacer » gives them the archive's. A part
#: holding no row is not named (`_holds_rows`): a new hosted espace holds no
#: source.
SEEDED_SECTIONS = ("fournisseurs", "sources", "regles_banque", "types_consignes")

TABS = (
    ("export", "Exporter", "transfer:data_home"),
    ("import", "Importer", "transfer:data_import"),
    ("clear", "Effacer", "transfer:data_clear"),
)


# -- the selection -------------------------------------------------------------------


def usable_keys(mode: str) -> set[str]:
    """Registered sections whose whole closure is registered too: while a
    lane has not landed, a section requiring it cannot be exported, imported
    or cleared, and says so rather than failing half way."""
    present = set(registry.registered())
    reach = registry.dependents if mode == "clear" else registry.needs
    return {key for key in present if reach({key}) <= present}


def _posted_selection(request, allowed: set[str]) -> set[str]:
    return set(request.POST.getlist("sections")) & set(INFO) & allowed


def _posted_strategies(request, keys) -> dict[str, Strategy]:
    strategies = {}
    for key in keys:
        value = request.POST.get(f"strategie-{key}", Strategy.MERGE.value)
        strategies[key] = Strategy(value) if value in (Strategy.MERGE.value, Strategy.REPLACE.value) else Strategy.MERGE
    return strategies


def _quoted(keys) -> str:
    """« Recettes », « Ventes » et « Banque »."""
    names = [f"« {label} »" for label in registry.labels(keys)]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " et " + names[-1]


def _missing_message(selected: set[str], missing: set[str], mode: str, available=None) -> str:
    """Labels are quoted as parts: « Recettes a besoin de » put a plural
    label with a singular verb (review, 19/09)."""
    if mode == "clear":
        subject = (selected & registry.closure(missing, "export")) or selected
        head = f"Effacer {_quoted(subject)} efface aussi : {', '.join(registry.labels(missing))}."
    else:
        needing = [key for key in registry.ordered(selected) if registry.closure({key}, mode, available) - selected]
        part = "La partie" if len(needing) == 1 else "Les parties"
        verb = "a besoin de" if len(needing) == 1 else "ont besoin de"
        head = f"{part} {_quoted(needing)} {verb} : {', '.join(registry.labels(missing))}."
    ticked = "Elle est maintenant cochée" if len(missing) == 1 else "Elles sont maintenant cochées"
    # The Effacer tab has no « Prévisualiser »: its button says « Voir ce qui
    # sera effacé ».
    again = {
        "export": "exportez de nouveau",
        "import": "prévisualisez de nouveau",
        "clear": "voyez de nouveau ce qui sera effacé",
    }[mode]
    return f"{head} {ticked} : {again}."


def _number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


#: Words that end in s in the singular too.
_INVARIABLE = {
    "alias",
    "appris",
    "avis",
    "colis",
    "compris",
    "fils",
    "fois",
    "mois",
    "pays",
    "poids",
    "pris",
    "prix",
    "repas",
}
#: Where the head of a count's label ends: what follows is its complement,
#: which keeps its own number - « 1 jour de vente », « 1 Mo de fichiers ».
_COMPLEMENT = {"à", "au", "aux", "avec", "chez", "de", "des", "du", "en", "par", "pour", "sans", "sur"}


def _singular(word: str) -> str:
    lower = word.lower()
    if len(word) > 2 and lower.endswith("s") and not lower.endswith("ss") and lower not in _INVARIABLE:
        return word[:-1]
    return word


def _one(label: str) -> str:
    """A count's label - always plural - for a count of 1: « 1 source », not
    « 1 sources » (review, 19/09). Only the head is put in the singular, and
    a label naming two things (« pertes et corrections ») stays as it is:
    guessing wrong reads worse than the plural."""
    words = label.split(" ")
    head: list[str] = []
    for index, word in enumerate(words):
        if word == "et":
            return label
        if word in _COMPLEMENT or word.startswith(("d'", "d’", "(", "«")):
            return " ".join(head + words[index:])
        head.append(_singular(word))
    return " ".join(head)


def _counts_text(counts) -> str:
    """« 30 fournisseurs · 1 prix connu ». The manifest's counts come from
    outside: what is not a number is left out - a string made every GET of
    its stage a 500, its « Annuler » with it (review, 19/09)."""
    if not isinstance(counts, dict):
        return ""
    parts = []
    for label, number in counts.items():
        if isinstance(number, bool) or not isinstance(number, int):
            continue
        parts.append(f"{_number(number)} {_one(str(label)) if number == 1 else label}")
    return " · ".join(parts)


def _counts(keys) -> dict[str, dict[str, int] | None]:
    """count() of each section, drawn on every visit; one failing is logged
    and shown as unknown rather than taking the page down."""
    result = {}
    for key in keys:
        try:
            result[key] = registry.get(key).count()
        except Exception:
            logger.exception("count() failed for %s", key)
            result[key] = None
    return result


def _picker(
    mode: str,
    user: set[str],
    *,
    available: frozenset[str] | None = None,
    strategies: dict[str, Strategy] | None = None,
    archive_counts: dict[str, dict] | None = None,
) -> dict:
    """The rows of the two fieldsets. `user` is what the person ticked;
    the closure is ticked with it and marked as forced, so the page reads
    the same with or without its script."""
    present = registry.registered()
    usable = usable_keys(mode)
    if available is not None:
        usable &= set(available)
    user = user & usable
    ticked = registry.closure(user, mode, available) & usable if user else set()
    forcing = registry.forcing(mode)
    counts = _counts(registry.ordered(present))
    strategies = strategies or {}
    groups = []
    for group in Group:
        rows = []
        for key in registry.ordered(INFO):
            info = INFO[key]
            if info.group != group or key not in present:
                continue
            forcers = [other for other in forcing[key] if other in present]
            forced_by = [other for other in forcers if other in user]
            forced = key in ticked and key not in user
            if available is not None and key not in available:
                disabled_reason = "absent de l'archive"
            elif key not in usable:
                disabled_reason = "indisponible : une partie dont elle dépend n'est pas encore installée"
            else:
                disabled_reason = ""
            rows.append(
                {
                    "key": key,
                    "label": info.label,
                    "description": registry.description(key),
                    "counts": _counts_text(counts.get(key)),
                    "archive_counts": _counts_text((archive_counts or {}).get(key)),
                    "checked": key in ticked,
                    "forced": forced,
                    "forced_note": (
                        ("effacé avec " if mode == "clear" else "nécessaire pour ")
                        + ", ".join(INFO[other].label for other in forced_by)
                    )
                    if forced and forced_by
                    else "",
                    "forced_by": json.dumps({other: INFO[other].label for other in forcers}, ensure_ascii=False),
                    # Where it applies: never on the Effacer tab, where « conseillé :
                    # Factures » read as advice to clear the invoices too - it says
                    # what a clear costs instead - and only for what can be ticked
                    # (on a stage, what the archive holds).
                    "hints": [
                        info.recommend_reason[other]
                        for other in info.recommends
                        if mode != "clear" and other in usable and other in info.recommend_reason
                    ],
                    "clear_note": info.clear_note if mode == "clear" else "",
                    "disabled": bool(disabled_reason),
                    "disabled_reason": disabled_reason,
                    "strategy": strategies.get(key, Strategy.MERGE).value,
                }
            )
        if rows:
            groups.append({"key": group.value, "label": GROUP_LABELS[group], "rows": rows})
    invoice_counts = counts.get("factures") or {}
    return {
        "mode": mode,
        "groups": groups,
        "checked": ticked,
        "invoices_mb": invoice_counts.get("Mo de fichiers"),
    }


# -- the tenant ----------------------------------------------------------------------


def session_key(name: str) -> str:
    """The session entry `name` of the bound tenant: `name:<tenant id>`."""
    return f"{name}:{tenant_key()}"


def _shown_backups(backups: dict[str, str]) -> dict[str, str]:
    """The backups as the report keeps and shows them: their names alone,
    never a server path."""
    return {kind: Path(path).name if path else "" for kind, path in backups.items()}


# -- rendering -----------------------------------------------------------------------


def _render(request, tab: str, *, status: int = 200, **context):
    context.setdefault("busy", busy_reason())
    context.update(
        tab=tab,
        tabs=[{"key": key, "label": label, "url": reverse(name), "active": key == tab} for key, label, name in TABS],
        # No path on the page: the templates say BACKUPS_PLACE, and send a
        # database copy's restore to the administrator.
        backups_place=BACKUPS_PLACE,
    )
    return render(request, "transfer/page.html", context, status=status)


def _refused(request, page_url: str, refused_page):
    """None when this POST may import or clear: the espace's owner, his
    MarginMate password confirmed - the confirmation is then kept alive.
    Otherwise the answer: the confirmation page coming back to `page_url`
    (the tab it was posted from) for an owner not confirmed; and for a
    member `refused_page()` (the tab, drawn again with `OWNER_ONLY`, 403) -
    the second lock: the gate (accounts/access.py) refuses him every page
    of « Données » before this runs."""
    if not is_owner(request):
        messages.error(request, OWNER_ONLY)
        return refused_page()
    if not sudo.confirmed(request):
        return sudo.ask(request, next_path=page_url)
    sudo.refresh(request)
    return None


def _pop_report(request, mode: str) -> RunReport | None:
    """The final report, shown once after the redirect."""
    if not request.GET.get("rapport"):
        return None
    key = session_key(SESSION_REPORT)
    stored = request.session.get(key)
    if not stored or stored.get("mode") != mode:
        return None
    del request.session[key]
    return RunReport.from_json(stored["report"])


def _fresh(moment: datetime | None) -> bool:
    return moment is not None and timezone.now() - moment < PREVIEW_MAX_AGE


def _shows(request, stored: dict) -> bool:
    """Whether the confirm was clicked on the page showing the preview
    stored. A form that names none was not drawn from it either."""
    return request.POST.get(SHOWN_PREVIEW, "") == RunReport.from_json(stored).fingerprint


def _still_shown(request, fresh: RunReport) -> bool:
    """Whether a confirm naming another preview than the one stored still
    names what would happen now: the page announced this very outcome, so
    running it keeps the promise, whichever preview was stored since.

    Without this, a page whose preview was replaced by any other - the tab
    left open on the second tick, an earlier confirm that previewed again -
    was refused for ever: the owner ticked, typed EFFACER and clicked, saw
    an orange warning and nothing deleted, again and again (20/09)."""
    posted = request.POST.get(SHOWN_PREVIEW, "")
    return bool(posted) and posted == fresh.fingerprint


def _why_not_shown(request, old_page: str, other_tab: str) -> str:
    """Why a confirm is not the one its page showed: a form drawn before
    this version names no preview at all, and telling the two apart is what
    the message has to say - « la base a changé » sent the owner looking for
    a change nobody had made (20/09)."""
    return old_page if not request.POST.get(SHOWN_PREVIEW) else other_tab


def _backup_message(done: str, backups: dict[str, str]) -> str:
    names = [Path(path).name for path in (backups.get("database"), backups.get("archive")) if path]
    return f"{done}. Sauvegarde{'s' if len(names) > 1 else ''} faite{'s' if len(names) > 1 else ''} avant : " + (
        " et ".join(names) + f" (dans {BACKUPS_PLACE})."
    )


# -- Exporter ------------------------------------------------------------------------


def configuration_keys() -> list[str]:
    """The « Configuration » group's sections that can be exported, in
    their order: what « Configuration seule » ticks."""
    usable = usable_keys("export")
    return [key for key in registry.ordered(INFO) if INFO[key].group == Group.CONFIG and key in usable]


def _export_page(request, user: set[str], *, status=200):
    # A link, not a script: the closure of the « Configuration » group is
    # the group itself, and the page drawn with it ticked reads the same
    # with or without JavaScript.
    configuration_only = (
        reverse("transfer:data_home") + "?" + urlencode([("cocher", key) for key in configuration_keys()])
    )
    return _render(
        request, "export", status=status, picker=_picker("export", user), configuration_only=configuration_only
    )


def data_home(request):
    """Exporter tab. `?cocher=` pre-ticks (§3.1)."""
    pre_ticked = set(request.GET.getlist("cocher")) & set(INFO)
    return _export_page(request, pre_ticked)


def data_export(request):
    if request.method != "POST":
        return redirect("transfer:data_home")
    selected = _posted_selection(request, usable_keys("export"))
    if not selected:
        messages.error(request, EMPTY)
        return _export_page(request, set())
    missing = registry.missing(selected, "export")
    if missing:
        messages.error(request, _missing_message(selected, missing, "export"))
        return _export_page(request, selected)
    busy = busy_reason()
    if busy:
        messages.error(request, busy)
        return redirect("transfer:data_home")
    path = staging.exports_dir() / f"{secrets.token_urlsafe(12)}.zip"
    try:
        manifest = run_export(selected, path)
    except Exception as exc:  # said on the page, the temp file removed
        logger.exception("export failed")
        path.unlink(missing_ok=True)
        messages.error(request, f"L'export a échoué : {safety.error_text(exc, logged=True)}")
        return redirect("transfer:data_home")
    left_out = manifest.get("notes") or []
    if left_out:
        messages.warning(
            request,
            f"{len(left_out)} fichier(s) laissé(s) hors de l'archive (absents du disque) : "
            + ", ".join(left_out[:5])
            + ("…" if len(left_out) > 5 else ""),
        )
    # An archive of configuration alone - what another bar takes - says so in
    # its name, beside the full ones it would otherwise look like.
    kind = "configuration-" if all(INFO[key].group == Group.CONFIG for key in selected) else ""
    filename = f"marginmate-{kind}{timezone.localtime(timezone.now()):%Y-%m-%d-%H%M}.zip"
    return FileResponse(DeleteOnClose(path), as_attachment=True, filename=filename)


# -- Importer ------------------------------------------------------------------------


def data_import(request):
    if request.method == "POST":
        refused = _refused(request, reverse("transfer:data_import"), lambda: _import_page(request, status=403))
        if refused:
            return refused
        busy = busy_reason()
        if busy:
            messages.error(request, busy)
            return redirect("transfer:data_import")
        upload = request.FILES.get("archive")
        if upload is None:
            messages.error(request, "Choisissez un fichier à importer.")
            return redirect("transfer:data_import")
        try:
            stage = staging.stage_upload(upload)
        except ArchiveError as exc:
            messages.error(request, str(exc))
            return redirect("transfer:data_import")
        except OSError as exc:
            logger.exception("staging failed")
            messages.error(request, f"L'archive n'a pas pu être préparée : {safety.error_text(exc, logged=True)}")
            return redirect("transfer:data_import")
        return redirect("transfer:data_import_stage", token=stage.token)
    return _import_page(request)


def _import_page(request, *, status=200):
    """The Importer tab: the report of the last import, the archives
    waiting and the backups."""
    staging.sweep()
    backups = safety.list_backups()
    return _render(
        request,
        "import",
        status=status,
        report=_pop_report(request, "import"),
        pending=_pending_stages(),
        zip_backups=[backup for backup in backups if backup.kind == "zip"],
        sqlite_backups=[backup for backup in backups if backup.kind == "sqlite"],
    )


def _manifest_moment(manifest: dict) -> datetime | None:
    """None when the page cannot show it (archive.shown_moment): the
    template says « date illisible » rather than fail."""
    return shown_moment(manifest.get("created_at"))


def _pending_stages() -> list[dict]:
    """« Archives en attente »: the tab never showed an archive already sent,
    reachable then only through the browser's history until the sweep
    (review, 19/09)."""
    return [
        {
            "url": reverse("transfer:data_import_stage", args=[stage.token]),
            "made": _manifest_moment(stage.manifest),
            "staged": stage.created_at,
            "parts": ", ".join(registry.labels(stage.sections)),
            "backup": stage.backup,
            "legacy": stage.legacy,
        }
        for stage in staging.pending()
    ]


def data_import_backup(request):
    if request.method != "POST":
        return redirect("transfer:data_import")
    refused = _refused(request, reverse("transfer:data_import"), lambda: _import_page(request, status=403))
    if refused:
        return refused
    busy = busy_reason()
    if busy:
        messages.error(request, busy)
        return redirect("transfer:data_import")
    try:
        stage = staging.stage_backup(request.POST.get("nom", ""))
    except ArchiveError as exc:
        messages.error(request, str(exc))
        return redirect("transfer:data_import")
    return redirect("transfer:data_import_stage", token=stage.token)


def _fresh_database() -> bool:
    from invoices.models import Invoice, Supplier

    return not Invoice.objects.exists() and not Supplier.objects.exclude(code__in=SEEDED_SUPPLIERS).exists()


#: The mailbox search invoices/0007 installs: (its supplier's code, its name).
SEEDED_SOURCE = ("UBA", "UBA - Factures")


def _migration(app: str, name: str):
    """A migration's module, for the literals it seeded with (as the tests
    read them): a migration replays the same whatever the code becomes."""
    return importlib.import_module(f"{app}.migrations.{name}")


def _holds_only_bank_seeds() -> bool:
    """« Règles de la banque » holds nothing but what a new espace starts
    with: no ignore rule (none is installed), no format and no recognition
    rule but those of bank/0007 and bank/0006 (their literals) and those a
    new espace that is not the owner's is given (`bank.presets.
    NEW_ESPACE_FORMAT_NAMES`, `NEW_ESPACE_RULE_NAMES`, frozen)."""
    from bank import presets
    from bank.models import IgnoreRule, OperationRule, StatementFormat

    rules = [rule[1] for rule in _migration("bank", "0006_operation_rules").RULES]
    formats = [_migration("bank", "0007_statement_formats").NAME]
    return (
        not IgnoreRule.objects.exists()
        and not OperationRule.objects.exclude(name__in=[*rules, *presets.NEW_ESPACE_RULE_NAMES]).exists()
        and not StatementFormat.objects.exclude(name__in=[*formats, *presets.NEW_ESPACE_FORMAT_NAMES]).exists()
    )


def holds_only_seeds(key: str) -> bool:
    """Whether this database holds nothing of section `key` but the rows
    the migrations installed - by the names they gave them, edited or not:
    « Remplacer » then deletes nothing a person made, and gives the
    installed rows the archive's settings. A row of a person's own - an
    ignore rule (none is installed), a format, a recognition rule, a type, a
    slip format, a source - and the note would ask for its deletion: an
    espace without its first invoice may well have imported statements and
    typed rules already (review, 02/10/2026). The suppliers are
    `_fresh_database`'s."""
    if key == "fournisseurs":
        return _fresh_database()
    if key == "sources":
        from invoices.models import InvoiceType

        code, name = SEEDED_SOURCE
        return not InvoiceType.objects.exclude(supplier__code=code, name=name).exists()
    if key == "regles_banque":
        return _holds_only_bank_seeds()
    if key == "types_consignes":
        from returnables.models import ReturnableType, SlipFormat

        seeds = _migration("returnables", "0002_seed_defaults")
        return (
            not ReturnableType.objects.exclude(name__in=[seed[0] for seed in seeds.TYPES]).exists()
            and not SlipFormat.objects.exclude(name=seeds.FORMAT_NAME).exists()
        )
    return False


def _seeded_parts(stage) -> list[str]:
    """The labels of the archive's sections whose rows a new database
    already holds (`SEEDED_SECTIONS`), when this database is a new one and
    still holds nothing else of them (`holds_only_seeds`): the « Base
    neuve » note asks for « Remplacer » on those. Empty otherwise. Only
    those whose box can be ticked: a section not installed is not drawn, one
    whose closure is not is drawn disabled - and the note named them all the
    same."""
    usable = usable_keys("import")
    present = [key for key in SEEDED_SECTIONS if key in stage.sections and key in usable]
    if not present or not _fresh_database():
        return []
    return registry.labels(key for key in present if _holds_rows(key) and holds_only_seeds(key))


#: The tables each of `SEEDED_SECTIONS` installs rows into.
SEEDED_MODELS = {
    "fournisseurs": ("invoices.Supplier",),
    "sources": ("invoices.InvoiceType",),
    "regles_banque": ("bank.StatementFormat", "bank.OperationRule", "bank.IgnoreRule"),
    "types_consignes": ("returnables.ReturnableType", "returnables.SlipFormat"),
}


def _holds_rows(key: str) -> bool:
    """Whether this database holds a row of seeded part `key`: one holding
    none has nothing installed for « Remplacer » to replace - a new espace
    that is not the owner's starts without the original bar's mailbox
    source (invoices.seeds). Read from the tables, as `holds_only_seeds`."""
    from django.apps import apps

    return any(apps.get_model(label).objects.exists() for label in SEEDED_MODELS.get(key, ()))


def _stored_strategies(stage) -> dict[str, Strategy]:
    stored = stage.state.get("sections") or {}
    return {
        key: Strategy.REPLACE if value == Strategy.REPLACE.value else Strategy.MERGE for key, value in stored.items()
    }


def _stage_page(request, stage, *, user=None, strategies=None, status=200):
    stored = _stored_strategies(stage)
    # A re-render after a refused selection shows no preview: it would read
    # as the preview of what is ticked, and it is of something else.
    preview = RunReport.from_json(stage.state["preview"]) if stage.state.get("preview") and user is None else None
    if user is None:
        if stored:
            user = set(stored)
            strategies = dict(stored)
        else:
            user = set(stage.sections)
    strategies = strategies or {}
    # Filtered by _counts_text: the manifest comes from outside. A section an
    # older archive carried inside another's file has its counts taken out of
    # that one's (archive.manifest_counts).
    archive_counts = {key: manifest_counts(stage.manifest, key) for key in stage.sections}
    at_risk = safety.sections_at_risk(preview, strategies=stored) if preview else set()
    revision = stage.manifest.get("app_revision")
    return _render(
        request,
        "import",
        status=status,
        stage=stage,
        stage_created_at=_manifest_moment(stage.manifest),
        stage_revision=revision if isinstance(revision, str) else "",
        picker=_picker("import", user, available=stage.sections, strategies=strategies, archive_counts=archive_counts),
        preview=preview,
        preview_fresh=_fresh(stage.preview_at),
        safety_mb=safety.estimated_megabytes(at_risk) if at_risk else 0,
        # Only where « Remplacer » can be chosen for them: an old associations
        # file has no suppliers, and the note asked for a disabled box.
        fresh_database=_seeded_parts(stage),
    )


def _keep_preview(stage, strategies: dict[str, Strategy], report: RunReport) -> None:
    stage.state = {
        "sections": {key: value.value for key, value in strategies.items()},
        "preview": report.to_json(),
        "preview_at": timezone.now().isoformat(),
    }
    staging.save(stage)


def _preview_import(stage, strategies: dict[str, Strategy]) -> RunReport:
    with stage.open() as reader:
        report = run_import(reader, strategies, preview=True)
    _keep_preview(stage, strategies, report)
    return report


def data_import_stage(request, token):
    stage = staging.get(token)
    if request.method == "POST":
        # Before anything posted is read, an archive gone included: nothing
        # a member posts, nor an owner before his password, is acted on.
        refused = _refused(
            request,
            reverse("transfer:data_import_stage", args=[token]),
            lambda: _stage_page(request, stage, status=403) if stage else _import_page(request, status=403),
        )
        if refused:
            return refused
    if stage is None:
        messages.error(request, GONE)
        return redirect("transfer:data_import")
    if request.method != "POST":
        return _stage_page(request, stage)

    action = request.POST.get("action", "")
    if action == "annuler":
        staging.discard(stage)
        if stage.backup:
            messages.info(request, "Import annulé. La sauvegarde reste dans le dossier des sauvegardes.")
        else:
            messages.info(request, "Import annulé : l'archive envoyée a été retirée.")
        return redirect("transfer:data_import")

    available = set(stage.sections) & usable_keys("import")
    selected = _posted_selection(request, available)
    strategies = _posted_strategies(request, selected)
    if not selected:
        messages.error(request, EMPTY)
        return _stage_page(request, stage, user=set(), strategies={})
    missing = registry.missing(selected, "import", available=stage.sections)
    if missing:
        messages.error(request, _missing_message(selected, missing, "import", stage.sections))
        return _stage_page(request, stage, user=selected, strategies=strategies)
    busy = busy_reason()
    if busy:
        messages.error(request, busy)
        return redirect("transfer:data_import_stage", token=token)

    try:
        if action == "importer":
            same = stage.state.get("sections") == {key: value.value for key, value in strategies.items()}
            previewed = bool(stage.state.get("preview"))
            if not (same and previewed and _fresh(stage.preview_at)):
                _preview_import(stage, strategies)
                messages.warning(request, EXPIRED if same and previewed else CHANGED)
                return redirect("transfer:data_import_stage", token=token)
            if not _shows(request, stage.state["preview"]):
                # The preview stored is another page's (a second tab, an
                # earlier confirm). What this page announced still decides:
                # worked out again, it either says the same - and runs, held
                # to it - or something changed since and nothing is done.
                fresh = _preview_import(stage, strategies)
                if _still_shown(request, fresh):
                    return _confirm_import(request, stage, strategies)
                messages.warning(request, _why_not_shown(request, OLD_PAGE_IMPORT, OTHER_TAB_IMPORT))
                return redirect("transfer:data_import_stage", token=token)
            return _confirm_import(request, stage, strategies)
        _preview_import(stage, strategies)
    except (ArchiveError, Busy) as exc:
        messages.error(request, str(exc))
    return redirect("transfer:data_import_stage", token=token)


def _confirm_import(request, stage, strategies: dict[str, Strategy]):
    previewed = RunReport.from_json(stage.state["preview"])
    try:
        # Chosen from the preview: the run is held to it (expected=), so it
        # touches exactly these sections or it is undone.
        backups = safety.before("import", safety.sections_at_risk(previewed, strategies=strategies))
    except SafetyError as exc:
        messages.error(request, str(exc))
        return redirect("transfer:data_import_stage", token=stage.token)
    try:
        with stage.open() as reader:
            report = run_import(reader, strategies, preview=False, expected=previewed)
    except NotAsPreviewed as moved:
        _keep_preview(stage, strategies, moved.report)
        messages.warning(request, MOVED_IMPORT)
        return redirect("transfer:data_import_stage", token=stage.token)
    except (ArchiveError, Busy) as exc:
        messages.error(request, f"{exc} Rien n'a été importé.")
        return redirect("transfer:data_import_stage", token=stage.token)
    except Exception as exc:  # rolled back; the stage is kept to try again
        logger.exception("import failed")
        messages.error(request, f"L'import a échoué, rien n'a été changé : {safety.error_text(exc, logged=True)}")
        return redirect("transfer:data_import_stage", token=stage.token)
    report.safety = _shown_backups(backups)
    request.session[session_key(SESSION_REPORT)] = {"mode": "import", "report": report.to_json()}
    staging.discard(stage)
    messages.success(request, _backup_message("Import terminé", backups))
    return redirect(reverse("transfer:data_import") + "?rapport=1")


# -- Effacer -------------------------------------------------------------------------


def _pending_clear(request) -> dict | None:
    pending = request.session.get(session_key(SESSION_CLEAR))
    if not pending:
        return None
    try:
        at = datetime.fromisoformat(pending["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return pending if _fresh(at) else None


def _clear_page(request, user: set[str], *, preview: RunReport | None = None, status=200, report=None):
    affected = safety.sections_at_risk(preview, cleared=user) if preview else set()
    return _render(
        request,
        "clear",
        status=status,
        picker=_picker("clear", user),
        preview=preview,
        preview_fresh=preview is not None,
        report=report,
        safety_mb=(round(safety.database_size() / 1_000_000) + safety.estimated_megabytes(affected)) if preview else 0,
    )


def _keep_clear_preview(request, selected: set[str], report: RunReport) -> None:
    request.session[session_key(SESSION_CLEAR)] = {
        "sections": registry.ordered(selected),
        "report": report.to_json(),
        "at": timezone.now().isoformat(),
    }


def _preview_clear(request, selected: set[str]) -> RunReport:
    report = run_clear(selected, preview=True)
    _keep_clear_preview(request, selected, report)
    return report


def data_clear(request):
    if request.method != "POST":
        report = _pop_report(request, "clear")
        pending = None if report else _pending_clear(request)
        if pending:
            return _clear_page(request, set(pending["sections"]), preview=RunReport.from_json(pending["report"]))
        return _clear_page(request, set(), report=report)

    refused = _refused(request, reverse("transfer:data_clear"), lambda: _clear_page(request, set(), status=403))
    if refused:
        return refused
    action = request.POST.get("action", "")
    selected = _posted_selection(request, usable_keys("clear"))
    if not selected:
        messages.error(request, EMPTY)
        return _clear_page(request, set())
    missing = registry.missing(selected, "clear")
    if missing:
        messages.error(request, _missing_message(selected, missing, "clear"))
        return _clear_page(request, selected)

    pending = _pending_clear(request)
    # Typed, not ticked: it is irreversible from the page, and typing is the
    # deliberate act. Any case, spaces around it forgiven.
    if action == "effacer" and request.POST.get("confirmation", "").strip().upper() != "EFFACER":
        messages.error(request, TYPE_TO_CONFIRM)
        preview = RunReport.from_json(pending["report"]) if pending and set(pending["sections"]) == selected else None
        return _clear_page(request, selected, preview=preview)
    busy = busy_reason()
    if busy:
        messages.error(request, busy)
        return redirect("transfer:data_clear")

    try:
        if action == "effacer":
            if not pending or set(pending["sections"]) != selected:
                stored = request.session.get(session_key(SESSION_CLEAR)) or {}
                expired = not pending and set(stored.get("sections") or []) == selected
                _preview_clear(request, selected)
                messages.warning(request, EXPIRED if expired else CHANGED)
                return redirect("transfer:data_clear")
            if not _shows(request, pending["report"]):
                # The preview stored is another page's (a second tab, an
                # earlier confirm that previewed again). What this page
                # announced still decides: worked out again, it either says
                # the same - and runs, held to it - or something changed
                # since and nothing is done.
                fresh = _preview_clear(request, selected)
                if _still_shown(request, fresh):
                    return _confirm_clear(request, selected, fresh)
                messages.warning(request, _why_not_shown(request, OLD_PAGE_CLEAR, OTHER_TAB_CLEAR))
                return redirect("transfer:data_clear")
            return _confirm_clear(request, selected, RunReport.from_json(pending["report"]))
        _preview_clear(request, selected)
    except Busy as exc:
        messages.error(request, str(exc))
    return redirect("transfer:data_clear")


def _confirm_clear(request, selected: set[str], preview: RunReport):
    try:
        # Chosen from the preview the run is held to (expected=).
        backups = safety.before("effacement", safety.sections_at_risk(preview, cleared=selected))
    except SafetyError as exc:
        messages.error(request, str(exc))
        return redirect("transfer:data_clear")
    try:
        report = run_clear(selected, preview=False, expected=preview)
    except NotAsPreviewed as moved:
        _keep_clear_preview(request, selected, moved.report)
        messages.warning(request, MOVED_CLEAR)
        return redirect("transfer:data_clear")
    except Busy as exc:
        messages.error(request, f"{exc} Rien n'a été effacé.")
        return redirect("transfer:data_clear")
    except Exception as exc:  # rolled back
        logger.exception("clear failed")
        messages.error(request, f"L'effacement a échoué, rien n'a été changé : {safety.error_text(exc, logged=True)}")
        return redirect("transfer:data_clear")
    report.safety = _shown_backups(backups)
    request.session.pop(session_key(SESSION_CLEAR), None)
    request.session[session_key(SESSION_REPORT)] = {"mode": "clear", "report": report.to_json()}
    messages.success(request, _backup_message("Effacement terminé", backups))
    return redirect(reverse("transfer:data_clear") + "?rapport=1")
