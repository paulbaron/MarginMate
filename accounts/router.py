"""Which database a model's rows live in.

The logins, sessions, admin log, content types and this app's rows go to
the `accounts` alias; everything else is left to `default`, which a binding
makes the tenant's own file (accounts/tenancy.py). config/settings.py
always configures both (the single mode that had only `default` is gone).

No business model has a foreign key to auth, contenttypes or accounts
(accounts/tests/test_router.py keeps it that way): the two sides are
different SQLite files, and a foreign key cannot cross between them.
"""

from django.db import DEFAULT_DB_ALIAS

ACCOUNTS_ALIAS = "accounts"

#: The apps whose rows are central, not a tenant's.
ACCOUNTS_APPS = frozenset({"auth", "contenttypes", "sessions", "admin", "accounts"})


class AccountsRouter:
    def db_for_read(self, model, **hints):
        if model._meta.app_label not in ACCOUNTS_APPS:
            return None
        # A row stays in the database it was read from or written to - what
        # Django itself does when no router decides (a test database's
        # snapshot restores its own content types into itself).
        instance = hints.get("instance")
        if instance is not None and getattr(instance, "_state", None) is not None and instance._state.db:
            return instance._state.db
        return ACCOUNTS_ALIAS

    db_for_write = db_for_read

    def allow_relation(self, obj1, obj2, **hints):
        # Rows of the two sides never point at each other; within one side,
        # Django's own rule (same database) applies.
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label in ACCOUNTS_APPS:
            return db == ACCOUNTS_ALIAS
        return db == DEFAULT_DB_ALIAS
