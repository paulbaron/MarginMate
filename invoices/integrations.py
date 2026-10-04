"""The connectors, and whose accounts they sign in with.

Metro, the invoice mailbox, the customer portals and the AI reading
(ANTHROPIC_API_KEY) run in EVERY tenant (`accounts.tenancy.
integrations_allowed`: any bound tenant), each with the accounts typed on
ITS « Identifiants » page (accounts/vault.py). The server's own accounts -
the .env's values - stand in for a value not typed in the platform owner's
tenant only (`Tenant.uses_server_integrations`,
`accounts.tenancy.server_accounts_allowed`); every reader of a credential
asks that question before it reads a setting or the .env file
(`vault.server_setting`, `scrapers.generic_email.mailbox_credentials`,
`scrapers.website.credentials`). Since 04/10/2026; before, every tenant but
the owner's saw these features as « à configurer ».

Every entry point still refuses an UNBOUND thread, each on its own:

* the views, before a job is made or a thread started (the gather, the
  mailbox's and a portal's « Tester », a source saved, the AI chosen on the
  PDF import);
* the task bodies, once their thread is bound (tasks.gather_invoices_task,
  test_email_pattern_task, test_website_task);
* each connector last, BEFORE it reads a setting or the .env file
  (scrapers.metro.scrape_metro_invoices, scrapers.generic_email.
  find_matching_emails, scrapers.website.credentials, parsers.llm_fallback):
  a refusal names no variable and reaches no account.

A message seen in a hosted bar names the feature and the « Identifiants »
page, never a server variable or the .env file: « FREEBOX_PASSWORD est
absente du fichier .env » would tell which variables the server holds.
"""

TO_CONFIGURE = "à configurer — disponible prochainement dans les réglages de votre espace"


def refused(feature: str) -> str:
    return f"{feature} : {TO_CONFIGURE}."


#: « Récupérer depuis les sources », the whole of it.
GATHER = refused("Récupérer les factures depuis Metro, la boîte mail ou les espaces clients")
#: « Récupérer les bons » on the Consignes page: the slips come through the
#: same gather (tasks._gather_slips), so its view still refuses with GATHER;
#: this is the sentence the page draws in place of the button.
SLIPS = refused("Récupérer les bons de consignes depuis la boîte mail")
METRO = refused("Metro")
MAILBOX = refused("La boîte mail des factures")
PORTALS = refused("Les espaces clients des fournisseurs")
#: The « Sources » tab and a source's form: both channels are the server's.
SOURCES = refused("Les sources de factures (boîte mail, espaces clients)")
AI_READING = refused("L'analyse IA")
#: The AI reading chosen where no key is typed (and, in the owner's tenant,
#: none in the server's settings either).
AI_KEY_MISSING = (
    "L'analyse IA demande une clé d'API Anthropic : renseignez-la sur la page Identifiants (réservée au "
    "propriétaire de l'espace)."
)
