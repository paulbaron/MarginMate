"""Which login an e-mail address names - one rule for the login page, the
signup and `manage.py adopt_database`.

A signup makes the username the address itself, lower-cased. A login made
another way (``createsuperuser --database accounts``, which asks for a
username AND an address) may carry any username: the address typed on the
login page still finds it, through its e-mail field. Several logins behind
one address name nobody - never a guess between two accounts.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q

#: auth.User.username's length: a signup's address becomes the username.
USERNAME_MAX_LENGTH = 150


def normalize_email(value) -> str:
    return str(value or "").strip().lower()


def users_for_email(email):
    """Every login this address could name (its username or its e-mail
    field, case aside)."""
    User = get_user_model()
    email = normalize_email(email)
    if not email:
        return User.objects.none()
    return User.objects.filter(Q(username__iexact=email) | Q(email__iexact=email))


def user_for_email(email):
    """The one login this address names, or None (none, or several)."""
    found = list(users_for_email(email)[:2])
    return found[0] if len(found) == 1 else None
