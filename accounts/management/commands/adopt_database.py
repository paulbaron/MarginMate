"""Adopt a database of the old single mode (one db.sqlite3, no login) into
an espace - a ONE-OFF, with the owner's approval. His own was adopted on
28/09/2026; single mode itself was removed on 29/09 (accounts/adoption.py).

    python manage.py adopt_database --email <son adresse> --name "<le bar>" --from <chemin>/db.sqlite3 \\
        [--media <dossier>] [--private <dossier>] [--downloads <dossier>] [--backups <dossier>] \\
        [--leave-current] [--dry-run]

The login must already exist in the accounts database (``createsuperuser
--database accounts``, or a signup with an invitation - then --leave-current
closes the empty espace the signup made). The source database and folders
are only read. --dry-run checks everything and says what would be done,
writing nothing. What a real run does, step by step: accounts/adoption.py.
"""

from django.core.management.base import BaseCommand, CommandError

from accounts import adoption


class Command(BaseCommand):
    help = "Adopte une base de l'ancien mode « single » dans un espace (une fois) : copie, jamais de modification."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="L'adresse du compte, déjà créé dans la base des comptes.")
        parser.add_argument("--name", required=True, help="Le nom du bar, affiché en haut des pages.")
        parser.add_argument("--from", dest="source", required=True, help="La base à adopter (db.sqlite3) : lue seulement.")
        for option in adoption.FOLDERS:
            parser.add_argument(f"--{option}", help=f"Le dossier à copier dans {option}/ de l'espace.")
        parser.add_argument(
            "--leave-current",
            action="store_true",
            help="Le compte est déjà membre d'un espace (une inscription) : le fermer, fichiers gardés.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Tout vérifier, tout dire, ne rien écrire.")

    def handle(self, *args, email, name, source, leave_current=False, dry_run=False, **options):
        folders = {option: options.get(option) for option in adoption.FOLDERS}
        try:
            plan = adoption.plan(email=email, name=name, source=source, folders=folders, leave_current=leave_current)
        except adoption.AdoptionError as refusal:
            raise CommandError(str(refusal)) from None
        for line in adoption.describe(plan):
            self.stdout.write(line)
        if dry_run:
            self.stdout.write("Essai à blanc : rien n'a été écrit.")
            return
        try:
            tenant = adoption.adopt(plan)
        except adoption.AdoptionError as refusal:
            raise CommandError(f"Adoption interrompue, rien n'en reste : {refusal}") from None
        self.stdout.write(
            f"Fait. Espace « {tenant.name} » ouvert, dossier {tenant.dir_name} ; "
            f"{plan.user.get_username()} y entre comme propriétaire. La base d'origine n'a pas été modifiée."
        )
