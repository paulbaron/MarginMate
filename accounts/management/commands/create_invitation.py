"""Make a signup code.

    python manage.py create_invitation
    python manage.py create_invitation --note "Bar de la gare" --days 14
    python manage.py create_invitation --days 0

The code is printed ONCE: only its hash is kept (accounts.models.Invitation),
so a lost code is a new invitation. It opens one signup, then it is used. It
expires after 30 days (`invitations.DEFAULT_DAYS`), after --days N days, or
never with --days 0 - and the command says which, with the date. The person
signs up at /inscription/ with it, the bar's name, an e-mail address and a
password.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts import invitations
from accounts.models import Invitation

NOTE_MAX = Invitation._meta.get_field("note").max_length


class Command(BaseCommand):
    help = "Crée un code d'invitation à l'inscription : affiché une seule fois, gardé haché."

    def add_arguments(self, parser):
        parser.add_argument("--note", default="", help="Pour qui, pour quoi : gardé avec l'invitation (le code, non).")
        parser.add_argument(
            "--days",
            type=int,
            help=(
                f"Valable ce nombre de jours, de 1 à {invitations.MAX_DAYS} (sans l'option : "
                f"{invitations.DEFAULT_DAYS}) ; 0 : n'expire pas."
            ),
        )

    def handle(self, *args, note="", days=None, **options):
        note = (note or "").strip()
        if len(note) > NOTE_MAX:
            raise CommandError(f"--note : {NOTE_MAX} caractères au plus.")
        chosen = days is not None
        if not chosen:
            days = invitations.DEFAULT_DAYS
        if not 0 <= days <= invitations.MAX_DAYS:
            raise CommandError(
                f"--days : un nombre de jours de 1 à {invitations.MAX_DAYS}, ou 0 pour une invitation sans expiration."
            )
        invitation, code = invitations.create_invitation(note=note, days=days)
        self.stdout.write("Code d'invitation - affiché cette fois seulement, il n'est gardé que haché :")
        self.stdout.write(f"    {code}")
        self.stdout.write(f"Note : {note}" if note else "Sans note.")
        if invitation.expires_at is None:
            self.stdout.write(
                "Sans date d'expiration (--days 0) : valable pour une inscription, jusqu'à ce qu'elle ait lieu."
            )
        else:
            until = f"{timezone.localtime(invitation.expires_at):%d/%m/%Y à %H:%M}"
            if chosen:
                how_long = f"{days} jours"
            else:
                how_long = f"{days} jours, sans --days ; --days N pour une autre durée, --days 0 pour aucune"
            self.stdout.write(f"Valable pour une inscription, jusqu'au {until} ({how_long}).")
        self.stdout.write("À saisir sur la page /inscription/, avec le nom du bar, une adresse e-mail et un mot de passe.")
