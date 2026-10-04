"""What the till's tests share.

`LADDITION_ACCOUNT`: an L'Addition account with a value to sign in with, as
the owner's .env gives one (`accounts.vault.server_setting`, the owner's
espace only - the test espace is). The test settings blank every credential,
and L'Addition's card and fetch are drawn and run only where the account is
ready (recipes/pos/connectors.py). Invented values; nothing ever signs in.
"""

from django.test import override_settings

LADDITION_ACCOUNT = override_settings(LADDITION_EMAIL="caisse@example.invalid", LADDITION_PASSWORD="mot-de-passe-essai")
