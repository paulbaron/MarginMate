"""The connectors, and whose accounts they sign in with.

Two questions, asked by every entry point (accounts/tenancy.py):

* `integrations_allowed()` - any bound tenant (since 04/10/2026; before,
  the platform owner's only): the invoice mailbox (its sources and the
  returnables slips), L'Addition and the AI reading (ANTHROPIC_API_KEY) run
  in EVERY espace, each with the accounts typed on ITS « Identifiants » page
  (accounts/vault.py). Refused unbound only.
* `server_accounts_allowed()` - the platform owner's tenant only
  (`Tenant.uses_server_integrations`, at most one: accounts.E005):
  - the server's own accounts - the .env's values - stand in for a value not
    typed there and nowhere else (`vault.server_setting`,
    `scrapers.generic_email.mailbox_credentials`, `scrapers.website.
    credentials`): another bar never signs in with the owner's accounts;
  - **Metro stays his**: every bar's sign-in would leave from the server's one
    IP, which Metro's firewall judges for everybody (it blocked the owner
    twice); a pause kept per espace would protect nobody. Elsewhere Metro is
    « à configurer » (`METRO`) - its PDFs dropped by hand are still read by
    its own reader in every espace;
  - **the supplier portals stay his**: the server's Chrome, on the owner's
    home network, would go wherever a bar's page sends it (the router, the
    server itself) with nothing in between. Elsewhere they are « à
    configurer » (`PORTALS`).

Every entry point refuses on its own, because the data is no guard (a
« Données » import or the admin can switch a source or Metro back on):

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
#: TO_CONFIGURE after a plural subject (« Les espaces clients … »).
TO_CONFIGURE_PLURAL = "à configurer — disponibles prochainement dans les réglages de votre espace"


def refused(feature: str, *, plural: bool = False) -> str:
    """« <feature> : à configurer — disponible(s) prochainement … », the
    adjective agreeing with a plural `feature`."""
    return f"{feature} : {TO_CONFIGURE_PLURAL if plural else TO_CONFIGURE}."


#: « Récupérer depuis les sources », the whole of it (unbound only).
GATHER = refused("Récupérer les factures depuis Metro, la boîte mail ou les espaces clients")
#: « Récupérer les bons » on the Consignes page: the slips come through the
#: same gather (tasks._gather_slips), so its view still refuses with GATHER;
#: this is the sentence the page draws in place of the button.
SLIPS = refused("Récupérer les bons de consignes depuis la boîte mail")
#: Metro, outside the platform owner's espace.
METRO = refused("Metro")
MAILBOX = refused("La boîte mail des factures")
#: The supplier portals, outside the platform owner's espace: the source
#: form's channel, the Sources tab, the gather, « Tester » and the connector.
PORTALS = refused("Les espaces clients des fournisseurs", plural=True)
#: The « Sources » tab and a source's form, unbound.
SOURCES = refused("Les sources de factures (boîte mail, espaces clients)", plural=True)
AI_READING = refused("L'analyse IA")
#: Said in place of the mailbox's sources and slips (the gather card,
#: Consignes) where the espace's mailbox is not filled in on « Identifiants ».
MAILBOX_TO_FILL = "Boîte mail : à renseigner sur la page Identifiants."
#: The names the mailbox signs in with (accounts/credentials.py).
MAILBOX_NAMES = ("INVOICE_EMAIL_ADDRESS", "INVOICE_EMAIL_APP_PASSWORD")
#: The AI reading chosen where no key is typed (and, in the owner's tenant,
#: none in the server's settings either).
AI_KEY_MISSING = (
    "L'analyse IA demande une clé d'API Anthropic : renseignez-la sur la page Identifiants (réservée au "
    "propriétaire de l'espace)."
)
#: The AI reading's refusals once a key is there: the server's own limit,
#: and what Anthropic answered - each a fixed sentence (parsers.llm_fallback
#: maps the SDK's errors), never the SDK's English.
AI_BUSY = "L'analyse IA lit déjà deux factures sur le serveur : réessayez dans un instant."
#: Another bar's second reading while its first runs (one each: one bar's
#: uploads must not take both of the server's slots).
AI_BUSY_HERE = "L'analyse IA lit déjà une facture pour votre espace : réessayez dans un instant."
AI_KEY_REFUSED = "La clé d'API Anthropic a été refusée : vérifiez-la sur la page Identifiants."
AI_RATE_LIMITED = "Le compte Anthropic de la clé a atteint sa limite : réessayez dans quelques minutes."
AI_BAD_REQUEST = (
    "Le compte Anthropic de la clé a refusé la lecture (crédit épuisé ?) : vérifiez-le sur console.anthropic.com."
)
AI_UNAVAILABLE = "L'analyse IA est momentanément indisponible chez Anthropic : réessayez plus tard."
AI_MODEL_GONE = "Le modèle de l'analyse IA n'est plus proposé par Anthropic : prévenez l'administrateur de MarginMate."
AI_NO_ANSWER = "L'analyse IA ne répond pas : réessayez plus tard."
#: The credential the AI reading signs its requests with (« Identifiants »).
AI_KEY_NAME = "ANTHROPIC_API_KEY"


class AiReadingRefused(RuntimeError):
    """The AI reading did not run, or Anthropic refused it: a French sentence
    of this module, said as it is on the page (receipt_batches.
    READING_REFUSALS) - never « Erreur inattendue »."""


def mailbox_offered(state=None) -> bool:
    """Whether the gather offers the mailbox's sources and slips: in the
    platform owner's espace always, as before (his .env or his page); in
    any other once its address and app password are on its « Identifiants »
    page (`vault.ready`) - a source that could only fail, at every gather,
    is not ticked for it. `state`: the store already read for this request
    (a page reads it once)."""
    from accounts import vault
    from accounts.tenancy import server_accounts_allowed

    if server_accounts_allowed():
        return True
    return vault.ready(*MAILBOX_NAMES, state=state)


def ai_offered(state=None) -> bool:
    """Whether « Analyse IA » is offered on the PDF import: in the platform
    owner's espace always, as before; in any other once its key is on its
    « Identifiants » page - chosen without one, the upload is refused before
    it is sent. `state`: the store already read for this request."""
    from accounts import vault
    from accounts.tenancy import server_accounts_allowed

    if server_accounts_allowed():
        return True
    return vault.ready(AI_KEY_NAME, state=state)
