"""`manage.py backup_data`: a dated, checked copy of the data folder.

    .venv\\Scripts\\python.exe manage.py backup_data
    .venv\\Scripts\\python.exe manage.py backup_data --dest E:\\Sauvegardes\\MarginMate
    .venv\\Scripts\\python.exe manage.py backup_data --sans-env

What it copies, how, what it refuses and what a failure leaves behind:
accounts/data_backup.py. deploy.cmd runs it before every deployment, with
``--chemin-dans`` so it can name the backup in its rollback instructions.
Exit code 0 when the backup is whole, 1 otherwise.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from accounts import data_backup


class Command(BaseCommand):
    help = (
        "Sauvegarde le dossier des données dans un dossier daté : chaque base SQLite copiée par SQLite puis "
        "vérifiée, tous les autres fichiers, le .env et un manifeste."
    )
    # A backup is never held back by a setting a check dislikes.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--dest",
            help="Le dossier où créer la sauvegarde datée (par défaut : « backups », à côté du dossier des données).",
        )
        parser.add_argument(
            "--sans-env",
            action="store_true",
            dest="sans_env",
            help="Ne pas copier le fichier .env (il contient la clé secrète, la phrase de passe, les mots de passe).",
        )
        parser.add_argument(
            "--chemin-dans",
            dest="chemin_dans",
            help="Écrire le chemin du dossier de la sauvegarde dans ce fichier une fois fini (deploy.cmd le lit).",
        )

    def handle(self, *args, dest=None, sans_env=False, chemin_dans=None, **options):
        try:
            folder = data_backup.make_backup(dest, with_env=not sans_env, say=self.stdout.write)
        except data_backup.BackupError as exc:
            if exc.folder is not None:
                self.stderr.write(
                    f"Le dossier commencé est gardé sous le nom {exc.folder} : ce n'est PAS une sauvegarde "
                    "utilisable. Supprimez-le une fois le problème compris."
                )
            raise CommandError(f"Sauvegarde non faite : {exc}") from exc
        if chemin_dans:
            Path(chemin_dans).write_text(f"{folder}\n", encoding="utf-8")
        self.stdout.write(
            "Gardez plusieurs sauvegardes, dont une copie hors de ce PC (disque externe, dossier synchronisé) : "
            "une sauvegarde sur le même disque ne survit pas à ce disque."
        )
