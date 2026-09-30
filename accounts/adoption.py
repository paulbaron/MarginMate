"""Adopting a database of the old single mode (one db.sqlite3, no login)
into an espace (`manage.py adopt_database`) - a ONE-OFF. The owner's own
was adopted on 28/09/2026, before single mode was removed (29/09); the
command stays for a copy of an old installation still to bring in: its
source is only ever read, whatever the app's settings are today.

What it does, in order (`adopt`), after checking everything first (`plan`,
which is also the whole of a dry run and writes nothing):

1. an espace for him - a Tenant with ``uses_server_integrations`` (his .env
   accounts: Metro, the mailbox, L'Addition, the AI, the portals), CLOSED
   until the end, so nothing half-made can be reached;
2. his database COPIED into it with SQLite's backup API, read through a
   read-only connection: the source is never moved nor written. Its -wal
   is read with it (his server may be running: its last writes are there);
   a WAL database nobody has open is opened ``immutable`` instead, which
   leaves no -wal/-shm beside it either;
3. the central tables dropped FROM THE COPY (logins, sessions, the admin's
   log, content types, the accounts app's own), then the copy VACUUMed so
   their bytes go too: they live in the accounts database now. Kept, the
   copy would carry password hashes and live session keys into every safety
   backup of the espace, and differ from
   every espace made from the template - which has none of these tables
   (their migrations are recorded there all the same, as no-ops, and so they
   stay recorded in the copy). His logins are NOT copied: the one given by
   --email must already exist in the accounts database;
4. his folders copied into the espace's (media, private, downloads,
   backups); the Tickets' waiting uploads (media/receipt_batches) go to
   imports/, where an espace keeps them (accounts.paths.imports_dir);
5. `migrate` bound to the copy - its data migrations write into it;
6. the signing links' index (accounts.links) from the copied requests: EVERY
   request's link, whatever its state - the index holds exactly the hashes
   the espace's requests hold (`staff.signature_requests.index_links`), and
   a link leaves it only with its request (deleted or purged). So a link he
   already sent still opens, and a cancelled or superseded one still says
   so (410), as it did before the adoption, rather than « lien inconnu »
   (404);
7. his membership (owner), and the espace opened.

Refused when another open espace already uses the server's accounts: there
is one Metro account and one pause (accounts.checks.one_owner_espace).

Anything failing removes the espace's folder and its row: a second run
starts from nothing.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from django.apps import apps
from django.db import transaction

from . import paths, provisioning
from .models import Membership, SigningLink, Tenant
from .router import ACCOUNTS_ALIAS, ACCOUNTS_APPS
from .tenancy import bound_tenant
from .users import normalize_email, user_for_email

#: The folders an adoption copies, by option: each lands in the espace's
#: folder of the same name (accounts.paths).
FOLDERS = (paths.MEDIA, paths.PRIVATE, paths.DOWNLOADS, paths.BACKUPS)
#: invoices.receipt_batches.STAGING_DIR (a test keeps the two equal): the
#: Tickets' uploads waiting for their import, under media in a single-mode
#: installation, under imports/ in an espace.
RECEIPT_BATCHES = "receipt_batches"


class AdoptionError(Exception):
    """Why an adoption does not happen, in French, for the command to say."""


@dataclass
class FolderCopy:
    option: str
    source: Path
    files: int = 0
    size: int = 0
    waiting_uploads: int = 0


@dataclass
class Plan:
    user: object
    name: str
    source: Path
    size: int
    counts: list = field(default_factory=list)
    central_tables: list = field(default_factory=list)
    source_logins: int = 0
    links: int = 0
    pending: list = field(default_factory=list)
    folders: list = field(default_factory=list)
    leaving: list = field(default_factory=list)


def read_only_uri(path) -> str:
    """An SQLite URI reading `path` without writing anything beside it:
    ``immutable`` for a WAL database with no -wal file (nobody has it open,
    and a read-only connection would leave an empty -wal and -shm behind),
    ``mode=ro`` otherwise (his running server's last writes are in the
    -wal, which a read-only connection reads)."""
    path = Path(path).resolve()
    with open(path, "rb") as handle:
        header = handle.read(20)
    in_wal_mode = len(header) == 20 and header[18] == 2
    closed = not Path(f"{path}-wal").exists()
    return path.as_uri() + ("?immutable=1" if in_wal_mode and closed else "?mode=ro")


def open_read_only(path) -> sqlite3.Connection:
    return sqlite3.connect(read_only_uri(path), uri=True)


def copy_read_only(source, target) -> None:
    reader = open_read_only(source)
    try:
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()


def central_table_names() -> set[str]:
    """Every table of the central apps (their many-to-many tables too)."""
    names = set()
    for label in ACCOUNTS_APPS:
        for model in apps.get_app_config(label).get_models(include_auto_created=True):
            names.add(model._meta.db_table)
    return names


def _megabytes(size: int) -> str:
    return f"{size / 1_000_000:.1f} Mo".replace(".", ",")


def _folder_stats(copy: FolderCopy) -> FolderCopy:
    for root, _dirs, files in os.walk(copy.source):
        waiting = Path(root).relative_to(copy.source).parts[:1] == (RECEIPT_BATCHES,)
        for name in files:
            try:
                size = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            copy.files += 1
            copy.size += size
            if copy.option == paths.MEDIA and waiting:
                copy.waiting_uploads += 1
    return copy


def _source_facts(source: Path) -> dict:
    """What the source holds, read through a read-only connection."""
    from invoices.models import Invoice, Supplier
    from bank.models import BankTransaction
    from recipes.models import Recipe
    from staff.models import Employee, SignatureRequest

    try:
        connection = open_read_only(source)
    except sqlite3.Error as exc:
        raise AdoptionError(f"{source} ne s'ouvre pas en SQLite : {exc}") from exc
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "django_migrations" not in tables:
            raise AdoptionError(f"{source} n'est pas une base MarginMate (aucune table django_migrations).")
        applied = set(connection.execute("SELECT app, name FROM django_migrations"))
        if not any(app == "invoices" for app, _name in applied):
            raise AdoptionError(f"{source} n'est pas une base MarginMate (aucune migration des factures).")

        def count(table, where="", params=()):
            if table not in tables:
                return 0
            return connection.execute(f'SELECT COUNT(*) FROM "{table}" {where}', params).fetchone()[0]

        counts = [
            (count(Invoice._meta.db_table), "documents (factures et tickets)"),
            (count(Supplier._meta.db_table), "fournisseurs"),
            (count(BankTransaction._meta.db_table), "opérations bancaires"),
            (count(Recipe._meta.db_table), "recettes"),
            (count(Employee._meta.db_table), "salariés"),
            (count(SignatureRequest._meta.db_table), "demandes de signature"),
        ]
        links = count(SignatureRequest._meta.db_table)
        central = sorted(central_table_names() & tables)
        logins = count("auth_user")
    except sqlite3.DatabaseError as exc:
        raise AdoptionError(f"{source} ne se lit pas comme une base SQLite : {exc}") from exc
    finally:
        connection.close()
    return {"counts": counts, "links": links, "central": central, "logins": logins, "applied": applied}


def _pending(applied: set) -> list[str]:
    """The business migrations the copy still needs (the central apps'
    are recorded as no-ops in an espace: the router keeps them elsewhere)."""
    from django.db.migrations.loader import MigrationLoader

    graph = MigrationLoader(None, ignore_no_migrations=True).graph
    return sorted(f"{app}.{name}" for app, name in set(graph.nodes) - applied if app not in ACCOUNTS_APPS)


def plan(*, email: str, name: str, source, folders: dict | None = None, leave_current: bool = False) -> Plan:
    """Check everything an adoption needs and say what it would do. Reads
    only: this is the whole of a dry run."""
    name = (name or "").strip()
    if not name:
        raise AdoptionError("--name : le nom du bar, tel qu'il s'affichera en haut des pages.")
    if len(name) > Tenant._meta.get_field("name").max_length:
        raise AdoptionError(f"--name : {Tenant._meta.get_field('name').max_length} caractères au plus.")

    email = normalize_email(email)
    user = user_for_email(email)
    if user is None:
        raise AdoptionError(
            f"Aucun compte (un seul) pour « {email} » dans la base des comptes : créez-le d'abord "
            "(« manage.py createsuperuser --database accounts », ou une inscription sur invitation)."
        )
    leaving = []
    for membership in Membership.objects.filter(user=user).select_related("tenant").order_by("pk"):
        tenant = membership.tenant
        if not leave_current:
            raise AdoptionError(
                f"« {email} » est déjà membre de l'espace « {tenant.name} » (dossier {tenant.dir_name}). Un compte "
                "n'a qu'un espace aujourd'hui : relancez avec --leave-current pour fermer celui-là (ses fichiers "
                "restent) et rattacher le compte à l'espace adopté."
            )
        if Membership.objects.filter(tenant=tenant).exclude(user=user).exists():
            raise AdoptionError(
                f"L'espace « {tenant.name} » (dossier {tenant.dir_name}) a d'autres membres : --leave-current le "
                "fermerait pour eux aussi. Retirez-les d'abord, ou adoptez pour un autre compte."
            )
        leaving.append(tenant)

    # One Metro account, one pause: a second espace using the server's
    # accounts would sign in to Metro on a pause of its own
    # (accounts.checks.one_owner_espace refuses to start with two).
    owners = Tenant.objects.filter(is_active=True, uses_server_integrations=True).exclude(
        pk__in=[tenant.pk for tenant in leaving]
    )
    if owners.exists():
        named = ", ".join(f"« {tenant.name} » (dossier {tenant.dir_name})" for tenant in owners)
        raise AdoptionError(
            f"L'espace {named} utilise déjà les accès du serveur (Metro, boîte aux lettres, L'Addition, analyse "
            "IA, portails) : un seul espace le peut - un seul compte Metro, une seule pause. Fermez-le d'abord, ou "
            "relancez depuis son compte avec --leave-current."
        )

    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise AdoptionError(f"--from : aucun fichier « {source} ».")
    facts = _source_facts(source)

    root = paths.tenants_root().resolve()
    copies = []
    for option in FOLDERS:
        folder = (folders or {}).get(option)
        if not folder:
            continue
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            raise AdoptionError(f"--{option} : aucun dossier « {folder} ».")
        if root == folder or folder in root.parents:
            raise AdoptionError(f"--{option} : « {folder} » contient TENANTS_ROOT - la copie se copierait elle-même.")
        copies.append(_folder_stats(FolderCopy(option, folder)))

    size = source.stat().st_size
    wal = Path(f"{source}-wal")
    if wal.exists():
        size += wal.stat().st_size
    return Plan(
        user=user,
        name=name,
        source=source,
        size=size,
        counts=facts["counts"],
        central_tables=facts["central"],
        source_logins=facts["logins"],
        links=facts["links"],
        pending=_pending(facts["applied"]),
        folders=copies,
        leaving=leaving,
    )


def describe(plan: Plan) -> list[str]:
    """What the plan does, in French, a line each."""
    lines = [
        f"Base d'origine : {plan.source} ({_megabytes(plan.size)}) - lue seulement, jamais modifiée ni déplacée.",
        f"Compte : {plan.user.get_username()}{' (superutilisateur)' if plan.user.is_superuser else ''}, "
        "propriétaire du nouvel espace.",
        f"Nouvel espace : « {plan.name} », avec les accès du serveur (Metro, boîte aux lettres, L'Addition, "
        "analyse IA, portails du fichier .env).",
    ]
    for tenant in plan.leaving:
        lines.append(f"Espace actuel du compte fermé (--leave-current), ses fichiers gardés : « {tenant.name} » "
                     f"(dossier {tenant.dir_name}).")
    held = ", ".join(f"{number} {label}" for number, label in plan.counts if number)
    lines.append(f"Contenu : {held or 'aucune donnée'}.")
    if plan.central_tables:
        lines.append(
            "Tables des comptes retirées de la COPIE (elles vivent dans la base des comptes) : "
            + ", ".join(plan.central_tables) + "."
        )
    if plan.source_logins:
        lines.append(
            f"Comptes de la base d'origine : {plan.source_logins}, non repris - seul "
            f"{plan.user.get_username()} entre dans l'espace."
        )
    lines.append(f"Liens de signature à indexer : {plan.links}.")
    lines.append(
        "Migrations à appliquer à la copie : " + (", ".join(plan.pending) if plan.pending else "aucune") + "."
    )
    if not plan.folders:
        lines.append("Dossiers : aucun (--media, --private, --downloads, --backups).")
    for copy in plan.folders:
        # « -> », not an arrow: Windows writes a redirected or piped output in
        # its ANSI code page (cp1252), which has none - `adopt_database …
        # > adoption.log` stopped on it before doing anything.
        line = f"Dossier {copy.option} : {copy.source} -> {copy.option}/ - {copy.files} fichier(s), {_megabytes(copy.size)}"
        if copy.waiting_uploads:
            line += f", dont {copy.waiting_uploads} en attente d'import ({RECEIPT_BATCHES}/) -> {paths.IMPORTS}/"
        lines.append(line + ".")
    return lines


def _copy_folder(copy: FolderCopy, tenant) -> None:
    espace = paths.tenant_dir(tenant)
    for entry in copy.source.iterdir():
        kind = paths.IMPORTS if copy.option == paths.MEDIA and entry.name == RECEIPT_BATCHES else copy.option
        target = espace / kind / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


def _drop_central_tables(database: Path, tables) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in tables:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.commit()
        # A DROP only puts the pages on SQLite's freelist: their bytes -
        # password hashes, live session keys - stayed in the file, and in
        # every safety copy of it (the backup API copies the freelist too).
        # VACUUM rewrites the file without them, and without the source's own
        # stale freelist pages, which PRAGMA secure_delete would not clear.
        # Outside any transaction: nothing is open after the commit.
        connection.execute("VACUUM")
    finally:
        connection.close()


def _check_copy(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        verdict = connection.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        connection.close()
    if verdict != "ok":
        raise AdoptionError(f"La copie de la base ne passe pas le contrôle d'intégrité de SQLite : {verdict}")


def adopt(plan: Plan) -> Tenant:
    """Do what `plan` says (see the module's docstring). Returns the espace,
    open. On failure, nothing is left: no folder, no row."""
    from staff.models import SignatureRequest

    tenant = Tenant.objects.create(
        name=plan.name,
        dir_name=provisioning.new_dir_name(),
        uses_server_integrations=True,
        is_active=False,
    )
    folder = paths.tenant_dir(tenant)
    made_folder = False
    try:
        folder.mkdir(parents=True, exist_ok=False)
        made_folder = True
        for sub in paths.FOLDERS:
            (folder / sub).mkdir()
        database = paths.tenant_database(tenant)
        copy_read_only(plan.source, database)
        _check_copy(database)
        _drop_central_tables(database, plan.central_tables)
        for copy in plan.folders:
            _copy_folder(copy, tenant)
        provisioning.migrate_tenant(tenant)
        with bound_tenant(tenant):
            # Every request's link, whatever its state (the module's docstring).
            hashes = list(SignatureRequest.objects.values_list("token_hash", flat=True))
        with transaction.atomic(using=ACCOUNTS_ALIAS):
            SigningLink.objects.bulk_create([SigningLink(token_hash=token_hash, tenant=tenant) for token_hash in hashes])
            for old in plan.leaving:
                Membership.objects.filter(user=plan.user, tenant=old).delete()
                Tenant.objects.filter(pk=old.pk).update(is_active=False)
            Membership.objects.create(user=plan.user, tenant=tenant, role=Membership.Role.OWNER)
            tenant.is_active = True
            tenant.save(update_fields=["is_active"])
    except BaseException:
        if made_folder:
            provisioning.remove_tenant_files(tenant)
        Tenant.objects.filter(pk=tenant.pk).delete()
        raise
    return tenant
