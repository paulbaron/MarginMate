"""The server's own accounts, and the one espace that may use them.

Metro, the invoice mailbox, the customer portals' credentials (the .env
variables a portal names) and the AI reading (ANTHROPIC_API_KEY) are the
OWNER's accounts, configured on the server. With one database per bar,
only the espace flagged
`Tenant.uses_server_integrations` may use them
(`accounts.tenancy.integrations_allowed`): every other espace sees them as
« à configurer » - a later step gives each espace accounts of its own - and
EVERY entry point refuses, each on its own, because the data is no guard (a
« Données » import or the admin can switch a mailbox source or Metro back
on in any espace):

* the views, before a job is made or a thread started (the gather, the
  mailbox's and a portal's « Tester », a source saved, the AI chosen on the
  PDF import);
* the task bodies, once their thread is bound (tasks.gather_invoices_task,
  test_email_pattern_task, test_website_task);
* each connector last, BEFORE it reads a setting or the .env file
  (scrapers.metro.scrape_metro_invoices, scrapers.generic_email.
  find_matching_emails, scrapers.website.credentials, parsers.llm_fallback):
  a refusal names no variable and reaches no account.

The sentences name the feature, never a variable, a file or a host: in
another bar's espace, « FREEBOX_PASSWORD est absente du fichier .env » would
tell which variables the server holds.
"""

TO_CONFIGURE = "à configurer — disponible prochainement dans les réglages de votre espace"


def refused(feature: str) -> str:
    return f"{feature} : {TO_CONFIGURE}."


#: « Récupérer depuis les sources », the whole of it.
GATHER = refused("Récupérer les factures depuis Metro, la boîte mail ou les espaces clients")
#: « Récupérer les bons » on the Consignes page: the bons come through the
#: same gather (tasks._gather_slips), so its view still refuses with GATHER;
#: this is the sentence the page draws in place of the button.
SLIPS = refused("Récupérer les bons de consignes depuis la boîte mail")
METRO = refused("Metro")
MAILBOX = refused("La boîte mail des factures")
PORTALS = refused("Les espaces clients des fournisseurs")
#: The « Sources » tab and a source's form: both channels are the server's.
SOURCES = refused("Les sources de factures (boîte mail, espaces clients)")
AI_READING = refused("L'analyse IA")
