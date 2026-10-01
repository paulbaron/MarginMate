# MarginMate — working notes

Django 5.2 / Python 3.11 / SQLite. Bar inventory and costing: invoices in,
real per-unit costs out, recipe margins on top.

## Language: English code, French screens

**Every comment and all the code are in English; only the text the app
DISPLAYS is in French** (the owner's rule, 30/09/2026). English: identifiers
(variables, functions, classes, constants, tests, template variables, URL
names, CSS classes and internal ids, `data-*` names, JS, batch labels and
variables), comments and docstrings of every kind (Python, templates, HTML,
JS, CSS, `rem` lines, config files) and these notes. French: what reaches a
screen, a PDF, a message, a log line or an `echo` - and DEPLOY.md, written for
the owner. A comment may quote the French it talks about, « like this ».
French names that something outside the code depends on are kept on purpose
(« Toolchain » lists them: the database, the HTTP interface, stored keys,
archives, command lines); a Python name holding one is still English. Name new
code in English from the start, and translate any French comment you touch.

## Running the tests

```bash
.venv/Scripts/python.exe manage.py test --settings=config.settings_test
```

or `uv run python manage.py test --settings=config.settings_test` (the same
`.venv`, synced first).

`config/settings_test.py` uses two in-memory databases (the test espace's
`default` and the central `accounts`, `tests/runner.py`), a temporary
`TENANTS_ROOT` (every folder is the test espace's, `accounts.paths`), a test
client logged in as the test espace's owner,
and **blanks every credential** so no test can reach the real mailbox, the
real Metro site or the Anthropic API. `tests/support.py::NoNetworkTestCase`
additionally makes an accidental outbound connection fail loudly.

Add `--exclude-tag=browser` for the fast loop: the browser tests drive a
real headless Chrome (`invoices/tests/test_website_scraper_browser.py`,
against a customer portal served from the machine) and take about twelve
minutes (93 tests in 16 modules on 29/09, eight minutes of it the website
scraper's 31; list them with `grep -rl 'tag("browser"'` and run them module
by module). They skip themselves where Chrome or its driver is
missing. They also reach the network by themselves:
`invoices.scrapers.website.build_chrome` asks webdriver-manager, which looks
the latest driver up online - from a session that must stay offline, run them
with the cached chromedriver given by path.

**Every `TransactionTestCase` sets `serialized_rollback = True`** (the
browser classes, « Données »'s page and safety tests;
`tests/test_transaction_cases.py` checks it). One that does not fires
post_migrate when it flushes, which recreates the content types under new
pks, and the next class restoring its snapshot fails in setUpClass on
« UNIQUE constraint failed: django_content_type ». Run apart, the fast loop
and the browser suite each passed; a whole run failed every « Données » page
test after the browser classes (19/09).

**A browser test logs its Chrome in** (`tests.runner.log_in_the_browser`, in
`setUp`: a TransactionTestCase empties the sessions after every test): every
page but the login, the signup and the employee's signing pages sends a
visitor to `/connexion/`. Read a storage key through the page's own variable
(`DRAFT_KEY`, `EXPANDED_STORAGE_KEY`), never a literal: it carries the espace's
opaque scope - where the page keeps it inside a function (returnables.js),
build it with `accounts.tenancy.storage_scope`. A class sharing one Chrome
empties the storage in `setUp`: the next test reads what the last one left.

## Toolchain

Set up 30/09/2026 at the owner's request; README.md, « First-time setup » and
« Development », has the commands.

- **mise** (`mise.toml`) pins the tools: Python 3.11, uv, prek. Its shims
  (`%LOCALAPPDATA%\mise\shims`) must be on the PATH for `uv` and `prek` to
  answer in a terminal, in the git hooks and in deploy.cmd. A shim works only
  in a folder whose mise.toml names the tool ("No version is set for shim"
  elsewhere).
- **uv** (`pyproject.toml`, `uv.lock`): every Python package, locked. The
  `dev` group holds ruff, ty, django-stubs (pinned to Django's minor) and the
  typeshed stubs; production installs without it (`uv sync --locked
  --no-dev`, deploy.cmd). `[tool.uv] package = false`: the project is run from
  its folder, never built. `uv.lock` was made from the old pip freeze and
  reproduces every production version exactly (python-barcode, httpx and
  httpcore went: nothing imported them). **There is no `requirements.txt`**
  (deleted 01/10/2026 with the hook that exported it): it was the lock's
  export the previous deploy.cmd installed 05a80b4 from, with pip, and a copy
  back would be a second list of versions nothing keeps in step
  (`tests/test_toolchain.py`). A production still before 05a80b4 is finished
  by hand (DEPLOY.md 10.6), and nothing goes back before it (10.4).
  **`.python-version` (3.11) must stay**: without it `uv sync` threw a
  pip-made `.venv` away and rebuilt it on the first Python it found (mise's)
  - measured 30/09, and production's `.venv`, its downloaded OCR models with
  it, would have gone at the next deploy (`tests/test_toolchain.py`).
  **And mise's `python.uv_venv_auto` stays OFF** (`mise.toml` says `false`):
  on, it exports `UV_PYTHON` as the exact Python mise installed (3.11.16)
  into every `uv` a shim runs - deploy.cmd's step 7, prek's `uv run` - an
  explicit request that beats `.python-version`, and uv then replaces a
  `.venv` made on any other 3.11. On 01/10/2026 a `uv run ty` in this folder
  began deleting its pip-made `.venv` (3.11.9) that way and took
  `aiohappyeyeballs` and most of `aiohttp` before a file the dev servers held
  stopped it (put back from uv's cache, byte for byte against RECORD). Check
  with `uv sync --locked --dry-run -v` through the shim: « Using Python
  request `3.11` from version file » and no « Would replace ». What it did
  that is worth keeping, the `python` and `pip` shims running `.venv`'s in
  this folder, is `[env] _.python.venv` (no `UV_PYTHON`; « no venv found »
  until `uv sync` has made one). **Every production `uv sync` also says
  `--python 3.11`** (deploy.cmd's step 7, the way back it prints, DEPLOY.md's
  commands; `test_every_uv_sync_names_the_python_of_python_version`): the
  way back puts an older `mise.toml` back on disk, every one of them with
  the setting on, and an explicit `--python` beats whatever mise hands uv. The
  deploy that SHIPS this runs the previous deploy.cmd (from its copy), whose
  printed way back has no `--python`: add it by hand there. A `.venv`
  whose dev group is not installed (`ty`, the stubs) is that folder's state,
  not this rule's: `uv sync` with the servers stopped completes it.
  VS Code's Ruff extension runs `.venv\Scripts\ruff.exe server`, and Windows
  will not let `uv sync` delete that file (« Accès refusé »); it can be
  renamed while it runs (`ruff.exe.held-by-vscode`), then synced, and the old
  copy deleted once the editor has restarted.
- **prek** (`prek.toml`, `prek install` wires both stages). pre-commit, on
  the staged files: merge markers, files over 1 MB, private keys (the three
  files naming PEM markers excepted), end-of-file and trailing whitespace
  (never in Python: a string literal's whitespace is data), `uv lock`,
  `ruff check --fix`, `ruff format`, `ty check` (shown, never blocking).
  **pre-push is the CI**: `uv lock --check`, ruff check and
  format check and ty over the whole repository, `manage.py check`,
  `makemigrations --check`, and the fast suite with `--parallel 6` (about two
  minutes). A deploy takes GitHub's `main` (« Two copies », below), so the
  push it takes is the CI's moment: run `prek run --hook-stage pre-push
  --all-files` before it - the hooks are not installed in every copy. The browser tests
  stay manual (« Running the tests »).
- **ruff** (`[tool.ruff]`): line length 120, ruff's default rule set less
  FURB157 (`Decimal("0")` stays: money is built from strings), RUF012
  (Django's class-level lists) and SIM117; DTZ001 is off in tests. A finding
  silenced on purpose says why on its line: `# noqa: BLE001 - <why>`.
- **ty** (`[tool.ty.rules]`): the rules Django's run-time attributes and
  unittest's narrowing make noisy are warnings (the comment there lists them
  and why); every other rule blocks. A false positive is silenced on its line
  with `# ty: ignore[<rule>]` and the reason.
- **Language** (the rule: « Language: English code, French screens », at the
  top): code and comments in English; what the app displays - and the
  documents written for the owner, DEPLOY.md - in French. The English sweep of
  30/09/2026 renamed ~860 identifiers (`espace` -> `tenant`, `consignes` ->
  `returnables`, `reprise` -> `pickup`, `bon` -> `slip`, `motif` -> `pattern`,
  `poste` -> `charge_item`...) and translated every comment. French names were
  KEPT where something outside the current code depends on them: displayed
  text; the database (model and field names such as `colisage`, choice values,
  constraint names, stored JSON keys); the HTTP interface (URL paths, query
  parameters such as `?mois=`, `?sans=`, `?tout=`, `?lot=`, form field names
  and the values forms post, ids a URL fragment or a redirect targets such as
  `#a-classer`, `#regles`, `#semaine-type`); the browser's storage keys and
  the session key's value; the « Données » archive (section keys `factures`,
  `fournisseurs`, `liens_ventes`...); files on disk (`etat.txt` and its keys,
  `preuve.pdf`, the backup manifest's `espace`); command lines (`serve
  --verifier`, `backup_data --chemin-dans`, `--sans-env`: the previous
  version's deploy.cmd runs them); HT/TTC, SIREN, Factur-X, L'Addition.
  A Python name HOLDING such a value is English (`TENANT_SESSION_KEY =
  "_marginmate_espace"`).

## One database per bar - the only mode

Every bar is an « espace »: its own SQLite file and folders under
`TENANTS_ROOT` (the owner's: `../data/tenants/`), the logins and espaces in
the `accounts` database, a login on every page, each request bound to the
user's espace (`accounts/tenancy.py`, `accounts/middleware.py`; the module
docstrings of `accounts/*` are the reference).

**Single mode is gone (29/09/2026).** It was one `db.sqlite3`, no login and
every folder from a setting, chosen whenever `MARGINMATE_TENANCY` was
missing: a clone deployed without its .env served every page, the « Données »
export and `/media/` to anyone (security audit ANON-1). Removed with it:
`TENANCY_MODE`, `multi_mode()`, the `HOUSE` marker and `Tenant.is_house`,
the single-mode `DATABASES`, the `/media/` route, `MEDIA_ROOT`/`MEDIA_URL`,
`STAFF_PRIVATE_DIR` (`MARGINMATE_PRIVATE_DIR`), `SCRAPE_DOWNLOAD_DIR`,
`DATA_BACKUP_DIR`, `DATA_STAGING_DIR` - business code asks `accounts.paths`
for a folder. `MARGINMATE_TENANCY` unset or « multi » is ignored; any other
value is **refused when the settings load** (ImproperlyConfigured - a WSGI
server runs no system check). `manage.py adopt_database` stays as a one-off
for a single-mode database still to bring in (the owner's was adopted on
28/09). `accounts/tests/test_no_single_mode.py` loads the shipped settings
with an EMPTY environment and no .env and asks every URL anonymously, GET and
POST: all but the public views (listed there) go to the login.

**A session, a logout and the browser's storage (29/09/2026, security audit
LOAD-1, LOAD-2, LOAD-3, LB-6; `accounts/tests/test_sessions.py`).**
- A session belongs to the espace it was opened in: every login writes it
  (`_marginmate_espace`, `accounts.middleware.pin_the_tenant` on
  `user_logged_in`), and a request whose login now works in another espace -
  a membership edited in the admin - is logged out and sent to the login page
  (« Votre accès a changé : reconnectez-vous. »; htmx: 401 + HX-Redirect).
  Before, every browser still logged in opened the NEW bar's pages, the old
  bar's shared PC included. `adopt_database` therefore logs its owner out
  once. A test that moves a membership and keeps the session writes the
  session's espace itself (transfer/tests/test_tenancy.py).
- « Se déconnecter » forgets the espace's **drafts** only: ui.js, as a
  `form.topbar-logout` is sent (base.html's, and the « indisponible » 503
  page's, which carries the scope for it), removes its `DRAFTS` -
  `stock-take-draft:*` and `consignes:brouillon` under
  `marginmate:espace-<scope>:`, and under a legacy session's old id too: an
  unsaved stock count stayed readable from the public login page. The
  **preferences stay** (the `mm:` keys - the gather's sources left unticked,
  Metro included -, datatable sorts, `stock:rows`, `stock:panel-closed`,
  `achats:import-tab`): the first fix answered `Clear-Site-Data: "storage"`,
  which emptied them all and ticked Metro again with nothing on screen
  saying so (review LOGOUT-PREFS). No header, from any logout - the admin's
  is Django's own and forgets nothing (the superuser's). A new key kept in
  the browser is a decision: a draft goes into ui.js's `DRAFTS`, anything
  else into `test_sessions.KEPT_AT_LOGOUT` with why - the test fails on an
  unclassified `marginmate:` key. A count never saved is lost on an
  explicit logout.
- An espace whose database will not open (missing, 0 bytes - which SQLite
  would take for a new, empty database - or no database: its PRAGMAs fail
  as the connection opens) answers « Votre espace est
  momentanément indisponible », a 503 no-store page rendered unbound
  (`accounts/unavailable.html`), the cause in the log. Only the binding and
  the opening of its connection are caught, the binding undone; a view's
  errors stay the view's. The test settings carry no `init_command`: a test
  of a broken file patches production's in (test_sessions.py).
- `<body data-tenant>` is an opaque scope (`accounts.tenancy.storage_scope`,
  16 hex of an HMAC of the pk keyed by SECRET_KEY), no longer the sequential
  pk, which told every bar how many espaces came before it. Every storage key
  is still built as « espace-<data-tenant>: », so no page script changed. A
  session opened BEFORE the change (no `_marginmate_espace` in it) is pinned
  on its next request and flagged: its pages carry `data-tenant-legacy` (the
  pk it already saw on every page) and load `static/js/tenant_storage_legacy.js`
  first in the body, which moves THAT pk's keys - never another espace's -
  under the scope once. A session opened since is never given the pk. Delete
  the script and the flag two weeks after the deployment (the session age).
  Changing the SECRET_KEY changes every scope: an unsaved count is then no
  longer offered back (it only ever lived in that browser).

## Two copies: development and production

Since 30/09/2026 (the owner's decision) the site and the code being edited
are separate copies on the owner's PC, each with its own code, .env and data
- production, and SEVERAL development copies (« Bar application gestion »,
« … 2 », « … 3 », a session in each). DEPLOY.md, section 10, has the owner's
steps (the one-off move included).

- **PRODUCTION**: code `C:\MarginMate\app`, a git clone whose `origin` is
  **GitHub** (`https://github.com/paulbaron/MarginMate.git`, branch `main`)
  since 01/10/2026 - the owner's choice, one source whatever copy pushed
  (`git remote set-url origin https://github.com/paulbaron/MarginMate.git`).
  It was cloned from the first development copy and followed it: that
  copy's `main` was already live, and deploy.cmd said « Rien de nouveau »
  while the day's work sat in « … 2 ». Data `C:\MarginMate\data` (`tenants\`,
  `accounts.sqlite3`, `logs\`); backups `C:\MarginMate\backups\<AAAA-MM-JJ_HHMMSS>\`
  (`manage.py backup_data`, `accounts/data_backup.py`: every SQLite database
  through the backup API, checked, the rest as files, the .env, a manifest;
  a failure is renamed `-INCOMPLET`); its own .env (`DJANGO_DEBUG=False`,
  `MARGINMATE_HTTPS=1`, absolute paths into `C:\MarginMate\data`). Its
  `start_production.cmd`, the logon task « MarginMate », `serve` on 8765.
- **DEVELOPMENT**: this folder - one of the copies where the owner and
  coding sessions edit; each pushes its `main` to GitHub.
  Its .env says `DJANGO_DEBUG=True`, local hosts only, no `MARGINMATE_HTTPS`,
  **no integration credentials** (Metro, the mailbox, L'Addition, the AI,
  the mail server, the portals' variables: blank, so a gather from here
  refuses instead of reaching Metro), and points `MARGINMATE_TENANTS_ROOT` /
  `MARGINMATE_ACCOUNTS_DB` at `..\data-dev\` - a copy of a production
  backup, made by `refresh_dev_data.cmd`. runserver on 8000, which
  config/wsgi.py refuses to the tunnel anyway. The parent folder's
  `.claude/launch.json` preview is that runserver, so it now runs on
  data-dev, no longer on the site's database.
- **A coding session edits this folder only and never touches `C:\MarginMate`**:
  not its code, not its data, not its .env, no command run there, no
  backup read. Production changes only through `deploy.cmd`, double-clicked
  by the owner in `C:\MarginMate\app`: it takes GitHub's `main` (`git fetch
  origin`, so the network; its window still says « dossier de developpement »),
  so a change goes live once committed, merged into `main` AND pushed.
  **The repository is public**: before a push, `git fetch origin`, then
  every outgoing commit, message and patch (`git log -p origin/main..main`
  - a diff of the two ends misses data a later commit removed) is read for
  real data: the names, payers and amounts of data-dev (« Privacy », at the
  end). A push refused because another copy pushed first is fetched and
  merged (`git fetch origin`, `git merge origin/main`; never a bare `git
  pull`, which REBASES on this PC: `pull.rebase=true` in Git's system
  config), tested, then pushed - never forced. deploy.cmd refuses while
  a job runs (`manage.py running_jobs`, exit 1), stops the server, backs up,
  fast-forwards, `uv sync --locked --no-dev --python 3.11`, `migrate_tenants`, `serve --verifier`,
  restarts; a failure before the merge restarts the server as it was, a
  failure after it restarts NOTHING and prints the rollback. A migration
  shipped therefore reaches production at the next deploy, backed up first.
- **deploy.cmd runs once at a time, and remembers a run left half way**
  (review of 30/09, each rule in `test_deployment_scripts.py`): before step
  1 it takes a MARK, `mkdir .git\marginmate-deploy` (atomic; never a
  `9>"file"` handle, which `start` would hand to the server's window for its
  whole life), writes `etat.txt` there from the stop on (`ancien=`,
  `donnees=`, `sauvegarde=`, every `etape=`) and removes it in `:finish` only
  when THIS run made it (`MM_RELEASE`) - not after a failure past the merge
  (`:failure_after_merge`, `:server_silent`): the next run then refuses and
  prints the way back from etat.txt, where it used to say « Rien de
  nouveau » and exit 0 with the site down. « Rien de nouveau » also checks
  8765 listens. Before the stop it refuses a task « MarginMate » whose
  action is not this folder's start_production.cmd, and after the stop it
  asks `running_jobs` again: a job started in the gap was killed, so the old
  server is restarted and the deploy refused. A git error (« dubious
  ownership ») is said as one. `migrate_tenants` and `running_jobs` pass
  over a CLOSED espace that has no base or will not open (serve and DEPLOY
  §11 tell the owner to close it), `backup_data` lets a file that VANISHED
  mid-copy go (listed in the manifest, `vanished`), and
  refresh_dev_data.cmd takes the newest backup with a manifest.json and a
  data\ folder.
- **deploy.cmd installs with uv** (step 7, `call uv sync --locked --no-dev --python 3.11`;
  `call` because a mise shim may be a batch file) and, before its mark,
  refuses touching nothing when `call uv --version` fails - a shim on the
  PATH answers « mise-shim: failed to execute mise », exit 1, when mise
  itself is not there; with a mark already left, the way back is printed
  instead. The first deploy of the uv version, 05a80b4, was run by the
  PREVIOUS deploy.cmd, with pip on a `requirements.txt` the code no longer
  has: that deploy.cmd, run on a later version, fails past its merge, and
  DEPLOY.md 10.6 finishes that deploy by hand. **Nothing names pip** - the
  way back (`:offline`, `:rollback_instructions`, DEPLOY.md 10.4) installs
  with uv only, and a version from before uv is not gone back to (the
  owner, 01/10/2026: its deploy.cmd, back in place, could not deploy the
  next one). The scripts' labels and variables
  are English; their echo text, `etat.txt` and its keys (`ancien=`,
  `donnees=`, `sauvegarde=`, `etape=`, read back as `MM_NOTE_<key>`), the
  mark folder, the task's name and the `manage.py` commands and options are
  not (another version's deploy.cmd reads or runs them).
  `accounts/deployment.py`'s answers (`DATA=`, `BACKUP=`, `PREVIOUS=`, read
  into `MM_<NAME>`) and its `development` mode are read by scripts of the
  same version only. A new uv (or Python) in mise.toml is installed by its
  shim during step 7, after the merge, from the network: before deploying
  one, the owner runs `mise install uv@<version>` once.
- **Both scripts ask the settings through `accounts/deployment.py`** (never
  the .env read by hand): deploy.cmd refuses a folder whose settings say
  DEBUG or no HTTPS, refresh_dev_data.cmd one that says HTTPS, a data folder
  that is or holds the code's, and the data folder a backup was taken FROM;
  both refuse an accounts database outside the data folder (a dev .env left
  on production's `MARGINMATE_ACCOUNTS_DB` had runserver and
  `migrate_tenants` work on the live logins).
  deploy.cmd is in the code it updates and cmd.exe reads a batch file line
  by line from disk: it copies itself to %TEMP% and runs from the copy. Any
  .cmd here is ASCII, CRLF (`.gitattributes`), without delayed expansion;
  `accounts/tests/test_deployment_scripts.py` reads them statically - **never
  run deploy.cmd or refresh_dev_data.cmd from a session**: they stop the
  owner's server and move data folders.
- **data-dev holds real data** (invoices, bank, staff), copied: never into a
  fixture, a test, a docstring or git - « Fixture privacy » applies to it
  exactly as to the site's data. A session may run runserver on it, never a
  gather.
- **What no copy carries** (01/10/2026, the « Identifiants » review): a
  backup leaves out the credential store (`credentials.bin`, `.key`,
  `.credentials-*.tmp` directly in a tenant's `private/`), every `_debug`
  folder (the scrapers' kept pages) and `downloads/test-<n>/`, listed in the
  manifest's `left_out`; and NO database copy has sessions - the accounts
  database, the tenants', and every other file of the data folder whose
  header says SQLite (a `.bak_*` copy, « Données »'s safety copies), each
  through the backup API: deleted, VACUUM (the API copies free pages too),
  checked again; a failed purge deletes its copy before the folder is
  renamed -INCOMPLET. A file with the SQLite header that does not open fails
  the backup (and so a deploy): its message says to move it out.
  refresh_dev_data.cmd excludes the same files (robocopy /XF /XD), runs
  `deployment.py purge-sessions` over every SQLite file of data-dev
  (realpath-guarded: a junction out is skipped), and says to delete a
  data-dev folder it set aside; `development` and `purge-sessions` refuse a
  backup whose .env has THIS folder's SECRET_KEY (dev must have its own: it
  would open production's sessions and half the store's key). **A copy of a
  .env** (`.env`, `.env.*`, any case, anywhere in the data folder -
  `deployment.is_env_copy`) is never opened,
  never backed up (`left_out.env_copies`, a warning naming it), never copied
  into data-dev (robocopy /XF), and makes `development`/`purge-sessions`
  refuse. Paths are compared written AND resolved (`deployment.inside`
  permissive for refusals, `really_inside` strict for what is opened), and a
  data folder named through an alias (junction, short name, `\\?\`) is
  refused: purge-sessions could otherwise have reached production's live
  data.

## Going online / production

The site is served from the owner's PC (29/09/2026): a Cloudflare Tunnel
(cloudflared, a Windows service the owner installs himself) forwards
`https://gestion.<domaine>` to `http://127.0.0.1:8765`, where
`manage.py serve` runs. `DEPLOY.md` (French, for the owner) has his steps and
the exact `.env` lines; never put the real domain, a token or a key in the
repo. The security audit of 29/09 (25 findings) is fixed below, each point
with a test that failed first.

**The tunnel is `serve`'s alone** (review PROD-1). It pointed at 8000,
runserver's default and both `.claude/launch.json` previews' port, so
whichever dev server held it was the public site: Django's technical page for
the public Host under DEBUG (the settings, `request.META` with the .env's
addresses), every visitor at 127.0.0.1 without it. Three locks:
- **8765** (`serve.DEFAULT_PORT`), a port no dev tool defaults to; the
  launch.json files keep 8000. Never give the tunnel or serve 8000 again.
- **`config/wsgi.py`** - what runserver serves, and any WSGI server loading
  `config.wsgi` - answers 503 (a French sentence, said once in the console)
  to a request carrying Cloudflare's headers (`TUNNEL_HEADERS`: CF-Ray,
  CF-Connecting-IP, CF-Visitor, CDN-Loop). `serve` builds its OWN
  `WSGIHandler` and never goes through there.
- **DEPLOY.md adds the public hostname last** (section 6, step 6): runserver
  stopped, the .env lines in, `serve --verifier` passing and
  `start_production.cmd` « En ligne » - it used to be section 3, before the
  .env said DEBUG=False. `accounts/tests/test_serve.py::DeploymentFilesTests`
  pins that order and the port in every file that names it.

**`manage.py serve`** (`accounts/management/commands/serve.py`, run by
`start_production.cmd`), in order:
- DEBUG off whatever .env says: `manage.py` sets `DJANGO_DEBUG=False` before
  the settings load for `serve` (so a weak key is refused right there), and
  the command sets `settings.DEBUG = False` again.
- Every system check, the deployment ones included; ANY unsilenced WARNING
  or worse refuses, printed with its « À faire ».
- The migrations of the accounts database, the `_template` and every open
  espace, and any missing database: each named, **never applied** - the
  owner backs up `data\` and `.env`, then runs `migrate_tenants`. A new
  migration therefore stops the production server until he does: say so
  when you ship one.
- An EXCLUSIVE socket on 127.0.0.1 (Windows lets a forgotten runserver share
  a port otherwise): a port in use is refused before any work.
- `collectstatic --clear` into `STATIC_ROOT`, served by WhiteNoise (the
  second middleware: the login page needs its stylesheet before any login)
  from there alone - serve sets `WHITENOISE_USE_FINDERS` and `_AUTOREFRESH`
  off. Cleared (PROD-3): collectstatic skips a source OLDER than its copy, so
  a zip, a date-keeping copy or a rollback left the old scripts served under
  the new `?v=` of `{% asset %}` (which dates the SOURCE).
- **runserver serves `/static/` from the source folders whatever DEBUG
  says** (settings: `WHITENOISE_USE_FINDERS = WHITENOISE_AUTOREFRESH = DEBUG
  or runserver`, PROD-4): with the deployed .env (DEBUG off) Django's
  runserver adds no static handler, and WhiteNoise's defaults served the copy
  the last `serve` made - an edit to `static/` did nothing, a new file 404'd.
- Waitress, 8 threads, **ONE process, on purpose**: the espace binding is
  per thread, the login limiter counts in Django's LocMemCache (accounts.W002
  says it is right for one process), the gathers and imports are threads of
  that process. Never several workers.
- `--verifier` runs the checks only; `--port`; Ctrl+C stops it cleanly.
  `manage.py tenant` refuses to wrap `serve` (as runserver and testserver).

**The settings are the environment's** (`config/settings.py`,
`config/security.py`, the checks in `accounts/checks.py`):
- `DEBUG` defaults to **False**. `accounts.E007` refuses DEBUG on while
  `ALLOWED_HOSTS` names a non-local host - in `check`, runserver and
  `migrate_tenants` too: once the owner's .env names the public host it must
  say `DJANGO_DEBUG=False`. DEPLOY.md shows how to try DEBUG on the PC, on a
  COPY of the data (`MARGINMATE_TENANTS_ROOT`, `_ACCOUNTS_DB`, `_LOG_DIR` set
  to it, PROD-2): the live data is `serve`'s alone. A runserver started there
  anyway no longer fails serve's running gathers - the startup reaper waits
  `REAP_DELAY_SECONDS` and takes only the jobs unheard since its process
  started (see « Gathering invoices ») - but whatever a trial writes, it
  writes for good.
- `SECRET_KEY` has no public fallback on a server: DEBUG off and a key that
  is missing, public (the old fallback, the .env.example placeholder),
  « django-insecure… », under 50 characters or with fewer than 5 distinct
  ones is refused **at load** (ImproperlyConfigured, French, the key never
  printed - a WSGI server runs no check); DEBUG on, the development key and
  `accounts.W001`; `accounts.E006` in `check` with DEBUG off.
- `MARGINMATE_HTTPS=1`: secure session and CSRF cookies and HSTS 3600 s
  (`MARGINMATE_HSTS_SECONDS`), never includeSubDomains nor preload. No
  `SECURE_SSL_REDIRECT` (Cloudflare's « Always Use HTTPS » does it, and the
  owner's http://127.0.0.1:8765 has no https) and **no
  `SECURE_PROXY_SSL_HEADER`**. `CSRF_TRUSTED_ORIGINS` from
  `DJANGO_CSRF_TRUSTED_ORIGINS`. nosniff, referrer same-origin, COOP and
  `X-Frame-Options: DENY` are pinned (the file view says SAMEORIGIN itself).
- Deployment-only checks (`check --deploy`, `serve`): E008 a public host
  and no « * », E009 the cookies HTTPS-only, E010 `MARGINMATE_SITE_URL` in
  https and allowed, E011 the espaces and the accounts database OUTSIDE the
  code folder (their defaults, `tenants/` and `accounts.sqlite3` beside
  manage.py, are for a developer). security.W005, W008 and W021 are
  silenced on purpose (comment in settings.py).

**Waitress is the ONE place that trusts the proxy** (`serve.waitress_options`):
`trusted_proxy` 127.0.0.1 (cloudflared), `trusted_proxy_count` 1 (the
RIGHT-MOST X-Forwarded-For entry, the one Cloudflare's edge appends),
X-Forwarded-For and X-Forwarded-Proto only, `clear_untrusted_proxy_headers`.
`REMOTE_ADDR` and `request.is_secure()` are the visitor's before Django
runs - the login limiter and the signature proof file read them. **Never
read X-Forwarded-For or CF-Connecting-IP in Django code**: from anything but
the tunnel they are whatever the sender wrote. Not verifiable offline: that
cloudflared adds no hop of its own - DEPLOY.md asks the owner to look at the
IP of the first proof file signed from a phone (a Cloudflare address there
means `trusted_proxy_count` 2). `max_request_body_size` is the « Données »
archive's cap plus its framing (Waitress's 1 GB default refused a big
archive in English); online, Cloudflare's free plan refuses any body over
100 MB before us - a big archive is imported from the PC.

**The login limiter** (`accounts/limiter.py`, ANON-4, LIMITER-LOCKOUT): the
hard stop is 10 failures in 15 minutes per (place, e-mail); a **place** is
REMOTE_ADDR, an IPv6 client's /64 (Cloudflare hands over the full /128, and
a /64 is free to rotate in; an IPv4-mapped address counts as its IPv4 - the
signup's counters too). Beside it, 50 per place across e-mails
(`IP_LIMIT`) and 100 per e-mail from everywhere (`EMAIL_LIMIT`). A stranger
who knows the address CAN fill that last one (ten places), and it then held
the owner out of both doors, the PC included - so it never holds back:
- **a known device**: every successful login (the login page, the signup,
  the admin's - `MarginMateAdminSite.login` hands its answer to
  `limiter.remember_device`) sets the signed `marginmate_appareil` cookie
  (HttpOnly, SameSite=Lax, Secure with the session cookie, 180 days, keyed
  hashes of up to 5 addresses, never the addresses). Carrying a valid one
  for its address, an attempt is judged on its pair, its place and that
  device's own counter of 10 - never the e-mail's ceiling. A logout keeps it.
- **the PC itself** (`limiter.directly_from_this_pc`): a loopback
  REMOTE_ADDR, a Host naming the PC, and no X-Forwarded-*, Forwarded,
  CF-*, CDN-Loop or Via header - a tunnelled request never qualifies.
The limits are read at call time (tests patch them).

**The log** (`config/logs.py`, DEPLOY-5): stderr and `marginmate.log` (5 x 5
MB, rotating) in `MARGINMATE_LOG_DIR`, by default `logs/` beside TENANTS_ROOT
(`../data/logs/`), the file and folder made at the first record. **Only
`manage.py serve` writes the file** (`logs.is_the_server(sys.argv)`, PROD-5);
runserver and every other command log to their console only: on Windows a
second process holding the file made its rotation fail (WinError 32) and
every record past the size was dropped;
django.request at ERROR, django.security and the rest at WARNING (the
`accounts` logger's forced logouts and 503 causes reach it). Every handler
cuts `/personnel/signer/<token>/` to 4 characters (`SigningLinkFilter`:
message, arguments, traceback): the link opens a timesheet for 14 days.
Waitress keeps no access log. **The test settings say `LOGGING = {}`**: a
test must never write into `../data/logs`.

**The Content-Security-Policy** (`config/security.py`, DEPLOY-6): every HTML
response without a policy of its own gets `APP_POLICY` (default-src 'self',
object-src 'none', base-uri 'none', form-action 'self'); frame-ancestors
follows X-Frame-Options; non-HTML (the PDF in the invoice's frame) gets
none; the employee's signing pages and « sandbox » downloads keep theirs.
script-src keeps 'unsafe-inline' (the inline scripts and `on…` handlers the
module lists) and 'unsafe-eval' (htmx compiles a trigger filter,
`_receipt_batch_status.html`); style-src 'unsafe-inline' (`style=`).
`tests/test_security_headers.py::PolicyFollowsTheTemplatesTests` counts them
again at every run and fails the day one is no longer needed: then tighten
the policy. **Code a browser test runs with `execute_script` is exempt from
the page's CSP**: to test what the page's own code may do, schedule it with
`setTimeout` from there.

**Error pages**: `templates/400.html`, `403.html`, `404.html`, `500.html`
and `403_csrf.html` (one layout, `errors/page.html`), French, no path, no
setting, no reason - the CSRF reason goes to the log. Django's default
handlers render them.

**What a page may say about an error, a redirect, an upload:**
- Never `str(exc)` of a library's error on a page (LB-3): PIL's names the
  server's path. `common.error_for_page(exc, said=(…))` keeps the app's own
  French refusals (`said`) and turns anything else into one fixed sentence
  by kind (`SERVER_ERROR`, `UNREADABLE_IMAGE`, `UNREADABLE_PDF`), the detail
  to the log. Left as they were: the gather's and the till import's job logs
  (`invoices/tasks.py`, `recipes/tasks.py`), shown in the owner's espace
  only (`integrations_allowed`), still carry the exception and its
  traceback.
- Every `next` / `retour` goes through `common.safe_next(request, default)`
  or `local_path` (LB-5): it starts with « / », not « // », holds no control
  character and names an allowed host - `?next=abc` was reversed by
  `redirect()` and made a 500.
- Upload caps (UPLOAD-1): 25 MB per file on every upload form
  (`common.UPLOAD_MAX_FILE_BYTES`, refused in French by the file's name; in
  a receipt folder that file alone becomes an error line, never written to
  disk), 100 MB per request (`UPLOAD_MAX_TOTAL_BYTES`), 500 MB for a receipt
  folder (`invoices.forms.RECEIPT_BATCH_MAX_BYTES`) - counting only the
  files it STAGES: an ignored video or a refused file is listed and weighs
  nothing (UPLOAD-TOTAL-IGNORED) -, « Données » its own 4 GB and free-disk
  check. `common.ONLINE_SEND_MAX_BYTES` (90 MiB) is no server cap: it is
  what Achats' « Prendre une photo » lets one post carry, short of
  Cloudflare's 100 MB (« A folder is a background job » and below).
  `invoices/ocr.page_images` weighs a document BEFORE rendering:
  `MAX_PAGES` 30, `RENDER_MAX_PIXELS` 40 Mpx from `page.get_size()` (a lower
  resolution down to 100 dpi, then `DocumentTooBig`), `IMAGE_MAX_PIXELS`
  read from the header. **Tests patch the caps down and use tiny files -
  never a big render** (the owner's PC froze twice on 29/09).
- **A PDF's pages are counted before any pdfplumber page is made**
  (HARDEN-01): pdfplumber keeps every page it read until the file closes,
  and closing it MAKES every page not made yet - a PDF under 25 MB can hold
  tens of thousands. `ocr.check_page_count` (pdfminer's own walk of the page
  tree, stopped at `MAX_PAGES` + 1, never pdfium's count, which believes the
  /Count a file declares) runs in `receipts.import_document` after the
  e-invoice and before the bon guard, at the top of
  `importing.parse_and_import` and before the AI upload's bon guard;
  `ocr.pdf_pages` is how a reader walks a PDF (refused past the cap, each
  page closed once read): the text layer, `InvoiceParser.parse`, the AI
  reader. `einvoice.embedded_xml` opens a pdfminer document, never a
  pdfplumber one (it runs first, on every PDF). A bon (`returnables.
  reading.pdf_text`) is counted the same way, to its own 5 pages, before
  pdfplumber opens it. **Never `len(pdf.pages)` on a file from outside.**
- **One call into PDFium at a time** (`ocr.PDFIUM_LOCK`, re-entrant): it is
  not thread-safe, and a folder's thread reads its files without
  `receipts.OCR_LOCK` beside the requests and the gathers. `page_images`
  holds it for each call and never while the caller OCRs a page.

**Signing and signup** (staff/, accounts/signup.py):
- `check_code` reserves a try with ONE conditional UPDATE (`code_attempts__lt`,
  the same `code_hash`) before comparing, and uses the code up with an
  UPDATE filtered on that same hash (SIGN-1): read, compare, write back let
  16 simultaneous guesses through. `staff/tests/test_code_race.py` races
  threads on a real espace file - the test `default` has no
  `SQLITE_OPTIONS` (no WAL, no IMMEDIATE), so a threaded test patches
  production's in.
- Signature mail is capped (LB-4, `signature_mail.cap_reached`): 3 link or
  copy mails per request per hour, 60 signature mails per espace per day,
  failed sends counted. Over a cap the owner's page shows the link
  « transmettez-le vous-même »; a code by e-mail is refused before any code
  is issued.
- The link's `document/` needs no code (ANON-6, decided): the page the link
  opens already shows everything the PDF holds, and once signed no code can
  be had while « Voir le PDF » stays offered
  (`staff/tests/test_link_only_reading.py`).
- Signup (ANON-5): an address that already has an account gets the SAME
  sentence, on the same field, as a code that opens nothing; the
  invitation counts them (`Invitation.refused_addresses`, accounts 0002)
  and is voided at 3.

**Testing production itself**: `config/settings_test.py` sets a test key of
its own BEFORE the .env loads (load_dotenv never overrides a set variable),
pins the hosts and HTTPS values and keeps `LOGGING = {}`. A test of the REAL
settings runs them in a child process given
`accounts.tests.test_production_settings.child_environment(...)`: the test
process's own environment holds the whole .env (load_dotenv put it there),
and a child inheriting it would take its outcome from the owner's file. The
settings also read `sys.argv` as manage.py does (only `serve` writes the log
file; `runserver` reads `/static/` from the source folders), so a child
standing for a command sets `sys.argv = ["manage.py", "<command>"]` before
`django.setup()` (`test_production_settings.LOGGED`, `test_serve.MANAGE`).
`accounts/tests/test_serve.py` mocks the command's Waitress, and runs a real
one (the proxy wiring) on 127.0.0.1 only, on a port the system picks - never
8765 nor 8000, where the owner's servers may be running.

**Still on the owner's side at deployment**: his .env lines (DEPLOY.md
section 5), `migrate_tenants` for accounts 0002, and the old single-mode
folders still inside the code folder (`db.sqlite3*`, `media/`, `private/`,
`backups/`, `imports/`, `scraped_invoices/`, the `.bak_20260928` copy) -
gitignored and never served, to be archived outside the code tree.

## The testing contract

This codebase has a specific history: nearly every bug found in it has been
**silently wrong money**, not a crash. Three parsers dropped real charges
(a social-security levy, excise duties, packaging deposits), a FIFO
valuation priced a €40 count at −€140, a fuzzy matcher merged
"RICARD 45D 1.5L" into "RICARD 45D 1L". None of that raised an exception.
None of it was visible on screen. That is what the tests are for.

**Every change ships with tests.** Concretely:

| You wrote | You owe |
|---|---|
| A new parser | A `PdfPage` fixture per layout quirk, in `invoices/tests/` |
| A new route | A smoke-GET in `tests/test_views_smoke.py` |
| A new formset | A payload-shape test with **non-contiguous indices** |
| New pure logic | Unit tests, including the zero/None/negative edges |
| A bug fix | A test that **fails before** the fix and passes after |

Write the failing test first. A fix without a test that demonstrated the bug
is a fix you can't prove, and this project has already re-broken the same
thing three times.

### Parsers: never open a PDF in a parser

`InvoiceParser.parse()` does all the pdfplumber I/O and hands
`parse_pages(pages, date_hint, source_name)` a list of `PdfPage(text,
tables)`. Subclasses implement `parse_pages` **only** — that's what lets
every parser be tested from hand-written pages with no PDF file at all,
which matters because real invoices carry IBANs and delivery addresses and
must stay out of git. `invoices/tests/test_parser_contract.py` enforces it.

Fixtures are **structurally faithful, data invented**: copy the real column
positions, separators and quirks exactly; invent every name and amount. Get
the structure from a real PDF first (`pdfplumber` in a scratch script) — a
guessed fixture tests a layout that doesn't exist. Metro's "②" footnote
marker is a real example: substituting a plain "(2)" silently changes which
store number the regex picks up.

### The electronic invoice: the figures are data, not a reading

Since **1 September 2026** every VAT-liable business in France must be able
to RECEIVE its invoices electronically, and from **1 September 2027** a small
business must issue them that way too. The mandated formats are the EN 16931
ones: **Factur-X** (a PDF/A-3 carrying an XML attachment), **UBL** (OASIS
XML) and **CII** (UN/CEFACT XML, which is what Factur-X embeds).
`invoices/einvoice.py` reads all three.

**This is the biggest accuracy win in the codebase, and it is a subtraction.**
Everything else here INFERS: OCR reads a photograph, a regex finds an amount
in a column, and the whole `parse_checks` apparatus above exists to catch the
inferences that are wrong. An EN 16931 invoice STATES the number, the date,
the seller's SIREN, every line, the VAT breakdown and the totals. A Factur-X
PDF read by the ticket reader is a document whose exact figures were sitting
inside it, thrown away and replaced by a guess. So the e-invoice check
belongs **before** everything in `receipts.import_document`, next to
`ocr.text_layer_pages` - the other place that looks at a file and decides.

**Fetching from the « plateforme agréée » is out of scope.** The owner's
accountant holds the platform and its name is not known. There is no API
here, no source kind, nothing contacted. What the reader does is read these
documents exactly **however they arrive** - by e-mail, from a portal, in the
folder import, or downloaded by hand from the platform. Measured on the real
data (read-only, 22/09): none of the PDFs filed carries an embedded XML yet,
so every fixture is hand-written and there is no corpus to measure against.

**It is not an `InvoiceParser`.** `test_parser_contract.py` forbids a parser
from overriding `parse()`, which is what keeps every layout testable from
hand-written pages. This reader works from bytes rather than a layout, so it
is its own module, the way `ocr.py` is: `embedded_xml(path)`,
`looks_like_an_invoice(data)`, `read(data)`. Pure - no database, no model,
no request, nothing on the network. No new dependency either: pdfminer.six
(pdfplumber's own, opened directly - a pdfplumber document makes every page
as it closes) resolves the PDF attachment, and `xml.etree.
ElementTree` plus `zlib` do the rest. **Do not add pypdf or pikepdf, and do
not use lxml here.** lxml is installed ONLY as pyHanko's dependency (the
timesheet signatures, `staff/signing.py`, 28/09); this reader keeps
`xml.etree.ElementTree`, which every refusal below - the DOCTYPE guard, the
encodings - is written and tested against.

`ParsedInvoice.einvoice` (an `EInvoiceFacts`, `parsers/base.py`) is set by
this reader and by nothing else, so `parsed.einvoice is not None` is the one
question worth asking downstream: it means the figures are the document's own
data. Everything the shared shape has nowhere to put - the syntax, the
profile, the seller's name, whether it is a credit note, whether it carried
lines, the currency, what the document-level charges were for - lives there.

Six rules, each of them somebody's money:

- **A rate is a percentage in the file and a fraction in the database.** The
  XML says `20.00`; `InvoiceLine.vat_rate` holds `0.2000`. Read across, every
  cost downstream is a hundredfold wrong.
- **A credit note states its amounts POSITIVE.** Type 381 (CII
  `ram:TypeCode`, UBL's `CreditNote` root; 261 and 396 too) means money going
  back, and this codebase writes that as a negative count AND a negative
  amount. The reader signs everything once - lines, VAT breakdown, total,
  adjustment - so nothing further down has to remember which kind of document
  it is on. The **unit price stays positive**: it is what one of them costs,
  and negative beside a negative count it would price the return twice. A
  charge supplier needs nothing extra - `charge_reading` rebuilds its
  count-of-1 negative-amount lines from the negative VAT table. **Some
  senders state a credit note negative already** (OVH's AFR1176742,
  29/09/2026: a 381 whose GrandTotalAmount is -3.92): signed again it was
  filed at +3,27 €, a refund read as a purchase. The sign is taken from the
  stated total (BT-112, else the HT bases): a credit note always ends as
  money going back.
- **Document-level allowances and charges** (BG-20/BG-21) are duty,
  eco-participation and discounts, and they become
  `Invoice.reconciliation_adjustment` - charges positive, allowances
  negative. A sender may state **only the totals** (BT-107/BT-108) without
  listing the blocks behind them; read from the blocks alone the adjustment
  was 0 and the invoice was filed short of what it charges, with its own
  total check still passing.
- **MINIMUM and BASIC WL carry no lines at all** - the totals and the VAT
  breakdown only. That is a VALID invoice, not a failed reading: it goes down
  the total-only path (`charge_reading`) and the page must SAY so
  (`NO_LINES_CHECK`, **passing**, naming the profile). A failed check there
  would park every such invoice in « À vérifier » for ever with nothing
  anybody could do about it. A document that DID carry lines and lost one is
  the opposite case and gets its own failed check (`LINES_READ_CHECK`): one
  is exact, the other is a hole, and they must never read the same.
- **A check failing here is the SUPPLIER's arithmetic**, not a misreading -
  reported with both figures and never repaired. They are run on the figures
  exactly as the file states them: round 12.345 to 12.35 first and the check
  blames the supplier for this application's own storage. Storage rounds half
  away from zero, as everywhere else (`total_ht` 2 decimals, `unit_cost_ht`
  4, `quantity` 3, `vat_rate` 4).
- **The seller is named through `invoices/identifiers.py`, not a second
  matcher.** Its SIREN and VAT number are written into
  `ParsedInvoice.source_text` in the shape that module already reads (the
  word SIREN or SIRET beside the digits is load-bearing: a bare run of nine
  digits is a phone number to it otherwise). Three things stay OUT of that
  text, each a supplier it would name wrongly: the **buyer's** company number
  (the bar's own, on every supplier's invoice); the **profile URN**, since
  `urn:cen.eu:en16931:2017` reads as the web site « cen.eu » - a figure EVERY
  electronic invoice carries, which would be learned by whichever supplier
  filed enough of them first; and the **file's name**. And the label follows
  the digits: BT-30 can be a GLN, and « SIREN » in front of one is a lie.

Two more that cost real debugging:

- **A document may state its VAT twice** - BT-110 in the invoice's currency
  and BT-111 in the currency the seller accounts for tax in - as two
  identical elements in either order. `currencyID` is what tells them apart;
  taking the first put a foreign figure beside French ones and failed the
  total check on an invoice that adds up (same trap on UBL's `cac:TaxTotal`).
- **The name tree is a tree.** Attachments live in the catalog's
  `/Names /EmbeddedFiles`, which splits into `/Kids` as soon as a file
  carries a few; walked one level deep, the invoice of a multi-attachment
  PDF is simply not there. `/AF` is read too. **Which attachment is the
  invoice is decided by its root element, never by its name** - ZUGFeRD says
  zugferd-invoice.xml, XRechnung xrechnung.xml, and a sender may say
  anything.

**The XML comes from outside**, so the refusals are part of the contract,
each a French sentence and never a traceback or a hang:

- a **DOCTYPE or ENTITY declaration** is refused before anything parses it.
  `ElementTree` expands internal entities, so that is the billion-laughs and
  the external-entity door at once, and an EN 16931 instance never carries
  one. **Do not swap in a parser that resolves entities.**
- a **currency other than EUR** is refused, never converted: a conversion
  rate is a decision nothing here is entitled to take.
- **well-formed XML that is not an invoice** (and a home-made `<Invoice>`
  with no UBL namespace - the root element is more than its name).
- **bytes that are not XML**, and anything **over the cap**. What comes out
  of a PDF attachment is decompressed with `zlib`'s own bound rather than in
  one go: a few kilobytes of deflate expand to gigabytes, and the cap is what
  makes that a message on the import rather than a machine that stops
  answering. An oversized attachment beside a readable invoice costs nothing.

`EInvoiceError` is a **ValueError** on purpose: `receipt_batches` already
reports a plain ValueError as one file's error and carries on with the
folder, which is what a broken or hostile attachment deserves.

**How it arrives, and where it lands.** `receipts.import_document` asks
`einvoice.document_xml` **first**, before the text layer, before
`document_supplier` and before any OCR: a Factur-X PDF carries a text layer
and prints a perfectly readable page, so every other order ends with its
exact figures thrown away and guessed at again. `document_xml` answers for
both shapes a document arrives in - the attachment of a PDF, or **the XML on
its own**, which a platform or a supplier may simply forward. So `.xml` is
one of the things the one import takes (`forms.RECEIPT_EXTENSIONS`, the
single-file upload, the folder scan, and an e-mail source's
`attachment_pattern`, whose default widened to `\.(pdf|xml)$` - only the
exact old default was rewritten, migration 0032; a pattern someone tuned
says something a migration does not know). A well-formed XML that is not an
EN 16931 invoice is **one file's error, in French**: there is no page to
photograph and no text layer to fall back on, so the alternative to a
sentence is a traceback out of a PDF renderer.

`Invoice.einvoice_format` - "Factur-X" (the XML came attached to a PDF),
"CII" or "UBL" (it arrived on its own), blank for everything else - is the
stored answer, because the lists that have to know are database queries
(`workspace.py`). **Factur-X is the PDF around a CII invoice**: a CII file
on its own is CII, and calling it Factur-X is a claim about a file that
never came.

- **It is not a receipt.** `Invoice.is_receipt` excludes it although it
  carries `parse_checks`. A check is what makes a document a receipt, and as
  one this would be queued to be re-typed from a photo it has not got, read
  again through OCR by « Relire le document », and have its exact figures
  replaced by `recheck_after_review` on validation. `IS_TICKET`,
  `IS_INVOICE`, `TICKET_TO_CHECK` and `Invoice.waiting_check` all ask.
- **What it cannot be used as it stands goes to « Documents à corriger »**
  (`DOCUMENT_TO_FIX`), never to the ticket queue: its own totals not
  holding is the SUPPLIER's arithmetic - nobody can re-type that - and an
  absent date puts it outside every valuation and the bank match. Both
  reach `error_message`, which is what that list reads.
- **Its checks say what it is and nothing about a reading**: « Facture
  électronique (Factur-X) » first, the document's own arithmetic under it,
  and « Frais et remises sur la facture » when BG-20/BG-21 put money outside
  the lines - the reasons in the sender's own words beside the amount, since
  a figure with no reason is one nobody can check. **A supplier of charges
  keeps the charge's own checks** (`charge_state` rewrites them from every
  path that touches a charge, and would drop these); there, only
  `einvoice_format` says what the document is, and every screen reads that
  rather than a label.
- **A profile with no lines takes the total-only path for goods too.**
  `charge_reading` rebuilds one line per rate from the table MINIMUM does
  state - filed as it stands the invoice would be worth nothing, out of
  every total and of the bank match - and a credit note's rebuilt line goes
  through `credit_as_return`, or a count of 1 at a negative amount books
  stock at a negative unit cost.
- **The supplier is the one the invoice names.** `receipts.einvoice_supplier`
  asks `identified_supplier` (the stated SIREN and VAT number) **before**
  `recognise_shop`, the other way round from a ticket: a header is a guess
  about printed words two companies can share, and this is data. The header
  and the tills are still asked when nothing stated names anybody. Nobody
  named is nobody guessed at - `UnrecognisedShopError`, and the file waits
  for a person exactly as a PDF of nobody known does. What it states is
  **learned at import** (`learn_identifiers`), as a PDF's text is: a stated
  SIREN is the strongest thing a document ever says about its sender.
- **The file kept is the one received** - the Factur-X PDF or the XML: it is
  the legal invoice, and `source_sha256` still answers for a folder scanned
  again. « Relire le document » reads that file's XML again
  (`_reread_einvoice_file`) and never a parser, which would be handed an XML
  to open as a PDF. **So does every other path that reads a document
  again**: a supplier ticked « charges » (`redo_as_expenses`) and a document
  moved to one (`move_documents`) go through `importing._read_again`, which
  handed the ticket reader the summary `source_text` - OVH's FR80644402,
  22,62 €, came out at 20,00 € at 0 % (« 20.00 % » read as the total), and
  Total Energie's 114005409336, 228,07 €, at 0,05 € (01/10/2026). It reads
  the XML of the stored file now (`_einvoice_again`).
  `manage.py tenant <espace> reread_einvoices` reads again every e-invoice
  filed at a total its XML does not state (`--dry-run` first); one whose
  lines a person corrected kept the stated total and is left alone.
- **`Invoice.total_ttc` is the total the invoice states** (BT-112) while the
  lines are within a cent a line of it: they are stated in HT, and 169,00 at
  20 % beside 25,20 at 5,5 % works back out to 229,386 where the invoice
  says 229,39. Past that slack somebody has edited the lines, and what they
  now say wins.
- **A check on the correction page counts what the lines do not carry.**
  `receipts.adjustment_counted` adds `reconciliation_adjustment` (TTC) to
  the lines for a document that is not a ticket, in the saved check and in
  the page's script both (`data-adjustment`) - left out, an invoice
  balancing to the cent read as failing its own check, and turned red at the
  first keystroke. Never on a ticket, where that figure is the cents each
  line lost being divided by (1 + rate).
- **The correction page must not pretend it was read**: it says the lines
  are the invoice's own data, and for an XML with no page to frame it shows
  what the document states beside them. They stay editable - a person may
  still disagree with a supplier - and nothing here is validated the way a
  ticket is.

**A figure wider than the column behind it is refused, at the door.** SQLite
accepts it without a word and Django's decimal converter then raises
`decimal.InvalidOperation` on every **read**, which puts the document beyond
the reach of the application for good: it can no longer be opened, corrected,
re-read **or deleted**, and Achats, Marges and its own page all return 500.
Only raw SQL gets it out. So `einvoice._fits` bounds every Decimal against
what stores it - `MAX_AMOUNT` (12,2), `MAX_ADJUSTMENT` (10,2), `MAX_UNIT`
(10,4), `MAX_QUANTITY` (12,3), `MAX_RATE` (5,4, i.e. 999,99 %) - and the
refusal names the figure, in French, the way a foreign currency is refused.
Never a truncation: ten billion euros on a bar's invoice is the supplier's
arithmetic, which this module reports and never rewrites. The cliff is the
column exactly; 9 999 999 999,99 is read. **A name is the exception** - it is
cut to 255 (`einvoice._short`), because a name is not money and the exact
figures beside it are worth keeping. Stored whole it went onto the line, onto
a new `Product` and onto the page, and past ~50 000 characters SQLite's own
LIKE limit turned the import into an English `OperationalError`.

**`decimal.InvalidOperation` and `decimal.Overflow` are ArithmeticErrors, not
ValueErrors.** `1E+500` and `1E+999999999` both parse as Decimals and explode
on the first sum, so they went past every handler the import has and reached
the owner as a traceback. `read()` catches `DecimalException` around the
assembly and re-raises an `EInvoiceError`. The contract - **every refusal is
an `EInvoiceError`, which is a `ValueError`, carrying a French sentence** - is
pinned over every hostile fixture in `test_einvoice_limits.py`.

**The encoding is decided BEFORE the DOCTYPE guard**, which is a byte grep:
in UTF-16 `<!DOCTYPE` is `b"<\0!\0D\0…"` and the grep misses entirely while
expat reads the BOM and expands every entity. A 2 KB billion-laughs came back
with a one-million-character seller name; what stopped the machine was
libexpat's own amplification limit, not this code. An EN 16931 instance is
UTF-8, so a wide encoding (BOM, NUL in the first four bytes, or a declared
utf-16/32) is **refused rather than decoded**.

**The attachment cap belongs on what comes OUT of a stream, not on what went
in.** `MAX_ATTACHMENT_BYTES` bounds the compressed bytes, which bounds
nothing: 8 MB of double-deflated zeros is terabytes. Every inflate is bounded,
**each stage of a chain**, and a stream behind a `/DecodeParms` predictor or
any filter outside Flate is **not decoded at all** (`_Undecodable`, skipped) -
no Factur-X producer emits one, and pdfminer builds the whole result before
anything can measure it. Measured before: 148 MB of memory for a 970-byte
file, and 396 seconds of CPU for a 66 KB one, single-threaded in the folder
import.

**BT-114 is part of BT-112's identity**: BT-112 = BT-109 + BT-110 + BT-114.
Ignored, an invoice that balances to the centime is filed as its supplier's
arithmetic failing, in « Documents à corriger », where nobody can do anything
about it - a false accusation, which is worse than a silence.

**BG-20/BG-21 carry their own VAT rate** (BT-96/BT-103), and it is stored:
`Invoice.adjustment_vat_rate` (migration 0033, null everywhere else).
`adjustment_ttc` uses it when it is there and goes on deducing the rate from
the lines when it is not - which is all a supplier's PDF or a till receipt
offers. Deduced on an e-invoice, 100,00 € of duty at 20 % was taxed at the
5,5 % of the soft drinks beside it: a 1 175,00 € invoice filed at 1 160,50 €,
unmatchable against the bank, understated in « Marges », **and not one failing
check** - every check works on the stated figures and those all balance. Several
at different rates blend into one rate, since that is the shape
`reconciliation_adjustment` has. **BT-113/BT-115** (already paid, left to pay)
are said in `source_text`: the purchase is the whole invoice and the bank will
only ever show the remainder, so the document carries its own reason for not
matching.

**The sign guard runs over EVERY line, not only the rebuilt ones.** A count of
1 at a negative amount is how a charge takes a credit and how goods book stock
at a **negative unit cost** - the FIFO valuation's worst known failure, which
`LineCorrectionForm` refuses outright for a supplier of goods. An ordinary
invoice (380) states that shape itself, as « REMISE COMMERCIALE 1 × -60,00 »,
and the importer was filing a row its own form would reject. `credit_as_return`
now passes over `parsed.lines` whatever produced them. The **mirror** shape - a
negative count at a positive amount - is neither a purchase nor a return and
nothing can decide which it meant, so it is a **failing** check
(`LINE_SIGN_CHECK`) naming both figures: the supplier's arithmetic, reported
and never repaired.

**A date outside 2000-today is said on the document, not refused**
(`receipts.einvoice_date_problem`, beside the absent-date branch and using
`forms.EARLIEST_DOCUMENT_DATE`). 01/01/0001 put an invoice in no window, no
valuation, no bank match and no margin - and, with `error_message` empty, in
no queue either. The invoice is real and its figures are exact; it is the date
that has to be typed in.

**`receipt_batches._record_import` runs INSIDE the try.** Its first act is to
format the invoice's total, and in the `else:` of that try - outside every
handler - a figure that cannot be read back escaped `_read_file` and marked
the whole **batch** failed: the file that blew up got no outcome at all, and
the files behind it were never read. One bad file is that file's error.

**« lignes » means the lines.** `receipts.lines_check` prints the lines alone,
then the adjustment, then the two added up (`lignes 120,00 € + 14,40 € de
frais facturés globalement = 134,40 € / total de la facture 134,40 €`);
folded into the first figure and announced beside it, the sentence read
« lignes 134,40 € + 14,40 € / ticket 134,40 € », which adds up nowhere. And
**« ticket » only on a receipt**: on an e-invoice and on a supplier's PDF
nothing was printed and there is no ticket. The page's script says word for
word what the saved check says (`document_review.html`), or the two disagree
at the first keystroke.

**What the screens must say**, each of them measured saying something else:
« **Avoir** » where the stated total is negative (Achats, the document page,
the correction page - the word existed only inside `source_text`, which is
never shown for a PDF); a **MINIMUM profile's line was rebuilt from its VAT
table** (« reconstituée depuis sa table de TVA ») and is the invoice's total
rather than an article, said where
the lead paragraph called every line the supplier's own declaration; the
document page's pill is **`review_state`**, the same answer every list gives,
and never `get_status_display` (« À vérifier » on a document an e-invoice can
never be); « montants issus des données de la facture » is **conditional on
`error_message`**, since where BT-112 disagrees the page shows the lines; the
VAT table on an e-invoice is **not something to copy out** (« rien n'est à
recopier »); « non lue » on a
missing date becomes « absente de la facture »; and `error_message` is
repeated on the correction page, which never said why the document was sent
there.

**Nothing in the application fetches from the « plateforme agréée », and the
application says so.** The Sources tab and the import card both carry the
sentence: since 1 September 2026 the invoices arrive through an accredited
platform, the owner's is held by their accountant, and MarginMate does not go
and get them - they are downloaded there and dropped in « Tickets et
factures », where they are read in their own data. Reading them exactly was
easy to mistake for « the e-invoicing side is handled »; it is not, and a
screen that does not say so leaves the owner believing it.

Measured on the real data (read-only, 22/09) there is still **not one** of
these filed: not one of the PDFs carries an embedded XML, and every stored
documents `.pdf`. So nothing here has been proved against a real invoice -
the first one that arrives is worth opening beside its page.

Fixtures are in `invoices/tests/einvoice_files.py`, hand-written: namespaces,
element names, nesting and attributes (`schemeID="0002"`, `format="102"`) are
the standard's own because that is what the reader keys on; every name,
SIREN, number and amount is invented. There was nothing to copy and there
must never be. The PDF/A-3s are **built by the test**
(`tests/pdf_files.py::write_pdf_with_attachments`, a few hundred bytes of PDF
syntax with an `/EmbeddedFiles` name tree): a test that needs a binary
fixture it cannot build is a test nobody can fix, and a real Factur-X invoice
would carry a supplier's IBAN into a public repository.

### Facturettes: the input is a photograph

The small-shop receipts (Franprix, Monoprix, Sabbh Oriental, Wing Seng) are
phone photos with **no text layer at all** — `pdfplumber` extracts an empty
string from every one. A PDF that does carry text (a web shop's invoice) is
read from it and never OCR'd (`ocr.text_layer_pages`): two Nisbets invoices
read as seven characters because the OCR was given their logo, the only
image in the file, for the page - an embedded image is the page only when it
covers it (`ocr.covers_page`). `invoices/ocr.py` stands in for `extract_text()`, and
`parsers/receipt_base.py::ReceiptParser` is the only class allowed to
override `parse()` besides the LLM fallback. The ticket reader still
implements `parse_pages` **only**, so every layout is still testable from
hand-written text with no photo and no OCR engine — `test_parser_contract.py`
enforces that the override lives in the base and nowhere else.

**One reader for every till** (`parsers/generic_receipt.py`). The four shop
parsers were replaced by one that reads a line for what its numbers do; a
shop is data (`TicketShop`: header patterns, the placeholder name its till
prints, whether its items carry a VAT code), registered once per supplier.
Measured against the 368 tickets a person had checked (`eval` against their
stored lines, on a scratch copy of the database), it disagreed on 5 where the
shop parsers disagreed on 21 - each of the 5 a person's shortcut (a quantity
left at 1, a refund typed as a price) or something the photo lost, and said -
and passed every check on 361 tickets against 336.
What it knows, all arithmetic:

- a **count** is the integer that multiplies a unit price into the amount
  ("8 X 1,89 5,67" is 3: the money wins, and "Quantités recalculées" says so);
  a **detail line** ("2x 0.50EUR", "BRUTWEIGHT 0.920 KG / @3.49 / KG")
  belongs to the neighbouring item whose amount it explains, and makes the
  amount of a name standing above it when that faded;
- a **cancelled item** is a negative amount under the same name ("NUL
  LIGNE"): the pair goes. A negative amount under another name is a promotion
  on the item above; one that cancels more than the item cost leaves a refund
  line (quantity -1);
- **the items are the longest run that adds up** - to what was paid, or to a
  pre-discount total printed after them. When the ticket *proves* a promotion
  (its amount printed twice between that total and the amount paid, see
  `printed_promotion`; change printed after the amount paid never counts),
  only the pre-discount total does: two loaves making exactly what was paid on
  a "3 pour 2" ticket are a coincidence;
- a **repair** is tried only when no run adds up, towards a proven total, and
  kept only when exactly one amount changed makes it: an item priced like its
  namesakes (0,45 among loaves at 0,49), or an amount whose leading digit was
  a VAT code ("120.30" for "T2 0.30"). Said under "Montants recalculés".
  **Never down to 0,00**: no line read keeps an amount of 0, and where the
  document's base and tax were read as lines they already make the total -
  zeroing the purchase was then the one repair that added up ("240,00" filed
  as "0,00" beside « Total net HT » and « Total TVA », every check passing).
  A zero still COUNTS as a way to add up (`_repair` refuses it only once it
  is the one found): dropped from the count, a coffee's cut leading digit
  passed for the only repair beside a voucher line making the total too;
- a **row printing its own tax** (Monoprix's invoice layout: unit HT, count,
  HT, rate, VAT, TTC) carries its rate and HT, and its TTC is worked out from
  the printed rate when unreadable ("0 63e");
- **rates**: a row's own, else the bucket its code's items add up to, else -
  uncoded items on a two-rate ticket - the one split of the items that makes
  both buckets. Nothing proven: 5.5% (the shops sell food) and "Taux par
  article" fails;
- lines read but left out of the run (a header, a total misread) are listed
  under "Lignes écartées", in case one was an item; a promotion printed under
  an item never goes beyond its price; a phone number is 0 and nine digits
  (the pairs pattern it replaced took "10.49 31.47" for one, and dropped the
  line); a percentage stays in a name ("FROMAGE BLANC 20% MG");
- a **table row** - a DIY store's invoice, a web shop's - is a code, the
  name, an EAN (told by its check digit: kept on the line, out of the name), a
  count, prices and, last, the row's own rate as a plain number ("20,00").
  **Patterns, never names or column positions**: a column missing changes
  nothing. A count can be a **fraction** (0,35 m² of plywood;
  `InvoiceLine.quantity` is a decimal, shown through the `quantity` filter or
  `common.plain_number` - never raw, "4.200"), except beside "kg", where it is
  a weight, and a fraction is never "recounted";
- a **document priced in HT**: the rows making the VAT table's HT base *to
  the cent* are the purchase when their TTC, worked out, is what was paid and
  the other reading is no better (`_prefer_ht` - including when the run making
  the amount paid is the VAT table's own base and tax, read as two lines).
  A VAT row may print its rate as a bare "20,00" (`unmarked_vat_row`: base x
  rate is the tax, and base + tax is printed elsewhere), or no rate at all - a
  web shop's order page printing the goods' value and the tax
  (`untabled_vat`): that stands for a one-rate table only for HT rows printed
  above both lines, and says so ("Taux déduit"). Two items of 10,00 and 2,00
  on a ticket fit the same arithmetic, and must stay TTC at no proven rate;
- **read past a total** none of the items makes (`_segment`): an invoice and
  the till's ticket on one photo, the ticket's half misread. What is found
  further down has to be a table (`_structured`) - after a total, "CB 0,98"
  alone also makes what was paid. Further down, only rows saying what makes
  their amount count, so a charge included in one of them ("Dont éco-part
  DEEE 0,02") no longer breaks the run;
- a line opening on **« dont »** ("of which") prints part of the item above
  and is **never an item** (`INCLUDED_RE`): "Dt Ecopart. unit. EcoMob 0.72"
  under a tool box - Leroy Merlin's till abbreviates it -, "- Dont DDS 0.20"
  at Mr.Bricolage's. Read as items, those eco-participations put four of
  Leroy Merlin's seven tickets wrong: the lines 0,92 € over what was paid, an
  included cent passing within the tolerance (6,00 € for 5,99 €), a shelf
  whose sub-total no longer restated it and came out as the item instead. A
  **sub-total** is stepped over when it restates every item read or **those
  since the last one** (that till prints a sale in blocks, each ending on its
  own) - never a row printing what no sub-total prints (a VAT code, an EAN,
  a count and its unit price), nor a row with no name of its own that
  completes the name printed on the line just above it: a pack of four
  after two packs of two costs what they do, and so can a worktop's cut on
  the store's own reference (no EAN); stepped over, either left the block's
  sub-total to be filed as the item, and every check passed. That row
  prints its reference beside the amount: an amount **alone** under a name
  is still a sub-total, printed on two lines ("SOUS TOTAL" / "9.00") - kept
  as the item « SOUS TOTAL », it made what was paid with the next sale's
  items, silently too. **A size in
  one word** ("76X46", "21X35", "16*25") is no count, unless its two numbers
  multiply into an amount the line prints ("TASSE 2x4 8,00" is two at 4,00:
  `_is_size`, the money decides): read as 76, a rug's name became a detail
  line and its price went with it. Measured on the 627 documents the reader
  reads (scratch copy, 19/09): the seven tickets match their checked lines
  (three before), 470 documents match against 468, and the three no longer
  matching are checked lines keeping an old misreading - a slate board filed
  as "Total" (its ticket prints 2 x 7,95), 16 clamps and 21 battens that are
  a size. The sub-total's row guards and the amount guards change none of
  those readings;
- a **row printing only a product code** takes the name, and the count, of
  the line that named that code above it (`_take_linked_name`): an
  electronics till prints "1  5550001-ENCEINTE PORTABLE XL", then
  "5550001  120,00 €  A  100,00 €  120,00 €" three lines further down;
- a **run that is the document's own base and tax** read as lines of their
  own (a base and a tax per rate) is not the purchase (`_is_vat_table`) -
  **and does not hide it**: with fewer rows than those lines (one row, one
  row per rate) a shorter run makes what was paid too, and the first
  segment's search goes on to it (`_fitting_run(refuse=)`), every line of it
  reading as a row (`_looks_like_a_row`). The search used to stop at the
  refusal: the row was then filed beside the total lines, or repaired to
  0,00 beside them. Rows only, because past the refusal the totals are known
  to be among the items: one rate's base and tax beside the other rate's row
  make what was paid as well, every check passing; a line alone restating
  the total too. And **last**, where no reading in HT holds (`_segment`'s
  `rows_above_own_totals`): offered as the TTC reading beside rows in HT, a
  recap printed at the top ("Total HT 200,00  Total TTC 240,00", two figures:
  a row) took their place. Later segments keep their own rule (explained
  rows only);
- a **discount printed again in HT** under the one already taken is the
  same discount, and a row whose discount line says what it was taken off
  ("sur 11,76 soit -1,76" under a row of 10,00) is already net of it;
- an **amount in brackets that is the one before it excluding tax**
  ("Abonnement 19.99 (16.66)") is that amount printed twice, not two; a
  **date spelled out** ("19 mai 2026") is the document's own, read before the
  day it says it will be debited; and past a total, a run has to say
  something the total does not - a table, or a price and the discount under
  it making what was paid (`_priced_detail`, a phone bill printing its
  totals first and its lines below them). A line whose amount is the
  document's HT base, printed alone or twice, is that total restated, not a
  row: a row has more figures than its amount (`_looks_like_a_row`);
- a **percentage is never an amount**, whatever sign is in front of it;
  "100X35X2.5" is a size, not a count of 100; a **quantity column of one**
  between a price and the amount it makes is a count; and a number in front
  of a name is a count when the ticket says how many articles it sold
  ("3 ARTICLE(S)", `_count_from_articles`). A count in front of the name
  stays **out of the name** (`_without_leading_count`), or the same
  champagne bought by six and by twelve is two products that never meet -
  as a whole number only: cut as a prefix, a count of 2 took the first digit
  off an article number ("2000123"), on 19 rows of Metro's invoices when the
  ticket reader reads them;
- a **row printing its price and its amount both ways**, HT and TTC, with no
  tax column ("7  44,45  53,34  311,15  373,38") is read by `_ht_ttc_row`:
  nothing on it adds up, so what proves it is one French rate turning both
  prices into both amounts and a whole number of them making the amount -
  in HT only, since a unit price is rounded before it is multiplied (10
  bottles at 30,78 TTC print 307,85, not 307,80). Without it those seven
  bottles came out as one at 311,15, and the unit cost is what every value
  downstream is divided by (figures invented, as in the fixtures);
- **such a row printing its rate ("… 20,00%") is never the VAT table's**
  (`_priced_by_count`, in `_read_line`): its figures fit a VAT row twice
  over - alone on an invoice, its HT and TTC are the document's base and
  total, and six bottles at 20 % (eleven at 10 %) print a unit TTC equal to
  their tax. The columns read it when there are positions; read as text
  (« Relire le document », `reread_receipts`: every stored reading) the row
  was taken for the table's and the totals printed under it filed as the
  purchase - loudly, the sum failing, and reading it again gave the same. No
  VAT table multiplies a price by a count. A row of ONE still reads as the
  table's (`_ht_ttc_row` asks for two at least). Measured on every stored
  reading of a scratch copy (30/09), as text, with positions and from a
  digital document's text: no reading changed, and the only lines no longer
  taken for VAT rows are a water bill's consumption rows (a count of m³ at a
  four-decimal price) - which never were;
- a **bucket of VAT covers the whole document**, so between two readings of
  one rate the larger base is the table's (`_better_bucket`): a water bill
  whose every row prints its own tax states its real table fifty lines
  below, and the subscription's row - read first - made its 27,42 € the
  260,63 € the bill charges;
- a **tax printed beside its rate** ("TVA [20.00%]  2.16") with the value it
  taxes and their sum printed too is a total, each printed once
  (`_taxed_total`): the rate is what makes one printing enough, where
  `_ht_and_tax` searches without one and asks for each figure twice;
- **thousands are grouped**: by a space ("1 011,00"), by a point
  ("1.162,80") or, in English, by a comma ("1,162.80") - one amount, not
  eleven euros and not none at all (`UNITS`: groups of exactly three digits,
  and a point or comma only when the *other* one is the decimal separator,
  or "2.261" and two amounts in neighbouring columns would read as one);
- a **date may be written month first** ("août 03, 2026"), abbreviated
  ("déc. 02, 2025") or in English ("Dec 02, 2024"): a platform billing in
  French dates its invoices in whichever language its template was written
  in, and 26 documents were filed with no date at all - counting in no stock
  valuation and matching no payment. The first date the document prints is
  still its own, so the next billing date below it names nothing;
- a **line holding a date is an item all the same** when it prints amounts:
  an invoice's rows carry the period they cover ("Abonnement 01/08/2026au
  31/08/2026 1,00 64,44 20,00% 64,44"), and thrown away for its date, one
  left the subscription unread with the bill's own "Total hors TVA" standing
  in for it. A line stamped with an **hour** is never an item: a till stamps
  the hour and nothing it sells carries one;
- a **document's number** is the one it prints for itself ("N° document",
  "Commande N°", "Référence interne", "Facture # FR-F0001", "Nº" with an
  ordinal indicator: `DOCUMENT_NUMBER_RES`) and **never an IBAN**, which is a
  long digit run once its spaces are gone and the same one on every invoice a
  supplier sends - read as the number, the second invoice of the year was
  refused as a duplicate of the first. A dash a PDF's own rules leave inside
  a number or a month ("FR-F033—763", "avr—. 26, 2024") is taken back out.
  A number **printed with its date** ("N° 2026100000001 DU 10 AVRIL 2026",
  "n°1400000001 du 19 Janvier 2024") is the document's own, whatever lines
  stand between it and the word « facture »: looked for beside that word
  only, Eau de Paris' and the Freebox bills were filed under a payment
  reference or a number made up from the date and the total - and the
  portal's list, printing the real number, never recognised an invoice
  already imported. `manage.py refresh_document_numbers --dry-run` puts the
  printed number in place of such a stand-in (a date-total or a long digit
  run, never a real number, never one another document of the supplier
  holds, never a supplier with its own reader): 139 documents on 19/09.

Those rules replaced two parsers: measured on every De Poivre and Plou &
Fils invoice filed, the one reader reproduces them line for line - names,
counts, unit prices, rates, dates and numbers - so `parsers/depoivre.py` and
`parsers/ploufils.py` are gone (migration `invoices/0024`, which forgets
their keys) and their layouts are fixtures in `test_generic_documents.py`.
Metro's and UBA's stay: theirs are not one table.

Evaluate a change the same way before trusting it: parse every stored
`ocr_text` and compare with the checked lines, per shop, counting separately
the tickets whose only difference is how a promotion was spread.

The engine is **PP-OCRv6 medium through `rapidocr` 3.x** (ONNX on CPU, about
5 s a receipt; models download into the package on first use, ~30 s once).
It was chosen by a bake-off on the 42 receipts: eight engines through the
same parsers, plus a parser-free score of how many printed amounts and names
(typed out by hand from the photos) turn up in each engine's output.
PP-OCRv4, the previous engine, read 73% of amounts; PP-OCRv6 medium 93%, and
100% of names. Tesseract read 22%. Local vision LLMs (Qwen2.5-VL 3B/7B,
Qwen3-VL) add tax codes that aren't printed, drop the price column, and take
minutes a receipt on a 6 GB GPU. Lines are grouped from each detected box's
own angle and position (`ocr.group_boxes_into_lines`), not by a y tolerance.

**OCR is a guessing machine, so nothing here is trusted.** A misread digit
produces a perfectly well-formed wrong price — precisely the failure this
codebase keeps getting bitten by. The lever is that a till receipt is
redundant: it prints the item lines *and* their sum *and* a VAT table, and
those three have to agree. Each parser checks its own arithmetic against the
ticket's own totals and hands the verdict up as `ParseCheck`s, stored on
`Invoice.parse_checks` and shown beside the photo. **Nothing silently
repairs a receipt that does not add up.** On the 42-receipt corpus, through
the real import path: **42/42 right, 38 passing every check, 0 wrong without
saying so** — which is the number that matters. The four flagged are real:
two VAT amounts cut off or faded, a promotion not tied to a product, and a
Franprix ticket whose free baguette was misread, which only the printed
pre-discount total gave away.

**Anchors are arithmetic, never words.** Where the items stop, what the total
is and whether a promotion applies are found by the numbers:
`printed_total` (the largest amount printed at least twice that the VAT table
confirms), `ends_items` (the first line repeating the amount paid, or the
running sum once it has reached it), `amount_printed` (a promotion counts
only if the ticket prints the items' sum) and `printed_promotion` (a
pre-discount total the items fail to reach means one is missing). The parsers
used to look for "TOTAL", "HORS AVANTAGES", "DUPLICATA" and to repair one
engine's misreadings ("T1Q.49", "1.G0", "1,33t"); on a better engine they
scored *worse* than on the one they were tuned to, because the new one spelled
the words differently. **Do not add a pattern for an engine's misspelling.**
Genuine layout logic is fine; a spelling workaround is not.

Six things are load-bearing:

- **Two sums are checked, not one.** The TTC sum proves the amounts were
  *read* right; the HT sum proves they survive the `/ 1.055` each line gets
  on the way in. Six baguettes at 0.49 TTC are 0.4645 HT each, stored as
  0.46: the ticket says 2.79 and the lines say 2.76. The TTC arithmetic
  balances perfectly, so only the HT check catches it. That gap goes into
  `reconciliation_adjustment`; a gap bigger than rounding fails instead.
- **5.5% is a fallback, never an answer.** Two of the 42 are at 20%
  (cleaning vinegar at Franprix, a discounted line at Monoprix). Reading those
  at the food rate understates the cost by 14% with nothing downstream able
  to tell - so a line priced at 5.5% because nothing proved a rate always
  fails "Taux par article". `read_rate` rejects any percentage France does
  not have — OCR reads the VAT *amount* "0,26" as a rate of 26% given the
  chance.
- **Group lines by geometry.** A price column a row off its name column
  (curl, tilt) gave each item its neighbour's price; two tightly printed
  rows merged into one product at the second one's price. The deskew plus
  box-angle grouping fixed both; boxes that overlap horizontally are never
  put on one line.
- **Franprix items sum to the PRE-discount total.** Promotions print in
  their own block rather than reducing the item lines, so the lines add up
  to "TOTAL SANS AVANTAGES" while only "TOTAL A PAYER" reflects the "3 pour
  2". Eleven of nineteen carry one. The discount is what the items add up to
  minus what was paid — **only once the ticket prints that sum**; taken on
  trust, a misread price would pass as a promotion. It is spread over the
  products the block names — pro-rata across *matching* lines, so a bread
  promotion makes bread cheaper rather than shaving centimes off the lemons -
  and over every reading of that product ("BAGUETTE BLAND", "BLAVC"): matched
  to the one spelled like the block, a 0,50 "3 pour 2" made one 0,49 loaf
  cost -0,01. Never more than those products cost; cents go to the largest
  remainders. The line keeps its printed price, the share sits beside it
  (`discount_ttc`).
- **A Franprix weight belongs to the item BELOW it.** "BRUTWEIGHT 0.920 KG
  @ 3.49 / KG" is the orange's (0.920 x 3.49 = 3.21, the orange's price). The
  parser used to put the kilos on the item above, silently. Arithmetic
  decides; with no legible price per kilo, the item below.
- **The tills disagree about their own VAT tables.** Four shops, five
  layouts, and Sabbh's "Base TVA" column is tax-INCLUSIVE where everyone
  else's is exclusive. `parse_vat_line` settles it by arithmetic — it
  searches the row's plausible readings for the pair that satisfies a VAT
  identity *and* reproduces the printed grand total. Franprix draws its
  table with rules the recogniser reads as digits ("2.261" for "| 2,26 |"),
  and only the grand total separates that from Monoprix's genuinely
  4-decimal HT ("3.0237"). Both identities are tried on every pair: at 2,80
  and 0,15, both hold, and trying the tax-exclusive one alone left five
  one-line Sabbh tickets without a total.

**"Article divers" is a price, not a name.** Sabbh's till prints no product
names at all. `ShopItemPrice` maps a **unit price** (not a line total —
0,70 appears as 3pcs/2,10, 6pcs/4,20, 7pcs/4,90 and 11pcs/7,70) to a
product, and the mapping is applied in `receipts.label_placeholder_lines` at
import time, never in the parser, which must stay free of database access.
An unmapped line is named by its own price ("Article divers (0.70 EUR/u)")
so the review screen is actionable without opening the photo. Recording a
price on the review screen names that line on **every ticket of the shop still
waiting to be checked** (`receipts.apply_known_prices`) - one Pita price left
25 tickets of the queue unnamed when it named only the ticket it was typed
on. Every known price is applied, each as of its own ticket's date, and a
checked ticket is never rewritten (except the one the price was typed on).

### Reading by columns (`parsers/layout.py`)

The generic reader reads a line for what its numbers do. An invoice prints a
TABLE, and read as text its rows give the wrong name ("1 704411 Câble HDMI
2 m"), pass a row printing its own tax off as the VAT table, and file the
footer's "Capital" as a purchase. `ReceiptParser.parse_ocr_pages` now hands
the reader WHERE each piece of text sits (`PdfPage.rows`, one `layout.Row` of
`Cell(text, x0, x1)` per text line, from a PDF's own words or a photo's
boxes) and `layout.find_table` says which rows are a table's header, its
items and its wrapped descriptions, and which columns hold what. A page read
as text alone (`parse_text`, a hand-written fixture) reads exactly as before:
measured on the stored readings of every checked ticket with the positions
left out, the reading is the same but for one DIY ticket read better.

**Three things decide, in this order, and the last one wins.** Alignment:
cells overlapping horizontally across rows are one column (the OCR's own
rule). Vocabulary: a line made of the generic words a French or English
invoice heads its columns with - désignation, référence, quantité, prix
unitaire, montant, valeur, TVA, remise… - over one to three lines, PROPOSES
a role per column. Arithmetic CONFIRMS: the quantity column is the one whose
value times the unit price makes the amount; an amount is HT when its rate
makes the row's tax, when the row's two unit prices are one rate apart, or
when another row of the same column proved it (a column is HT or TTC as a
whole; a row at 0 % proves nothing); a column the header calls "TVA" holds
rates or taxes, and its cells say which. Without a header, two rows whose
columns multiply out the same way are a table too.

**What it never does**, each a bug found:

- name a column from its position;
- take the rightmost figures for the amount because nothing else was found.
  Under a header the amount column is one the header calls an amount, the
  one a quantity times a unit price makes, or the only money column, and it
  has to HOLD the amount of the rows it is for: a column that misses more
  priced rows than it holds is not the column and the page is no table (a
  PDF printing each line as one string of text puts its figures wherever the
  words before them end, and the repo's own hand-written invoice was a
  one-row table with a truncated name, every check passing);
- give a header word to more columns than it has roles (a DIY till's column
  of lone "1"s - the VAT code - became every row's quantity);
- let the description header name a numeric column, or a numeric role's
  header word exclude a name lying under it: the figures bound the name, not
  the words, and a footnoted "2025 (1)" under a row is inside the
  description, an integer, never 2 025 €;
- join a sentence, or a line reaching under the figures, onto a name (a
  one-row table has no row pitch to refuse the shop's terms with);
- read a body line made of header words ("Prix Unitaire Brut HT: 123,45 €",
  a figure run into it or not) as an article;
- trust a header's "HT" alone;
- or decide what the purchase is: the reader's anchors (the printed total,
  the VAT table) go on doing that over the readings the columns hand it.

**The reader keeps the say over what the text logic already knows**
(`GenericReceiptParser._read_items`): a department heading - dots or
"** … **" - with the product's amount glued on belongs to the name printed
under it; a line opening on « dont » is part of the item above; a discount, a
detail, a sub-total inside the table is left to the text reading. A table
with no quantity column keeps the text's count and the name it was cut from
("CHAINE D4 X4" is four), and `name_cell_text` cuts a trailing whole number
as the text reading does, or the same article read two ways is two products.
A row in HT keeps the TTC it prints (`ItemRow.ttc_printed`): 6,64 HT at 5,5 %
does not convert back to 7,00.

**« Tableau reconnu » states what the reader DID, not what the table looked
like** (`LayoutView.report`): the columns found (a column no item row fills
is left out, a doubled role counted), which of the quantity, unit price,
amount and rate actually have a column, how many table rows the arithmetic
kept, the names taken from the text, the rows handed to the text on purpose
(a calculation, a heading), the lines read outside the table - and it FAILS
when a table row was left out by the arithmetic or a body row printing money
was read as text: on a scratch copy of the real data that flagged twice as
many wrong readings that had passed every check as right ones. The check's
detail is rendered verbatim on the review screen, in French.

**Fixtures are positions copied from real layouts, every name, code and
figure invented** - the public repository is audited against the real
strings AND against every real amount printed even once: a docstring ships
like code, and the first version quoted real rows. `find_table` is linear in
the cells times the columns (`_cluster` sweeps left to right;
`_cluster_pairwise` is the reference its tests compare it with): a quadratic
grouping took seconds on a page of a few hundred rows.

### A supplier of charges has no products

A subscription, a rent, a water bill: there is no product behind them and
nothing to classify, so `Supplier.expenses_only` (switched from the
supplier's page, « changer… », behind a confirmation saying what it redoes -
`importing.redo_as_expenses`, reading each document again from the text it
kept; a box on the suppliers' list, then on the Sources tab, saved on a
click, and a POST from a page still showing it now gets the confirmation)
files them by
`importing.charge_reading` instead of resolving products. Its lines land on
`Product.is_expense` products, which reach no stock page, no review queue
and no stock movement; **Produits & charges** shows what they cost in a fold of its
own ("Charges et abonnements", `inventory.views.charge_suppliers`, over the
stock-take window or the last twelve months; outside a period the headline
also gives their twelve months as « Charges (TTC) », beside « Total
acheté ») - charges are not stock, but they are spending.

**`Product.is_expense` follows its supplier**, both ways
(`replace_invoice_lines`, `importing.stop_expenses` when the box is unticked,
`redo_as_expenses` when it is ticked again - not a product a stock item has
claimed, which is stock after all). Set one way only, a box ticked by mistake
was irreversible: unticked, the supplier's products stayed flagged for ever -
out of the review queue, out of every stock page, out of every stock movement -
and correcting the document by hand, which is what the message after unticking
asks for, resolved the very same flagged product.

Three readings, in this order: **the VAT table** when it accounts for the
total to the cent (one line per rate); **the charge items the document names**
(`invoices/charges.py`); **the total alone**, on one line named after the
supplier. What was paid is never the sum of whatever was read as lines - a
rent statement lists the previous balance, the direct debit, the tax and the
rent, and adding those up gives a figure nobody ever paid. A charge whose
total was not read at all is filed with what was read and held in "À
vérifier" (`charge_needs_a_look`): it must not pass for settled.

**Every path that reads a document again goes through
`importing.refile_as_charge`** (`receipts.reread_receipt`, "Relire le
document" for a ticket and for an invoice): read as a ticket, a rent
statement's lines are the previous balance and the direct debit beside the
rent, and a document read again that way went back to being worth what it was
before the charge reading settled it. A charge also **never enters the
review queue** (`receipts.pending_receipts`): there is nothing to type on a
rent, and forty-two of them behind the tickets is a queue nobody works
through - a total that could not be read holds the document in "À vérifier"
with what is wrong written on it instead.

`refile_as_charge` makes the document's charge items inside the savepoint of
`replace_invoice_lines`. A document a stock take was priced from cannot
have its lines replaced (InvoiceLinesInUseError) and is left alone, and its
charge items go back with the refusal. Made before it, a charge item named
after the supplier stayed on no line (scratch copy, 19/09).

**A charge keeps its own checks** ("Total de la charge", "Date du ticket":
`importing.charge_state`). A charge fetched by a portal or the mailbox goes
through the ticket reader first, and `import_receipt` stored that reader's
checks over the ones the charge reading had just set - about lines the
charge reading replaced: Eau de Paris' bills, filed at exactly what they
charge, waited in « À vérifier » under « 5.5 % supposé » and « lignes
310,15 € HT / ticket 303,28 € ». `manage.py refresh_charge_checks
--dry-run` gives the documents filed before that their own checks back
(never one a person validated): 36 on 19/09.

**A charge keeps the amount it charges, tax included** (`InvoiceLine.printed_ttc`,
set by `_expense_line` from the figures the document prints): 33,33 € HT at
20% works back out to 40,00 € where the bill says 39,99 €, and 39,99 € is
what leaves the bank. Everything that shows what a charge cost adds those
up, `inventory.views.charge_suppliers` included.

**A credit on a charge is a line like any other**: a count of 1 and a
negative amount (`LineCorrectionForm`, `charge=`). An electricity bill takes
the month's subscription back at 5,5 % and bills it again at 20 %, printing
-2,76 € HT at 5,5 %; the correction page refused it ("Un retour a une
quantité et un montant négatifs" - the guard against stock worth less than
nothing, and a charge has no stock, bar the one below), so that bill could
not be entered at all - nor two rent statements saved untouched, whose deposit given back the
charge reading files the same way (-3,00 at a count of 1). The count stays
the one typed: nothing reads a charge's count, and at -1 its unit price would
read as a charge. A count of -1 at a positive amount is still refused (the
count says credited, the money charged), and goods keep the guard. Every
total adds the credit with its sign - `Invoice.total_ttc`, the bank match,
the charges fold and its rows - and HT and TTC convert as for a positive
amount, half away from zero. Validated on the page, a charge takes its state
and its own checks from its total, the one just typed included
(`charge_state`, in `views._save_corrections`): kept from the import, "Total
de la charge" went on saying a total typed there was never read, and a
charge saved with none came out settled. The ticket reader still drops the
minus of a VAT row (`amount_candidates` is unsigned): that bill was filed at
+2,76 € on its 5,5 % row, its total unread, and waits for the credit to be
typed.

**Where there is stock, the guard stands** - on a charge too. A product a
stock item claimed stays one when its supplier turns to charges
(`redo_as_expenses`: it is stock after all), and a line on it books a
movement like any line of goods; relaxed on every row of a charge, a credit
typed there was stock at -30 € the unit. So a charge takes a credit at a
count of 1 only while none of its supplier's products is stock
(`views._correction_page`) - the supplier's products rather than the stored
line's, since a row lands on a product by its name once saved, and a row
added or renamed onto the stock item is that item (no such supplier on
19/09). A supplier **leaving charges** turns such credits into returns
(`importing.stop_expenses`, counted on the confirmation page by
`charge_credits`), and so does a document moved from charges to a
supplier of goods (`receipts.move_documents`; both through
`credit_as_return`): the count negative, the amount as it was. Kept at 1, the
goods guard refused the row on a document saved untouched, and classifying
its charge item booked stock at a negative unit cost - on 19/09, the three
deposits given back on two rent statements.

**Every charge opens like a stock item.** The charges fold lives inside
`#catalogue`, so the page's own toggle script reaches it: a row opens on the
documents behind it (each linking to the document it came from, with its
"Corriger") and its 📈 shows what it has cost over time (the same chart a
stock item's price history draws, one point per document, against the
document's own date). The rent is followed month after month like anything
else bought.

**The supplier's own row is one of them.** Only the charge items opened at
first, and a charge item is listed only where a document names several - so
the water, the phone, the alarm, the venue (one charge item each, the charge
itself under another name) had nothing at all to click: five suppliers out
of six. The supplier's row opens on all its documents
(`inventory.charge_supplier_documents` / `charge_supplier_history`), a charge
item's on that charge item's (`charge_documents` / `charge_history`), and both go through
`_charge_document_rows`, which is **one row per document**: a bill printing
two rates is read as two lines, and 29 Total Energie bills showed up as 46.
It prints the tax as an **amount**, never as a rate - on a document with two
of them, naming one would be a lie about the other, and "—" says nothing.

Two things the row and the panel do not share, said on the page rather than
left to be discovered: what a row **opens** is the whole history - except
under « Du … au … », where it is those dates, see below; and **"Dernier" is
the last document ever**, window or not. The column says which window it counts
("Documents (12 mois)", "(période)" for a stock take, "(ces dates)" for a
free « Du … au … » - « période » is this page's word for two physical counts
and says nothing about two dates someone typed) and a row with older documents says
what its count is out of - « 12 sur 33 » (`documents_all`). Read as the
total, "12" is a year of a monthly subscription against the 33 bills the
row opens on, and that is how it was read (owner, 20/09). A supplier is listed as soon as it has
a document at all - windowed, a water bill arriving twice a year dropped off
the page between two of them, taking its history with it, and the date is
exactly what a row with nothing over the window has left to say.

Three traps a review found on the charges fold, each a test that failed
first: the first stock take's window has no start (a None in the filter was
a 500), a curve adds up what one day charged, and a document filed with
nothing read is listed on its supplier's row.

**A charge item is a label and the amount printed after it**, and a
document's charge items are the run of them adding up to an amount printed
below them. That run is what proves the reading *and* settles the total: a
statement puts two columns on one line (the account's history left, this
month's charge items right), so only each line's **last** amount is this
month's; a subtotal printed among them is the run restated and is stepped
over; a minus on its own in front of an amount is its sign; and the tax
charge item - the one that is a French rate of exactly one other - is folded
into the charge item it taxes rather than kept as one. Measured on the 31
rent statements filed, that separates the rent from the building and water
provisions and corrects six totals the reader had taken from the left
column (last month's instalment, printed twice, is bigger than this
month's). **A document is worth what it charges**: where the debit also
settles arrears, those were charged on the notice they come from, and
counting them again would book them twice.

A breakdown is a **block**, and its total is printed at the foot of it
(`MAX_LINES_BETWEEN`): two rows of a consumption table pages apart that add
up to something printed elsewhere are not one, and an electricity bill read
that way turned its 298,05 € into the 67,94 € of its network charges. The
document's **VAT table comes first** all the same (`charge_reading`): where
it accounts for the total to the cent, that is the reading, and the charge
items are only asked when it does not.

**One import for every document.** Tickets and PDF invoices went in through
two cards, and the person importing had to know which; the Achats page has
one now (files or a whole folder, of anything), and the file decides
(`receipts.import_document`): an **electronic invoice** is read from its
own data first (see above), then a photo or a scan is read as a ticket, a
digital document goes through its supplier's own reader when that supplier
has one (`has_own_reader`) and the document says whose it is - otherwise the
ticket reader, which reads an invoice's table too. Nothing is guessed from
the file's name or extension: `einvoice.document_xml` says whether it
carries an EN 16931 invoice, `ocr.text_layer_pages` whether it carries text,
and `detect_shop` who printed it. The import reports each file as what
it became, and links a ticket to its review screen, an invoice to its lines.
`/invoices/upload/` still takes one PDF with its supplier named by hand -
folded under the import card, for a document that says nothing about its
sender, or for the AI pseudo-supplier.

**The review screen is the deliverable, not the parser.** The import card
takes a batch (`/invoices/tickets/`) and detects each shop from its own
header (a Franprix ticket run through the Monoprix parser *would* produce
lines, and they would be wrong — an unrecognised file is reported, never
guessed). `.../verification/` is the queue tab, oldest first;
`.../<pk>/verifier/` puts the photo beside the checks and the editable lines
and moves to the next receipt on save (a ticket already checked, reopened
from its page, goes back to its page). With `?lot=<batch>` it goes through
that import's tickets only and ends on the import; the whole queue ends on the
list of tickets checked recently. Saving goes through `replace_invoice_lines`,
the same path as a hand-typed invoice.

**One correction page for tickets and invoices** (`views._correction_page`,
`document_review.html`): `<pk>/lignes/` is the same page for a supplier
invoice, its PDF framed beside the lines (media may be framed by this site
only, `config/urls.py`); a ticket's `lignes/` redirects to its review URL. Each
row shows the amount **both ways, HT and TTC**, each following the other as it
is typed through the line's rate; the one typed last (`amount_source`) is kept
and the other worked out - checking an HT price against a photo meant
converting every one in one's head. A ticket starts from its printed TTC, an
invoice from its HT. A ticket's **promotion sits beside its price**
(`discount_ttc`), and the live check says "articles X € moins Y € de
remises", which is what the ticket prints as its pre-discount total. The
weight is a field (`total_volume`), and a row of several says what one costs
(`LineCorrectionForm.unit_price_hint`, redone by the page as typed): five
baguettes at 2,45 read like one at 2,45 until divided. A row left as drawn keeps every stored
figure to the cent (`LineCorrectionForm.untouched`) - so a line kept from
before promotions were kept apart stays worked out from HT - and a TTC typed
converts back to its HT to the cent. New ticket lines start at 5.5%, invoice
lines at 20%. **A line taken out stays where it was**, struck through, with a
button to put it back and a count of the lines kept: removed at once, the rows
below moved up under the pointer, and a repeated click silently took out the
first line typed below - a ticket re-typed in full then "did not add up" by
exactly that line. Build test posts from the page (`invoices/tests/page_posts.py`):
a hand-written subset tests a request no browser sends, and several such tests
passed without ever saving.

**A check compares two things, and both have to be on the page.** The lines
and the printed total were; the VAT table the document prints was not, so
"TVA 5,5% cohérente", "Table TVA lue" and "Somme HT des lignes = base HT du
ticket" were warnings nobody could answer - 21 of them still standing on
tickets checked long ago. The table is stored (`Invoice.vat_breakdown`, from
`ParsedInvoice.vat_breakdown` at import) and **typed on the review screen**
beside the total, a row per rate; validating rebuilds every check from what
the page holds (`receipts.recheck_after_review`, `vat_table_checks`). An
empty table asks nothing - a document that prints none is not wrong, and a
failure nobody asked for is the noise this page exists to avoid - and a row
typed half way is refused rather than half read. On the real data that took
21 unanswerable failures to 6, every one of them pointing at a field: three
tickets whose lines and printed table genuinely disagree (one by 1,25 €),
two whose lines miss the total, one charge whose total was never read.

The rate is drawn at two decimals (`_vat_initial`): drawn as "5.500" from
0.055, it failed its own `decimal_places` invisibly on 361 documents, and the
row read « Un taux se saisit… » beside three typed values. Bases and taxes
take four decimals and a sign (a credit, a discount), and a field's own error
is always shown. A table a person saved - even empty - is theirs
(`Invoice.vat_table_typed`, set by migration 0031 on the documents validated
since the table shipped) and is no longer read again from the OCR. An empty
table is drawn with two blank rows (`EmptyVatTableFormSet`): a row is only
added by saving, and with one spare a two-rate ticket took two saves.

**The HT check follows the lines as they stand, like the sum**
(`views._checks_context`, `ht_check`). Validating did rebuild it from the
lines as saved, but under the label the reading writes too, and the page
marked any check carrying a `READING_CHECKS` label "(à la lecture du
ticket)": all 445 documents holding it on 19/09 - three tickets whose
reading failed it passing under that mark once corrected (two Leroy Merlin,
read with each item's eco-participation as an item), and the three still
wrong blamed on the reading. Nor did it move while lines were typed. The
page now works it out from its lines and its VAT table, and the script
follows both as they are typed (each line after its promotion, a cent of
slack a line, as `vat_table_checks`). With no table it is hidden, and a stored one can only
be the reading's - validating without a table drops it - so that one stays
marked as read until a table is typed. What stays "as read" after
validating is about the reading, not the lines: "Confiance OCR", the shop
checks, an unread total nobody typed. It is a **ticket's** (a charge's
too, opened as one): a supplier's invoice never showed nor stored it, and
its lines leave out what the reconciliation adds
(`Invoice.reconciliation_adjustment`, a duty) where the printed base counts
it - a table typed there exactly as printed failed it, on 68 of the 84
invoices carrying one.

The page also **reads the document again** ("Relire le document",
`receipts.reread_document`): the photo through OCR, or the PDF through its
supplier's parser, replacing date, total and lines - corrections included, the
page asks first - and a ticket goes back to the queue. Nothing changes when
there is no file, no reader, or nothing read, and a line a stock take was
priced from stops it. A checked ticket can be put back in the queue ("Remettre
à vérifier"), and a known price forgotten ("Oublier"; the lines it named keep
their name).

**Every document is dated between 2000 and today** (`forms.check_document_date`,
on this page and on a hand-typed invoice). An import still files an undated
document - there is no one to ask - but a ticket gets a failed "Date du ticket"
check (so it waits in the queue) and a PDF an `error_message`; the invoice list
counts them and lists them (`?sans_date=1`).

**A receipt line keeps its printed TTC** (`InvoiceLine.printed_ttc`, set by
the receipt parsers and by this form). HT to the cent does not convert back:
7,00 at 5.5% is 6,64 HT, which is 7,01 - ten pitas at 0,70 read 7,01 on the
screen meant to check them. `InvoiceLine.total_ttc` and everything shown in
TTC use it, less the line's promotion (`discount_ttc`). A receipt line with no
printed amount - imported before promotions were kept apart - is worked out
from HT, and saved untouched it stays so: passed off as printed, one Franprix
ticket validated that way came to 7,91 for 7,92 paid. `Invoice.total_ttc`, when
every line has a printed amount, is their sum
(never plus the adjustment, which in HT puts back cents the printed amounts
never lost: added on top, six 0,49 baguettes came to 2,97) - or the ticket's
own printed total (`Invoice.printed_total_ttc`, what was paid) when the lines
are within the parser's tolerance of it, so a cent the OCR misread never costs
the bank match. Otherwise the whole invoice stays on the HT arithmetic.
Receipts imported before these fields got them back from their stored reading
(`manage.py restore_printed_ttc`, matched by count, rate and HT, never by
name, and never from a promoted reading, whose printed amount is before the
promotion; `--dry-run` first after a parser change): 34 totals a cent or two
off became the printed one, none went the other way.

**A folder is a background job** (`invoices/receipt_batches.py`). The upload
page takes files or a whole folder (`webkitdirectory`; both inputs post as
`files`), and anything that is neither a PDF nor a photo is listed as ignored
rather than refusing the selection. Files are staged under the espace's
`imports/receipt_batches/<id>/` (never in its media/) and a thread imports them one at a time into a
`ReceiptBatch`, whose page polls with htmx and gives every file exactly one
outcome - `ok`, `duplicate`, `unrecognised`, `error`, `ignored`, `cancelled` -
because a receipt that failed silently shows up weeks later as stock never
bought. Three traps: Django refuses more than **100 files** a request by
default, with a 400 before any view runs (`DATA_UPLOAD_MAX_NUMBER_FILES`); a
phone photo's rotation lives in its EXIF tag, so `ocr.page_images` applies it
or the receipt is read sideways; and scanning the same folder again has to be
cheap, so `import_receipt` checks the file's SHA-256
(`Invoice.source_sha256`, backfilled by migration 0014) before any OCR.

**« Prendre une photo » on the import card** (the owner, 01/10/2026: « comme
pour les consignes »). A third choice, the first of « Tickets et factures »:
a camera input (`capture="environment"`) posting as `files` like the other
two, inside a `[data-photos]` box set up by `static/js/photos.js` - the
photo picking Consignes had in returnables.js, moved out and shared
(purchases.html calls `MarginMatePhotos.setUp(form)` on DOMContentLoaded:
the script is deferred, the page's inline one is not). After each shot the
filled input moves into the box's hidden store, still in the form, and a
fresh one takes its place, with a preview and « Retirer ». « Des fichiers »
and « Un dossier entier » are no photo slots: native inputs, as before.
- **Touch screens only** (`.upload-choice-camera`: hidden at the top level,
  drawn in the « touch » section, with its gap): a desktop browser ignores
  `capture`, and the tile would only open a file dialog beside « Des
  fichiers ». A touch screen is `(pointer: coarse)`, never a width.
- **What a shot says sits right under its tile**: the tile, then the
  store, the refusal (`role="status"`, `aria-live`) and the previews, then
  the grid of the two other choices - as on Consignes. After the whole grid,
  a phone drew a refused shot's sentence a screen below the tile tapped
  (review of 01/10), and the paper ticket could be thrown away on the
  belief it was taken.
- **One photo is one document.** Each shot is a file of the batch, read on
  its own: a paper invoice of several pages photographed page by page
  becomes several documents. A PDF (a scan of every page) stays the way
  for those; the tile says « chacune lue à part ».
- **Each shot is renamed** `photo-YYYYMMDD-HHMMSS` + its extension, from
  its `lastModified` in local time (`data-photo-rename`; `-2`, `-3`… for a
  name a photo of the box already has; the extension lower-cased, else
  taken from the type: `image/jpeg` is `.jpg`). iOS names every camera shot
  `image.jpg` and Android a bare number - which, with no extension,
  `ReceiptBatchUploadForm` lists as ignored - and the import names each
  file by its name (the batch page, the document's source file). Where
  DataTransfer is missing the names stay as they came: two `image.jpg`
  still stage apart (`stage_batch` stores by index, duplicates are found by
  content).
- **No post with a shot in it passes the cap, whatever the order**
  (`data-max-bytes`, `common.ONLINE_SEND_MAX_BYTES`, 90 MiB, read when the
  page is drawn). Online, a body over 100 MB is refused by Cloudflare's free
  plan with its own English page before the server sees it, and a browser
  never gives the photos of a refused post back - nor, usually, are they
  in the phone's gallery. Three checks, each said in one sentence, the weight said
  by `MarginMatePhotos.weight`, `common.weight`'s twin:
  - a shot that would take what the FORM posts (every file input of it, the
    other choices' files included) past the cap is refused as it is taken
    (« Photo non ajoutée : l'envoi dépasserait 90 Mo. Importez d'abord ce
    qui est déjà choisi, puis reprenez-la. »);
  - while a shot waits, a pick in « Des fichiers » or « Un dossier entier »
    that would pass it is emptied (« Fichier non ajouté : … puis
    choisissez-le à nouveau. »). Weighed only at the next shot, shots first
    and a big PDF next went past Cloudflare and lost the shots (review of
    01/10);
  - and while a shot waits, a submit past it is held (« Envoi arrêté : … »),
    before ui.js's busy label, which leaves a held submit alone - the last
    line, for a selection that changed with no `change` event.
  The last two sentences are scrolled into view (the finger is on another
  choice, or on « Importer »). **With no shot waiting nothing is checked,
  and the server does not enforce it**: a refused post then loses nothing,
  and a folder imported on the PC itself never meets Cloudflare and goes to
  500 MB. No count cap (`data-max-photos` absent; Consignes' box keeps its
  own, and none of the above runs there: it has no `data-max-bytes`).
- **The busy label**: while a shot waits, « Importer » sends as « Envoi des
  photos… gardez la page ouverte » (Consignes' words), else « Envoi… ».
- **A shot is not lost without a word** (purchases.html): leaving the page
  with one waiting asks first (`beforeunload`, as the timesheet's grid; an
  Achats tab is an htmx swap and keeps them), and how many wait is noted
  for the TAB (`sessionStorage`, `achats:pending-photos`, kept at logout:
  a count, no photo - another tab of Achats has photos of its own). A page
  drawn again with none says once that they were lost (« 2 photos prises
  n'ont pas été importées : la page a été quittée ou rechargée avant
  « Importer ». Reprenez-les. ») - Android may drop the tab while its camera
  app is in front, and it came back empty with nothing saying so. Consignes'
  draft says the same (« N photos à reprendre »).
Tests: `invoices/tests/test_purchases_page.py` (`CameraOnTheImportCardTests`:
the markup and its order, the cap followed), `test_upload_limits.py` (two
`image.jpg`), `tests/test_ui.py` (the tile's CSS, photos.js writes no
markup), `returnables/tests/test_views.py` (photos.js before returnables.js),
`accounts/tests/test_sessions.py` (the key classified), and in Chrome
`invoices/tests/test_receipt_camera_browser.py` (a phone and a desktop -
every check above, and no hold with no shot - `start_batch` patched: no OCR
runs).

**An unrecognised ticket is not a dead end - the reader works for any shop.**
A torn or faded header, or a shop nothing was set up for, is enough for
`detect_parser` to find nothing. The batch raises `UnrecognisedShopError` for
that - a plain `ValueError` is a broken file, reported as an error - keeps the
file (`"kept"` on the entry), shows what the ticket reads as (the line that
looks like its name, date, total: `receipts.first_reading`) and, once the
batch has finished, offers the suppliers on that row - or a **new shop**
(`receipts.create_shop`), named and given the text its tickets print at the
top (`Supplier.ticket_header`). The ticket is read whatever the shop:
`receipts.parser_for` gives every supplier but the AI pseudo-supplier a
reader, the configured till's or the same reader without settings (a Metro
paper ticket included). **The header is given from the review screen, not from the import card**: the
card is filled in before anyone has seen the document, so it asks for a name
only, and the review page - the photo beside it - has the box, filled in with
what the document seems to print (`header_guess`) and with its own top lines
offered as chips (`header_choices`, `set_shop_header`, refusals as
`create_shop`'s). Giving it sends every recent import's unrecognised files
through again (`requeue_everywhere`), and
`detect_parser` looks for headers people gave **before** the configured tills,
a header printed inside another giving way to it: "EPICERIE SABAH" before the
"SABAH" Sabbh's till answers to. Headers compare without accents, case or
punctuation, as whole words (`receipts.prints_header`, the one definition),
and one shorter than four characters, one another supplier already has, or
one already printed on the tickets of two other shops or on more than three,
is refused - a few tickets of one shop carrying it are more likely the new
shop's, filed before it existed, and are named so they can be moved, each
from its own page (`views._say_new_shop`, `_set_shop_header`). The chips
offered are only those the save would take (`offerable_headers`, over the
documents read once, `document_corpus`): the customer's own street is on
every supplier's documents, and a line with no space before the length a
header may have is not offered at all - cut, it matched nothing. A document filed under the
wrong supplier is moved from its own page ("Changer d'enseigne" on a ticket,
"Changer de fournisseur" on an invoice, `receipts.move_to_shop`): its lines
stay and find their products among the new supplier's, the orphans go. A
digital invoice gets no check added by the move - a check is what makes a
document a receipt (`Invoice.is_receipt`) - and seven invoices had no way
back at all before that page offered it to them. A file waiting for its shop is counted apart
("À ranger", `ReceiptBatch.awaiting_shop_count`), not as a failure:
`failed_count` is the errors plus the unrecognised files no longer kept.

**A shop is not a supplier with invoices** (`parsers.is_ticket_shop`): a
configured till, or any supplier with no PDF parser of its own. Only a shop's
products match OCR readings tolerantly and can be renamed from a ticket;
Metro's are named by its invoices.

The shop choice is `import_with_shop`, **while the batch still runs** too - a
folder of a hundred tickets used to have to finish before the one without a
shop could be checked. So `results` has three writers (the thread, a shop
choice, a new shop's re-read) and each takes `receipt_batches.RESULTS_LOCK`,
reads the entries fresh and writes back only its own: the thread used to save
the copy it started with after every file, which would have undone a choice
made meanwhile. The thread takes the next pending file each time round, so a
file sent back to "pending" while it runs is read by the same run; the end of
the batch is written under the lock with the check for pending files, so a
re-read a moment later starts it again instead of being lost. A file being
imported by hand is left out of re-reads (`_BY_HAND`, in memory), and
`append_log` appends in the database. The live part is fetched every second:
its shop forms keep what was typed (`hx-preserve`, stable ids) and the polling
waits while one has the focus (`shopChoiceInUse`, ui.js) - a swap takes the
focus away. The OCR runs in the request, so shop choices and "Relire le
document" take turns on one lock (`receipts.OCR_LOCK`) - two tabs used to
import the same file twice.

**A shop is recognised by what its documents print, header or not**
(`invoices/identifiers.py`, `receipts.identified_supplier`): a SIREN (alone
where "SIREN"/"RCS" names it, in a SIRET, in a VAT number whose key matches),
a phone number (same separator between every pair: "01.23 45.67 89.00" is
prices), a web site (never an e-mail's domain - a customer's address is on
invoices too). Checked the way each is built, since a misread one must not
name a shop. Stored on `Supplier.ticket_identifiers` and learned only when a
person said whose a document is - a shop chosen, a ticket moved (the old shop
forgets what it printed), a ticket checked on the review page, a digital
invoice imported through its supplier's own reader (`learn_identifiers`;
`manage.py learn_shop_identifiers --dry-run` for the documents filed before).
A digital document's text is kept for this (`Invoice.source_text`; only
`ocr_text` makes a document a receipt), which is what makes **the customer's
own SIREN** - printed on every supplier's invoice - name nobody: the command
reads the PDFs already filed before learning anything. Measured on the real
tickets, these rules keep it honest (`identifiers_naming`, `still_naming`,
arithmetic with no database):

- a **new** identifier is learned when **a quarter of the supplier's
  documents print it** - or, for a supplier with a header, a quarter of
  those **not** printing the header, two at least (misreadings -
  "mmoprix.fr", "monoprii.fr" - and labels on some goods - "fsc.org" on
  Mr.Bricolage's wood - are on one ticket or two), and **no other
  supplier's documents print it** (the customer's own phone);
- one it **knows** is dropped only for a reason about that identifier: another
  supplier's documents print it, or none of its own do any more. Measured
  again against a quarter of ALL its documents, Free's mobile figures (7 bills
  among 37) went on 18/09 - silently, on a correction validated - once thirty
  box bills had been filed beside them by the header;
- a figure a person set aside (« Retirer », `Supplier.refused_identifiers`)
  is never learned again;
- **a web site alone names no one** (the one branding the goods is printed
  at every shop selling them).

It comes after the headers people gave and the configured tills, and says so
("Enseigne reconnue"). One identifier learned by two suppliers names
neither. Every document's figures are read once and kept by their text
(`identifiers._document_identifiers`, `may_print`): a supplier's page reads
all 880 documents, and reading them again made it half a second.

**An identifier is TYPED as well as learned** (« Ajouter un identifiant » on
the supplier's page, `identifiers.read_typed`). Until 25/09 a supplier could
only be given what its OWN documents already printed (« Retenir », limited to
`identifier_report`'s `can_keep`) or have one taken away, and on 24/09 that
cost the owner a two-page repair: one supplier had learned another's SIREN,
its two phone numbers and its web site, so they had to be removed from it one
at a time before the rightful supplier could claim them back. Four rules came
out of it:

- **Typed and printed read the same.** `read_typed` hands what was typed to
  `_line_identifiers`, the very function that reads a printed page, with the
  word SIREN in front of it (that is what makes a bare run of nine digits a
  company number rather than a phone number). Read by a second function, a
  SIREN typed by hand and the same SIREN found on an invoice would be two
  keys, and a supplier would be recognised by one of them and not the other
  with nothing on screen able to say why. Checked on the real data, read-only:
  every stored identifier round-trips, typed bare or as the page prints it.
- **The refusal names the fault** - « sa clé de contrôle ne tombe pas juste »,
  « 8 chiffres : un n° SIREN en a neuf », « commence par 0 », « un seul
  identifiant à la fois » - because the person typing has the invoice in front
  of them and a bare « non » is one nobody can answer. `TypedIdentifierError`
  is a ValueError, like every other refusal that reaches a message.
- **A figure two suppliers retain names NEITHER** (`identified_supplier`), and
  that state was rendered nowhere: typing one another supplier holds is
  therefore refused, the page comes back carrying it (`?deplacer=`), says whose
  it is, and offers « Le déplacer ici ». The move is **one transaction** - half
  of it leaves the figure on both suppliers or on neither, and on both every
  document printing it silently stops being recognised under a success message.
  It records **two changes sharing one `operation`**, the shape
  `record_type_moved` uses, so `supplier_change_undo` already marks the pair
  undone together; `_undo_identifier_move` gives it back from either side and
  refuses when it has moved on again. Routed to `_undo_identifiers` it would
  have spoken for one supplier only and left it on both.
- **What a person typed is never unlearned** (`Supplier.typed_identifiers`,
  migration 0035; `still_naming(..., typed=)`). Learning may forget what
  learning found; a typed figure is a statement about that supplier, and
  `still_naming`'s two rules are about documents stored today - a SIREN typed
  for a supplier whose filed documents do not print it yet was dropped at the
  very next import, giving as its reason something the person had never
  claimed. `set_identifiers` prunes the list when a figure stops naming the
  supplier at all, so « Retirer » still works; the page marks each one « saisi
  à la main ». It rides in the « Données » archive (`SUPPLIER_FIELDS`).

**And the recognition SAYS what it is**, in two places, because the order is
not the one anybody guesses and four of its six steps were on no screen:

- **« Ce qui range un document ici »** on the supplier's page
  (`receipts.filing_rules`): its sources (« rangé ici quoi qu'il imprime »),
  the electronic invoice's declared SIREN (the one case where the order
  inverts and the SIREN beats the header), its till or its own reader (which
  needs a **SIREN** to start - a phone or a site never launches one), its
  header (whole words, « ajoute des documents, n'en retire aucun »), the
  **guard** that a SIREN another supplier retains overrules a header that
  matched, then each learned identifier. Rules, never counts:
  `identifier_report` already counts documents and two readings of one count
  is how they drift.
- **« Rangé chez X parce que… »** on the document
  (`receipts.filing_report`, the partial `_filing_block.html`, on the document
  page and above the move form on the correction page). It is **computed on
  every render**, because nothing is stored: a document filed by its header
  records no check, no field and no history, and a digital invoice cannot be
  given a check at all (a check is what makes a document a receipt). It states
  **facts** - what the document prints, and who retains each figure - never a
  second verdict about the order. `names_another` is the view that did not
  exist: every existing screen asks what a supplier's DOCUMENTS print, this
  one asks what suppliers RETAIN, which is what actually files a document, and
  `shared` is the both-sides state that names nobody. One query for the
  suppliers, no other document ever read - the same cost at nine documents and
  at nine thousand (pinned by a test).

It generalises `Invoice.supplier_doubt`, which **stays a field**: it is
queried as a fact (`DOCUMENT_TO_FIX`, `_stored_texts`, `_why_lost`,
`identifier_report`) and it carries a consequence - a doubted document teaches
nobody - that would flicker if it were recomputed. Measured on the real data
(read-only): nearly every document carrying text prints an identifier its
own supplier retains, the few others print nothing that names anybody, and
**none** prints one another supplier retains - the alarm is a guard against
the next such mix-up, not a backlog, and the screens say what they found rather than
implying they found something.

**Every change of what names a supplier is recorded** (`SupplierChange`,
`supplier_changes.py`): its name, header, identifiers, nature, a
type moved, its first document - with the cause of the act it came from (a
view or a gather says what it is doing around the call, `with
cause("validation de …", invoice=…, by_person=True)`; nothing set is
"automatique") and what an undo needs. `receipts.set_identifiers` is the one
writer of `ticket_identifiers`, and a contract test says so: the 18/09 loss
left no trace anywhere. A figure dropped that nobody asked to drop is
`needs_review` until someone has seen it; `collect()` hands the changes of
an act back, and the message, the batch log or the gather log says them.

Two more rules came from seven Free invoices filed under UBA, on a mobile
number both print - **the customer's own**, learned while UBA was the only
supplier printing it. A document that prints a **company number nobody
knows** is not the supplier whose phone or web site it also prints
(`identified_supplier`): a SIREN is what a company is, a phone is where
someone answers. And a **supplier's own reader** - which turns a whole
document into lines - runs only for a supplier named by its company number
or by the text it prints at the top (`document_supplier`); read as a ticket,
a document is checked against its own totals, so that path can be less
strict. What a document moved away from a supplier printed is forgotten by
it (`move_to_shop`), which is what unlearns a number that named it wrongly.
(The seven themselves were deleted and imported again under Free; UBA lost
the number at the `learn_shop_identifiers` pass that followed, once Free's
documents printed it too. Nothing names the customer's number because two
suppliers' documents print it - no list of "my numbers" is kept anywhere.)

**A header only adds documents.** It says "a document printing this is
this supplier's", never "a document not printing it is someone else's": a
torn or faded top is exactly what the learned identifiers are for. So
giving Free the text of its box subscription ("Abonnement Freebox Pop")
left its seven mobile bills where they were - filed by the company number
and web sites Free had learned from them. Saving a header now says how many
of the supplier's documents do not print it, what filed them there, and
that one not its own is moved from its own page (`views._say_headerless`).
Giving a header never takes a second subscription's documents out of a
supplier: two subscriptions are two suppliers (next paragraph).

**One source, one supplier.** A source (`InvoiceType`) files what it fetches
under its supplier, whatever it prints (below), so two sources of one company -
Free's box and its mobile line - are two suppliers from the start, each
learning what its own documents print. Filed as one, they needed a page to
take one subscription back out: a split, chosen, previewed inside a
rolled-back transaction, confirmed against a fingerprint of everything it
was worked out from, undone from either side's history - all for a case
that only exists when a source is filed under another's supplier. The
owner removed it on 19/09 as unneeded, its history kind (`SPLIT`) with it:
no split had ever been done. The one real case was fixed the same day by a
data operation. The owner had created Free Mobile and moved the mobile
portal's source to it from the source form (`TYPES`) - left behind, a source
files what it fetches back under Free; then Free Mobile's seven bills,
filed under Free by its SIREN and web sites, went over together through
`move_documents`. Free Mobile learned its SIREN and web sites but not the
customer's phone (UBA's documents print it too), Free kept its header
and its support site, every box bill is recognised as Free and every
mobile bill as Free Mobile, and no other document moved. A second
subscription found under one supplier later is the same fix: a supplier of
its own, its source moved to it, then its documents.

**Documents of one source move together** (`receipts.move_documents`, the
one definition of a move; `move_to_shop` is it for one document, and a
page only ever moves one - a group move is a data operation). Every
document first, then each supplier left forgets what they print and checks
what it still knows, then the destination learns **once**, from all of
them. Moved one at a time (`move_to_shop`), the first of the seven mobile
bills taught its new supplier nothing: the siblings left behind printed the
same SIREN and web site, so nothing was the new supplier's alone, and the
next mobile bill came back unrecognised - the new supplier learns them only
once the last has left. All or nothing: two of them with one number, or a
number the destination already has, and none moves. What both sides print
(the customer's own number) names neither; where two contracts of one
company print the same company number too (two meters, two sites), only a
header tells them apart.

What a move keeps, each a test that failed first:

- **Learning corrects the others** (`learn_identifiers` re-checks every
  supplier holding something the documents print, `_recheck`): one that
  had learned the customer's company number while it was the only one
  printing it refused every bill of another, header and all, until someone
  filed one of its own; and a third supplier sharing a number with the one
  the documents left was left its only owner.
- **What the lines do not say** (`move_documents`): a document read as
  goods and moved into a supplier of charges is read again as a charge
  (`refile_as_charge`) - its previous balance, direct debit and rent had
  become three charge items, three times what it charges; a charge's state is its
  total's (`charge_state`) - an unread total came out COMPLETE and left
  "À corriger"; and a classified line's product at the new supplier takes
  the same stock item (`link_product_to_stock_type`) - re-resolved, the
  purchase silently left the stock ledger. Between two suppliers of
  charges, a charge line named after the supplier it leaves takes the new
  name; a new supplier made from a charge's page is charges (the move
  form's box starts ticked).

Two guards **ask rather than choose** (`recognise_shop`, the reason travels
to the import's message): the headers of **two suppliers** on one document,
neither inside the other - the longest used to win, and a mobile bill
advertising the box would have gone to the box - and **a header against a
company number** another supplier learned. A header still beats another
supplier's phone or web site. Achats' « Enseignes et fournisseurs » tab
(`supplier_list`, `workspace._suppliers`) lists every supplier with what
names it and the sources fetching for it, those with a reader of their own
too (UBA, Metro), and how many of a shop's documents print its header; a
row opens its page.

**A supplier has a page of its own** (`supplier_views.py`,
`/invoices/fournisseurs/<pk>/`): what files its documents under it (header,
identifiers retained - « Retirer » -, printed but not retained with why -
« Retenir » when the rule would keep it -, set aside - « Ne plus
l'écarter »), its sources (« + Nouvelle source pour X »; Metro has none, and
says its own module fetches it - `workspace.OWN_MODULE`, what the gather
card lists it by), its history with
an undo per change, and « Modifier » and « Supprimer… », the latter disabled
with the reason when it cannot be (a till, a reader of its own, documents
filed, a source fetching for it). It leads back to « ← Achats · Enseignes et
fournisseurs ». Every action answers where it was taken and is recorded; an undo is
recorded too, marked with what it undoes (`data["undoes"]`), and has no
undo of its own - offered one, it redid the change in one click, nothing
shown first; posted by hand, it is refused. A value the page did not offer
is a message, never a 500, and no GET writes. Two suppliers never share a
name whatever its case, accented capitals included (`supplier_named`:
SQLite's case-blind comparison is ASCII only).

- **Created before its first document** (`supplier_create`, « + Nouveau
  fournisseur » on « Enseignes et fournisseurs », or « + Nouveau
  fournisseur… » in the source form, saved with the source in one
  transaction - a source refused leaves no supplier). A new source had to
  name an existing supplier, and one never sent a document did not exist.
  Where its invoices will come from sends the next page: the source form,
  filled in (`?fournisseur=&source=&retour=`).
  Its creation is undone by deleting it, while nothing rests on it.
- **Modified after a look** (`supplier_edit`): « Vérifier les changements »
  says what a rename takes along (a supplier of charges: the lines and the
  charge item named after it, `rename_supplier` - never to the name of
  another of its charge items; the code never changes, bank matching keeps the payee names it
  learned) and what a header does (printed
  on how many of its documents, on how many of others', refused by
  `check_header`) - nothing saved; the save applies only if the supplier is
  as it was checked. One with no documents saves in one step.
- **Deleted only when empty** (`supplier_delete`): no document, no source;
  its unused products, known prices, payee names and history go with it,
  said before.
- **Its first document** is recorded, to be seen (`FIRST_DOCUMENT`,
  `needs_review`): what it taught, or what it prints that was not retained
  (one recognised by its header teaches nothing). Nothing else vouches for
  a first reading. Suppliers waiting for theirs come first in every import
  choice (`WAITING_GROUP`).
- **A source files what it fetches under its supplier, whatever it prints -
  and a document printing what names another supplier teaches nothing**
  (`type_supplier_doubt`, `by_type`: another supplier's header, till or
  learned figures - or the company numbers of others printed beside this
  one's own, every one of them: `_companies_of_others`).
  The doubt is kept on the document (`Invoice.supplier_doubt`), not as a
  check: reading it again, or as a charge, rewrote the checks and the doubt
  went with them. The document waits (the ticket queue while unchecked,
  "À corriger" otherwise, `DOCUMENT_TO_FIX`), says why on its page and its
  row, is **nobody's** to learning (`_stored_texts` leaves it out - counted
  among everybody else's, it made the other supplier forget its own
  number the next time it learned; `learn_shop_identifiers` too), and is
  answered by a person: validated on its page (it then teaches, a digital
  invoice included) or moved (`move_documents`). It is not its supplier's
  first document either: the next one, which teaches, is. A source moved to another
  supplier is recorded on both and given back from either (`TYPES`, « Rendre
  cette source à X »); the documents it already fetched stay where they were
  filed, and the message says to change their supplier from their own pages
  if they are the new one's. A source's page drawn before it moved (a
  « Rendre » in another tab, or the source saved from another tab) does not
  move it back unseen
  (`supplier_was`). Enter in one of its fields saves: « Tester », the
  form's first button, signed in on a portal.

**A PDF invoice from a supplier with no reader of its own** - or a new one,
named in the import card ("+ Nouveau fournisseur…", `InvoiceUploadForm` is a
`ReceiptShopForm`) - is read the way a ticket is (`import_receipt` with the
supplier) and opens on the correction page beside its PDF; the supplier learns
what it prints. A supplier with its own reader (Metro, UBA...) keeps it, and a
digital invoice dropped among ticket photos and filed by hand under one goes
through it (`receipts.import_invoice_pdf`, when the file has a text layer; a
scan is read as a ticket whatever the supplier). The AI pseudo-supplier is
still offered there, last. A reader that fails or reads nothing (or the AI
pseudo-supplier, which has none) still files the ticket, empty, with a failed
"Lecture automatique" check - that check is also what puts it in the review
queue, which lists only receipts with checks. Either way the operator lands on
the review screen, which also takes the ticket's date and total (a blank
total keeps the one read: it is a field nobody filled in, not a total
removed).

A ticket number of four digits or fewer ("Ticket no 4278") is the till's count
of the day: it comes round, so it is stored with the date, or a later ticket
was refused as a duplicate. **The digits are counted without the zeros a till
pads with**: Wing Seng prints « Ticket:000172 », six digits, and its tickets
run from 000035 to 000473 every day of the year - bare, the 18/09/2026 ticket
was refused as the 19/11/2025 one (the owner, 01/10/2026). Tickets filed bare
before that are dated by `manage.py tenant <espace> refresh_document_numbers`
(only where the text prints that very number after « Ticket »): left bare, a
second photo of one, read dated now, would not be recognised.

**The autoreloader kills an import outright** on any code change: a
137-ticket batch died one second in, while code was being edited, and showed
"En cours" for half an hour. So a running batch beats every 15 s from a
thread of its own (`receipt_batches._Heartbeat`), counts as dead after 90 s of
silence (`ReceiptBatch.STALE_AFTER`; the page's live part reaps too, or it
never changes), and can be resumed ("Reprendre l'import"): staged files stay
until every one has been read. Not resumable while it may still run - a beat
within STALE_AFTER could come from a machine that just woke up, and the beat
puts a batch reaped that way back to running. **Check no import or gather is
running before editing code**, or run the server with `--noreload`.

One trap found by opening the page rather than by a test: `vat_rate` is
stored to four decimals, so 20% renders as "20.0000" and the line form
(`decimal_places=2`) refuses the value it just rendered — the save dies with
an error under a field nobody touched. `forms.line_initial` quantizes it, and
writes a weight as 4.184 or 10, never "10.000" or the "1E+1"
`Decimal.normalize()` makes of it.

**Product names read by OCR match tolerantly — receipts only.**
`resolve_products(..., ocr_tolerant=True)` (set from `ParsedInvoice.from_ocr`
and `Invoice.is_receipt`) attaches "CITRON SHT 5OOG" to the shop's existing
"CITRON SHT 500G": spacing, punctuation and accents are ignored, letters that
share a shape with a digit (O/0, I/1, S/5, B/8...) are cheap, a real typo
costs one. A digit never becomes another digit and is never added or dropped
— "250G" never finds "500G". Two equally close candidates: no match. Digital
invoices keep the strict matcher. The review screen shows "lu sur le ticket"
under any line matched this way, which is what makes the forgiveness safe.

A product is compared by **every name it has been read as**, not only its own
(`InvoiceLine.read_as`, carried through the review screen in a hidden field).
Its own name is just whichever reading came first, and one Franprix label came
back as BAGUETTE BLANC, BLAND, BLAVC, BLAVD, BAGLETTE, BAGUETRE and AGUETTE
BLANC across fourteen tickets — readings up to three mistakes apart. **Do not
widen the budget instead**: measured against the products already in the
database, a budget wide enough for two readings to meet joined
"1/16CANTAL AOP ED" with "... JNE" and "GINGERBEER 1L" with "... 1L BIO" (at
L/6, "PORTO CRUZ BLC" with "RGE"). Readings reach the far spellings one
mistake at a time. It follows that a receipt's lines are resolved together
(`resolve_products`), so a line reaches a product through its neighbour on the
same ticket whatever their order, and that a misreading attached by hand on
the review screen is recognised on the next ticket. `read_as` is never set on
"Article divers": a price is not a name, and as a reading it would bypass the
price list's dates. The product a corrected line stops using is deleted if
nobody classified it (`deletion.remove_orphan_products`), or it would wait in
the review queue for ever.

**A product is renamed under its line, not in it.** A receipt's product is
named after its first reading ("BAGUETTE BLAND"), which fills every ticket's
form. Typing the right spelling in a line relabels that line only - the typed
name resolves straight back to the same product, since it is one of its
readings - so the review screen offers "Renommer … sur tous les tickets" under
the first line of each product (`receipts.rename_product`), filled in with what
that row says (a typed "ORANGE" on the product "RANGE"), and says so after a
save that kept a typed name on a product of another spelling. A rename keeps
every OCR reading as read (still recognised), relabels the lines named after
the product and the shop's price list, and refuses a name another product of
the shop has: that is a merge, done by typing the name in the line. **Only the
products of shops with a ticket reader**: a paper ticket filed by hand under
Metro shows Metro's catalogue, which its PDFs find by exact name with no
reading to fall back on - and its typed lines match strictly for the same
reason. Renaming, moving, reading again and the price list reload the page, so
the page asks before dropping line corrections not yet saved (and Enter in a
line moves to the next box instead of validating the document). The price
list is folded away where the till prints names and none was started. A price already
known is refused by the form (the shop is not a form field, so Django never
checked the uniqueness it is part of, and the database answered with a 500);
a wrong one is forgotten from the list under it.

### Deleting an invoice (`invoices/deletion.py`)

Deleting the row alone is wrong twice over. `StockMovement.invoice_line` is
SET_NULL, so the purchases would stay in the ledger with no invoice behind
them — deleted explicitly first. `StockTakeLineSource.invoice_line` is
PROTECT: an invoice that priced a past stock take is **refused**, naming the
count, since deleting it would rewrite what that count was worth. Unclassified
products that only it created go with it (a misread receipt's garbled names
would otherwise sit in the review queue for ever); files go on commit.

### Correcting an invoice's lines

The correction page shows a name, a count, a weight, the amounts and VAT (and
a ticket's promotion); a stored line carries more - duty, Metro's discount,
pack size, category, a receipt's reading. Each row posts back its line's id,
and `importing.corrected_line` keeps what the form doesn't show, and the
volume unless one was typed (scaled when the count changes: an item's size
didn't). Rebuilt from the four
visible fields, a Metro invoice saved untouched turned 4.2 L of vodka into
6 L of stock, and a weighed Wing Seng lemon lost its kilos.
`replace_invoice_lines` then **updates those lines in place** - a stock take
priced from one keeps its trail, where deleting and recreating it hit the
PROTECT with a 500 (66 real invoices) - and removes the others, refusing
(`InvoiceLinesInUseError`, nothing saved) to remove one a count was priced
from. A refund is a negative count **and** a negative amount (Metro's "1-" /
"15,00-", on 41 real invoices); a positive count at a negative price is
refused - stock worth less than nothing is what the FIFO guard exists for
(not on a charge whose supplier holds no stock item: "A credit on a charge",
above).

### Charges spread over the lines: the delivery

A supplier prints « LIVRAISON » once, for the whole order. It is not a
product, and what it costs belongs on the goods it brought: a bottle that had
to be delivered costs what it was billed at **plus its share of getting it
here**, and that is the only figure a margin can be taken against. Ticking
« Frais » on a row of the correction page (or of the hand-entry page) says
so - `InvoiceLine.is_spread_charge`, and `spread_ht` on each of the others.

**The line stays a line, and no total moves.** That is the whole design, and
it is arithmetic rather than taste: Metro delivers at **20 %** invoices whose
food is at 5,5 % (one invoice can print goods at 0 %, 5,5 % and 20 % and its
delivery, alone of its kind, at 20 %). Folded into the goods' `total_ht`
the delivery would be taxed at their rate, and `Invoice.total_ttc` - the
figure `bank.matching` needs to the cent - would stop being the one the
supplier debits. So `total_ht` is untouched, `reconciliation_adjustment` is
untouched, every check and every screen goes on reading the document's own
figures, and what moves is the **cost**: `InvoiceLine.cost_ht` is
`total_ht + spread_ht`, and **zero on the charge line itself**, whose money is
already on the goods. The shares add back up to the charge exactly, so the
lines' costs still come to `lines_total_ht`. Nothing is created; it only
moves between lines.

- **Two places turn a line into a price per unit, and both read `cost_ht`**:
  `inventory.services.compute_movement_amounts` (which writes
  `StockMovement.unit_cost_ht`, and from there every stock value, recipe cost
  and margin) and `_fifo_value` (which prices a stock take). One of them
  reading `total_ht` is a count priced on one basis off a shelf valued on the
  other - a figure with two definitions, this codebase's oldest sin.
- **`InvoiceLine.unit_cost_ht` stays the DOCUMENT's unit price**, so the
  invoice page's « P.U. HT » and « Total HT » columns still multiply out. The
  delivered price lives on the movement, which is a different question:
  « what did the supplier charge for one » against « what does one cost me ».
  The page says the second under the line (« + 0,05 € HT de frais répartis :
  revient à 12,45 € HT »).
- **`importing.spread_charges` runs BEFORE the movements are booked**, inside
  `replace_invoice_lines` and `import_parsed_invoice`. Run after, every
  movement is created from the cost the line had before the delivery was
  shared out, and nothing recreates them until somebody happens to save the
  page again.
- **Pro rata of what was BOUGHT**: only lines priced above zero weigh. Signed,
  a returned deposit would take a negative share and the beer beside it more
  than the whole delivery - the rule, and the reason, `_where_it_went` already
  follows. Nothing positive to carry it and nothing is shared; the page
  refuses the save (`forms.NOTHING_TO_SPREAD`) rather than saving a charge
  that looks shared out while every product still costs what it did.
- **The leftover centimes go to the largest remainders**, as a ticket's
  promotion is spread (`generic_receipt._spread`): three lines sharing 1,00 €
  get 0,34 0,33 0,33. Lost, a euro of delivery a year vanishes out of every
  cost with nothing saying so. A credit on the delivery is negative and rounds
  the other way; the same rule covers it.
- **The product is `is_expense`** - « no stock item, and never will be »,
  which is exactly what a delivery is. Reusing that flag rather than inventing
  a second one is what keeps the nav badge, « Produits & charges », « Tout
  approuver », `assign_product`, `Invoice.needs_review_count`,
  `refresh_invoice_statuses`, the associations import and the archive all
  right without being told. `importing.flag_products` is the one writer and it
  works **both ways**, from the LINE: untick the box and the product is a
  product again. Left as an article, a delivery held its invoice in « À
  vérifier » for ever asking which bottle « LIVRAISON » is, and « Tout
  approuver » would have booked a stock movement for every one ever filed.
- **The refusal is on the line, not on the product**:
  `create_stock_movement_for_line` returns None for a spread charge whatever
  its product has since been classified as, and
  `rebuild_purchase_movements` filters it out in SQL. A product is classified
  from three screens; the stock ledger must not depend on nobody having
  clicked.
- **« Marges » and « Dépenses » follow the box**
  (`margins.computation._charges_over_the_goods`): the delivery's HT and TTC
  go onto the places of the goods it delivered, from the **stored** shares, so
  the two pages cannot drift from the stock one. The pinned invariant holds -
  with nothing left out the places add up to the invoice to the cent, HT and
  TTC, ticked or not (checked on a long real Metro invoice, scratch copy).
- **A move keeps it, a re-read does not.** `move_documents` passes each
  stored line's own flag through `corrected_line` (whose default is False,
  right for a page where unticked means somebody unticked it, and silently
  wrong there: a moved document came back with its delivery a product again
  and every unit priced below what it cost). « Relire le document » replaces
  the lines with what the reader says, corrections included - it says so
  before it runs, and the box goes with them.
- **The formset**: `is_spread_charge` is a `BooleanField(required=False)`
  with **no field-level `initial`** - pre-ticked per row through
  `forms.line_initial` - and it is **not** a bookkeeping field. An `initial`
  makes an invisible gap row read as filled in and the save fails « ce champ
  est obligatoire » where nobody can see it; suppressed as bookkeeping, a row
  where somebody ticked only the box vanishes in silence. `quantity` is no
  longer required on either line form: a delivery is not sold by the unit, and
  `required` is enforced before `clean()`, so the rule for an ordinary row
  moved into `clean()` with it.
- **The live checks are untouched.** The charge is printed on the document and
  is inside its total, so it still counts in `refreshCheck` and
  `refreshHtCheck` exactly as any row does, and `data-adjustment` stays what
  it is - money charged OUTSIDE the lines. Folded in there too it would be
  counted twice. The shares are never on that page's arithmetic: they are a
  redistribution among rows, and every check there compares the DOCUMENT's own
  figures.

Measured on the real data (read-only): the deliveries filed before this
existed are products - Metro's delivery line above all, then web shops'
delivery and shipping fees - most of them pointing at articles invented for
them (category « Livraison ») which accumulate units nobody ever consumes.
**No stock take had been priced from one**, so they are safe to tick; each is
one box on its own correction page.

### The suggestions of the « À classer » panel (`inventory/product_matching_rules.py`)

Three sources, asked in this order per product and named on the card: a
classified NEIGHBOUR (`ClassifiedNeighbours.find`: the same words once sizes,
pack counts and house-brand prefixes are set aside; the same sizes for an
article counted by the unit), the RULE table, then the RAW NAME under the
learned category. The confidence is the least confident of the article and
the conversion factor (`least_confident`), since « Approuver les N sûres »
books stock movements from both and a right article at a wrong factor is
silently wrong money. A rule or a raw name is never « haute » any more:
measured strictly by leave-one-out, a rule names the right existing article
about half the time and a raw name almost never. « Approuver les
suggestions » still takes everything; « Approuver les sûres » takes the
« haute » ones only.

- **« Haute » needs** the same words spelled the same (a plural allowed, an
  abbreviation never), an article tracked by volume or weight or - counted
  by the unit - the same sizes AND the same pack, and a factor that is sure:
  copied from the same pack without the product's OWN line contradicting it
  (`_line_settles`: a printed volume on an L/KG article, or a colisage
  counting the items, make the factor 1 whatever the neighbour says - and
  both figures are said, in French), or read off a printed size for an L/KG
  article. Two names printing NO number are never « même conditionnement »:
  medium, unless the line's own reading is the copied figure
  (`_reads_the_same`). Another pack of a unit-counted article is medium (the
  name's count against the supplier's convention); another size of one is
  low (two sizes of one cup are two articles); « à un mot près » is medium,
  and low when the shorter name has one word.
- **Everything is learned from the owner's classifications, never listed.**
  `tells_apart`: a word that already separates two articles refuses a loose
  match, in every spelling `alike` accepts (« CONS. » teaches « CONSIGNE »).
  `two_words` / `alike`: a prefix pairing is refused once both spellings are
  printed under disjoint articles - « VIN » is not « VINAIGRE » - while a
  word only ever printed short, « GOB », stays an abbreviation. **Do not add
  supplier words or OCR spellings to fix a pairing**; file the twin and the
  index learns it.
- **A stored suggestion is a snapshot**: it carries `classified_fingerprint`
  (every classified product's article and factor, every article's name, unit
  and category) and `neighbour_product_id`.
  `apply_rules_to_pending_products` (run every time the panel is drawn) and
  `approve_all_suggestions` (before anything is booked) remake any
  suggestion that is not `is_current`, so a stale « haute » is never drawn
  or booked; the approval message says how many were remade (« refaites »)
  and how many were left because they were no longer sure.
- `least_confident` answers "low" for an unknown level - nothing unmeasured
  is ever approved in bulk. A rule's reasoning names the word matched and
  the article, in French, never the regex.
- **The benchmark and its counts stay out of the repository** (strict
  leave-one-out: the index rebuilt WITHOUT the judged product, its words out
  of the category classifier; the scratchpad's `loo_pipeline.py` and
  `fix2_measure.py`, which the code comments cite). The repository is public
  and a count of the owner's products is the owner's business, so the code
  states its ratios in words.

### Gathering invoices

**Never make AdminMate contact Metro, a portal or the mailbox from a
coding session** - no gather submitted in the browser pane, no curl POST,
no `gather_invoices_task` / `scrape_metro_invoices` from a shell or a
script. Coding agents launched 7 of the 18 recorded Metro runs and some
fourteen unrecorded dev-script sessions on 31/08, and both refused sign-ins
of 02/09 were an agent's end-to-end check: Metro's firewall blocked the
owner's access twice. Test against the fakes (`test_scraper_metro.py`,
`test_metro_refusal.py`); the owner clicks "Tester" for a portal.

**Metro's firewall judges each automated sign-in** (an Akamai edge: « Vous
avez été bloqué par notre pare-feu … identifiant :#18.… », at the moment the
credentials are sent, even after two quiet days). So AdminMate signs in
rarely and never argues with a refusal (`scrapers/metro.py`):
- the refusal (`blocked_reference`) is looked for wherever it can show - the
  page loaded, before typing, the credentials sent (`_await_sign_in`: the
  filters, the refusal, or the sign-in page kept = `MetroLoginFailed`), a
  search coming back empty, a download that never came, any page that
  did not come - and raised as `MetroBlocked`, never retried;
- `metro_pause` - kept on the METRO supplier (`scrape_*` fields), checked
  before any browser starts, whoever calls: 7 days after a refusal, 14 if
  it refused again within 30 days, and 24 h between two sign-ins (noted
  before the password goes). A person can ask for one sign-in through it
  ("Réessayer Metro maintenant" = `ignore_pause`); its own box is
  disabled, since the browser re-ticked a remembered one;
- a gather with `source_codes=None` never includes Metro: named only;
- the browser is restarted only when it died (`_session_died`), once,
  after `RESTART_PAUSE_SECONDS`; a page not as expected, or a window closed
  by hand, stops the run - each hiccup used to mean a new sign-in;
- one download at a time, one click every `CLICK_INTERVAL_SECONDS`;
  `MAX_CONSECUTIVE_TIMEOUTS` downloads in a row that never came stop the
  run (counted across windows; one late download alone says nothing - the
  old "timeouts" were a counting bug);
- rows are followed by their number (`ROWS_JS` reads every row's button
  and checkbox id in one call), never their place: a list re-rendering
  under a click had a row skipped and another clicked twice. A row counts
  as fetched once its **file landed** - marked at the click, the download
  a dying browser cut off was skipped after the restart, and lost;
- "Annuler" is heard before the browser starts and before the password
  goes: a cancelled gather still signed in once;
- a stop carries the PDFs already landed (`MetroError.files`): imported,
  not fetched again next time.

Metro is searched from its own newest invoice at the latest
(`min(posted start, suggested_start_date("METRO"))`): while it was paused,
gathers of the other sources moved the offered start past it, and the
days between would never have been searched on Metro.

**One source failing is said on its own line and the others run**
(`tasks._gather_metro`, `_gather_email`, `_gather_website`): on 18/09
Metro's refusal failed the whole gather, and the mailbox and the five
portals were never searched. A run with a source in error ends "Terminé"
with "N source(s) en échec" beside its pill, and the form offers that
run's period again (the default, since the newest invoice brought in,
skipped what it missed) - unless the run before asked the same period and
missed the same sources (`workspace._missed_again`): a portal asking for a
code every time held every gather on 01/01 for good. A mailbox source with
no reader goes through `receipts.import_document` like a portal's -
`parse_and_import` filed it empty, and again at every gather. The gather
beats (`tasks._GatherHeartbeat`, like the receipt batches): silent through
a long step, it was reaped while running, and a second one could start -
but only while it moves (log or progress changed within `STALE_AFTER`): a
thread blocked for good in one call is left to the reaper.

**SQLite takes the write lock when a transaction starts**
(`SQLITE_OPTIONS["transaction_mode"] = "IMMEDIATE"`): in the default mode a
transaction that had read, then wrote after another connection committed -
a heartbeat, every 15 s - failed at once with "database is locked", the
timeout not even tried, and the invoice being imported was lost.

**A job's button follows its status card** (`data-job-control` ↔
`data-job-active`, ui.js): drawn disabled while the job ran, it stayed so
after the job ended - only the card was redrawn - and a failed gather
could not be run again without reloading the page. A dead job is reaped
where it is polled (`gather_status`, `sales_import_status`), and at startup
only by runserver's serving process (`apps.serving_requests`): a
`manage.py shell` used to mark a running gather failed seven seconds in.
`serve` never reaps at startup (its apps load before it knows it will
serve: `--verifier`, a second `serve` refused its port). And that reaping
waits `REAP_DELAY_SECONDS` (two heartbeats and a margin) in a daemon thread
of its own, then takes only the jobs **not heard from since the process
started** (`last_heartbeat`, else `started_at`, older than its start),
checked again as each is written (review PROD-2): run as the apps loaded, a
runserver started beside `serve` on the same data - the old debug recipe, a
preview refused its port - marked serve's live gathers FAILED, the Gather
button came back and a second gather could start beside the first.

A download is complete when **the PDF its own row produces** appears
(`134_52_14645_<timestamp>_invoice_cus_copy_main.pdf`, named after the row's
checkbox id `FRA_134_52_14645_…`) - never when the folder's file count goes
up. A click sometimes also starts a stray "downloads.htm" that appears and
vanishes; counted along, it hid PDFs that had landed in a second, and one
gather spent eleven of its twelve minutes timing out downloads long finished.
Deposit credit notes ("Consignes") don't print their store, so the parser
stores them as "052-014645"; the list page is matched on till and number too
(`_is_known`), or every credit note is downloaded again on every run. The
levy and discount lines belong to the product above them **across page
breaks** too.

**A customer portal is data, not code** (`models.WebsiteInvoiceSource`,
`scrapers/website.py`, set up on Achats → Sources → « + Nouvelle source »,
« Canal : Espace client »). The rent's, the water's, the phone's: a login page, the NAMES
the credentials are kept under (never the values - the database is copied
and shown on screen; left blank, the form derives them from the site's
host, `forms.portal_env_names`: PORTAL_<HOST>_<6 hex of the exact host>_LOGIN
/ _PASSWORD, so two hosts never share), and
nothing else required. The values are typed on « Identifiants » (below),
else read from the .env file at each run, so either counts without a
restart. The scraper does what a person does: refuses the
cookie banner (only a refusing button is ever clicked, shadow roots
included), finds the login form - the visible password field, the text
field in front of it, the button that submits them, identifier and password
on two pages if the site does that - follows the first link that speaks of
invoices and is not one (`leads_to_invoices`: a home page listing the
latest invoices by month had one clicked as the menu) and holds no other
link (`link_to_follow`: Eau de Paris draws its side menu as one clickable
block around its entries, first in the page; clicked, it only closed
itself - nesting read from the page, never from the words; the page script
clears the marks of its previous read first, or a page drawn in place had
its hidden menu clicked for its first invoice; and buttons whose address is
only "#" are not one file, or every invoice but the first was left), or the links named
under "Liens à suivre", or the page given - and downloads, page after
page, each invoice of the period, one click every
`CLICK_INTERVAL_SECONDS`. CSS selectors exist for a site that defeats that,
folded away under "Réglages avancés".

The deciding is pure Python over what one script reads off the page (every
link with the text of its row): `periods` (a date is that day, a month
named - "mai 2026", "05/2026" - that month; a day's figures are not read
again as a month), `in_window` (a row printing no date is downloaded: nothing
says it is out), `looks_like_an_invoice` (leads to a PDF, says
"Télécharger"/"PDF" - in words or by its icon's name, `icon-download…`
read into `label` - or opens one, "Voir ma facture", **and** sits in a row
printing a date or an amount), `known_number_in` (a row printing the number
of an invoice already imported is not clicked). **One link a row**, the one
that downloads first (`_strength`): Free Mobile's cards offer « Voir ma
facture » and a button holding only a download icon - nothing on them said
« Télécharger », and the run found no invoice. A link's row is its table
row, list item or card - never the block holding the rows: a footer's terms
of sale climbed to the whole page, whose dates were the invoices', and were
downloaded as one. A button with little text of its own climbs to the block
printing a figure (its month, its amount): stopped at the block holding
only its neighbour « Voir ma facture », it had no date. **A gather
recognising no invoice link at all fails** (its own line, the period
offered again, the page kept and its links named): said as "0 found", it
passed for a quiet month and left nothing to set the site up from. Only a
file that *starts* like a PDF counts as the download - Chrome's stray
"downloads.htm" appeared and vanished mid-wait (as on Metro's site).

**How the file is obtained** (`_Visit.download`), each rule a 45-second
wait per invoice on a real site: a link naming its file is **fetched** with
the browser's session - no click to wait on (Free Mobile's « Voir ma
facture » opened a tab a script's click could not); otherwise it is
**clicked as a person clicks** (`_press`; the script's click only when
something covers it), in a profile allowing pop-ups and several downloads
(Chrome held every download of a site after its first, asking a question
nobody saw) with downloads allowed in every tab (`Browser.setDownloadBehavior`
- the page's own setting left a new tab without); a tab the click opened is
**read** for the document it shows, one held in memory too; a click that
has started nothing - no file, no download under way, no tab - after
`DOWNLOAD_START_SECONDS` started nothing, and a file landing later is still
taken (`late_downloads`). A file counts as arrived when it is **new or
changed** (size, time): Eau de Paris names each file after its invoice, and
Chrome downloading as told through DevTools writes over a file of the same
name - a run after the first found every name already there and said
nothing arrived while the browser showed each download done. Each file is
moved at once to a name of its own (`_take`), so neither the next invoice
of the same name nor the next run writes over it. **One invoice once a
run** (`invoice_key`): a list growing under « Voir plus » (Free Mobile's
shows five, then eight) was downloaded again from its first row, and paging
stops when a page brings nothing new. Clicked and none arrived is a failure
("aucune des N factures cliquées n'est arrivée"), not "nothing to
download". What
arrives is imported through `receipts.import_document` with the supplier
named - never `parse_and_import`, which files a supplier without a reader
empty - one OCR at a time.

**A person is asked, never impersonated**: a code sent by SMS or a captcha
(`NeedsAPerson`) waits five minutes for whoever is at the keyboard when the
source's "Navigateur visible" is ticked, and stops the source with that
advice when headless. A check standing where the login form will be - a
slider "faites glisser vers la droite", "non pas à un robot" (TotalEnergies',
18/09) - is looked for where the form is not, and handed over the same way:
the scraper had looked for a form twenty seconds and said there was none.
**A login form on screen is filled, whatever its page says**: Free Mobile's
explains that a first sign-in asks for "un code reçu par SMS", and read as a
check, those words stopped every run before its form was filled - "waiting
for a verification" with nothing to verify. Words count only on a page
without the login form, and after signing in only beside something to
answer (an empty field, a slider: `asks_for_a_person`); a field marked for
a one-time code or a captcha a person can see counts anywhere, never an
invisible one (a badge scoring the visitor, a frame parked off screen). A
window closed by hand during the wait is said as such (`alive`): read as an
empty page, it passed for « vérification faite »; a cancel is heard during
the wait and before the password goes. Also, from a second review: the
password field gone is not yet signed in - a code prompt drawn a moment
later is looked for (`SETTLE_SECONDS`), or the run ended with nothing; a
search box, a chat or a newsletter field is nothing to answer, and the
notice every page reCAPTCHA protects prints is no check; a field named for
a one-time code (`otp`, `sms…`), or a slider over a form, is one; with no
password on the page, a text field is the identifier only if it says so -
on a check page the login went into the captcha's answer; a check at the
identifier step of a two-step login is handed over too; and a page's words
alone count only once its form has had `WORDS_GRACE_SECONDS` to be drawn.
The login form is filled and submitted as it is at that moment (fields
found again for each step): Free Mobile's redraws its button as it is
filled, and the button found a moment before was clicked, gone. A site turning automated browsers away
(`RefusedByTheSite`, "The requested URL was rejected" - TotalEnergies' was,
to a plain browser, at the time of writing) is said as such; nothing tries
to get past it. Either way the gather goes on with the next source and the
failure sits on its own line of the progress table. A failure leaves a
screenshot and the page's HTML under `<scrape dir>/<type>/_debug/`, which is
what a site that changed is fixed from. "Tester" signs in and lists what a
gather would download (`list_website_invoices`), downloading nothing - and
keeps the page it read, found or not, under `<scrape dir>/test-<job>/_debug/`,
naming the page's links when it recognised no invoice: a new site is set
up from that ("Liens à suivre", the selectors), never by signing in on the
owner's behalf.

The source form holds both channels' fields (« Réglages : boîte mail » /
« Réglages : espace client »); the one not chosen is a
`<fieldset>` **disabled** as well as hidden. Hidden only, its empty
required field stopped the browser sending the form, silently: "Tester"
did nothing, and no mailbox source could be saved. The test client posts
whatever it is given, so only `test_invoice_type_form_browser` (a real
Chrome) sees that class of bug. It says first that a source fetches for one
supplier only. Its channel reads « Canal », « E-mail » / « Espace client »
from the form (`forms.CHANNELS`), not from `InvoiceType.SourceKind`, whose
labels stay « Email » / « Site web » (admin only): a label changed on the
model is a migration to apply to the real database, for a word.

**« Identifiants »** (`/identifiants/`, `accounts/credentials.py`, the
store `accounts/vault.py`; 01/10/2026, the owner: « renseigner les logins et
mots de passe des différents sites et de mon email sur une page »): the
mailbox, Metro, L'Addition and every portal (one account per pair of names,
two sources of one site share it), typed on a page reached from « Données »'s
header and the Sources tab - no topbar link (the bar's rows are measured).
**Third-party passwords: what protects them** (security review of 01/10/2026,
the owner: « users will enter passwords from sensitive websites »; five
auditors and their skeptics, 25 confirmed findings - each rule below is one):
- **Two files, not the database**: `private/credentials.bin` (Fernet) and
  `private/credentials.key`, a random key sealed by **Windows DPAPI**
  (`CryptProtectData`, current account, the folder name as entropy, via
  ctypes - no dependency). The Fernet key is HKDF over that random key AND
  the SECRET_KEY: copied to another PC or account, or opened by a
  development copy with its own SECRET_KEY, nothing opens. **Neither file is
  in any backup nor in data-dev** (`data_backup`, refresh_dev_data.cmd): the
  page says to keep the passwords elsewhere and retype them after a restore.
  A key anybody can read (the development fallback) refuses every save.
- **A file that does not open is never written over** (`problem`
  UNREADABLE, or BUSY while Windows holds it during a replace - retried,
  never taken for unreadable): only the page's « Tout ressaisir » box starts
  again (`save(start_over=True)`). One writer at a time (`_LOCK`), fsync,
  then `os.replace`. `VaultState.values` has `repr=False`.
- **Owner, and the MarginMate password asked again** (`accounts/sudo.py`,
  `/identifiants/confirmer/`): 15 minutes from the last protected request,
  for that login and session only, checked through the LOGIN limiter (a
  guess here counts there), the session key cycled and the password's hash
  stored again (`update_session_auth_hash`: a hash upgraded by the check
  logged the session out). A login - the login page's, the admin's -
  confirms too (`sudo.stamp`). A POST refused for want of it says nothing
  was saved. Behind it: the page; every POST saving or testing
  a WEBSITE source (`invoices/views.py`, owner too); every « Données » POST
  that imports, stages or clears (`transfer/views.py _refused`, owner too;
  export stays open); and the whole Django admin but its login and logout
  (`MarginMateAdminSite.admin_view`) - from the admin a superuser's session
  switched a repointed portal on. `InvoiceTypeAdmin` makes a portal's
  channel and « actif » read-only. A new membership in the admin starts as
  MEMBER (the model's default stays OWNER: no migration). **The test
  client's implicit first login writes a confirmation** (`tests.runner`),
  an explicit `force_login`/`login` does not: `confirm_password(client,
  user)`, `forget_the_confirmation(client)`, `TenantClient(confirms_password=False)`.
- **A password goes where it was typed for** (`VaultState.bindings`):
  - a portal's values are bound to `invoices.models.portal_host` (the https
    host of its login page) when typed; `website.credentials` gives a stored
    value to no other host, and the scraper types nothing on a page of
    another registrable domain, nor a password into anything but an
    `input[type=password]`. https on port 443 only (`PORTAL_PLAIN_HTTP_HOSTS`
    is the test settings' local portal, nowhere else). A portal's value read
    from the .env goes only to a site the owner confirmed on the page
    (`env_bindings`, the box « Le fichier .env contient ces identifiants :
    les envoyer à … »); until then its gather stops and says so. The page
    flags a value stored here AND still in the .env, to delete there. While
    the store exists but does not open (or is busy), no portal gets
    anything. Each portal card posts the host it was drawn with: a site
    changed while the page was open saves nothing (`SITE_CHANGED`); and the
    form posts a keyed digest of the state it was drawn from
    (`state_digest`): drawn before another tab's save, or while the store did
    not open (every login then read blank), it saves nothing
    (`STATE_CHANGED`). Busy, the page draws no form. A portal's password
    typed while its login is the .env's binds that login to the site too.
    « Fichier .env » and « encore en clair » read the .env FILE (not the
    settings, which keep a deleted line until a restart, nor a default), and
    name the old `UBA_EMAIL_*` lines the settings still read. « Tout
    ressaisir » alone clears an unreadable store;
  - "same site" fails closed (`website.same_site`): under a multi-tenant
    suffix (`SHARED_SUFFIXES`: azurewebsites.net, github.io, auth0.com,
    sharepoint.com, atlassian.net…) and on free.fr / pagesperso-orange.fr
    (users' personal pages) only the exact host is the site - but Free's
    own service hosts (`FREE_OWN_HOSTS`: subscribe, adsl, mobile…) are one
    site, the Freebox signing in on subscribe.free.fr and listing its
    invoices on adsl.free.fr. A missing suffix is fixed by adding it: reading
    two sites as one is the dangerous direction. `_fetch` (downloads with the browser's cookies) goes
    to https on the portal's site only, hop by hop, each cookie with its
    domain, path and secure flag;
  - the mailbox's app password is bound to its IMAP server: changing the
    server or the address asks for the app password in the same save, the
    server is a plain DNS name, and `generic_email.mailbox_credentials`
    sends a stored password only to its recorded server, the .env's only to
    the .env's. **IMAP4_SSL gets `ssl.create_default_context()`**: Python's
    default (`_create_stdlib_context`) checks NO certificate - the app
    password went to whoever answered the TLS handshake;
  - names: a source may not use one name as login and password, nor a name
    another source uses in the other role, nor names another site's source
    uses; such sources are listed « à corriger » and offered no field. Any
    name that is a password anywhere is drawn as a password field (a source
    naming the bank's password as its LOGIN printed it in clear). Derived
    names carry a digest of the exact host (`portal_env_names`).
- **Nothing written in clear**: the scraper's `_debug` dumps blank every
  field, rewrite the text on screen (`SCRUB_SCREEN_JS`) and scrub what was
  typed (raw, HTML, JSON, URL forms; the LOGIN whatever its case - a site
  echoes it in lower case -, a password in its own case only) - a page whose
  text (`SCREEN_TEXT_JS`, or the HTML with its tags stripped) still holds it,
  split across elements included, is neither photographed nor written; URLs lose their query in logs; every visit
  starts by pruning every source's old dumps; they stay out of backups and
  data-dev. Metro's log goes through `_MaskedLog` and takes no screenshot. Values no source uses any more are listed by name to
  be removed. Password managers are told to keep away (`data-1p-ignore`…),
  `sensitive_post_parameters` on both views.
- **Not done, the owner's** (DEPLOY.md): restricting `C:\MarginMate`'s ACL to
  his account, and better, running production under a Windows account of its
  own - coding sessions run as the same account as the server today, so only
  this file's rule keeps them out of production's key and store.
- Metro and L'Addition read their pair in ONE reading (`vault.settings_of`,
  busy = nothing, not the .env's either): read one at a time, a save between
  the two sent a new login with an old password - a refused sign-in Metro's
  firewall counts. Metro reads it once a run (`metro_credentials`).
- **Keyed by the .env's own names**, so every connector asks one question:
  `vault.setting(name)` (the page's value, else `settings.<name>`) for Metro,
  the mailbox and L'Addition, and `website.credentials` reads the store
  before the .env for a portal - read at every call, never cached. It also
  refuses a portal naming an application variable (`app_env_name`) at run
  time, whatever the form and the import let through.
- **A password is never shown back**: always an empty `new-password` field,
  a placeholder saying one is stored, blank keeps it, « Effacer » removes it;
  a login is shown. A value only in the .env is said (« Fichier .env »),
  never printed. A posted name no account offers is ignored.
- Only where `integrations_allowed()` (anywhere else: the refusal sentence,
  a POST 403). `never_cache`.
- Tests: `accounts/tests/test_credentials.py` - every test removes both
  files before and after (the test espace's folder is the whole run's),
  patches `credentials._env_file` off the real .env, confirms the password
  by writing `sudo.SESSION_KEY`, and posts the page's hidden host fields as
  a browser does. A test changing SECRET_KEY logs the client out: patch
  `config.security.secret_key_problem` instead. A test giving a portal .env
  values confirms their site: `vault.save({}, env_bindings={...})`.
  `invoices/tests/test_website_protection.py` (the scraper's guards, fake
  drivers), `test_sources_protection.py`, `accounts/tests/test_admin_sudo.py`,
  `transfer/tests/test_protection.py`.

The mailbox search asks for BEFORE the day **after** the end date: IMAP's
BEFORE is exclusive (RFC 3501), and the form's end date is today - this
morning's invoice email was left for a later run.

The gather form starts from **the newest invoice the gathered sources have
already brought in** (`tasks.default_gather_start`, receipts and future dates
left out). It used to take the earliest of each source's latest - one
supplier billing twice a year sent every gather ten months back.

**Plou & Fils changes its layout**: a "Taux" column on product rows (2026),
a VAT summary one column shorter, "Référence interne" instead of "N°
document" (January 2025). The one reader reads it (`parsers/ploufils.py` is
gone, see « Those rules replaced two parsers »), through its header - the
latest: "Désignation | Qté | Px U. HT | Px U. TTC | HT | TTC | Taux de TVA",
the older ones without the rate or with a qualifier after "Px U. HT" - « Px »
is « prix » (`layout._ROLE_PHRASES`), and the lone "HT" / "TTC" flavour
columns the arithmetic names. Unknown, that header was no header, and a
table was found only where two rows aligned: one row alone is no table
without its header. A parser that reads nothing, or warns (`ParsedInvoice.warnings`),
leaves the message on the invoice (`error_message`) and holds it in "À
vérifier" - an empty invoice used to say "ce fournisseur n'a pas de parseur".

### Bank statements (`bank/`)

`/banque/` imports the account's CSV export (BNP Paribas) and links each
spending line to the invoice or receipt it paid. `bank/statements.py` reads
the file and `bank/matching.py` decides - both on plain values, no database;
`bank/reconcile.py` does the rest.

A link is made automatically only when **the amount is exact to the cent**,
**the date fits** (a receipt dated three days before to one day after the card
date the label prints; a supplier invoice dated up to 180 days *before* a
debit or transfer, never after) and **the payee the bank prints names the
supplier** ("SABBAH" names "Sabbh Oriental", "U.B.A." names "UBA"; a word of
five letters may be one letter off; legal forms, "FILS", "PARIS" never
count). Several invoices of that supplier adding up exactly to one debit count
too, when exactly one combination does. Anything short of that is a
suggestion, with its reason on screen: the same amount at a payee the bank
doesn't name, or the named shop with a different amount - a misread receipt,
since the bank's figure is the right one. An amount alone never links, and a
debit never gets unnamed suggestions: months of invoices are in reach of it,
and something always has the right amount. A receipt whose date the OCR
missed fits no window; for a card payment, the named shop with that amount -
within a cent, since a receipt's total is rebuilt from its HT lines (a printed
7,76 totals 7,75) - is still suggested.

Card payments are matched first, so a receipt goes to its own card payment
before a debit's wider search sees it. **The automatic pass only ever links
an invoice nothing pays yet at all** - `reconcile.unpaid_invoices`, which the
suggestions are drawn from too. A second line settling an invoice already
settled is a decision only a person takes: left to the pass, the next debit
of the same amount would quietly pay it again, and every figure on the page
would still add up. Anything a
person does to a line - link, unlink, "pas de facture" - sets
`settled_by_hand`, and the automatic pass never touches it again; "Rapprocher
automatiquement" hands it back. Linking a payee the supplier's own name
doesn't contain records a `CounterpartyAlias`, so next month's payment to it
links on its own. Re-importing an overlapping export is safe: an operation's
fingerprint includes its position among identical rows, so two identical
baguette purchases on one morning stay two lines.

**A line and an invoice link however they really are.**
`InvoicePayment.invoice` is a ForeignKey (migration `bank/0003`, WRITTEN and
left to be applied): a line pays several invoices, an invoice is paid by
several lines - one settled in two goes, one debit split - and what cannot
repeat is the **pair**, by constraint. The same invoice twice on one line
says nothing and would be counted twice by every figure the row adds up. The
one-to-one it replaces was never a rule about money, only about the table,
and it was the whole of what refused the three things a person could not
record: an invoice whose amount does not match the debit, two invoices
chosen by hand on one debit, one invoice across two debits.

- **The suggestions are untouched** (`matching.py`, `row.suggestion`,
  `row.options`, `row.choices`, `MAX_CHOICES`): they are what attaches a
  document in one click, and they work. Everything below is built beside
  them, never in their place.
- **A person may link any document, whatever it costs.** Each row carries a
  search (`?ligne=&recherche=`, `views._search`) that asks the DATABASE
  through `invoices.workspace.documents_matching` - supplier, number, date or
  amount, the same search « Achats » has - because the page holds one
  window's rows and the document being looked for is usually outside them.
  Several are ticked at once. An invoice another line already pays is offered
  too, **marked with the operation that pays it**: hidden, the invoice
  settled in two goes would be exactly the one nobody could record. One this
  line already pays is shown and not offered again.
- **The pick-list stays on a row that already has an invoice**
  (`views._fill`, `pick_rows`; the template is `bank/_pick_invoice.html`, one
  definition for both states). One debit for two deliveries is one of the
  three things this page was opened up for, and losing the list the moment
  the first invoice went on left the second reachable only through the
  search - which needs a word the reader has to know, where the list needs
  none. The **suggestion** stays for an open row alone: it is the matching's
  answer to « which invoice is this », and that question has been answered.
- **Both lists say how many they cut** (`Row.more_choices`, `Row.more_found`;
  `MAX_CHOICES`, `MAX_FOUND`). A list cut in silence reads as « the document
  is not there » and the reader stops looking - the lesson `SALES_PAGE_SIZE`
  and the sales tab's « de plus » already exist for.
- **An alias is taught only by a link that ADDS UP** (`reconcile._learn_payee`).
  A `CounterpartyAlias` is permanent, no page shows it, and the automatic
  pass acts on it next month without asking. Since a person may now link any
  document at any amount, the one corroboration left is the amount: invoices
  coming to exactly what left the account say « the bank prints this payee
  for that supplier ». A link that does **not** add up is the case the search
  exists for - a part payment, a document found by number months later - and
  it is evidence about one debit, not about a name; taught from it, one
  mis-tick in the search box renames the payee for every statement to come
  and next month's rent quietly settles a wholesaler's invoice. Measured over
  everything the line pays, so two documents linked one at a time teach
  exactly as much as both at once.
- **A suggestion carries a tier, and « Propositions » accepts them in one
  go** (`/banque/propositions/`, `bank:proposals`; POST
  `bank:link_proposals`). `Match.tier` is SURE / NEAR_SURE / TO_CONFIRM.
  SURE is exactly `Match.confident`, derived in `__post_init__` and never set
  beside it (a non-confident SURE raises), so no tier can be widened without
  widening the automatic pass on purpose. Each tier's rule is one string in
  `matching.TIER_RULES`, built from the constants (`RECURRING_DAYS_BEFORE`
  35 days, `RECURRING_MARGIN` 20 days, `NEAR_SURE_GAP` 0,05 €,
  `LATER_PAYMENT_WINDOW` 180 days, the phrase `NOT_BY_CARD`) and stated on
  the page's rules card (`#regles`), so the words cannot drift from the
  thresholds; the tests read the same constants.
  - **NEAR_SURE has two rules, both for ONE named supplier**
    (`_several_suppliers`: a payee naming two suppliers is TO_CONFIRM).
    `_recurring_tier`: several invoices at exactly this amount, the nearest
    within 35 days of the payment and every other at least 20 days further -
    a monthly invoice, and the month's is the one. `_close_tier`: exactly one
    invoice within 0,05 € of the amount and no other that close - a total
    rebuilt from HT lines - and, for ANY payment not made by card
    (`PAID_ON_THE_SPOT` is CARD only: debit, transfer and the bank's own fee
    lines of kind OTHER are all capped), dated within 35 days too: months of
    invoices are in reach of a debit, and a recurring supplier whose figure
    moved by cents has last month's a few cents off the moment this month's
    is not imported.
  - **A blank counterparty names a supplier through an alias ONLY.**
    `Payment.payee` falls back to the label's words without their digits
    (`payee_of`, `alias_key`: « FRAIS TENUE DE COMPTE N° 000123 DU 05/06/26 »
    keys as « FRAIS TENUE DE COMPTE N DU », the same under next month's
    number), and `names_supplier(..., alias_only=True)`
    (`Payment.payee_from_label`) checks the learnt aliases and nothing else -
    not a word of the supplier's own name, not the one-letter-off rule.
    Before it, such a line could never name a supplier; the fallback exists
    so it can be TAUGHT: `_learn_payee` asks the same question, so a
    hand-made link that adds up learns the label key and next month's fee
    line links on its own. A label also carries the bank's text and a free-text reason,
    so an exact word in it (a supplier « Assurance Exemple », a fee reading
    « ASSURANCE MOYENS DE PAIEMENT ») would have linked automatically on a
    coincidence. Measured on the real data (read-only): the fallback changes
    no verdict on the replayed and open lines.
  - **Every SURE match says its day distance** (`Match.days_back`,
    `_sure_reason`: « Facture datée N jours avant le paiement », « à N jours
    du paiement » for a card, « du jour du paiement », a sum dated by its
    most recent invoice; None for an undated receipt). For a payment not
    made by card past 35 days it is `Match.far_back`: the pass links it all
    the same (a late payment is months - that rule was NOT moved), but
    « Propositions » does not pre-tick it and says why: once a supplier's
    other invoices at one figure are linked in a bulk accept, the single
    exact one left may be another month's, the month's own not imported. A
    page-only rule (`views.Row.preticked`).
  - **One invoice, one line** (`views._share_invoices`). An invoice proposed
    to several rows is marked on each option (« proposée aussi pour
    l'opération du JJ/MM/AAAA ») and pre-ticked for the FIRST row in
    `reconcile.pass_order` only - card payments first, then date, then pk,
    the one order `reconcile()`, `accept_proposals` and the page share - so
    the page promises exactly what the POST does; the other row
    (`Row.rival`) says which operation takes the invoice first and is not
    ticked. Pre-ticked on both, a reader ticking the later line alone linked
    the wrong one.
  - **Three groups**, « Certaines » / « Quasi-sûres » / « À confirmer »
    (`_proposal_group.html`). A SURE line is on the page in two cases only:
    the pass has not been rerun since the last import (ticked), or a person
    unlinked the line (« Déliée à la main : pas cochée d'avance »). Each
    group's heading (`views.GROUP_NOTES`, built from the same constants)
    states its pre-tick rule and every exception with the figure - a heading
    reading « cochées d'avance » over an unticked row owes the reader the
    rule. The bank page's rows carry the tier pill (a link to
    `bank:proposals#regles`) and the tier's reason.
  - **`reconcile.accept_proposals` trusts nothing posted**: MISSING, INCOME,
    NOT_OPEN, RULED_OUT, PAID_MEANWHILE (by another line, or by a proposal
    accepted a moment before in the same batch - candidates are withdrawn as
    the pass does), CHANGED (a fresh match no longer offers exactly that
    set) - each skipped and said. An accepted proposal goes through
    `link()`: MANUAL, `settled_by_hand`, alias learnt when it adds up.
  - **Measured** by replays on a scratch copy, read-only, and said here in
    words only. Replaying every hand-made link with its invoices unpaid,
    nearly nine in ten land SURE or NEAR_SURE with the truth first, and none
    of those is wrong; the few TO_CONFIRM ones are part payments and a rent
    paid late. Every automatic link replays SURE and right. With every link
    removed at once - the state right after an import, before the pass -
    about three quarters of the lines are pre-ticked, every one right; the
    far-back rule withholds a handful (right ones, and one whose invoice was
    another month's), the rival rule withholds one whose first option was
    wrong; no invoice is pre-ticked twice, and the simulated POST makes only
    right links.
  - **Test fixtures**: `test_matching.FEE_LABEL` and `test_reconcile.FEE_KEY`
    (the fee wording above, invented), `test_matching.other()` for a
    kind-OTHER blank-payee payment, `test_proposals.ProposalsPage` (a
    subscription at three identical monthly invoices, an unnamed card
    payment, two identical deliveries, a rent with nothing to propose, an
    income line). Names: the repo's usual suppliers plus « Abonnement
    Exemple », « Bailleur Exemple », « Alarme Exemple », « Assureur
    Exemple », « Banque Exemple », « Compta Exemple ».
- **An income line never settles an invoice** (`views.bank_line_action`). No
  form is drawn on one, so it is a stale or crafted POST - but linked, the
  invoice would leave `unpaid_invoices` for ever and read « Payée » on its
  own page while showing on no tab here and in no figure of « Dépenses »,
  which counts debits only.
- **The document's own page compares what was paid with what it costs.**
  « Payée » is kept for when the two agree; otherwise « Réglée en partie » /
  « Réglée au-delà » and « X € réglés sur Y € » (`InvoiceDetailView`,
  `paid_total` / `paid_gap`). That page is where a person goes to find out
  whether a document is settled, and a 70,00 € debit under a 120,00 € invoice
  printed « Payée » over a list of amounts nobody added - on an invoice no
  pass and no pick-list will ever raise again.
- **Going back on `bank/0003` is not safe** once an invoice is paid by two
  lines, and it fails half way, with 0004 already un-applied and every typed
  category gone. The migration's docstring says so. Take the second link off
  first, or restore the backup.
- **The gap is said, never repaired.** « Rapprochée » no longer means « adds
  up », so the row says both figures and the difference (`views.Gap`):
  « Factures X € pour Y € débités — Z € de plus / de moins que la dépense ».
  The line stays reconciled - a person said so - and the figure is what tells
  a reader next month that half the invoice is settled elsewhere. Hidden, a
  debit paying half an invoice reads exactly like one paying it whole. The
  document's own page lists every operation that paid it and says how many;
  a row whose invoice is also paid elsewhere says which operation.
- **One invoice comes off at a time** (`reconcile.unlink_invoice`, « Délier »
  on the invoice; « Tout délier » for the line): with three documents on one
  debit the only button took all three off. Either way the line stays
  `settled_by_hand`, even with nothing left on it - the automatic pass
  putting the link straight back is the one thing a person cannot argue with.
- **« Données » keys links by the PAIR** (`transfer/sections/bank.py`), and
  neither « Fusionner » nor « Remplacer » refuses one for being « already
  paid » any more. What holds a link back is the LINE's own decision here -
  « pas de facture », paying something else, settled by hand without it -
  never the invoice being paid elsewhere. Keyed by the invoice alone, one
  payment answered for the other: merging a database's own export reported a
  conflict about a link that was already there, and restoring an archive put
  back one of an invoice's two links and dropped the other.

**Payments that never have an invoice** (a loan, URSSAF, salaries) are
excluded by `IgnoreRule`: a regular expression searched in the whole label,
case ignored. Rules are applied when the page is drawn (`views.classify`),
never stored on the lines, so pausing or deleting one puts its payments
straight back - and the automatic pass skips what an active rule matches. A
line's invoice beats any rule: a rule never hides a payment that has one. A
pattern that matches the empty string (".*", "URSSAF|") is refused - one
stray "|" would hide every missing invoice. The rules page has a "Tester"
button showing what a pattern catches before it is saved, including how many
of those already have an invoice (the sign of a pattern too broad). The page
filters by month (`?mois=2026-07`) and groups what is still missing an
invoice by payee, each with a pre-filled "Ignorer…".

**Found through the statement: `Invoice.total_ttc` left VAT off the
reconciliation adjustment.** The adjustment is duty the lines don't carry
(UBA's "VIG. SECU" and the like), and duty is part of the VAT base. Added
flat, all four UBA debits of July 2026 were five or six cents above the
invoice's total; taxed at the rate of the goods carrying duty, all four match
to the cent. The adjustment still never goes into any product's cost.

« Données » exports a line with its decisions and its links by invoice key;
an import restores them and never makes one (no automatic matching - the
report points at « Relancer le rapprochement »), and « Fusionner » never
overwrites a decision this database holds: a line whose record differs here
(« pas de facture », « réglée à la main », any field) is a conflict kept
whole, its links included, and the conflict names the archive's links it did
not take; a line paying another invoice here is a conflict; a line settled by
hand here that lacks one of the archive's links keeps lacking it (« réglée à
la main ici sans payer … »), since `reconcile.unlink` leaves the line exactly
as an invoice deleted with its payment does. Only an invoice the same run
brought back (absent when it started) gets its link back. Merged back, the
export once made again a link a person had undone, and put an AUTO link under
a « réglée à la main » the automatic pass never revisits.

### « Dépenses par catégorie » (`/banque/depenses/`, `bank/spending.py`)

**What left the account**, over a period, by category, with a pie chart.
Not « Marges »: that page counts what was **invoiced**, by invoice
date, and answers « ai-je gagné de l'argent ». This one counts what the bank
**took**, by the date it took it, invoice or no invoice, and answers « où est
parti l'argent ». Two bases, two figures, and **each page says which it is**
and links to the other - read one for the other and neither is worth
anything. It is TTC throughout: what leaves a bank account is tax included.
Income is not spending and is nowhere in it.

The base is the statement, so **the categories add back up to what left the
account, to the cent** (`bank/tests/test_spending.py`). Everything else
follows from that:

- **A line an invoice explains takes the categories of what that invoice
  bought**, through `margins.computation.where_it_went(invoice)` - the same
  split « Marges » reads `spend` off, made **public** for this rather than
  copied: a figure with two definitions is this codebase's oldest sin.
  `lines_prefetch(lookup)` is public with it, because « Dépenses » reaches an
  invoice's lines through a bank line's payments (`invoice__lines`).
- **A goods line's category is its ARTICLE's category**, a charge's is its
  **SUPPLIER** - not the « Charges » umbrella « Marges » groups them under.
  A charge has no article, and the rent, the electricity and the phone are
  three questions a single slice answers none of. A goods line no article
  claims stays « Sans article (à classer) », the same words as everywhere.
- **The invoices give the shape, the bank gives the amount.** Since a line and
  an invoice link however they really are, the two need not add up. Where the
  invoices cost **more** than the debit (settled in two goes, a debit split),
  every place is **scaled down pro rata** to what that line really paid, the
  remainder on the largest place - counted whole on both lines, an invoice
  would be bought twice and the page would state a spending that never
  happened. Where they cost **less**, the difference is money that left with
  nothing to explain it, and on a line that HAS invoices it is « Sans
  catégorie » **whatever that line or a rule calls itself**; `uninvoiced`
  says how much. Named after the line, it sat in the pie under a heading no
  row on the page can explain or correct - the list below only offers the
  lines with **no** invoice at all, so that line is not in it, and only
  « Sans catégorie » carries a figure reconciling it
  (`unsaid_beyond_the_list`) and a stat pointing at where the missing invoice
  is found. On a line nothing invoiced at all the difference IS the whole
  debit, and it takes the line's own category as below.
- **A line with no invoice takes the category a person typed**
  (`BankTransaction.category`, free text with a datalist of what already
  exists - `StockType.category` is the precedent), or the one an active
  `IgnoreRule` carries (`IgnoreRule.category`, optional: a rule may well say
  « nothing to link » without claiming to say what for; **edited from the
  rules page**, `rule_action`'s `category` action, and never the pattern
  beside it - a page editing what a rule decides ON in passing would silently
  change which lines it catches. « Dépenses » tells the reader that giving a
  rule a category is THE way to stop typing the same word every month, and
  every rule written before that field carries none, so with no control there
  the advice was not followable). **What a person typed
  wins**, and the page says which of the two named each line - a rule edited
  next month must not read as somebody's decision. Both fields ride in the
  « Données » archive (`transfer/sections/bank.py`), and a category changed
  here is a conflict kept whole like any other decision: nothing rebuilds it,
  since a statement imported again brings the line back and not one word of
  what was said about it. Migration `bank/0004`, **WRITTEN and left to be
  applied**; blank everywhere until somebody fills one in, so nothing existing
  changes meaning.
- **A category is its NAME**, not a key: « Bières » typed on a debit is the
  same slice as the article category « Bières », and two slices of one name is
  a chart nobody can read. So the datalist offers the article categories, the
  suppliers of charges, and every category already typed on a line or a rule.
  « Sans catégorie » (nobody said) and « Catégorie non renseignée » (an
  article whose own category is blank) stay **two different words for two
  different silences**, the way « _Sans catégorie » and « Sans catégorie » do
  on Marges.
- **Nothing is forced.** « Sans catégorie » is counted, **listed first** and
  **never folded into « Autres »**, whatever its size: dropped from the pie it
  would make every other slice look bigger than it is.

**The page.** « du … au … » as everywhere, defaulting to the last twelve
months and **saying so on screen**, with « Tout l'historique » (`?tout=1`) as
on Marges; `common.last_twelve_months()` is now the one definition of that
phrase, and Marges reads it from there. The pie is a **server-rendered inline
SVG** (`views._build_spending_pie_svg`, the shape of
`recipes/views.py::_build_ingredient_pie_svg`; the palette moved to
`common.PIE_COLORS` so two pies in one app cannot drift into two legends),
biggest first, at most `MAX_SLICES` wedges with the tail as « Autres » saying
how many it holds - and **the figures beside it in a table** listing every
category, its share, its operations and its foot, because a pie nobody can
read a number off is decoration. A category that came out **negative** over
the window (a keg given back) is no wedge of a pie: it stays in the table and
a sentence reconciles `drawn_total` with `total`, never dropped in silence.

Three things the figures on screen owe a reader:

- **A share is rounded to the figure the page prints** (`_share_out`,
  `SHARE_PLACES`), the rounding put back on the largest - the rule `_scaled`
  already follows for a centime. An unrounded division printed three equal
  thirds as « 33.3 % » three times, and a reader adding the legend got
  99,9 % of a pie that is by construction the whole of what was drawn.
  « Autres » is the **sum of the shares it folds in**, not a second division,
  or the legend and the table beside it print two figures for one slice.
- **« Opérations concernées » is not a total**, and the column, the foot and
  a sentence all say so: one debit whose invoice spans two article categories
  counts in both rows, so the column adds to more than the statement's own
  operation count. Both figures are right; one under the other, on the one
  page whose argument is that it adds up, they read as a subtraction error.
- **« Déduit par les factures » is not « what came back on the account ».**
  `given_back` is the credit notes and deposits carried by the invoices linked to
  DEBITS. A supplier refunding money on the statement is a credit, which is
  income (« Entrées d'argent »), and this page counts what went out - the stat says so
  rather than letting its old name (« Rendu sur la période ») promise a
  figure it does not hold. Counting a credit against a category would mean
  deciding which credits are refunds and which are takings, and nothing here
  knows that.

Two figures under one label, said apart: the « Sans catégorie » **stat is the
table's row** (`unsaid_total`), while the list below it holds the debits with
**no invoice at all**; `unsaid_beyond_the_list` is what separates them - a
debit's own invoices falling short - and the stat names it. That list holds
every line with no invoice, **named or not**, the unnamed first: listed only
while unnamed, a category typed by mistake would have nowhere left to be
corrected.

**« Classer » settles nothing.** `spending.set_category` leaves `settled_by_hand`
alone: what the money was for says nothing about whether its invoice is still
to be found, and set there, naming a spending would quietly take its line out
of the automatic pass for ever. `clean_category` trims, drops control
characters and cuts to the column - it arrives from a text input, so a NUL is
« A string literal cannot contain NUL » on the INSERT and an over-wide string
is Django's problem on every later read.

**`spending_for` costs four queries whatever the statement holds**
(`spending.QUERIES`, pinned by a test): the lines, their payments, those
payments' invoice lines, the rules. An invoice on two lines is read once.

No new navigation link: « Dépenses par catégorie » is one of **Banque's
three tabs** (`bank/_tabs.html`: « Opérations », « Dépenses par catégorie »,
« Entrées d'argent », each page passing `bank_url`, `spending_url`,
`income_url` over its period and `bank_tab` for the lit one). The owner,
01/10/2026: a button to it in several places (Banque's header, Marges'
header, each page's own) was one too many - the tabs are the only buttons
now, and Marges no longer links there. Another entry in the topbar moves where the links wrap, which is
measured width by width by `accounts/tests/test_topbar_browser.py` (the bar
without its script, under 860 px) and `TopbarRoomInBrowserTests`.

**Out of the pie.** A category can be left out of THE PIE, and of nothing
else - the VAT paid over to the State, say: in the pie, it makes every other
wedge read smaller than it is. It is a VIEW carried in the address like the
period (`?sans=<name>` repeated, no model field, no migration), with
« Marges »' words to the letter (« sans : … », « remettre », « tout
remettre ») and the page saying nothing is saved:

- **The table and the total do not move.** A left-out category stays a row
  and stays in `total`, marked (`Category.left_out`, « hors camembert »),
  with no share: the page's argument is that its rows add back up to what
  left the account. It is never a wedge and **never folded into « Autres »**,
  which is a share of what IS drawn; the shares (`_share_out`) and « Autres »
  (`SMALLEST_SLICE`) are worked out over what remains. `Category.drawn` means
  « the pie draws it »: money in it AND not left out.
- **Three totals reconcile the pie with the statement**, pinned by a test:
  `total == drawn_total + given_back + left_out_total`, each category exactly
  one of drawn, left out (whatever its sign) or given back (at or below zero,
  not left out). « Déduit par les factures » reads `report.deducted` (every
  category at or below zero, left out or not), NOT `given_back`: nothing a
  reader leaves out of a drawing may move a figure about the money.
- **What is out is said**, in the line above the pie and in its
  `aria-label`: a pie that silently lost its biggest wedge reads as a pie of
  everything. « Sans catégorie » may be left out like any other and is then
  named like any other. Everything out is no pie and a sentence
  (`nothing_left_to_draw`).
- **`sans` carries NAMES, compared CLEANED on both sides.**
  `spending.left_out_names` passes each through `clean_category`, drops the
  empty ones and the repeats and keeps the order asked (a NUL or an
  over-wide name is a view, never a 500); `spending_for` and
  `views._left_out_rows` compare `clean_category` of the stored names too. A
  stored name need not be in that shape (a double space on a rule, an
  article category only trimmed, a supplier's name): compared as stored,
  such a row unticked came back still in the pie, beside « rien sur cette
  période » for its own name. Two stored spellings of one name leave
  together and « sans : » says their sum; a name with nothing in the window
  is KEPT, and said. `IgnoreRuleForm` stores a rule's category through
  `clean_category` too. No query is added.
- **The table is the selector**, the « Marges » way (`common.left_out_from`,
  under "The « Marges » page"): « Dans le camembert », a box per row ticked by
  default, and « Recalculer le camembert », a GET form. The boxes join it
  through their `form` attribute (`#pie-choice`, the form under the
  table): wrapped round the table, the form would also hold datatable.js's
  search box, and Enter in a field submits its form. The box's cell carries
  `data-sort` and its state as text (UI conventions).
- **Every link and form of « Dépenses » carries `sans`** like the period and
  `classement`, from one builder for the links and the forms' hidden fields
  (`views._spending_url` / `_spending_fields`): the kind chips, « Tout
  l'historique », « Revenir à une période », « Effacer » (dates cleared, kind
  and selection kept), the category forms' `next`, the window form,
  « Recalculer ». Links to OTHER pages carry the window alone
  (`_other_page_url`); « Dépenses » → Banque carries the window as applied,
  and Banque's two header buttons carry Banque's period
  (`views._bank_period`: a month as its first and last day, every month as
  `tout=1`) - the one to « Dépenses » did not, beside one that did.
- **The datalist offers only categories typed on DEBITS**
  (`known_categories`, `amount < 0`): the same field names a credit on
  « Entrées d'argent », and an income word offered here files a spending
  under it.

Tests: `bank/tests/test_spending_left_out.py`, down to the payload a browser
really sends, read off the page it drew.

### « Entrées d'argent » (`/banque/entrees/`, `bank/income.py`)

**What came into the account**, beside what the till says it **took**
on the same days: does the money that came in match what was sold? Neither
« Dépenses » (what the bank took) nor « Marges » (what was invoiced) - each
of the three says which it is and links to the other two.
`income_for(window) -> IncomeReport` is pure like `spending_for`, and costs
`income.QUERIES` queries whatever the history holds (pinned by a test).

**What each credit is, said on the page** (a recognition nobody can see is
one nobody can correct) and read the same way by Banque's « Entrées » tab
(`income.entry_for`, pure, no query per row):

- **A card payout** carries the provider's gross in its label (`PAYOUT_RE`);
  the line's amount is the net, and the commission is the difference **as it
  comes** - a net above the gross prints a negative commission, never
  corrected. Recognised by the WORDS, **never by the provider's name**: a
  name is the one thing a new contract changes. A number the pattern cannot
  read whole is **no payout at all**, not a payout with a wrong gross: it
  lands in « Autres entrées », in sight.
- **Cash deposited** and **cheques** are read off the bank's operation type,
  accent- and case-blind (`common.search_key`).
- **Everything else is « Autres entrées »**, named with the same
  `BankTransaction.category` and « Classer » form as « Dépenses », « Sans
  catégorie » first. Each page's datalist offers only what was typed on its
  own sign (`income.known_categories` credits, `spending.known_categories`
  debits), and `_moved_out_of_view` runs for debits only. A category typed
  on a payout or a deposit renames nothing: the rules above name them.

**« En caisse »: a person says what a credit is** (the owner, 01/10/2026:
another payment terminal will not print « TOTAL ENCAISSE », and its payouts
landed in « Autres entrées », out of the card balance, for good). Every credit
of the window carries the menu - Automatique, Carte, Espèces, Chèque, Avoir,
Titres-restaurant, Pas une vente (`models.IncomeSource`, the ONE vocabulary:
`income.CARD`… are its values) - and sits in exactly one of three lists
(`payouts`, `others`, `other_means`), the row's id `entree-<pk>` being where
the choice answers (`views.income_source`), whichever list it moved to.
- **Order, and nothing else** (`income.reading_of`): the LINE's own choice
  (`BankTransaction.income_source`), else the rules above WHERE THEY
  RECOGNISE the line (a gross printed, a deposit type: data about that
  line), else its PAYER's (`IncomePayer`), else « Autres entrées ». A payer
  never un-recognises a line: the provider prints the bar's own name as the
  payee of its payouts, so « Pas une vente » retained for a transfer from
  the bar's other account under that name moved every payout out of the card
  figures when the payer came before the rules (review, 01/10/2026). A stored
  value that is no source is passed over, never raised on. `entry.how`
  (`BY_LINE`, `BY_PAYER`, `BY_RULE`) is printed on the row, and on Banque's
  tab where a person decided - who decided is part of the answer.
- **The payer** is `income.payer_key`: `matching.alias_key(payee_of(...))`,
  the counterparty the bank prints, else the label's words without their
  digits, cut to the column - asked the same way everywhere, and an empty
  key is never retained. « retenir pour ce payeur » (the owner's choice: one
  click teaches a new terminal) makes the PAYER hold the choice and the line
  follow it like its siblings, so « Oublier » (`income.forget_payer`)
  undoes it whole - except a line the rules recognise, which no payer
  reaches: it keeps the choice as its own (`set_source`). AUTOMATIC with the
  box ticked forgets the payer. Unticked, the choice is the line's alone and
  beats everything. The counts said after a choice and on « Oublier » are of
  the credits a payer really decides (`follows_its_payer`). The box is drawn
  ticked only where the payer decides or nothing was recognised
  (`Entry.remember_by_default`). Measured on a scratch copy (read-only,
  01/10): every card payout of the statement shares one payer key, the
  other credits one each.
- **Read when the page is drawn, never written onto the lines**, like
  `IgnoreRule`: the payers are ONE query (`income.QUERIES` went to 6;
  Banque's tab reads them once, `income.known_payers`), and a statement
  imported again never touches `income_source` (`import_statement` only
  adds lines). A choice settles nothing - `settled_by_hand` is untouched.
- **A card credit printing no gross counts the amount received as its gross**
  (`Entry.gross_from_amount`; the owner's choice - a bank's own terminal pays
  the gross and takes its fee apart). Its commission is **None, never 0**:
  left out of `card_commission`, of its rate (`card_printed_gross`) and of a
  month's, said as « commission inconnue », and the card stat and the
  balance say how many payouts are counted that way (`card_from_amount`).
  Summed as 0 it read as « no fee » and lowered the rate; `month.commission
  += None` was a TypeError. **Every commission figure says what it leaves
  out**: `card_commission` is None when no payout of the window printed its
  gross, a month whose payouts all printed none reads « inconnue »
  (`Month.payouts`, `Month.from_amount`), and a partial figure - the card
  row's, a month's - carries « hors N au montant reçu ». 0,00 € printed for
  a month of such payouts read as « no fee » (review, 01/10/2026).
- **Meal vouchers and « Avoir » have a bank side** once a credit is said to
  be one (`BANK_SIDE`, `COMPARED`): their rows draw « — » while none is, never
  0 - an Écart of the whole till figure would accuse a transfer still filed
  under « Autres entrées ».
- **The payouts' menu is drawn on the row asked for only** (`?changer=<pk>`,
  « changer »): a payout a day is a form a day otherwise.
- **« Données »**: `income_source` rides with the line (compared, a conflict
  kept whole like `category`), the payers as `income_payers` (merged like
  rules). `IncomeSource.AUTOMATIC` is a member, not just a blank: `codec.load`
  checks every value against the choices, and a blank line was refused. An
  archive written before them says nothing of either: a line keeps its
  choice, and `income_payers` absent is « not said » - never an empty list,
  which « Remplacer » would read as « forget every payer ». **A payer a merge
  creates brings its lines' own choices** onto lines saying nothing here
  (`_took_its_payers_choice`): there they were one decision, and the payer
  alone turned a fee refund kept « Pas une vente » beside it into a card
  payout neither database counted, under « gardée telle quelle ». A
  « Remplacer » reads every field of a record inside its try (the moments
  included) - one it could not read was a 500 on the preview.
- Migration `bank/0005`, **WRITTEN and left to be applied** (the owner, after
  a backup, `migrate_tenants`; `serve` refuses to start until then).

**The till beside it** (`recipes.PosDailyPayment`, under « L'Addition »):
payments per method over the same days, and the takings (`revenue_ttc` of
the rows whose money was read, the rule « Marges » follows). `tips` is
payments less takings **on the days both are read whole** - a day with
unread takings would count its whole payments as a tip. What the till could
not read is said at the top with the command that fills it
(`laddition_backfill_payments`, `laddition_backfill_revenue`).

**Each side says where it starts and stops, and the gap (« Écart ») is taken
over the days both cover.** The till's payments can reach back long before the
statement's first line; compared over « tout » - where Banque's « Entrées »
stat lands - most card takings read as never arrived, with no warning.
`till_before_statement` and `bank_before_till` (card at the gross, cash,
cheques, meal vouchers, « Avoir »; never « Autres entrées », compared with
nothing) are counted apart
and left out of `MethodRow.difference`, while the two columns still show the
whole window; a warning names what the other side cannot see, and
« Comparer sur les jours couverts des deux côtés » opens the page from
`covered_since`. The statement's first and last day are one aggregate:
`QUERIES` did not grow.

**One row per means of payment.** The card compares the till with the
payouts' **gross** - the figure the till can equal - the commission in a
column of its own. Cash: the difference is « gardé en caisse ou payé en
liquide », said, never judged - the page sees neither the drawer nor what
was paid in notes. « Avoir » (paid before, usually by transfer) and meal
vouchers are the till's alone until a person says which credits they are
(« En caisse », above); « Autres entrées » is the bank's alone.

**A payout is tied to the sales by a RUNNING BALANCE**, « ventes carte pas
encore versées » (`running_balance`), **never by a claim that it paid given
days**: the provider's batches do not follow the till's service days and a
payment after midnight moves, so a payout matches a run of till days to the
cent only some of the time.

- **Computed over the WHOLE history, shown for the window**: a window never
  moves it (a test holds that).
- Only payouts after the till's first card day count. The balance starts at
  an **anchor**: the day within `ANCHOR_DAYS` before the first payout day
  whose card sales up to it come closest to what that day's payouts
  collected, **the latest on a tie**; after each payout it is the card sold
  since the anchor less every gross paid so far. The page says where it
  counts from.
- It sits near a level (the days not yet paid), and **a card sale never paid
  is a step it never comes back down from** - a ticket settled by transfer, a
  payout missing. That step is the finding; the page says so under the chart.
- No card day read, or no payout after the first: no balance, and the page
  says why (`NO_CARD_DAYS` names the backfill).
- The chart is `views._build_balance_svg`, one point per payout DAY, with its
  zero line (UI conventions); its figures are the « Versements carte »
  table's, the balance after every payout.
- **An exact run is an annotation**, « même montant au centime »
  (`exact_runs`): consecutive card days (adjacent among the days that sold
  by card, so a closed day breaks nothing) within `RUN_DAYS` before the
  payout, none claimed by an earlier one, summing to its gross to the cent;
  the run ending latest wins, then the shortest. Taken over the whole
  history in (date, pk) order, so a window never changes a payout's run.
  Most payouts have none with nothing missing: the balance says whether
  money is.

**The period** is « Dépenses »'s exactly (`?du=&au=`, the last twelve months
said on screen, `?tout=1`; `views._window_fields` is the one spelling of it
for both pages). « Mois par mois » runs from the first month holding
anything on either side to the last, never back to the empty years a wide
window reaches. The till counts by the day of the sale, the bank by the day
it received - a payout of the 1st pays the month before, and the page says
so. « Versements carte », a row a day, comes last: earlier, it pushed the
rest of the page out of reach.

Reached from Banque's tab and its « Entrées » stat, both over
Banque's period (`_bank_income_url`), and from « Dépenses ». No topbar link.

### A document's file: its download name, and Banque's zip

**Every door out names the file « Darty 11€55 01_10_2026.pdf »** (the owner,
01/10/2026): the supplier, the total TTC with « € » for the decimal point,
the date with underscores, « sans date » when there is none, the stored
file's own extension (a ticket's photo stays « .jpg »; a file stored with
none gets none - named « .pdf » it would be shown as one). One definition,
`invoices/filenames.py::download_name`; the stored file keeps its name.

**Every door opens the stored file through `accounts.views.open_stored`**:
the file under the bound tenant's media folder, resolved, a file - or None
(a name climbing out with « ../ », a link pointing out, a folder, a NUL: each
was a 500 or worse when the route opened `source_file` itself). The
`/fichiers/` view, the document's file route and the zip all use it.

- `invoices:invoice_file` (`/invoices/<pk>/fichier/`) serves a document's
  file under that name - inline for a PDF or a photo, so the frame and
  « Voir le PDF » show it and the browser's own « save » takes the name from
  the header; `?telecharger=1` saves it. The document's two pages link here,
  never to `source_file.url` (`/fichiers/…`, which still serves the stored
  name). Its headers are `accounts.views.file_response`, shared with that
  view: nosniff, no-store, sandboxed when not inline, SAMEORIGIN.
- **« Factures de la période »** on Banque (`bank:invoice_zip`,
  `/banque/factures/`, `bank/invoice_files.py`, a GET only - a HEAD would
  build it for nothing): every document a DEBIT of Banque's period paid,
  once each, in one zip. The period is read by `views._period` /
  `_in_period`, the same as the page, so the zip holds what the operations
  on screen are linked to; its name says the period (« Factures juin
  2026.zip »), and the tab rides along so « rien à télécharger » answers
  where it was asked. Two files of one name are numbered (`UniqueNames`,
  case-blind as Windows is). A document with no file, or whose file is gone
  from the disk, is listed in « Factures sans fichier.txt » inside the zip,
  and the page says how many - never dropped in silence. No document with a
  file at all: no button, and the route goes back to the page with a
  message. Served by `file_response` (nosniff, no-store), `application/zip`
  set by hand (Windows' registry says `x-zip-compressed`). The count beside
  the button is taken off the rows the page already read (no query).

Tests: `invoices/tests/test_filenames.py`, `bank/tests/test_invoice_files.py`.

### Export, import and clear (`transfer/`, « Données »)

One page (`/donnees/`, in the navigation) replaced « Exporter / Importer les
associations »: three tabs - Exporter, Importer, Effacer - each with the same
two groups of boxes, « Configuration » (fournisseurs, sources, associations
produits → articles, recettes, liens recettes ↔ ventes) and « Données »
(factures et tickets, banque, ventes, inventaires, consignes). The owner asked for it on
19/09; the old addresses redirect there and the old associations JSON still
imports (`transfer/legacy.py`).

- **A section is a class** (`transfer/sections/`, contract in `base.py`):
  `count`, `snapshot`, `export`, `load`, `apply`, `prune`, `clear`. What
  depends on what is **one table**, `transfer/registry.py::INFO`: ticking a
  section to export or import ticks what it requires; ticking one to clear
  ticks what requires it (clearing the suppliers clears everything but the
  bank). The server enforces the same closure the page's JS shows - a
  selection that is not closed is refused, never completed in silence.
  The bank requires nothing on purpose: a hard link to the invoices would make
  « Effacer les factures » wipe the bank too.
- **Natural keys, never pks** (`transfer/keys.py`): a supplier by its code
  (then its name), a product by (supplier code, raw name) and **never
  fuzzy** - an import must not merge two products -, an invoice by (supplier,
  number), else its stored sha, else its file's sha, with an occurrence for
  byte-identical documents (two of the real Monoprix tickets), an invoice line
  by its rank in its invoice, a bank line by its fingerprint. A supplier the
  fournisseurs section skipped is refused for the rest of the run
  (`SupplierResolver.refuse`): found by its name instead, its documents would
  land on the supplier that name belongs to here. A product's folded name
  finds it only when the archive does not name that product itself
  (`ImportContext.products()` hands every section a resolver told
  `keys.archive_product_keys`: the product keys of the associations, the
  invoices and the stock takes, imported or not). SQLite's case-blind
  comparison is ASCII only, so the app makes « KLOSTERBRAU FÛT 30L » and
  « Klosterbrau Fût 30L » two products of one supplier. Imported into an
  empty database, the second was merged into the first: its classification
  skipped « en double », its invoice line and litres moved (the till's
  `TillProducts` has the same rule).
- **Derived data is rebuilt, not copied** (`transfer/rebuild.py`): purchase
  movements (`inventory.services.rebuild_purchase_movements`), invoice
  statuses (`refresh_invoice_statuses`), the till's sales per recipe
  (`resync_recipe_from_daily_quantities`) and till product totals
  (`recipes.sales.recount_pos_products`) - each bulk helper proven equal to
  the per-object service it stands for (`inventory/tests/
  test_rebuild_movements.py`, `recipes/tests/test_recount_pos_products.py`).
  A stock take's value is **not** derived: it is frozen, and copied as it is,
  with its sources re-pointed by key - a take whose invoice line cannot be
  found, or no longer names the product it was priced from, is refused whole
  rather than revalued.
- **An import writes rows; it reads nothing again.** No OCR, no learning, no
  `SupplierChange` - going through `import_receipt` would have recorded a
  « premier document » for every supplier and lit « À voir » thirty times.
  A supplier renamed by an import goes through `rename_supplier` (a charge
  supplier's charge items follow), and the change it records is deleted in the same
  transaction. An invoice replaced by an import keeps a line a stock take was
  priced from, updated in place; one whose replacement would remove such a
  line, or put another product or another name at its rank, stays as it is,
  and the report says why (`_trail_kept`). Lines are paired by rank, and a
  line taken out above a priced one after the export shifts every line
  below it: on a 19/09 copy, 8 of 322 counts came out priced from another
  product's purchase, and nothing said so. A keep in apply must not rest on
  data the same run's prune removes. Under « Inventaires » « Remplacer », a
  count the archive does not name is pruned after the invoices apply, so
  `_trail_kept` ignores it. It asks `stock_takes.named_takes`, which pairs
  counts the way that section's apply does (by moment, then by rank among
  the counts of that moment), so the two cannot disagree. A line only such
  a count holds goes in the invoices' prune, after the counts' (PROTECT).
  On a 19/09 copy, a full « Remplacer » restore kept a Monoprix ticket for
  the count it then deleted, and brought it back one line short. A document
  whose checks or VAT table are not in the shape the application writes is
  skipped (« contrôles illisibles », « table de TVA illisible »): stored, one
  `parse_checks: [1]` took « À vérifier » down.
- **Fusionner** adds what is missing, fills what a section lists as fillable
  when it is blank here, and never changes a value that exists (a conflict,
  said). **Remplacer** makes the section exactly the archive, except what kept
  data still needs (kept, said). Chosen per section, not per group: a section
  ticked because another requires it is merged, so « Remplacer les recettes »
  cannot wipe the classifications the recipe file does not carry. A record
  keeps the moment it was made (`imported_at`, `created_at`, `recorded_at`,
  restored after the insert that overwrote them - a product's too, whichever
  of associations or factures creates it), and that moment is never
  compared: an export of the same data taken a minute later merges as
  « inchangé ». A till product « à lier » here is linked as the archive
  links it (a blank filled: after « Effacer », the links must come back).
  Nothing records that a person detached one by hand (`links.set_aside`
  keeps no trace), so the report names each product a merge links or
  ignores that way, under « À savoir », with how to detach it again. An
  « ignoré » here is on the model: a conflict, kept.
- **Prunes and clears run in reverse order**, so a replaced stock take
  releases its invoice lines before the invoices prune, and « Liens » applies
  « Ventes »' rule (a till product with no day and no link goes) to the till
  products it releases when « Ventes » is cleared or replaced in the same run -
  cleared first, « Ventes » had left 68 empty « à lier » products behind.
  Happy-hour names are checked against the state an import ends in, so two
  recipes can swap them.
- **A row is counted once, as what happens to it.** A clear or a prune of
  « Liens » removes links, not till products: its report counts « liens
  retirés » and « statuts « ignoré » retirés » (`till_links.release`, and
  « Recettes »' clear when it takes links with it); the till product stays,
  « Ventes »' data. A classification removed from a product that stays is
  « À modifier » (« produits classés »), and « À supprimer » counts only the
  products that go (« produits sans facture »). Counted as both, a full clear
  of the 19/09 copy announced 1 462 products for the 794 it held, and 279
  till products for 211; it now reads 668 + 126, and 211. « Ventes » counts
  its per-day rows as « quantités par produit et par jour (caisse) » and the
  days apart (« jours de caisse »): as « jours de vente », the 15 850 rows of
  19/09 read as 43 years of sales (211 products over 662 days). The
  manifest's counts are `count()`'s, under the same labels (« prix connus »
  is the review page's word, since an « article » is a StockType), so the
  import tab compares like with like. The AI pseudo-supplier is no supplier
  on the page: count(), the manifest and the report's « fournisseurs » all
  leave it out (`suppliers._tally`). A change to it is counted on a row of
  its own (« fiche de l'analyse IA »), so the preview shows it and the
  safety archive still takes the section. Counted in the report only, a
  merge of the 19/09 copy said « 30 inchangés » for the 29 suppliers the
  page announced. A document updated, or given a file back by a merge,
  still counts the files it keeps - and, merged without a conflict, its
  lines - « inchangés » (`InvoicesSection._untouched_files`): a « Remplacer »
  restore of that copy that updated one ticket counted 1 518 of its 1 520
  files.
- **Two steps, and the preview is the real run rolled back**
  (`runner.run_import(preview=True)`), so what it announces is what happens.
  Files are only written by the confirm, and removed if it fails; deletions
  of files happen on commit. And the confirm is held to its preview
  (`run_import`/`run_clear(expected=)`): just before commit its report is
  compared with the preview's, and any difference - a ticket imported, a
  payment linked in the up to 30 minutes between - undoes it
  (`runner.NotAsPreviewed`) and the page shows the new preview (« La base a
  changé depuis l'aperçu »). A preview of « 0 à supprimer » had let a
  document that arrived since be pruned, its photo deleted and in no archive
  (review, 19/09). The confirm also names the preview on its own page
  (`views.SHOWN_PREVIEW`, the hidden field `apercu`: `RunReport.fingerprint`,
  a sha256 of the outcome). With the stage open in two tabs, « Importer »
  clicked under « 0 à supprimer » ran the preview the other tab had made
  since, and deleted a ticket that had arrived in between, with its photo
  (review, 19/09). **A confirm naming another preview than the one stored is
  worked out again, and runs when the page's announcement still holds**
  (`_still_shown`): the guarantee is that the run does what the page said,
  not that the session's last preview is the page's. Refused outright, it
  was a dead end - the owner ticked, typed EFFACER and clicked, got an
  orange warning and nothing deleted, over and over (20/09), because the
  stored preview had been replaced by one whose only difference was a note
  counting stray files. **What a run DOES is what it is held to**
  (`RunReport.outcome`, `SectionReport.outcome`): tallies, conflicts,
  skipped and kept - never the notes, which also say things about what the
  run leaves alone (« 4 fichiers que plus rien ne cite dans media/ restent
  tels quels » changes when a file appears). **A refusal says which it is**:
  another preview says something else now (`OTHER_TAB_*`), the page names no
  preview at all, drawn before this version (`OLD_PAGE_*`,
  `_why_not_shown`), or the database moved between the confirm and its
  commit (`MOVED_*`). So a report's notes read the same in the preview and
  the confirm - a participle, never a tense.
- **Before any confirmed import or clear**, a SQLite backup of the database,
  and an importable archive of every section whose rows the run changes or
  deletes, whichever section's code does it (`safety.sections_at_risk`,
  chosen from the preview the run is held to): the bank when the invoices'
  prune or clear takes its payments, imported or not. And every section
  imported with « Remplacer » that changes at all, creations included:
  importing that archive back with « Remplacer » is the undo, and only its
  prune removes what the run created. A bank link a person had undone, made
  again under a line whose record had not changed, stayed after the undo
  (review, 19/09). Only a section merged that just fills blanks or adds is
  left to the database copy. Both go in the espace's `backups/` (never
  deleted by the app, so a confirm undone as `NotAsPreviewed` leaves its
  backups there; the page names no server path). Staged
  archives wait in the espace's `staging/`, **not in its media/** (the file
  view serves media), listed on the Importer tab (« Archives en attente », Reprendre /
  Annuler) until the 24 h sweep. Clearing asks for « EFFACER » typed.
  Putting a database copy back is the administrator's, server stopped, and
  means deleting the espace's `db.sqlite3-wal` and `-shm` first: in WAL mode,
  a `-wal` left by a server stopped hard is read back into whatever file is
  named db.sqlite3 (a scratch probe, 19/09: the copy put back came out
  holding the newer rows).
- **The bank's links come back with their invoices, in one import.** When
  invoices go (« Effacer factures », or a « Remplacer » that prunes a paid
  invoice), their payments go with them and the lines stay « réglées à la
  main »; the report says to re-import « Factures et tickets » and
  « Banque » together from the backup (`sections/invoices.BANK_NOTE`).
  « Banque » alone restores none of them, since every link names a document
  that is gone. Imported together, every link comes back (scratch copy
  19/09: 41 of 41). Imported in two runs, the bank cannot tell those lines
  from ones a person unlinked: it brings back the AUTO links (10) and reports
  the hand-settled ones as conflicts (31), with a note (`bank.UNDONE_NOTE`);
  « Remplacer » on the bank then restores them.
- **Never exported:** Metro's `scrape_*` fields (the firewall's pause - a
  restore resetting it would let the next gather sign in), `SupplierChange`
  (its undo data holds pks), job history, `ai_suggestion` (the review panel
  fills it again when it is drawn), « Combler les écarts »' list
  (`GapFillEntry`, a scratch list that goes with its stock take), its
  exclusions (`GapExclusion`) and its duration (`GapFillSetting`). Suppliers
  with a reader or a till of their own are never deleted by a clear or a
  replace; a clear only forgets what they learned.
- **Nor the till's money and payments.** « Ventes » carries the quantities
  only; the day's money and `PosDailyPayment` are read again from the files
  on disk (`laddition_backfill_revenue`, then `laddition_backfill_payments`),
  and the section's description on the page says so. `count()` leaves the
  payments out, since the Importer tab compares it with the archive's counts.
  « Effacer » deletes them all, « Remplacer » those of every day it leaves
  without till sales, and both add `sales.PAYMENTS_NOTE`, the command that
  brings them back.
- **« Personnel » is in no section**: employees, timesheets and signature
  requests are neither exported nor cleared, and the espace's `private/` is in
  no archive. Only the SQLite copy taken before a run holds those tables.
- **« Consignes » is the tenth section** (`sections/returnables.py`, « Données »
  group, order 100): it requires « fournisseurs » (a format's and a
  reprise's supplier, by code) and only recommends « factures » (the invoice
  check is read when a page is drawn, nothing of it is stored). Keys: a type
  and a format by `search_key` of their name with spaces collapsed (the
  forms' own uniqueness rule, so « Futs » finds « Fûts »), a reprise by its
  random `reference`, a bon by its sha256. A merge compares what lies
  outside a bon's reading (format, origin, names, mail, text, file); the
  reading is copied when the bon is created, and under « Remplacer » only when
  its text or its format changed - never compared, never read again (the
  tests make `pdf_text` and `read_slip_text` fail). The moments come back as
  they were; `Pickup.updated_at` (« modifiée ici ») and `Slip.read_at`
  (« relue ici ») never travel. Every motif imported goes through
  `returnables.patterns` exactly as the forms check it (« motif refusé :
  <champ> — <raison> »), references and checks are validated (« lecture
  illisible »), a count outside 1..9 999 is refused before the database's
  CHECK - each skips its record with its reason. Files only under
  `consignes/` (`archive.STORAGE_FOLDERS`), written through the same
  `check_file`/`save_file` as the invoices', old ones deleted on commit; a
  photo missing from the exporting disk leaves its reprise without it (said),
  a bon whose PDF was missing is skipped. The seeded types and format are
  counted and cleared like the rest - the safety archive brings them back.
  The supplier page and « Données » name the consignes rows holding a
  supplier (`supplier_views.returnables_refusal`, one sentence for both).
- **A portal from an archive is never trusted** (`sections/sources.py`): the
  next gather types the .env variables it names into the page it names. A
  portal naming a variable the application reads for itself is refused, by
  the import and the source form alike (`invoices.models.APP_ENV_PREFIXES`,
  in `WebsiteInvoiceSource.clean`: Metro, the mailbox, the till, the AI,
  Django, the signatures and the mail server; a test checks the list against
  config/settings.py). An import
  never switches a portal on **unless the archive is one this installation
  wrote itself**: a file in `backups/`, where only `safety.before` writes
  (`ImportContext.own_backup` - a manifest can claim any `reason`, so the
  folder is what says it). Restoring its own backup puts the portals back as
  they were, .env note included. Left inactive, the owner's five portals sat
  out the next gather, which searched the mailbox only, and nothing on the
  page said why (20/09). From any other archive, a portal it creates, or
  whose sign-in settings it changes (`SIGN_IN_FIELDS`: every portal field
  but « Navigateur visible » - the address, the variables, the links to
  follow and every selector, since each decides where or how a password is
  typed), arrives inactive, and the report
  names its address and variables so the owner can tick « Active » after a
  look (`as_restored` in the tests is that case).
  Merged or replaced, a portal the archive has active and this database has
  inactive is not a conflict, since « Remplacer » would not switch it on
  either. It is « inchangée », with a note under « À savoir » saying why and
  what to do (`sources.LEFT_OFF`: « laissée inactive (un import n'active
  jamais un portail) »). One active here that the archive has off is still
  a conflict.
- **The old associations file** becomes an archive holding only
  `associations.json`, its suppliers named through placeholder codes « ~1 »…
  and found by name. Nothing is repaired on the way: a factor of 0, a blank
  article or a unit that disagrees with its article is skipped with its
  reason, never turned into 1 or dropped in silence as the old import did. A
  product it names that this database lacks is now created - under a supplier
  this database has: the file names no supplier of its own. It never carried
  an article's losses, so an article it creates takes 10 %.
- **A product is kept in its article's unit** (`inventory.views.assign_product`
  mirrors it), and the associations section refuses one that is not: its
  conversion would no longer mean anything. 0 such products on 19/09.
- **A charge item is a product, not a supplier**: the associations
  section refuses only a product flagged `is_expense` (assign_product's
  rule). A supplier turned to charges keeps the products a stock item
  claimed (`redo_as_expenses`). Refused at the supplier's level, they lost
  their classification and purchases in a round trip, and their own export
  never merged as « inchangé ». One the archive classifies but this database
  lacks is created classified, with `is_expense` False.
- Everything is refused while a gather or an import job runs: an import holds
  SQLite's write lock for its whole transaction.
- The archive (`.zip`, format `marginmate-archive` v1, `manifest.json`, one
  JSON per section, `files/`) holds the owner's invoices, bank and prices:
  never in git, never in a fixture. Build and read it streaming - the files
  are ~420 MB - and never `extractall`: only members the manifest declares are
  opened, and a dangerous name refuses the whole archive. A member damaged
  on the way (a CRC, a deflate stream, a local header) is refused in French
  (`archive.DAMAGED`), and the manifest's counts are shown only when they
  are numbers: both were a 500. So was text no UTF-8 write takes: half a
  UTF-16 pair written as a JSON escape (`"\ud800"`) is valid JSON, and is
  now refused as it is read (`archive.unencodable`, in the manifest, a
  section or an old associations file). A manifest's `created_at` the page
  cannot show in local time (year 1 at +14:00 overflows) reads « date
  illisible » (`archive.shown_moment`): it made the Importer tab itself a
  500, the page to restore from and the only « Annuler » of that stage.

Test every section the same way (`transfer/tests/support.py`): a round trip
(export, clear, import, same snapshot by natural keys, files byte-identical),
importing its own export changes nothing (every record « inchangé » - this is
what catches a Decimal's places or a time zone), merge versus replace on one
record of each kind, the preview changing nothing - and all ten at once
(`test_full_round_trip.py`), since what crosses sections (a stock take's
invoice line, a payment to a ticket known only by its file) only shows there.
Rehearse on a scratch copy of the real database, never on it: `preview_start`'s
server runs on the real one. On the 19/09 copy, through the page: exporting
everything took 3 s (422 MB, 24 MB of Python memory), clearing everything 5 s
with its backups, importing it all back with « Remplacer » 5 s to preview and
9 s to confirm - each confirm held to the preview its page showed, none
refused -, and the same archive merged again said « inchangé » for every
record (14 s to preview - it hashes every file - and 4 s), the five portals
with a note each. On battery (the CPU at 1.9 GHz) every step took about
twice as long, the merge's preview 30 s. Every row, file and section
snapshot came back equal to the untouched copy, except what is rebuilt
(when the purchase movements and the till's sales per recipe were written),
the review panel's pre-fills, the suppliers' history and those five
portals: back inactive, since an import never switches one on, and named by
the merge under « À savoir » (« laissée inactive (un import n'active jamais
un portail) ») until the owner ticks « Active » - 0 conflicts on the 19/09
copy.

### A big POST is rejected before any view runs

Django caps a request at `DATA_UPLOAD_MAX_NUMBER_FIELDS` — **1000 by
default**, which a stock take reaches at **332 rows** (three fields a row plus
the management form; 249 when editing, since each saved row also posts its
id). Over the line the request dies with `400 Bad Request` in core handling,
*before* the view exists: there is no form to re-render, no message, and every
count the user typed is gone.

That is not hypothetical — it is what "j'ai perdu mon inventaire après avoir
cliqué sur enregistrer" was, on a real evening's counting. `config/settings.py`
now sets it to 25,000. Any new formset that can grow with the data inherits
the same ceiling, so check it before assuming a save is "just slow".

### An id read from `request.POST` is checked before it reaches a query

`pk="abc"` in a filter is a `ValueError`, so a tampered or stale form is a
500 instead of a message. A view that reads an id by hand (not through a
form field) checks `common.is_id(posted)` first and treats anything else as
not found (`_forget_price`, `merge_stock_type`, `pos_product_assign`, the
type pages). Not `str.isdigit()`: "²" is a digit to it, and no
int - the query raised all the same; nor more than 18 digits, past which
SQLite's integer overflows.

### Formsets: no spare row on a saved record

`extra=1` renders a blank line under the real ones. On a *new* record that's
a convenience; on a *saved* one it reads as a bug — take an item out of an
inventory, reopen it, and the blank row sitting where something used to be
looks exactly like a removal that half-worked. It was reported as one.

`StockTakeLineFormSet` uses `extra=0` and the page adds the first row itself
(`stock_take_form.html`), so what is on screen is only ever what is really in
the count.

### Formsets: test what the browser actually posts

Removing a row client-side leaves a **gap** in the posted indices (0, 1, 3
with `TOTAL_FORMS=4`); index 2 is absent from the POST entirely. Django only
skips an empty extra row when `has_changed()` is False, and any field with
an `initial` makes that True — so the invisible row gets validated and fails
"required" where nobody can see or fix it. This has now hit three separate
forms.

Use `common.BlankRowTolerantFormMixin` and list the fields that carry
defaults or bookkeeping in `bookkeeping_fields`. Then test it: post
non-contiguous indices, and post a row left at its pre-filled default.

### Recipe variations scale multiplicatively — never enumerate them

A recipe's variations are the cartesian product of its choice groups, so 20
either/or ingredients is 1,048,576 variations. That is not an exotic recipe.

Anything the app renders must be linear in the number of **ingredients**:

- `Recipe.summary(ingredients)` — count and all four ranges, O(ingredients).
  It works because every displayed quantity is monotonic in the total cost,
  so the extremes come from the per-group extremes.
- `Recipe.variation_at(i)` / `variation_for(selection)` — one variation
  without building the others.
- `Recipe.variations()` — the real cartesian product. **Only safe on a
  recipe you know is small.** Nothing rendered uses it; tests do, as the
  reference the O(n) version is checked against.

The detail page shows one variation, chosen by `?v=0.2.1` (one option index
per group). Under `MAX_LISTED_VARIATIONS` it lists them all; above it,
one dropdown per choice group.

### Counting an inventory: priced live, by the same code that saves it

The stock-take form prices every row as it is typed, and totals them. Two
rules keep that honest:

- **The preview is not a second implementation.**
  `views.value_stock_take_line` goes through `value_counted_quantity` /
  `value_counted_stock_type_quantity` as of the same date the save will use,
  so the figure on screen and the figure stored cannot drift. A "close
  enough" sum computed in JS is precisely how this codebase has shipped
  silently wrong money before. A row that *can't* be priced says so and drops
  out of the total rather than showing a stale number.
- **A saved line already knows what it is worth.** Its `value_ht` is frozen
  (see `StockTake`), so the running total is right the instant the page opens
  — no request, nothing re-priced. Only rows the user edits ask the server.

The datalist offers only what had actually been delivered by the date being
counted (`services.first_purchase_dates`), and follows the date field live.
`StockTakeLineForm` refuses it too, against the date **submitted** rather than
the one on the saved row — changing the date and adding a line happen in the
same POST. Something with no dated invoice is never "too new": we can't prove
when it arrived, and hiding it would drop real stock out of a count.

A row being **deleted** is never re-judged. Validating a row on its way out
traps the user in an inventory they can no longer fix.

**An invoice line with no printed volume stores `total_volume` 0, not NULL.**
`product_counting_ratios` skips those lines (`total_volume__gt=0`); tested
with isnull, the 0 ratio went through and `stock_units_per_item` turned
eleven 1 L bottles counted on a shelf into 0 litres - 62 real count lines on
210 products read as entirely missing in the « Écarts » and stock pages. An
unmeasured product's items convert with `stock_equivalent`, exactly as its
purchases were booked.

**A stock item in use is merged, not deleted.** Recipes, stock-take lines and
sale lines hold their stock item with PROTECT. `merge_stock_types` moves them
all, in one transaction (a count that measured both items becomes one line
adding both up) - it used to fail on them after the products had already
moved. Deleting an item still in use, alone or through "supprimer les articles
vides", is refused with where it is used; "vide" means no product and no
movement, so a loss written down against an item survives.

### N+1s hide in per-object properties

Three of these together were most of the app's runtime, and none of them
looks like a query at the call site:

| Looks innocent | Actually |
|---|---|
| `stock_type.current_unit_cost_ht` | one query per stock type |
| `typical_item_size(stock_type)` | three queries per stock type |
| `stock_units_per_item(product)` | one query per product |

The « Écarts » page called all three per pool — 1,766 queries, 2.4 s. The batched
forms (`_movement_totals`, `typical_item_sizes`, and `ratios` passed into
`stock_units_per_item`) took it to 125 queries and 0.64 s. Same for
`EntryResolver`, which resolves a whole formset's typed names in three
queries instead of one per row.

The tell: SQL time near zero while wall time is seconds. That is hundreds of
tiny queries, not a slow one — profile with `connection.queries`, not EXPLAIN.

**`prefetch_related` does not reach a method that builds its own queryset.**
`Recipe.choice_groups()` re-reads its ingredients (it wants them ordered,
with their stock items' movements), so prefetching them at the call site
bought nothing: every recipe was asked three times over - its usage terms,
its pools, its allocation - and each ask was a query, with another for the
movements behind it. That is 290 queries to draw **Produits & charges** and 277 for
**Écarts**. `recipes.models.variation_scope()` is the memo made for exactly
this, and wrapping `quantities_sold` and `compute_variance` in one took them
to 114 and 109. It is scoped, not cached on the instance: a grouping changes
every time someone presses "OU".

### Shrinkage: pool the alternatives, never guess the split

`inventory/variance.py` answers "where did the alcohol go" between two stock
takes:

```
unexplained = (opening + purchases - closing) - known_losses - sold
```

Recipes are fuzzy ("vodka OR gin"), so which bottle a drink came from is
unknowable. Rather than guess, stock items that appear as alternatives are
**pooled** (union-find over the choice groups, transitively), and the pool is
accounted for as one thing. Within a pool substitution is invisible; between
pools the accounting is exact.

The headline is deliberately a **floor**: the gap is valued at the *cheapest*
member of the pool, and stated in bottles of the format that item is usually
bought in (`typical_item_size` — the most-purchased format, not the largest).
The real loss is never smaller than what's reported.

Two rules learned from real data:

- **Never report an uncounted item as missing.** If a stock item wasn't in
  both counts, opening and closing are 0 and everything bought looks
  evaporated — 360 L of beer once topped the report, worth more than the
  genuine finding. Those go in `report.incomplete` ("count this next time").
- **Negative variance is a data error, not shrinkage.** You cannot pour stock
  you never had, so `is_impossible` means a miscount, a missing invoice, or a
  wrong recipe.

#### Three things eat the headline before shrinkage does

On the real database the report read €27,318 missing. Almost none of it was
theft, and the order these come off matters more than the arithmetic:

1. **One stock take means "since the beginning".** With no earlier count the
   report compares *everything ever bought* against today's shelf. A second
   count windowed it to twelve months: €27,318 → €12,311.
2. **`StockType.loss_percent` is deducted** (`PoolVariance.loss_allowance`,
   accumulated at each member's own rate, never averaged across a pool):
   €12,311 → €10,500. It comes off `unexplained_min` to give `shortfall`,
   which is what `value_missing_min` and `is_missing` use. `unexplained_*`
   stay raw on purpose, and `is_impossible` still judges on them — an
   estimate must never be the thing that declares the data wrong.
3. **An item no recipe uses can only ever read as 100% missing.** The till
   sells a glass of prosecco or a saucisson board; nothing says what's in
   one, so every drop that leaves is unexplained. That was €8,165 of the
   remaining €10,500 across 54 pools. `PoolVariance.in_recipes` flags them
   and `VarianceReport.only_in_recipes()` sets them aside — `?recettes=1` on
   the « Écarts » page, which shows **both** totals side by side so the filter
   can't hide what it costs. Actual candidate shrinkage: €2,335.

The fix for (3) is writing recipes, not tuning the report — see the "À lier"
backlog, 187 of 211 till products unmapped at the time of writing.

Sales come in through `recipes/sales.py::record_sales` and nowhere else, so a
new source (API, CSV, whatever the till turns out to be) is just a function
that produces `(recipe name, date, count)`. Unmatched names are **returned,
never dropped** — silently discarding one understates every later report.

### "Vendu" does guess — but only where the shelf lets it

The variance report pools alternatives and refuses to say which bottle a
drink came out of. The stock page's **Vendu** column can't do that: it has one
row per item and has to put a number on each. So it guesses — priciest first,
because nobody reaches for the well brand while the good stuff is open — but
what was actually bought rules most splits out, and
`variance.allocate_choices` walks the rest:

1. Fill the priciest option until it reaches **what was bought less that
   item's `loss_percent`** (10% by default, editable per stock item: the
   over-pour, the last centilitres, the keg's foam). Then the next-priciest.
2. Once **every** alternative has had that turn, release the allowance and go
   round again up to 100% of what was bought.
3. Anything still unexplained goes on the priciest option, pushing it past
   what was bought, and the row turns red.

Three things that are load-bearing:

- **Servings are whole, so round one rounds UP** against the allowance (never
  past what was bought). 22 half-litre pints out of a 6 L keg with a 10%
  allowance is 11 pints — 5.5 L — not 10 and a stranded 0.4 L that no sale
  could have produced.
- **Round one is global, not per recipe.** Releasing the allowance per demand
  would let one busy cocktail drain a bottle to the last drop while a full
  alternative stood untouched beside it.
- **A fixed ingredient is never clamped.** If the recipe says every Caipirinha
  takes 50 g of lime, 100 of them took 5 kg whether or not 5 kg was ever
  bought. Saying so is the whole point of the column: red means a missing
  invoice, a wrong recipe, or a till product linked to the wrong drink — *not*
  shrinkage, which is stock that left without being sold.

Greedy, not optimal: demands are served in a stable order (recipe, then
group) and an early one can take capacity a later one wanted. A global
optimum would be a different program and no more defensible to a supplier.

#### The ceiling is different between two counts

`?inventaire=<pk>` on the stock page scopes all of it to one stock-take
window (`variance.stock_between`, sharing `counts_by_stock_type` and
`movements_between` with the « Écarts » page so the two can't disagree about what
a period contains). The closing count is the only thing chosen; the opening
one is whichever came before it, exactly as in `compute_variance`.

All time, the ceiling has to be "everything ever bought" — nothing deducts
sales from the ledger, so that's all there is. It is the ledger (bought, less
the losses written down), not the « Acheté » column beside it, which counts
the purchases alone (`catalogue_context` sums both from one scan; they differ
only once a loss is recorded). So the red « Vendu », its « ? » and the
headline's « Vendu > acheté » say « pertes déclarées déduites »: 11 L sold of
12 L bought, 2 L broken, is red beside an « Acheté » above it, and « vendu
plus qu'il n'en a été acheté » alone was false. Between two counts both ends
were physically measured, which gives a far tighter one:

```
sellable = (opening + purchases − closing) − known_losses
```

Stock still standing there at the closing count obviously wasn't poured, and
a bottle already written down as broken can't have been either. The part of
`sellable` no sale explains (`SoldQuantity.unexplained`) is the shrinkage
figure; sales *above* it are the data error, same red as before.

Two things carried over rather than rediscovered:

- **An item missing from either count gets no verdict.** Its opening and
  closing read as zero, so everything it bought looks evaporated — the row
  says "non compté" and is excluded from every total. Same rule, same reason
  as `VarianceReport.incomplete`.
- **The per-item view is the actionable one, the pooled one is the
  defensible one.** Both stay: the « Écarts » page still values the gap at the
  *cheapest* pool member (a floor you can put to someone), while the stock
  page names bottles by attributing the pool (a guess you can act on). They
  cross-link.

### « Combler les écarts » (`/stock-takes/fill-gaps/`, `inventory/gaps.py`, `inventory/gap_planner.py`)

The owner, 01/10/2026: « entrer une somme d'argent à encaisser et que cela
donne une liste de produits (recettes) qu'il faudrait vendre pour cette somme
pour remplir les trous de stock ». A GET page of « Inventaires »
(`STOCK_TAKE_VIEWS`), linked from the stock-take list and from each take;
`?depuis=<pk>` is the count the gaps run from (the latest by default, an
unknown one falls back to it and says so) - not `inventaire`, which names the
CLOSING count on Produits & charges. An amount TTC is POSTed to
`stock_gap_filler_add` (`montant`, read by `common.read_amount` - moved from
`returnables.patterns`, which re-exports it: no NaN, no Infinity, two
decimals at most - above 0 and at most `gaps.MAX_AMOUNT`) and kept.

**A list, amount after amount** (the owner, 01/10/2026: « si je rentre 7 €
et encore 7 € … que cela soit mis à jour pour combler les trous au fur et à
mesure. Ensuite je peux "clear" la liste »). Each amount is a
`GapFillEntry` of its stock take, planned ON TOP of the entries before it -
their sales are handed to the planner as `already` and count as added - so a
second 7 € fills what the first left behind rather than proposing the same
glasses again. An entry keeps its lines as proposed (names, prices, till
button, the gaps they fill): the owner may have rung them up, and an
invoice imported or a recipe repriced later must not rewrite them. The page
shows the last entry first (« À encaisser : X € », `#a-encaisser`), the
earlier ones folded, the totals of the whole list, and the gaps with what the
whole list adds; « Annuler la dernière saisie » (`stock_gap_filler_undo`) and
« Effacer la liste » (`stock_gap_filler_clear`, `data-confirm`) are POSTs
redirecting back, a GET to any of the three goes to the page. An amount
nothing can be proposed for is said (why, by `Plan.reason`) and NOT kept.
`sales_seen` records every serving sold from `sales_from` on, as known when
an entry was made (`gaps.servings_from`); `sales_from` is the day BEFORE it
was made, because the till files a sale rung after midnight under the day its
service began - dated by the calendar, a list made and rung up at 00:30 was in
the previous day's sales and never flagged. Once that count moves - a newer
day imported, or one of those days imported again with more sales, which
leaves the last day of sales where it was - the rung-up list is in them and
counts twice, and the page says to clear it (`gaps.list_is_stale`). A late
import of an older day moves nothing; one of the day before is a false alarm
on purpose.
Each of the three forms carries the last entry the page showed (`derniere`):
a second click on « Ajouter » or « Annuler la dernière saisie », or « Effacer
la liste » from a tab left open, posts a list that has moved and is refused
rather than acting twice. The check is made again inside the transaction
that writes - SQLite's IMMEDIATE mode takes the write lock as it opens -
because planning takes a second and two clicks inside it both passed a
check made before; the buttons also go busy once pressed (`data-busy-label`). What an entry
stored is read by `gaps.entry_rows` alone (a bool is no count, NaN no price). The list is never exported by « Données »
and goes with its stock take (CASCADE).

Three answers of the owner fix the arithmetic:

1. **gap = counted at the take + bought since − lost since − sold since**,
   « sold » being the stock page's « Vendu » from the same engine, « OU »
   attributions included. There is no closing count, so the gap also holds
   what is still on the shelf - his choice; the page says it once.
2. **The loss allowance is not a hole**: what is left to fill (`room`, « À
   combler ») is the gap less `variance.loss_allowance(opening + purchases,
   loss_percent)` - the « Écarts » page's own rule, one function for both.
3. **Proportional**: every gap shrinks by about the same share, a big gap
   gets more sales, but every gap gets some.

**Every sale proposed is played through the engine.** The till records a
recipe, never which side of an « OU » was poured, and once imported a choice
is attributed priciest first and a fixed ingredient pushes a choice off a
bottle onto the next option (`allocate_choices`). `_quantities_sold` was
split in two for this: `read_sales` (once) and `attribute_sales(sales,
available, unit_costs, extra)` (pure, no query once `sales.terms` holds every
recipe asked about - fill it inside a `variation_scope`). The planner ranks
each recipe on its MEASURED effect - what one more sale adds per article, as
the engine attributes it - measured lazily (an effect a few sales old ranks
the recipe until it comes out on top, then is measured again). Ranked on a
model of its pour instead, a cocktail with a fixed spirit kept coming back
while each one shifted a « shot au choix » onto another spirit the ranking
never saw, which ended far past every other gap. A recipe no « OU » can
reach (`Offer.independent`) adds exactly its terms and is never measured. A
recipe set aside (its next sale would go past a room, or fill no gap) is
measured again once the plan has moved on (`_Run.revive`): a fixed pour taking
a bottle to its cap sends the next « au choix » onto another bottle with
plenty - set aside for good, the plan stopped far short of the amount and
said the gaps were full. The one promise: **nothing goes past max(0, room) as
the engine counts it** - the articles left out aside, which have no room to
keep to - so what the page predicts is what the stock page shows once the
sales are in.

**The ranking is Webster's divisor method, weighted by value.** An article's
level is (added + half its usual serving) / room - the usual serving being
what its most-sold recipe pours, so a pint and a half of one keg rank alike.
A sale's level is the levels of the gaps it fills averaged by what it pours
of each IN VALUE (the cost per unit, `values`), and the next sale goes to the
lowest. Both simpler keys fail: by its most advanced article, a cocktail
sharing a lemonade with the best-sellers is never chosen and its own syrup is
never filled, whatever the amount; by its least advanced, a garnish weighing
a few grams a glass (far behind, being huge beside what one glass pours)
drives its cocktail and sells the spirit past every other gap.
Weighted by value, a garnish neither drives nor blocks; it may run ahead of
the common level, within its room. Ties - recipes filling the same gaps alike
- go to the recipe furthest behind its share of what the till really sold
over the menu's window - since the take, or the months chosen (below) -
((planned + ½) / (sold + 1)), so the list reads like the bar's
own orders. A gap whose share is under half a serving waits for a bigger
amount. Then name, then pk: same input, same list.

**The total is the amount whenever the prices can make it.** Every sale is
chosen among those that leave a rest the prices of the recipes still
proposable can make up (`_Makeable`: unbounded coin change as a bitset on a
Python int, in steps of the prices' gcd, each price folded in by doubling - a
handful of shifts whatever the amount). Ranked freely, a sale taken early
left a rest no mix of the prices makes (a pint first, then nothing made what
was left where four halves made the amount). When a recipe drops out, what
can be made is worked out again and, if the rest no longer can be, the plan
aims at the closest total below that can. Rooms are not part of that
reckoning: once they bind, the total is the ranking's, not a proven best.
`Plan.reason` says why a plan is short - under the cheapest recipe, no more
sale fits the gaps, or no combination of prices.

**What the page sets aside, and says.** A recipe blocked (one sale of it
alone would take an article past its room: sold more than bought, within the
loss allowance, not counted, or less than a serving left) is named with the
article. A priced recipe not sold since the take - or over the duration
chosen, below - is not proposed (it is probably off the menu), and is listed
with its last sale. A gap some recipe pours but none proposed is listed
with its value (`GapReport.unreached`); a gap no recipe pours at all - the
equipment, the paper towels - is only counted (`outside_recipes`): no sale
fills it. A target no proposed sale reaches as the engine books one
(`ArticleGap.secondary`, measured from each recipe's first sale: the cheaper
side of an « OU » whose dearer side has room, or a fixed pour whose sale
pushes an « au choix » onto another bottle) is left out of the « Écarts
réduits de » average. An article absent from the count is read at 0 there,
which can only UNDERSTATE its gap: it stays fillable, flagged « non compté » -
unlike the « Écarts » page, where an uncounted CLOSING would overstate it.

**The till button is the one that rings the recipe's price.** A recipe often
has several buttons (a plain glass and a dearer cocktail linked to one recipe):
each button's price is what it charged most often since the take, or over
the months chosen when they are more recent (below) (a day's
money over its units, weighted by units - a comped glass makes one day odd,
never the most frequent), the line names the button at the recipe's price,
then the most rung, never the happy-hour one. Where no button rings the
recipe's price (the menu changed, the recipe did not) the cell says « en
caisse X € » and the entry's note says how many lines differ: the plan is
priced « au prix de la carte », which is the recipe's.

**Fresh data matters.** « ventes importées jusqu'au … · achats jusqu'au … »
heads the page, with a warning when the till's sales stop before yesterday
or before a delivery (the gaps still hold sales the till has not handed over:
ringing the plan up before importing them would count them twice) and one
when till buttons are linked to no recipe (what they sell reads as a gap).

**Articles and categories left out** (the owner, 01/10/2026: « exclure des
articles/catégories de produit de ces écarts, et que cela reste en
mémoire »). `GapExclusion` (inventory 0019) holds, for the espace, either one
article (`stock_type`, one-to-one, CASCADE) or one category name
(`category`, unique; "" is the articles with none) - exactly one, a check
constraint says so. A category left out covers every article filed under
it, those classified into it later too: that is what excluding a category
means, where ticking its articles one by one (the « Marges » pattern) would
leave a newcomer counted. `gaps_since` marks every such article `excluded`
and hands their ids to the planner as `ignored`: an ignored article is no
target (nothing ranks a sale on it, `share_summary` leaves it out) and no
limit (`_Run.past` skips it - a recipe it alone held back, a herb whose gap
reads « vendu plus qu'acheté », is proposed again; one that fills only
ignored articles fills no gap and is not). It leaves the gaps table, the
unreached and outside-recipes lists; an empty table says « Tous les articles
de ces écarts sont exclus. » rather than that no recipe uses one. « Exclus
des écarts » (`#exclusions`, open while anything is excluded) lists each
exclusion with « Réinclure » - a category with how many of the page's
articles it covers, an article alone with « catégorie exclue aussi » when
its category is left out too (taking it back alone then says it stays out).
Each gap row has « Exclure » (`stock_gap_filler_exclude`, `article`, back to
`#ecarts`); the section excludes a category of the report's articles
(`categorie`: one some article carries, else refused); `stock_gap_filler_include`
deletes one exclusion (`exclusion`). All POST, redirecting to the page; a
GET goes to it. Their messages are said where the redirect lands - above the
gaps table, or in the fold, opened for them (`views._messages_by_place`,
the tags `ecarts` and `exclusions`, as Marges does): said at the top, two
screens above, nobody saw « Catégorie introuvable ».

The planner works on the offers that reach a gap only (`plan_sales`:
`run.offers = live`): an offer pouring nothing but left-out articles kept
its price among those the rest of the amount could be made of, and a plan
fell short with « aucune combinaison » where one without it was exact. The engine still attributes the sales of an excluded
article - only the planner looks away - so the stock page and « Écarts »
are untouched, and so is a list already made: its entries keep what they
proposed. Not exported by « Données », like the list.

**Only the recipes sold lately** (the owner, 01/10/2026: « ne proposer que
des recettes ayant été vendues il y a moins de X temps » - some recipes are
off the menu). `GapFillSetting` (inventory 0020, one row, pk 1, absent until
something is chosen - `current()` never writes, a page drawn writes nothing)
holds `sold_within_months`, 1 to 120 (a check constraint), or None: the
recipes sold since the take. « Recettes vendues il y a moins de
[n] [mois / ans] » under « Depuis » posts it to `stock_gap_filler_recent`
(`duree`, `unite`; `depuis_inventaire`, a button with `formnovalidate`, goes
back to None), refused in French when it is no whole number of ASCII digits
or past ten years (`views.read_typed_duration`, never `int()` on thousands of
digits), and read back with whole years in years (`duration_fields`: 12 mois
is drawn [1] [an(s)] - the option's value stays `ans`). One redirect to the
count's page, the message at the top.
- Migration `inventory/0020`, **WRITTEN and left to be applied** on data-dev
  (the owner, after a backup, `migrate_tenants`; deploy.cmd applies it in
  production after its own). Until then the WHOLE page and « Ajouter »
  answer « no such table »: `gaps_since` reads the setting on every draw.
- **Counted back from the end of the window (today), not from the take**:
  right after a count nothing has been sold since it, so « since the take »
  proposed nothing for days while the menu had not changed. A window
  reaching before the take brings back a recipe sold only before it; one
  shorter than the take's age sets aside what stopped selling.
  `months_before` keeps the day of the month where the month has it (31/03
  less one month is 28/02), and a sale ON that first day counts
  (`GapReport.menu_since`, inclusive; the take's default is the day after it).
- **A recipe is on the menu when its last sale** (`gaps._last_sale_days`:
  a till day or a sale document, quantity above 0 - a refund is no sale) **is
  on or after `menu_since`**; the page lists the others with that date, or
  « jamais vendue » (`UnsoldRecipe`). A refund does not undo a sale either:
  sold, then refunded another day, a recipe stays on the menu, its mix 0 or
  below (the planner reads `max(sold, 0)`). Until 01/10/2026 the default
  asked for net sales above zero since the take; kept, that rule would list
  such a recipe « pas vendue » beside a last sale inside the window.
- **Only the menu moves.** The gaps, the allowance, the rooms and every
  attribution still run from the take (the engine reads the take's sales).
  The window gives the mix a tie goes to (`Offer.sold`, the window's net
  sales - `sales_between`, two more queries). The till's prices are read
  over the MORE RECENT of the take's window and the menu's
  (`max(start, menu_since - 1 day)`): a few months chosen on an older count
  name a button by what it rings now, and a year read for the menu does
  not bring back the price a button charged before the take (it did, review
  of 01/10: the old price, rung more often, won) - a recipe brought back
  from before the take is named by its most rung button, with no price. A list already made keeps its lines and
  still counts; the next amount builds on it with the new menu.
- An amount with no recipe to propose and none blocked says « aucune
  recette vendue depuis le … » (or « depuis l'inventaire ») when no priced
  recipe sold over the window (`GapReport.on_menu` 0), « … n'utilise un
  article » when those that did pour nothing - never that the gaps are full.

**What is typed.** `montant` goes through `common.read_number` for its size
(« 20 000 000 000 » is too big, not unreadable) then `read_amount`. A space
or « ' » only ever separates thousands (« 42 50 » is no amount - it read as
4 250 € - and that rule is the slips' too now), and one separator followed by
exactly three digits (« 10.000 », « 1,500 ») is asked again rather than read
as 10 € (`views.AMBIGUOUS_THOUSANDS`; a slip still reads « 4,000 » as 4).

**Cost.** `gaps_since` reads like the « Écarts » page (one query per
sub-recipe level in `build_pools`, two per recipe in `choice_groups`, every
recipe read once inside one `variation_scope`); the plan grows with the
amount, a sale at a time and an engine call per sale that is not
independent - which is what `MAX_AMOUNT` bounds.

### L'Addition (the till)

`manage.py laddition_import --from 2026-06-01 --to 2026-06-30` downloads and
records sales. Add `--dry-run` first: it reports which till products match a
recipe and which don't, without writing. `--file x.xlsx` skips the download.

Credentials come from « Identifiants » (`accounts/vault.py`), else `.env`
(`LADDITION_EMAIL` / `LADDITION_PASSWORD`), and are typed by the browser at
run time, same as the Metro scraper.

Four things that cost real debugging time:

- **The export is a signed URL, and the signature does NOT cover the dates.**
  So the browser is only needed once, to capture it: press "Exporter en XLS"
  with `window.open` stubbed out, keep the URL, then swap
  `date_start`/`date_end` for any range. The alternative — driving the
  react-day-picker calendar in a popover in an iframe — was flaky in the
  worst way, failing by selecting the *wrong range* rather than by raising.
- **openpyxl cannot read these files.** The export declares its "Total" row's
  cells as numeric and writes `-` into them, which is invalid SpreadsheetML;
  read-only mode raises `invalid literal for int()`, normal mode rejects the
  whole workbook. Hence `recipes/pos/xlsx_reader.py`, which reads every cell
  as text. Don't "fix" this by adding openpyxl back.
- **Every reporting page is an iframe.** The top-level document holds only
  the sidebar (~120 chars), so a readiness check on body text times out on a
  page that loaded fine. Use `report_frame()`.
- **The sign-in button has no `type` attribute.** `<button>` defaults to
  submit as a DOM *property*, so JS and `get_attribute("type")` both say
  "submit" while XPath `@type` matches nothing. It's matched on exact text —
  which also avoids the "Mot de passe oublié ?" button right next to it.

The UI is two tabs of **Recettes & ventes**: "Ventes" runs the import
(background thread + htmx polling, same shape as the invoice gather) and "À
lier" (`/recipes/caisse/`) is the backlog of till products with no recipe -
biggest sellers first, since that's where the unexplained stock is. Four
actions per row, in place: link to a recipe (the one with a close name is
chosen already, `links.suggest_recipe`; an "HH" name ticks happy hour), mark
as a happy-hour variant, create the recipe (`?caisse=`: named after it,
linked to it on save), or ignore (coffee, food, anything untracked). Linking
and unlinking go through `recipes/links.py` only - from these rows and from
the recipe form's "Vendue en caisse sous" - which rebuilds the sales and
clears a happy-hour name that leaves with its product.

`PosProduct` is that backlog, and an explicit mapping on it beats a
coinciding recipe name in `recipe_lookup()` - a mapping made by hand is a
deliberate statement about that exact till product.

Use `SalesDocumentLines`, not `ProductAnalytics`: it carries a **date per
line**, so sales can be sliced by stock-take window afterwards and a download
needn't be aligned to an inventory period.

Comped drinks (`TAG_Offered`) are **included** in the sales quantities — a
free drink is poured from the same bottle. Don't also record them as known
losses or they're subtracted twice.

**The export carries the money, and it is read** (`laddition_xlsx`): `Prix
TTC`, `Remises TTC` and `Taux` per line, summed per (till product, day) onto
`PosProductDailyQuantity.revenue_ttc` / `revenue_ht`. The rules below were
each measured against the whole of the stored exports (tens of thousands of
lines); **the figures themselves stay out of this file** - it is committed to
a public repository, and what the bar turns over is nobody else's business.
Re-measure on a scratch copy when it matters, and keep the number there:

- **`Prix TTC` is the line's own amount, never × `Qte`.** `Qte` is 1 on
  every line but seven refunds at -1, whose amount is already negative. Both
  readings agree on this data, which is exactly why a test pins the rule
  rather than the arithmetic.
- **HT is worked out per rate**, from the TTC accumulated for that rate: a
  day mixing food at 10 % and drink at 20 % comes out right, and three sodas
  at 3,50 are 9,55 HT, not 9,54. A rate that is no French rate leaves that
  line's HT unknown, counted and shown (`revenue_without_rate_ttc`) - never
  20 % by default. A refund prints its rate as « -20% »: the minus belongs
  to the amount, and read as a rate it would lose those lines' HT.
- **A comped line is already priced 0** (its value sits in `Offerts TTC`),
  which is what a margin wants: the stock left the shelf and no money came
  in. `Remises TTC` is 0,00 on every line stored and is taken off anyway, and
  counted - a column that has never fired is the one that fires silently.
  **Which SIGN it fires with has never been observed either**, so the
  arithmetic decides rather than a guess: a discount that would make the line
  bigger than its own gross price (6,00 € less -1,50 € is 7,50 €, which
  cannot happen) is not taken, and is counted under `discounts_not_taken`.
- **A (product, day) one of whose lines has no readable amount is left
  unread**, not filed at what the rest of that day took: it is short of an
  unknown figure, and the day filed anyway looks perfectly ordinary. It joins
  the « jours non lus » the margins page already has a banner for
  (`days_without_amount`, in both logs), and another export that CAN read the
  day whole still fills it.
- **Every way a file can fail to be this export answers as
  `LadditionExportError`.** An .xlsx is a zip, and the espace's `downloads/` is
  scanned whole: a half-finished download raises `zipfile.BadZipFile`, which
  is no `XlsxError` and escaped the reader AND the backfill's own
  « illisible » branch - one such file stopped the other sixteen being read
  at all, with a traceback. So do, through `laddition_xlsx._unreadable` (both
  sheets' one list), a zip holding no workbook (`KeyError`), broken XML
  (`ParseError`), a damaged deflate stream with the zip's directory intact
  (`zlib.error`) and a truncated one (`EOFError`) - none is a zip error, and
  the first damaged stream failed a job whose sales had read. NOT
  `RuntimeError`: `LadditionExportError` and `PaymentsSheetMissing` are
  RuntimeErrors, and a missing sheet would come back as a broken file.
- **`PosProductDailyQuantity.quantity` is signed** (recipes/0014, with
  `RecipeSale.quantity` and `PosProduct.total_quantity`): the seven refunds
  stored all happen to fall on days the product also sold, but a pint sold
  Tuesday and taken back Wednesday nets -1 on Wednesday, and on a
  positive-only column that INSERT failed and rolled back the **whole
  window's** import, money and quantities alike, every time it was re-run. A
  sale typed by HAND is still refused below zero (`ManualSaleForm`): nothing
  types a refund in, and a minus there is a slip. The « Données » archive
  carries a negative day for the same reason.
- **`Prix achat HT` is the till's own idea of a cost, 0 nearly everywhere,
  and is never read.** Costs come from the invoices, through the recipes.

`revenue_read` is the difference between « this day took 0 € » and « nobody
has read this day's money »: false on every row imported before this, and on
every row a « Données » archive restores - that archive carries the
quantities only (`transfer.sections.sales.DAILY_COLUMNS`), so a restore is
followed by a backfill. `manage.py laddition_backfill_revenue --dry-run`
fills them from the .xlsx already in the espace's `downloads/`, **contacting
nothing**: it names each file, the revenue per year and everything it cannot
match, and **creates no row** - money against a quantity nobody imported
would be a figure with no stock behind it. A (product, day) printed by two
overlapping exports replaces itself rather than adding up (no two of the 17
stored exports disagreed about one). Run at scale on a scratch copy of the
quantities, it filled every (product, day) on file and matched all of them,
and its revenue reproduced the reader's to the cent - which is the check that
the reading is right, and the reason it prints a total per year.

**The export also says how each ticket was paid, and that is read too**
(`recipes/payments.py`, `PosDailyPayment`: `sold_on`, `method`, `amount`,
`payments`; migration `recipes/0017`, applied to the real database by the
owner on 28/09 after a backup). The
`SalesDocument` sheet, one row per ticket, is read beside
`SalesDocumentLines`: its `Paiements` cell (`CB(4,50)`,
`Cash(5,00)/CB(3,50)`) is summed per (till day, method), the day being the
lines' own `Jour`. **The payments, not the tickets' totals, are what reaches
the bank**: a ticket's payments are its `Total TTC` plus its `Trop perçus` (a
tip, change not given back), and « Entrées d'argent » compares them with the
account.

- **One vocabulary, on the model**: `CARD`, `CASH`, `CHEQUE`, `CREDIT`
  (« Avoir »), `MEAL_VOUCHER`, and two pseudo-methods, `UNREAD` and `UNPAID`.
  `canonical()` matches the known ones accent- and case-blind
  (`common.search_key`) and keeps anything else as printed; `label_for()` and
  `sort_key()` are what a page shows and orders by. The reader imports the
  model lazily, so `laddition_xlsx` still loads without Django.
- **A ticket is read whole or not at all.** Outside the strict grammar
  (`PAYMENTS_GRAMMAR`), a blank method or an amount that is not one
  (`PAYMENT_AMOUNT`: one decimal separator, two decimals at most - « 1.234,56 »
  is refused, not guessed) makes the WHOLE ticket unread: kept as its card
  payment alone, it would look ordinary and be short of its cash. It is filed
  at its `Total TTC` under `UNREAD`, so the day still adds up, and counted. An
  empty `Paiements` on a total of 0 is a comped ticket; on any other total it
  is filed under `UNPAID`, never dropped. Neither has ever fired - which is
  exactly why each is counted and said in the import's log. Thousands are
  grouped by a space, a no-break space or a narrow no-break space, whichever
  the formatter's locale prints (`THOUSANDS_SEPARATORS`; two of them are
  invisible, see « An invisible character is written as a named escape »).
- **Checked against the ticket's own arithmetic**: payments that are not
  `Total TTC` + `Trop perçus` are kept as paid - they are what the bank sees -
  and counted (`tickets_not_adding_up`). With no `Trop perçus` column nothing
  is checked, or every tip would read as a mismatch. A ticket id seen twice in
  one file is read once.
- **A day is the unit, replaced whole.** Of two overlapping files, the later
  reading of a day replaces every method of it, a method it lacks included:
  merged method by method, a day would keep a cash payment its second reading
  no longer has. `record_payments(export)` rewrites each day the export read in
  one transaction, and leaves alone a day whose rows already say exactly that
  (a second run writes nothing, not even new ids). A day the reading does not
  cover is never touched.
- **The sheet is optional and never costs the lines.** Missing (an older
  export): `payments_read` False and nothing replaced - « no sheet » is not
  « paid nothing ». Unreadable: named in the log (`payment_sheet_errors`), the
  lines imported all the same. It is parsed apart and taken only whole.
- **A day's payments only beside a day « Ventes » holds**
  (`PosProductDailyQuantity`), whoever writes them: payments on a day with no
  sales are money the sales pages contradict. The import job and
  `laddition_import` run the same order - `sync_pos_products`,
  `record_sales`, `record_payments` - and « Remplacer » prunes the payments of
  a day it leaves without sales.
- **`manage.py laddition_backfill_payments [--dry-run] [--folder]`** fills the
  days already imported from the .xlsx in the espace's `downloads/`, **contacting
  nothing** and reading the ticket sheet alone. The revenue backfill's shape:
  each file named with what it read, an export without the sheet said as such
  (`PaymentsSheetMissing`, not « illisible »), an unreadable one stepped over,
  the days two files disagree about listed (the last reading kept whole),
  totals per year and method. It writes no day « Ventes » lacks and lists
  them; a day that paid nothing at all is counted apart, neutrally - listed
  with those, it asked the owner on every run to import again what no import
  brings.
- Known edge: a day with sales whose every ticket was comped stores no row,
  so it reads like a day never read.

A happy-hour variant is a separate till product ("Pinte Blonde" vs "Pinte
Blonde HH"). Put its till name in the base recipe's `happy_hour_name` and
both fold into one recipe. `record_sales` therefore aggregates by RESOLVED
RECIPE, not by raw name — summing by name would write one and overwrite it
with the other, silently losing every happy-hour sale.

Taking a variant off its recipe (ignore, back to the worklist, linked
elsewhere) clears that `happy_hour_name`: the import counts sales by name, so
its sales kept landing on the recipe, and a product sent back to the worklist
was relinked by the next import. An ignored till product sells no recipe
whatever its name (`record_sales` skips it, and does not list it as
unmatched).

### The three margins (`margins/computation.py`)

`margins_for(window: DateRange, left_out=()) -> MarginReport` - pure, no
request, no template - answers three different questions and blends none of
them:

- **the real margin** (« Marge réelle »): everything that came in against everything that was
  **invoiced** over the window, goods and charges alike
  (`Supplier.expenses_only`). « Ai-je gagné de l'argent ce mois-ci. »
- **the products margin** (« Marge produits »): the same income against what the recipes sold
  actually consumed, plus the articles flagged « compter dans la marge
  produits ». « Est-ce que je vends assez cher. »
- **the margins by category** (« Marges par catégorie »), on both dimensions the till already stores -
  `PosProduct.category` (Bières, Cocktails, Planches…) and `.typology`
  (« food, drinks »). Two readings of one till, so they foot to the same
  revenue, units and cost; the measurement below checks they do.

**HT is the headline, TTC beside it.** The till takes TTC and the invoices
charge HT. Every amount is a `Money` (both), every margin is worked out on
the HT, and the products margin has **no** TTC at all - a recipe's cost only
exists in HT, and the page says so rather than inventing one.

**Coverage is the figure that keeps this honest.** Only a minority of the
till products have a recipe: the boards, the dips, the coffee have revenue and
no cost, and counted as costed they print a **100 % margin**. So every unit
is counted twice - sold, and costed - and `coverage`, `revenue_uncosted`,
`revenue_coverage` and `top_uncosted` say what the gap is. A slice with no
costed unit returns **None**, never a number. Measured over all history,
the money costed is well short of the units costed, and a category can read
a margin above 90 % on well under half of its money costed. **A margin % is
never to be shown without its coverage beside it**; on this data that is the
difference between a figure and a fiction.

**A recipe is costed only when ALL of it is, and per SERVING.**
`_recipe_costs` keeps a recipe out of the cogs in three cases, all the same
lie in different clothes - a cost that is only partly known reads as margin:

- its **dearest variation costs 0**: every article in it has been invoiced
  never (`current_unit_cost_ht` is 0 with no movement behind it);
- **one article of it** has been invoiced never, and the others have: the
  recipe then prices at less than it costs and « 100 % chiffré » beside it
  is false. `_every_ingredient_priced` walks the sub-recipes too, an
  alternative counts like any other ingredient, and the row is listed under
  « ingrédient sans prix ». Latent today (every recipe fully priced, read-only
  on the real database, 20/09); it fires the day a recipe gains an article
  whose first invoice has not landed, and it moves the margin **up**;
- it **yields nothing** (`yield_quantity` 0) or **sells nothing**
  (`sale_quantity` 0), so there is no per-sale cost to divide out.

And the cost counted is **one sale**, not one whole preparation -
`summary()` prices a full run of the recipe, so a syrup made ten glasses at
a time costs its batch there. `inventory/variance.py` has scaled to the sale
since it was written, and a cogs that did not put the two pages a factor of
ten apart over one sale (a 20,00 € batch yielding 10, five glasses sold:
100,00 € against the 10,00 € `quantities_sold` takes out of stock). Note the
direction is not always the flattering one: the validator allows a yield
below 1, which costs MORE per sale.

**How much of a preparation ONE SALE is: `Recipe.sale_quantity`**, in the
unit the recipe produces, default 1 (`recipes/0015`). A terrine produces
1,6 kg and is sold in 150 g plates, so `sale_quantity` is 0,15 and one sale
is 0,15/1,6 of a batch. `Recipe.per_sale(amount)` is the ONE rule that
scales anything measured over a preparation down to a sale, and both readers
go through it - `_recipe_costs` here and `variance.recipe_usage_terms` -
because a cost and a consumption disagreeing about how much of a batch left
is this codebase's oldest sin. It multiplies THEN divides
(`amount * sale / yield`), never by a ratio worked out first: 1/11 is not
exact and `amount * (sale/yield)` rounds twice, which moves a recipe
yielding 11 in its 28th digit. At `sale_quantity = 1` it is the bare
division by the yield that both readers did before, to the digit.

**A recipe that is not sold as itself has NO price** (`selling_price_ttc`
null, `recipes/0016`), and `Recipe.is_sold_directly` is the one question
anything asks. A house syrup was filed at 0,00 € because the price was
required, and 0 is a price: its own page put that 0 against what the
preparation costs and read a negative « marge » and « facteur x0,00 », a loss on
something nobody ever sold. Blank, there is no margin, no percentage and no
factor - `_price_metrics` and `_price_factor_range` already answered None to
a None price, and the pages now say « Pas vendue directement » rather than
aligning « — ». `sale_quantity` is not asked either (the form leaves it
optional, blank is 1) and a quantity sold typed with no price is refused:
two answers that cannot both be true. **Never inferred**: not from being
used as a sub-recipe (most recipes used as one are SOLD as well - a cocktail
is poured into a jug and sold by the glass), not from a 0 (somebody
typed it; a comped drink is sold, at nothing). Existing data is untouched,
so a preparation filed at 0 keeps its 0 until the owner blanks it.
A merge keeps a deliberate « pas vendue » - `RecipesSection._update` reports
a conflict and changes nothing unless « Remplacer » was chosen.

Five traps a review found on it, each a test that failed first:

- **The form compares the VALUE, not what was posted.** `sale_quantity` is
  `decimal_places=4`, so a saved recipe draws « 1.0000 » in the box and a
  guard reading the raw string against « 1 » refused the one edit this
  exists for: opening a preparation filed at 0,00 € and clearing its price.
- **A field that RAISED is dropped from `cleaned_data`**, so « laissé vide »
  and « 12,50 » (a comma) look identical there: a typo answered « cette
  recette n'est pas vendue telle quelle » on a quantity box that was
  correct. The branch asks `"selling_price_ttc" in self.errors` too.
- **A happy-hour price is refused with no selling price**, and the header
  does not print one either: the page read « Pas vendue directement ·
  Happy hour : 4,80 € », one page saying both. « Vendu par … » is nested the
  same way - there is no sale to take anything.
- **A preparation is NOT offered on a sale document** (« bon de vente », `sale_source_choices`,
  with `keep=` so a line written before the price was cleared still opens).
  Offered, a line naming it books its full cost against 0,00 € of revenue -
  `SaleDocumentLine.total_ttc` has nothing to fall back on and
  `margins.computation` counts the cost, which is exactly the asymmetry the
  article-sold-as-itself rule forbids: both sides out, or neither.
- **The page says only what it has checked.** A blank price says « not
  sold » and nothing about what uses the recipe, so the recipe's page asks
  `used_in` before calling it a preparation - a recipe being drafted has a
  blank price too, and « utilisée dans aucune recette » is the case worth
  saying, since nothing then counts what it costs. And where the till DOES
  sell an unpriced recipe the recipe's page says so in a warning: the revenue comes
  from `PosProductDailyQuantity`, never from this field, so « Marges » goes
  on costing it and the one figure that would give it away is on another
  page.

**Left alone, and why:** an archive record that OMITS the price now creates
a « pas vendue » recipe where it used to be refused. That is what every
other field with a default already does (`category`, `yield_quantity`,
`vat_rate`, `sale_quantity`), and this app's own exports always carry it -
making the price special again would be the odd one out.

**`unit_cost_ht`, `unit_cost_bounds` and
`variance._sub_recipe_usage_per_yield_unit` are per YIELD UNIT and never
scaled by it**: a parent buys its sub-recipe by the unit produced, and how
that sub-recipe is sold over the counter is none of the parent's business -
folded in there, a syrup sold by the glass would price a cocktail using 2 cl
of it as if it drank the glass.

**A price is what one sale fetches, so the cost beside it has to be one
sale's.** `_summary` and `_build_variation` put `selling_price_ht` against
the BATCH cost, which was wrong before any portion existed: a recipe yielding
10 that costs 6,00 € the batch and sells at 4,00 € HT had its own page and
the « Recettes » tab read « marge -2,00 € · facteur x0,67 » - sold at a
loss - while « Marges » costed the same sale at one tenth and read a healthy
margin. Two pages a factor of the yield apart, on a kind of recipe the till
really sells. `margin_range`, `margin_percent_range`,
`price_factor_range` and the variation's own margin, factor and happy-hour
twin are all drawn from `per_sale` now. **`cost_range` and a variation's
`cost_ht` stay the whole preparation** - it is what the detail page prints
as « Coût total pour 1,60 kg » and what `unit_cost_bounds` divides by the
yield, so scaling it would apply the portion twice; `cost_per_sale_range`
and `cost_per_sale_ht` sit beside them. `_price_factor_range` keeps its
group arithmetic in batch terms and scales at its two divisions, since
`min_cost - group_min + min_positive` only means anything on one scale.

**Linear in ingredients, never in variations.** One pass over the recipes
sold, inside `recipes.models.variation_scope()`, with the ingredients handed
to `summary()` - `choice_groups()` builds its own queryset, so a prefetch at
the call site buys nothing without that. Measured on a scratch copy of the
real database: **11-12 queries and about half a second** for the whole
history, a few hundredths for a month.
A test asserts that costing three times as many recipes costs no more
queries.

**Every gap is a field, not a zero.** `unread_days` / `unread_units` (a day
whose money was never read counts its units and its cost, but no revenue -
so the margin reads LOW, which is the safe direction: putting a year back to
unread on a scratch copy took the real margin down by a third and more, and
the page said how many days and units were unread); `revenue_without_rate_ttc`;
`undated_invoices` / `undated_spend` / `undated_in_spend` (an undated invoice
is in no window, but over all time there is no window to be outside of, so
it IS the spending and the flag says which); `hand_typed_units` (a
`RecipeSale` from another source than the till carries no price anywhere).

**A percentage divides only by a positive revenue.** Zero is not 0 %, and a
window that only refunded has -8,75 € of margin on -7,50 € of revenue - which
works out to **+116 %**. A loss printed as a gain is worse than no figure.

**An article sold as itself on a sale document is income with no margin.**
It carries no VAT rate anywhere - a recipe has one, an article does not - so
its money stays TTC, lands in `revenue_without_rate_ttc` and never reaches
`revenue.ht`. Its purchase price is therefore left out of the cogs too:
counted there, the cost came off an HT its revenue never joined, and two
bottles bought at 3 € and sold at 10 € printed a products margin of
**-6,00 €**, a profitable sale shown as a loss. Both sides out or neither,
and the foot of the page names the amount. 0 such lines today.

**The two margins date one purchase on one day.** A flagged article's
purchase is counted on its **invoice's** date, like everything in
`spend` - only a movement with no invoice behind it (a correction typed by
hand) keeps its own. Deliberately NOT `StockMovement.effective_date`, which
« Produits & charges » sums a row by: that page is about the shelf, this one
about the bill. Dated by the delivery, an article invoiced 25/02 and
received 10/03 was in February on « Facturé » and in March on « Achats des
articles cochés », on one page, with nothing saying so.

**Each invoice is rounded to the cent before it is added** (`_invoice_money`),
the same rule the charges fold follows: twelve bills printed at 23,99 € must
foot to 287,88 €, not 287,86 €.

**The real margin « sans … » is the same margin with places taken out, never
a second definition of it.** The one pass over the invoices also puts every
invoiced euro in exactly one place (`_where_it_went`): a charge's document
WHOLE on its supplier, a goods line on its article (drawn under the article's
category as it is today), a goods line no article claims on « à classer », a
document with no line at all whole on its supplier (a charge) or on « à
classer » (goods). The duty adjustment (`reconciliation_adjustment`) belongs
to the invoice, not to a line: it is spread over the lines **pro rata to
their HT, over the lines with a positive HT only** - signed, a returned
deposit took a negative share of the beer's duty and the beer more than the
whole of it. Whatever still separates the rounded places from the invoice's
own cent-rounded totals (three shares of a duty rounding to one centime too
many; a receipt paid at its printed total, a few centimes above its printed
lines) goes on **the invoice's largest place**. That is what makes the rule
`margins/tests/test_spend_selection.py` pins hold: **with nothing left out
the places add up to `spend` to the cent, HT and TTC**, so the second margin
IS the first. And **the revenue never moves**: a purchase belongs to no till
category, so leaving one out takes away a cost, never a sale - mapping the
articles' categories onto the till's is not attempted.

The keys are `charges`, `fournisseur:<id>` (a supplier OF CHARGES only - a
goods supplier's money is its articles'), `categorie:<name as stored>` (the
blank one is `categorie:` and nothing after), `article:<id>` and
`a-classer`. A category has no row anywhere, so its name IS its key and has
to round-trip through `urlencode` exactly, accents and spaces included.
`_resolve` drops what it does not know - garbled, an id `common.is_id`
refuses, an article deleted since, a category no article carries - and never
raises; keeps a known key with nothing in the window (named: « sans
Consignes » over a month none were bought is still the question asked, and
reads « rien de facturé » - only then: a keg bought and given back takes out
0,00 € but WAS invoiced, `Exclusion.invoiced`); and
counts a place two keys reach (« Charges » and one of its suppliers,
« Matériel » and one of its articles) once, naming only the group. One query
per KIND of key; the lines come with their product and article in the one
prefetch (`Prefetch` + `select_related`), and a test asserts that three times
the invoices and the keys cost no more queries.

**A blank category is « Catégorie non renseignée »**, not « Sans
catégorie » - the till already prints a category of its own called
« _Sans catégorie » (and a typology called « N/D »), and two rows an
underscore apart meaning two different things is a page nobody can read.

**`StockType.count_in_products_margin`** (« Compter dans la marge produits »,
on the article's own form and on the Marges page, a category at a time) is
the paper towels: no recipe consumes them, so
what was **bought** over the window is the only measure there is, counted
exactly as « Produits & charges » counts a purchase (the line's own
`total_ht`). Off by default - an article counted here *and* in a recipe is
paid for twice. Signed, both ways: flagging an article whose deposits come
back gave a NEGATIVE month on a scratch copy, which is right and must not be
clamped. They belong to no till category, so they are the report's and not a
slice's, and the page has to say where they went. The field rides in the « Données »
archive (`associations.ARTICLE_FIELDS`): dropped by a round trip it would come
back unticked and silently *improve* the margin.

**An article ticked AND used in a recipe is paid for twice**, and the help
text saying not to do it was all there was. `flagged_in_recipes` is one
query (`RecipeIngredient.objects.filter(stock_type__count_in_products_margin=True)`)
and the page names them in a warning: ticking the beer's own keg charged it
once as what the recipe consumed and once as what was bought, and printed a
negative products margin with no explanation on the screen. `flagged_articles`
counts what is ticked at all, so « rien de coché » and « coché, rien acheté
sur cette période » stop printing the same empty table.

**The box's panel reads what the margin reads.** `MarginReport.countable`
(`CountableCategory` > `CountableArticle`) is every article under its
category as it is TODAY - alphabetical as a person reads it (accents and
case aside: SQLite orders by byte), the blank category last as « Catégorie
non renseignée » - with its box, whether any recipe line names it, and what
was bought of it over the window. That last figure comes out of the very
pass `extra_products` is summed from (`_bought_over`: every purchase once,
the line's own `total_ht`, dated by its invoice, signed), so **what a box
says ticking it adds is what the products margin moves by, to the cent** -
two readings would have been two figures free to drift. « Ce qui a été
facturé » prints another figure for the same article on an invoice carrying duty - it
adds the line's share of the adjustment, the products margin counts the
line's own amount - and the panel says so in its own sentence. `flagged_in_recipes`
is read off the same « used by a recipe » set. Three queries whatever the
number of articles (the articles, the recipe lines, the purchases); a test
asserts that three times the articles cost no more. A category's `state` is
words - « aucun », « 3 sur 70 », « tous » - and it is drawn `opened` when it
holds an article counted twice, where the tick is.

Measured all-time on a scratch copy (20/09, never the real database), in
shapes rather than amounts - what the bar turns over stays out of a public
repository: **the products margin came out far above the real one**, which
is not good news but arithmetic - only part of the
revenue has a costed recipe behind it, and the rest is counted at no cost at
all. That is why no margin is ever printed without its coverage. A quiet
month with the charges still running came out **negative**, and both category
dimensions footed identically, which is the check that the two groupings read
the same sales. Re-measure rather than trusting a figure written down here.

### The « Marges » page (`/marges/`, `margins/views.py`)

The three answers in the owner's own order - the real margin, the products
margin, the margins by category - each under the sentence saying what it
counts, because a number nobody can explain is a number nobody will trust.
The view does no arithmetic: `margins_for` answers and the page says what the
figures are worth.

**Short sentences** (the owner, 01/10/2026: « trop verbose »): each section
says in one line what it counts, and the warnings say what is wrong with a
figure and nothing more. The long explanations were cut from the page and
live here; the figures, the coverage beside every margin and every warning
stayed. A sentence added back to this page has to earn its line.

**The page says which base it counts, in its first sentence.** « Ce qui a
été **facturé** … à la date des factures ». It no longer links to
« Dépenses par catégorie » (one of Banque's tabs now). It used to open on
« ce qui est sorti » - the words « Dépenses » uses for the statement - and
said « facturé » a hundred lines down, where a reader arriving from the
topbar never sees it: the confusion the rule was written against, live in
the one direction nobody had guarded.

**A period is chosen, never assumed.** With no dates the page shows the
**last twelve months** and names them on screen (the same 365 days the
charges fold falls back on, `inventory.views.charge_suppliers`): an all-time
margin mixes three years of purchase prices with three years of selling
prices. « Depuis le début » is `?tout=1`, a **named period** that wins over
the dates the way Banque's `?mois=` does - the inputs are drawn disabled -
and it **keeps** `du`/`au` in the URL so the door swings both ways, as the
stock page's panels do (« Revenir du … au … »). `?du=&au=` otherwise, both
ends included, and a date that is no date is the default rather than a 500.

**No margin is printed without its coverage beside it.** Every slice's « Part
chiffrée » column carries the share AND the words - « tout est chiffré »,
« le reste n'a pas de recette : marge surestimée », « aucune recette : pas de
marge calculable », « N € encaissés sans taux de TVA : pas de HT, marge
faussée » - and a slice with no costed unit prints « — » in its cost and
margin columns rather than a 100 % margin.

**« Part chiffrée » means the MONEY, in both places it appears**, with the
units said in the same cell (`SliceRow.units_note`). The column used to be
the units while the headline was the money: on one category selling 100
coffees at 2 € with no recipe beside 100 cocktails at 10 € with one, that is
50 % against 83 % under two identical labels. The words answer the money
too - read off the unit counts, a row whose units net to what is costed (one
sold, one taken back at a different price) printed « tout est chiffré »
under a banner saying the margin was overstated.

**Both tables foot themselves, and the page says why the total is not the
headline.** The rows are the **till alone**; the headline products margin
also counts the flagged articles' purchases and the sales made off the till,
which belong to no till category - a positive margin on a row above a
negative one on the headline, same money, same screen, nothing bridging them.

**Zero invoiced is not zero spent.** `invoice_count` is beside « Facturé »
and a window with none says so in a warning: that is the state of the current
month, every month, until the suppliers' bills are in, and the page announced
a 100 % real margin for it.

**Both real margins, never one in place of the other** (the owner, 21/09:
« sans le Matériel, sans les charges - mais je veux voir la marge globale
aussi »). What is left out is a VIEW carried in the address like the period -
`?sans=` repeated, no model field, no migration - so two tabs hold two
questions and a bookmark keeps its own. « Marge réelle — sans : Matériel,
Charges » is drawn beside « Marge réelle » only when something is left out,
with « tout remettre » and a « remettre » per thing. A key inside a group
also left out is not named (« sans : Matériel », Perceuse unticked inside it),
so the group's « remettre » puts it back too (`Exclusion.within_key`): it
put back Matériel alone, and only then named Perceuse, still out.

**The breakdown is the selector.** « Ce qui a été facturé » lists the
charges (by supplier) and each article category (by article), HT and share
of the total, every box ticked by default, and says where the question is
asked that the revenue does not move (« on retire une dépense, pas une
vente »). It is a GET form, and a checkbox left unticked sends NOTHING: every
row sends its key under `montre` and a still-ticked box sends it again under
`garder`, so « left out » is read as **shown and not kept**, never as « not
sent » - a row a stale page never had (a newer invoice's article) would
otherwise drop out with nobody touching it, and a key the form never showed
(nothing in the window) keeps its state. The view answers `montre` with a
redirect to the clean address (`known_left_out`: `sans` alone, unknown keys
dropped), **in the order the keys were asked**, what is newly left out after
them - in the table's order, a « Recalculer » that changed nothing turned
« sans : Nappe, Matériel » into « sans : Matériel, Nappe ». The column reads
« Garder », not « Compter », and the page says nothing is saved and that the
selection lives in the address: the panel below is the one that saves. A category left out keeps its articles' own boxes, so ticking it
back brings them back. A group unfolds with no script - the `<details>` in its
name cell is only a switch, and `tbody:has(details[open])` shows its rows -
and one holding an unticked row is drawn open, since folded the only box
saying something is out would be out of sight. « Shown and not kept » has
ONE definition, `common.left_out_from(query, key=None)`, beside
`LEFT_OUT_PARAM` / `SHOWN_PARAM` / `KEPT_PARAM`, and « Dépenses » reads its
pie's « Recalculer » through it too. `key` is what two spellings of one row
are compared by: « Dépenses » passes `spending.clean_category`; « Marges »
compares keys as sent and lets `known_left_out` canonicalise after.

**Every link and form back to this page carries `sans` as it carries
`du`/`au`** (`views._page_url`; `here_url` for a form's `next`; the window
form's hidden fields; « Effacer » clears the dates and keeps the selection),
built from the keys the page UNDERSTOOD, so a garbled one does not travel.
The links to other pages carry the window alone: none of them reads `sans`.

**« Compter dans la marge produits » is ticked from this page too** (the
owner, 21/09: « pour éviter les allers-retours »), a whole category at a
time: « Articles comptés dans la marge produits », under the products
margin - each category with its state in words, how many of its articles a
recipe uses, what they were bought for over the window, « Tout cocher » /
« Tout décocher », and unfolding (the same `<details>` switch as the
breakdown) to each article's own box and what ticking it adds. It is the
article's own field - the box on its form stays - so unlike `sans` it is a
setting, and a POST: `margins:count_articles` (`POST /marges/articles/`), a
GET goes back to the page.

**A post only ever touches the articles its own form showed.** Each
category is its own form, sitting in its buttons' cell; the article boxes
join it through their `form` attribute (a form cannot wrap a `<tbody>`). The
whole-category buttons touch the category as it is TODAY; a newcomer
classified into it later arrives unticked and the category reads « 31 sur
32 », which the page says beside the buttons. « Enregistrer » posts the
category, every article the form showed (`affiche`), the boxes still ticked
(`coche`) and the boxes it DREW ticked (`etait`), and changes **only what was
changed on the page**: ticked is drawn unticked and sent back ticked,
unticked is drawn ticked and not sent back - never « not sent », since an
unticked box sends nothing and neither does a row the page never had. So an
article reclassified INTO the category after the page was drawn keeps its
tick, an id of another category - tampered, or reclassified out - is never
changed by it, and a page left open does not undo what another tab did
since: read as « shown and not ticked », it unticked Gobelets ticked
elsewhere, a box the person never touched nor saw ticked. An id `is_id` refuses, an
article gone, a category no article carries any more, an unknown action:
each a message in the panel, never a 500.

**It answers where it was asked.** The forms' `next` is `here_url` plus
`#articles-comptes` (period, `tout` and `sans` kept), checked with
`url_has_allowed_host_and_scheme` like Banque's `_back` - off-site, the bare
page's panel. The redirect lands on the panel, two screens under the page's
head, so its messages carry the tag `articles-comptes` and are said there:
`base.html` has a `{% block messages %}` that this page takes over, saying
every other message at the top as before (`views._messages_by_place`; a test
puts a message from elsewhere in the cookie and finds it at the top, once).
Ticking an article a recipe uses says at once that it is now counted twice,
and the list marks it where its tick is (« compté deux fois »; unticked,
« dans une recette »).

Under it, `top_uncosted` lists the biggest till products with no recipe,
linking to « À lier » - **and says what it is a top OF** (« les 12 plus gros
sur N — N € HT en tout », `uncosted_products` / `uncosted_revenue`): cut at
twelve in silence, a reader works the list to its end believing the hole is
closed.

**`unread_days` is said at the top, in a warning, with the command to run**
(`manage.py laddition_backfill_revenue`, which reads the files on disk and
goes nowhere near the network). Their units and their cost count and their
revenue does not, so every margin below reads LOW until it has run - and
until it does, that banner is the first thing on the page.

The gaps that are not about the margins themselves (revenue with no VAT rate,
undated invoices, sales typed by hand) are in a `<details>` at the foot,
« Ce que ces chiffres ne disent pas » - declared, never absorbed, **and named
in the `<summary>` itself**: on a page whose whole argument is that a gap is
said rather than absorbed, a closed fold that does not say what it hides is
one nobody opens.

**« Aucune vente » is asked of the money too.** A day that only refunded is
stored at quantity 0 with a negative amount, so `no_sales` read off the units
alone put « Aucune vente enregistrée » over a page showing -15,00 € of
takings, and offered an import of sales already imported.

**No chart.** The one this page would draw - a bar per category - is the one
figure that must never be shown without its coverage, and a bar chart has
nowhere to put « 38 % chiffré ». The tables carry both.

### One inventory is enough (if the invoices go back far enough)

`compute_variance` has two modes and picks automatically:

- **Two counts** — the previous count is the opening stock, and only what
  happened between them is in scope. Needs no invoice history before the
  opening count.
- **Since the beginning** — used when there is no earlier count. Opening
  stock is zero, and everything ever bought, sold and lost is in scope. One
  inventory really does reconcile: you know what you bought, what you sold,
  and what is on the shelf.

The second assumes the invoices reach back to the day the bar opened; the
page says so out loud (`report.since_beginning`), because otherwise every
bottle bought before the records start reads as missing.

### Sales are unique per (recipe, day, SOURCE)

Not per (recipe, day). A sale typed in by hand exists precisely because the
till never saw it, so an import must never overwrite it — and keyed without
the source, re-importing a period would silently delete the manual entry for
every day it touched. `sales_between` sums across sources.

### "OU" nests

An option in a choice group can be a recipe with choices of its own. So a
group offers not `len(options)` ways to satisfy it but the **sum** of what
each option offers (`Recipe.group_size`), and each option contributes a cost
**range**, not a number (`RecipeIngredient.cost_bounds`). Counting stays
linear in ingredients — a sum inside a product, never an enumeration.

Options are addressed by a flat index per group (`resolve_option` walks the
options accumulating sizes), so a selection is still one number per group
however deep the nesting and the detail page's `?v=` links are unchanged.

For the variance engine this means a nested choice must be pooled too:
`reachable_stock_types` is deliberately separate from the amounts and is
**never capped**, because a pool missing a member reports that member's whole
consumption as unexplained. Amounts are capped (`MAX_SUB_VARIATIONS`).

### The recipes that use an article (`recipes/usage.py`)

« Le sucre augmente, qu'est-ce que je dois reprendre ? » — the « Recettes »
tab filters on one article, `?article=<pk>`, from a picker listing the
articles at least one recipe uses with the count of recipes beside each.

**The walk is `inventory.variance.reachable_stock_types` and nothing else.**
A cocktail whose house syrup contains sugar IS a recipe that uses sugar, and
that walk already answers it — sub-recipes, alternatives, never capped, with
the cycle guard that goes with it. A second walker over the same graph is a
second set of rules about « OU » and nesting, and the two drift apart on the
first recipe nobody thought of. `article_uses` is a memo around it, keyed by
**sub-recipe**: one house syrup in thirty cocktails is read once, and the
« Recettes » tab's query count is per sub-recipe, never per recipe
(`NestedQueryCountTests.ARTICLE_PICKER_QUERIES` carries that constant, so a
fourth query is a decision rather than drift). Nothing is enumerated: 20
either/or ingredients is already a million variations.

**What it costs grows with the number of distinct sub-recipe NODES, and that
is not bounded today.** `_reached` memoises the top-level sub-recipe, but
`reachable_stock_types` re-queries every level it walks into, and `_settled`
asks `variation_count` besides. Measured on invented graphs: one flat syrup
under thirty cocktails is 16 queries for the page, five syrups of three
levels 63, ten syrups of three levels under sixty cocktails 113. The guard
above measures the axis that does not grow. **A memo inside
`reachable_stock_types` is the fix and it is not free**: that walk carries a
cycle guard (`seen`), so a result reached inside a cycle is TRUNCATED, and
caching one of those would silently shrink a variance pool - which reports
that member's whole consumption as unexplained. Cache only a walk that never
met the guard, or pass the memo down from `usage.py` and leave the engine
alone.

**« OU » is a choice, not a certainty**, so every use carries whether it is
settled, and the row says « peut-être ». A use is settled only when nothing
on the way is a choice: the ingredient is alone in its group **and**, where
it is a sub-recipe, that sub-recipe has no variations at all
(`Recipe.variation_count <= 1`, which is exactly « no choice anywhere below
it » — that is what catches a « sucre OU miel » syrup used as a fixed
ingredient). Deliberately cautious in one direction: a syrup whose only
choice is between two herbs reads as « peut-être » for its sugar, which is
certain. Understating leaves the recipe in the list with a hedge on it;
overstating is a purchase made on something that may never be poured.

**The sentence over an all-maybe list says a choice « quelque part », not one
on each recipe.** A cocktail whose only ingredient is a « sucre OU miel »
syrup offers no alternative of its own - the choice is inside the syrup, and
the « Utilisé » column is what says where. « chacune en alternative "OU" »
named it on the recipe itself, which is false in exactly the shape the
cautious rule above was written for.

**« Via » names the recipe's OWN sub-recipe, not the deepest one.** Through
two levels the row still says the one the recipe lists: that is the row the
reader opens next, and what is inside it is on its own page — where that
sub-recipe is itself listed as using the article.

**A `?article=` nobody can use is the whole list.** Not an id (`common.is_id`
— `?article=abc`, `²`, `-3`), an article the picker does not offer, an
article that no longer exists: these arrive from a query string, so a stale
bookmark and a hand-typed URL both land there, and an empty page under a
filter nobody can see reads as a page that has broken. Where the article
exists but no recipe uses it, the page **says so by name** — a whole list
with no explanation is the same silence. The filter travels on the recipes
tab's own link, built in the view (`menu.ARTICLE_PARAM`), and **only when it
is an id**: a link handing garbage back makes a stale bookmark permanent.

### Three workspaces, not eight pages

The navigation is **Produits & charges** (what was bought, by article, the
charges, and the products to classify),
**Factures** (invoices, tickets, their sources and suppliers - « Achats »
until 01/10/2026, renamed on screen only: the section key stays
`purchases`) and **Recettes & ventes**
(recipes, till products, sales), each with the count of what waits there
(`config/navigation.py` decides which link a page lights up), and **Données**
(export, import, clear). They were
separate pages, and checking that something added had landed meant going
back and forth between them. The rules that came with merging them:

- **Every old address still works** and draws the merged page on its tab or
  import (`inventory.views.StockListView` + `review_queue`,
  `invoices/workspace.py::render_purchases`, `recipes/menu.py::render_menu`).
  Tabs are links to those addresses; `hx-boost` swaps only `#workspace`, so
  the import card above keeps a running import as it is.
- **An action answers where it was taken.** A product classified in the
  side panel of Produits & charges gets the panel back with a note and an undo (the undo
  deletes a stock item the classification created - signed, `UNDO_SALT`),
  and `HX-Trigger: catalogue-changed` makes the list reload itself opened on
  that item. **What that opens is shown, not remembered** (`revealedDetails`
  / `collapseRevealed`, key `marginmate:stock:rows`): written into the
  browser's storage, one classification after another left every article's
  purchases open - on the real data, a dozen of them made the page 81 000 px
  tall to search through (owner, 20/09). Only what a reader opens themselves
  is remembered, and **a search answers with rows** (the reveal also drops
  the pending debounce of the panel's field: typed, then « Classer » at
  once, it filtered the list back onto a name already classified). A till product linked from
  its row gets the row back. A PDF
  imported comes back highlighted and opened in the list (`?surligner=`);
  the list reloads when an import or a gather ends (`documents-changed`,
  sent by their status partials once they stop polling).
- **A search narrows the list, not what a row opens.** « I want to be able
  to unroll the article and see the products bought even if their name
  doesn't match the search query » (owner, 25/09): the query is how an
  article is reached, and what it bought is the article's own business -
  an article found by typing « sucre » opens on every product it bought,
  cassonade and brand names included. A panel the reader had open before searching is
  still set aside (the 20/09 rule above); one they unroll **during** the
  search is theirs and stays (`.search-mine`).
  Three traps, all of them found by the browser test:
  - *Set aside is not closed.* Read as closed, the first click on an
    article "closes" what is already out of sight: the reader clicks,
    nothing happens, and they have to click twice (`isSetAside`).
  - *What a row opens leaves with the row.* Narrowing the query hides an
    article; without `data-child-of` on its panels, its **curve** stayed
    among the results explaining a row no longer listed.
  - *Nothing to run, nothing to race.* The flag is a class on the **body**
    and the hiding is one CSS rule, deliberately not a pass over the rows:
    the list replaces itself mid-search (a classification, an undo) and
    reopens what it had open. Measured - the pass ran while htmx was still
    settling the swap, so the panels came back **after** the only pass that
    would have set them aside, and sat among the results until the box was
    cleared. A flag on `#catalogue` itself went with the swapped element.
- **A bounded list needs a search the database answers.** The table's own
  box (`datatable.js`) only ever sees the rendered rows, so a page that
  shows its first 250 cannot use it: the Eau de Paris invoices sit at the
  277th row and the Total Energies ones at the 575th, and looking for them
  found nothing. `workspace.documents_matching` searches every document by
  supplier, number, date as it is written (12/07/2026, 07/2026, 2026) or
  amount, and the table is `data-table-sort-only` - two boxes filtering by
  two different rules is worse than one. Same on the sales list
  (`recipes.menu._sales_matching`, `SALES_PAGE_SIZE`): 8 099 rows was 2,6 MB
  on one page, and it only grows.
- **The list shows the newest documents** (`workspace.PAGE_SIZE`, 250), and
  "tout afficher" renders the rest. Every row is about 1,4 KB of HTML and a
  slice of a second of template: at 823 documents the page was 1,2 MB, 15 000
  nodes and 626 ms of server time, and opening a row moved a table 47 000
  pixels tall. Bounded, the same page is 393 KB, 4 000 nodes and 200 ms. The
  row that holds an opened document's lines is made when it is first opened,
  not printed hidden under all of them. Two things to keep: the document just
  imported (`?surligner=`) is shown whatever its date, since it may be older
  than everything on the page; and the table's search and sort only ever see
  what is rendered, which the page says out loud.
- **A page shows a document as it is, not as it was.** An import's log
  copied each file's shop, date, total and state at the second it was read,
  and its page went on showing those: a ticket corrected afterwards still
  read its first total there. `workspace.batch_rows` hands the template the
  documents themselves, and the state pill is `Invoice.review_state` - the
  same rule as every other list.
- **A badge and the list it stands for share one definition.** The "À
  vérifier" tab counted `TICKET_TO_CHECK` while the page listed
  `receipts.pending_receipts()`, and when charges were left out of one and
  not the other the tab read "102" over an empty page. `pending_receipts`
  is that Q and nothing else; the same went for the "Produits & charges" badge, which
  counted the charge items the page does not list (110 over 97). When a
  count is cheap to get from the list, take it from the list.
- **Where invoices come from and who they are filed under are two tabs** of
  Achats (the owner, 19/09): « Sources » lists the sources of invoices
  (`InvoiceType`), « Enseignes et fournisseurs » (`supplier_list`, which
  redirected to `types/#fournisseurs` before) every supplier, with a
  « Sources » column naming the sources fetching for each. Each tab builds
  only its own list (`workspace._sources`, `_suppliers`): the suppliers' list
  reads every document's text, and the tab counts are drawn on every page of
  Achats, so its count is a plain `Supplier` count, grey, and beside it an
  amber « N à voir » (the suppliers with a change to see, `_changes_to_see`),
  which the tab lists at its top with why each asks to be seen
  (`supplier_changes.why_to_see`) and a « Vu » - one number meaning two
  things, with nothing saying which, was a notification nobody could explain
  (19/09). Each change is listed as its supplier, its day and its summary,
  with no kind before it: every summary says what it is, and « Premier
  document du 19/09/2026 : Premier document : … » is what printing the kind
  first gave. Validating a supplier's first document answers its « premier
  document ». `id="fournisseurs"` stays
  on the Sources tab as a pointer to the new tab: an old bookmark's fragment
  never reaches the server, so nothing can redirect it.
- **Without JavaScript the same forms post and redirect** to the page; the
  in-place answer is chosen on `HX-Request`.
- **No out-of-band part beside a `<tr>`**: htmx 1.9 parses a row response
  inside a table, and the HTML parser moves a sibling `<span>` out of it.
  Counts next to a row answer travel as an `HX-Trigger` event instead
  (`to-link-count`, handled in `ui.js`).
- A page with a side panel is wider (`container-wide`), and the stock list's
  columns are shares, not pixels, so it fits beside the panel.
- **Under 1280 px the panel sits ABOVE the list, and is no scroll box**
  (the owner, 30/09: « I cannot scroll down when I have new products to
  classify »). Stacked, it kept `overflow-y: auto` and `overscroll-behavior:
  contain` from the sticky side panel, and Chrome takes a scroll container
  with `contain` for a boundary even when it has nothing to scroll: a wheel or
  a finger over the cards moved nothing - and on a phone the cards were the
  whole screen. There it is a plain block (`overflow: visible`,
  `overscroll-behavior: auto`, `overflow-wrap: anywhere`), it draws its
  **first product only** - the next takes its place as each is classified -
  and « Voir les N autres produits » shows the rest: a `<label>` for a box
  that sits OUTSIDE `#review-panel-body` (`#review-unfold`, stock_list.html),
  so it needs no script and a classification leaves the list as the reader
  set it. Two links lead between the panel and the list (« La liste ↓ »,
  the toolbar's « ↑ À classer », `.review-panel-jump`). A classification no
  longer scrolls the page down to the article there (`panelBesideList()`,
  read off the panel's computed `position`): the row opens and blinks where
  it is, and « classé dans … » takes the reader to it. Where a keyboard comes
  up on the screen (`KEYBOARD_ON_SCREEN`: a coarse pointer without hover -
  a touch screen, never a width: a laptop at 150 % zoom is under 860 px and
  types) nothing focuses the next field, and the toolbar's
  « Produits à classer » scrolls the panel into view instead. Beside the list,
  1280 px and up, nothing changed.

Beside the workspaces, Banque, Marges, **Personnel** (the staff's
timesheets, `staff/`), Inventaires and **Consignes** (the empties handed
back to the delivery driver, `returnables/`) are links of their own. A new app
lights its link only once it is in `navigation.SECTION_BY_APP`, and its words
in `navigation.SECTION_LABELS` (what the folded bar says, checked against the
lit link by tests/test_navigation.py); a new link is measured again (UI
conventions, the topbar).

**The words on screen, and why** (the owner, 19/09: tell the sources of the
invoices apart from the « Enseignes et fournisseurs », and the Produits page
"is not a stock but just a list of every spending (charges) + products
bought"). An `InvoiceType` is a **source** (« source de factures »: one
mailbox search or one customer portal, always for one supplier); a
`Supplier` is a **fournisseur** (« enseigne » only on ticket screens, where
it is what the ticket prints); fetching is **Récupérer**. A `StockType` is an
**article** - never « type de stock » - and a product no article claims yet
is **à classer** (a charge item never is: « poste de charge »). The
inventory workspace is **Produits & charges**, and its
all-time figures say what was bought (« Total acheté », « Acheté »), summed
from `PURCHASE` movements only, so a loss written down later never makes them
a lie. « Stock » stays where it is true: between two stock takes, what left
the shelf, what is missing, the losses, the ceiling of « Vendu ». Internal
names did not follow (models, fields, url names, context keys, `data-persist`
and localStorage keys, anchors), nor did texts already stored; these notes
still say "stock item" and "stock page" for the article and that workspace.

### « Du … au … »: one window, seven pages

Seven pages are read through a period: **Produits & charges** (the articles
bought between two dates), **Achats** (the documents), **Ventes** (the sales,
the sale invoices and « Par origine »), **Banque** (the operations),
**Marges** (the three margins), **Dépenses par catégorie** (what left the
account) and **Entrées d'argent** (what came into it). The last three are
the ones whose period has a **default** - the last 12 months - and all say
so on screen; `common.last_twelve_months()` is the one definition of that
phrase, so they cannot name the same period and count two different spans.
They read it once, through `common.date_range(request)` → `DateRange`, so
they cannot disagree about what "between these two dates" means:

- `?du=&au=` on every one of them, ISO dates (what `<input type="date">`
  posts), the context key `date_window`, the class `.date-range`;
- **both ends are included.** A person asking « au 28 » means the 28th. This
  is deliberately NOT the half-open window the stock pages slice with
  (`variance.movements_between`, `recipes.sales.sales_between`), where the
  opening date belongs to the count taken that day - hand a `DateRange` end
  to one of those and a day goes silently. `catalogue_context` converts on
  purpose (`window.start - timedelta(days=1)`) so « Vendu » counts the sales
  of the `du` day itself;
- a date that is no date is **no window, never a 500** (`?du=hier`,
  `2026-02-30`): these arrive from a query string, so a stale bookmark and a
  hand-typed URL both land there. Two dates the wrong way round are swapped;
  either end alone is a window (« depuis le 1er », « jusqu'au 28 »);
- **a row with no date is in no window** - `window.limit` drops it, and
  `window.holds(None)` is False. That is why « Sans date » on Achats drops
  the window entirely rather than opening an always-empty list, and why an
  import's own list (`lot`) ignores it;
- **the named period wins over the free dates**: `?inventaire=` on Produits &
  charges (two physical counts produce an opening, a closing and what is
  missing; two dates cannot) and `?mois=` on Banque. The inputs are then
  drawn `disabled` with a line saying which, and no link carries `du`/`au`
  while the preset is on;
- **every count beside the rows is counted over the same window** - a chip
  reading « Tickets 412 » over a page of 9 is the bug this invites - and
  **every link carries it**: chips, tabs, « tout afficher », the htmx
  `hx-get` that reloads a list in place, the `next` of every form. Build
  those URLs in the view (`window.parameters` + `urlencode`:
  `catalogue_url`, `workspace._list_url`, `bank._page_url`,
  `menu.sales_list_url`), never by pasting `{% if %}` fragments in the
  template - that is exactly where a parameter gets forgotten;
- **between two pages with a default, « tout » travels as `tout=1`**
  (`bank.views._other_page_url`): an empty window sent bare opens the other
  page on ITS default, a year, while this one said « tout l'historique ».
  Marges' own `_elsewhere` still sends it bare;
- **a GET form carries it as hidden fields**, from the same builder as the
  links (`bank._page_parameters` → `page_fields`). A GET form submits the
  fields it holds and nothing else, so a search box on a windowed page whose
  hidden `du`/`au` are missing answers on everything: the list, the four
  figures and the tab counts all widen at once with nothing on screen saying
  the dates were dropped. A test on the URL alone does not see this - assert
  the fields the page really drew and resubmit them
  (`bank/tests/test_links.py::search_form_of`);
- **what a row OPENS is the window too**, on Produits & charges: an
  article's purchases (`stock_type_movements`, narrowed on
  `StockMovement.effective_date` - what `catalogue_context` sums the row
  by, so the panel adds up to its own row) and a charge's documents
  (`_charge_panel`, on `invoice_date`, what `charge_suppliers` counts by).
  The three `data-movements-url` carry `panel_window_query`, built from the
  window **applied** - so a chosen `?inventaire=` keeps its all-history
  panels - and each panel names its dates with « tout l'historique » beside
  them (an `hx-get`, `hx-target="closest .row-panel"`: `closest td` is the
  foot of the charge table, which would redraw the panel inside itself).
  An empty one says « rien entre ces dates », never « jamais acheté ».
  That door swings **both ways** (`views._panel_doors`): « tout
  l'historique » keeps the dates in its URL under **`tout=1`** -
  « everything, and remember what was asked » - and the widened panel
  offers them back. Dropped, they were gone for good: a row is fetched
  exactly once (`stock_list.html` sets `dataset.loaded`), so closing and
  reopening it did not bring February back either, and the panel went on
  saying « tout » under « Acheté 12 ». The 📈 **curves are not windowed** and their URLs stay
  bare: a month of a curve is two points and « pas assez d'historique »,
  which is a feature removed rather than a window applied - so every
  sentence about what a row opens has to say which of the two it means.

`charge_suppliers(window)` takes either shape, a `StockPeriod` or a
`DateRange`, because both say `start` (possibly None) and `end`; an empty
`DateRange` is falsy and means its twelve-month default.

**A panel holding the row's own documents is read against it**, and four
disagreements only a window made visible were fixed with it (each a test
that failed first, `inventory/tests/test_panel_matches_its_row.py`): a
charge's panel **rounds each line to the cent** the way `charge_suppliers`
does before adding it up (raw, twelve bills of 19,99 € HT at 20 % footed
287,86 € under twelve printed lines of « 23,99 € », beside a row saying
287,88 €); the catalogue's scan works an article's « Total TTC » out of
`printed_ttc - discount_ttc` where a line printed one, exactly as
`InvoiceLine.total_ttc` does (from HT alone, a facturette printing 39,99 €
was 40,00 € on the row and 39,99 € in the panel below it); **« Total HT »
is the line's own amount**, not `quantity * unit_cost_ht` - that is the
same amount divided by the quantity, kept to four decimals and multiplied
back, so 2 000 units charged 28,84 € are priced 0,0144 and come back
28,80 € over a panel whose lines add up to 28,84 € (invented figures; on a
copy of the real database it moved a handful of articles by a few centimes,
20/09). Only
a movement with no line at all - a correction typed by hand - has nothing
but the ledger's arithmetic to go on, and `value_ht_by_type` stays that
arithmetic whatever happens: it is divided by the quantity to price a unit,
the way `StockType.current_unit_cost_ht` does. And the panel's
rows are **ordered by the date they print** (`effective_date`), not by the
invoice's - a delivery invoiced on 20/01 and received on the 27th sorted
between the 3rd and the 14th. The panel still lists **every movement** of
the window while the row's « Acheté » counts purchases alone, so its header
says « Achats et autres mouvements » when one of them is not a purchase and
each such line names its kind: a broken bottle under « Achats » is the page
asserting in French that it was bought.

### « Personnel » (`staff/`, `/personnel/`): the monthly timesheets

An employee, his typical week, and each month's « fiche de temps » - a grid
of days and a one-page PDF he signs (on paper, or as the next section
says). Migrations `staff/0001`-`0003` were applied to the real database by
the owner on 28/09, after a backup (`db.sqlite3.bak_20260928_pre_staff`).
Before that, /personnel/ answered « no such table » - the state of any
database that has the code and not the migrations - and no other page
touches a staff table. `staff/timesheet.py` is pure of request and template;
the views read the request, call it and say in French what happened, and
the page and the PDF only draw the `MonthSheet` it returns.

**A timesheet is personal data**: every name, address, typical week and
leave span in the staff tests and docstrings is invented
(`staff/tests/support.py::TYPICAL_WEEK`). The tests once used the real
contract week, to the hour - a week identifies somebody as surely as a name.

**The month:**

- **A month nobody saved is not stored.** It is the typical week, worked out
  each time it is drawn (`planned_days`: « Travail » where the typical hours
  are above 0, « Repos » elsewhere), so the page and the PDF show exactly
  what saving would store - the PDF byte for byte, which a test pins. The
  first save, whichever action makes it (« Enregistrer », « Du … au … », the
  holidays button), writes EVERY day (`_store`) **and copies the employee's
  typical week onto the sheet** (`Timesheet` is a `TypicalWeek` too).
  Everything about a saved month reads that copy (`MonthSheet.planned_week`):
  each day's typical hours and `differs`, « Semaine type », what was planned,
  the difference, a day put back to « Travail » without hours. Re-derived
  from today's week, a signed month came out, once the contract changed,
  with another « Semaine type » and an overtime nobody worked. Only
  « Revenir à la semaine type » takes today's week (`Outcome.week_before`,
  said in its message), and the month's page says when the employee's week
  is no longer the month's.
- **Public holidays are never zeroed by themselves**: whether one was worked
  is the owner's decision. The holiday's name travels with the day, and
  `mark_holidays_off` (a button) is the only thing making one « Férié
  chômé ». The Ascension can fall on 1 May or 8 May: `french_public_holidays`
  joins the two names rather than letting a dict drop one.
- **A day off is never a day of leave.** « Du … au … » with an absence, or
  « Travail » with hours, leaves the range's days off as they are
  (`_is_day_off`: « Repos » this month, or off in the month's typical week)
  and names them; the holidays button leaves a holiday on a day off alone; a
  range holding only days off writes nothing. Counted in, a week of leave
  printed its weekly rest days as leave, on a sheet the employee signs and
  that leave is taken out of. « Travail » without hours and « Repos » still
  cover every day.
- **Only « Travail » has hours**: an absence is 0 h whatever was posted.
  Enforced three times on purpose - `_settle`/`_store` (bulk writes skip
  `save()`), `TimesheetDay.save()` (the admin) and a CHECK constraint
  (`timesheet_day_hours_only_when_worked`) that no `update()` gets round.
  Hours on a leave day would print without counting.
- **The difference sets absences aside**: `MonthSummary.difference` is the
  hours worked less what was planned for the days that were **not** an
  absence, labelled « Écart hors absences » when there is one. Against every
  planned day, a month worked exactly as planned but for a week of leave
  printed the leave as a deficit, under « Lu et approuvé ».
- **Weeks are ISO weeks**; one straddling two months has a partial total in
  each (`MonthWeek.partial` is « runs over the month's edge », not « fewer
  hours »). A 31-day month starting on a Saturday or a Sunday has six week
  totals - 37 rows, the PDF's worst case.
- **The month in an address is a path converter** (`staff_month`, years
  1900-2999): a month that is none is a 404 that reaches no view, and
  `reverse` refuses it too, so « ← décembre » is not drawn on the first month
  it allows - a page must not link to its own 404. « This month » is Paris's
  (`views.this_month`, `timezone.localdate`; tests patch it). Day and month
  names are this app's constants (`DAY_NAMES`, `MONTH_NAMES`, `month_title`
  with its elision): `LANGUAGE_CODE` is en-us.

**What was typed:**

- **Hours as typed** (`parse_hours`): « 7 », « 7,5 », « 7.5 », « 7h30 »,
  « 7:30 », blank = 0. Refused with a French sentence naming the day
  (`HoursError` is a ValueError, never a 500): negative, over 24, garbage,
  two figures a space apart (« 1 5 » read as 15 h in silence; « 7 h 30 »
  still reads), and anything short of two decimals once converted - 7h20 is
  7,333… h, and rounded it prints a figure nobody typed. Minutes are two
  digits; digits are ASCII (`[0-9]`, never `\d`, which says yes to « ٣ »).
  The typical week's fields read hours the same way (`forms.HoursField`: a
  DecimalField refuses the comma under en-us) and speak French
  (`Meta.error_messages`). `format_hours` is the one way to print them, page
  and PDF.
- **Missing is not blank.** A disabled input is not posted: a field the post
  did not carry keeps its value (`PostedDay` fields are None), a blank hours
  field is 0 - so « Du … au … » reads its blank hours with
  `parse_optional_hours`, never `parse_hours`.
- **Hours typed on a « Repos » day mean it was worked** - only if it was
  ALREADY « Repos » before the post. One just switched to « Repos » with its
  old hours still in the field (no JavaScript) is a day off; only the state
  before the post tells the two apart, and the first is said
  (`Outcome.adjustments`).
- **The grid is not a formset**: every field is named by its day's ISO date
  (`heures-2026-02-17`), so no gap in row indices can move a day's hours onto
  its neighbour. Still tested as the browser posts it: `staff/tests/
  page_forms.py` reads every form off the rendered page (a select's chosen
  option, a disabled field not sent, the pressed button alone) through a
  CSRF-enforcing client. The margins helper will not do: it has no `<select>`.

**The pages:**

- A refused save writes nothing and draws the page back as typed (200), each
  error in its row. Every shortcut (« Du … au … », the holidays, « Revenir à
  la semaine type ») is a POST answering with a redirect and a message naming
  what changed - and that the month was saved for the first time, when it
  was; a GET on one writes nothing. A month a signature request holds is
  refused by `_store` itself (`MonthLocked`), and every action says so.
- **« Du … au … » here posts `debut`/`fin`, never `du`/`au`**, the windowed
  pages' GET period: two forms answering to one name is how a test finds the
  wrong one. Its days are two `<select>`s of the month's days, `required`
  with an empty first option (a default day silently chosen is a wrong
  range) - not `<input type="date">`, which Chrome in English draws
  mm/dd/yyyy and which can leave the month.
- « Revenir à la semaine type » wipes the month and is confirmed by a
  `<details>`, not `data-confirm`: `confirm()` does nothing without
  JavaScript.
- **The server never disables an absence's hours field**: without
  JavaScript, a day switched back to « Travail » takes its hours in the same
  post. `static/js/timesheet.js` disables it, puts the typical hours back,
  switches a « Repos » day to « Travail » when hours are typed on it, and
  recomputes every total, the cards and the orange mark live - in integer
  hundredths, by the server's rules, « — » for what `parse_hours` refuses,
  never a total that skipped a figure. The server's figures stay the
  reference: `test_month_browser.py` saves the grid and checks the page drawn
  after says what the script said.
- **Nothing typed is lost without a word.** While the grid holds unsaved
  changes: « Modifications non enregistrées », the cards in dashes, the
  browser's prompt on leaving, and a sentence on ← / →, the PDF and the three
  forms beside the grid (`data-leaves-grid`, `views.LEAVE_WARNING` /
  `PDF_WARNING`) - those forms reload the month from the database and the PDF
  prints it as SAVED, so corrections vanished and a sheet could go to be
  signed without them.
- A refused employee edit is bound to a second instance: a ModelForm writes
  what was typed onto its instance while validating, and the page's header
  showed the refused week. Deactivating posts the state wanted (`actif=0`),
  never « toggle »; nobody is deleted from the pages (PROTECT: a signed sheet
  is kept).
- On a phone the grid's rows are cards, and the grid is in no `.table-wrap`:
  « Enregistrer » sticks to the bottom of the window, which it cannot do from
  inside a scroll region.
- `QueryCountTests` compares each page's queries with one employee and with
  five holding a year of months - compared, never pinned, since the topbar's
  badges are other apps' queries.

**The PDF** (`staff/pdf.py`, `render_month_pdf(sheet, establishment=None)`):

- **Written by hand, PDF 1.4 - no PDF library is installed and none is to be
  added.** The two standard Helvetica fonts, nothing embedded,
  `/WinAnsiEncoding`, a Flate stream, counted xref offsets; glyph widths from
  `pdfminer.fontmetrics.FONT_METRICS`, which pdfplumber brings. It draws the
  sheet it is given and adds nothing up.
- **One page, always**: the signatures belong under the days they sign for.
  The rows get what is left (`_row_height`, at most `MAX_ROW_HEIGHT`), so
  everything else is bounded - the address prints `ADDRESS_LINES` lines, the
  rest joined onto the last, and every text is cut to its column with « … »,
  never wrapped. A line added to the summary is for the one-page tests over
  the worst case to judge.
- **cp1252, and « ? » rather than an exception** (`printable`): NFC first,
  format characters dropped, controls and what cp1252 lacks « ? »; the
  app's minus (U+2212) and the narrow no-break space have exact equivalents
  and print as them. A name goes through `printable_name` (its nearest
  letter, never « ? »), and the month's form refuses a note the sheet cannot
  print (next section). `Canvas.text` takes printable text and does not
  normalise it again: a second pass ate the leading space of a note.
- **Deterministic** (no date, no random id), and drawing reads nothing from
  the database (`assertNumQueries(0)` on a built sheet).
- **A download writes nothing** - not the month, not the header's row:
  `Establishment.current()` is a `get_or_create`, so the view reads
  `.filter(pk=SINGLETON_PK).first()` and passes None. **No name, no header,
  address included** (`_establishment_lines`), as the page where it is typed
  says.
- **`content_disposition(sheet)` is the whole header, in plain ASCII**: an
  ASCII fallback, then RFC 5987's `filename*` with the exact name. A header
  Django has to encode comes out as `=?utf-8?b?…?=`, which no browser reads
  as a file name; `/`, `"` and what no Windows name may hold become « - ».
- Tested by reading the PDF back through pdfplumber: every day, hour and
  total as extracted lines, the right alignment of every figure, and the
  worst case and every month of two years inside the margins with nothing
  overlapping.

### The electronic signature of the timesheets (`staff/signing.py`)

The employee signs his MONTH from his phone, through a link; the owner
countersigns. Open source and costing nothing on purpose (pyHanko 0.37, MIT;
pinned in uv.lock with its dependencies), so it can be sold later.

**What it is, in words:** a **simple electronic signature** (eIDAS art. 25,
Code civil 1366-1367) - never « qualifiée », « avancée » or « équivalente à
une signature manuscrite », on any screen, message or file (tests look for
those words). Since Cass. 3e civ. 5 March 2026 the employer must prove its
reliability if the employee denies signing, so the product is the
EVIDENCE: the exact document (PAdES, SHA-256), who (a certificate in his
name, a one-time code), when (a third party's RFC 3161 timestamp), and a
proof file a person can read, tied to the document by an ID printed in both.

Four modules, each pure of request objects:

- `staff/signing.py` - keys and certificates (an internal authority per
  installation, the employer's, one per employee made at his first
  signature; EC P-256, ten years), `freeze` (the saved month's PDF plus BOTH
  empty fields in one incremental update - its first revision is byte for
  byte `render_month_pdf(..., electronic=True)`), `sign_as_employee`,
  `countersign`, `verify`, and the drawn signature's checks
  (`clean_signature_png`). The stamps are drawn with `pdf.Canvas` inside
  `pdf.SIGNATURE_BOXES` - **the one definition of where the two boxes are**:
  the page draws them from it, the fields are placed by it - and both are
  laid out by `_stamp_style`, the one definition of what a stamp holds.
  **`electronic=True` is for `freeze` only** (28/09): the boxes of the frozen
  document say « Signature électronique du salarié / de l'employeur », same
  size, grey and place (`pdf.ELECTRONIC_SIGNATURE_BOXES`, the same frames -
  `signing.FIELD_BOXES` places the fields by them), instead of asking for a
  handwritten date and « Lu et approuvé » over the stamp. The download keeps
  the paper words **byte for byte** (`test_pdf.PAPER_SHEETS` pins the content
  stream as it was before the option); documents frozen before keep theirs,
  are never drawn again, and still sign where their fields are.
- `staff/signature_requests.py` - the workflow: a request per version of a
  month (`SignatureRequest`, migration `staff/0003`, applied on the real
  database 28/09), the link's token (only its SHA-256 is stored), the one-time code
  (HMAC, 15 minutes, 5 attempts, 3 an hour of EACH kind, remembered in the
  employee's session for that request only), the month read-only while a
  request holds it, « Corriger ce mois » (`reopen_month`: cancelled /
  superseded, files kept), and the events - an **append-only hash chain** per
  request, its head on the request row, so an event edited, removed or cut
  off the end no longer adds up (`verify_event_chain`) - unless whoever did
  it could rewrite the database and worked every hash out again (below).
- `staff/proof.py` - the proof file, written again after every step.
- `staff/signature_mail.py` - optional e-mail (off unless EMAIL_HOST is
  set); a failed send is a sentence and an event, never a 500.

Rules that cost nothing to keep and a lawsuit to lose:

- **A month nobody saved is never sent for signature**: it is the typical
  week, a planning, not a record (`signing.UNSAVED_MONTH`).
- **No timestamp, no signature.** The servers of `STAFF_TIMESTAMP_URLS`
  (DigiCert, then Sectigo) are tried in order; when none answers the
  signature is REFUSED and nothing is stored but the `timestamp_failed`
  event. `signing.timestampers()` is looked up at call time: **tests inject
  pyHanko's `DummyTimeStamper`** through `staff/tests/signing_support.py`
  (`OfflineTimestamps`, `SigningTestMixin`), settings_test has no server at
  all, and `tests.support.NoNetworkTestCase` makes pyHanko's HTTP clients
  fail loudly. Never sign against DigiCert from a test or a coding session.
- **The espace's `private/` folder** (`accounts.paths.private_dir`, under
  TENANTS_ROOT; single mode's `STAFF_PRIVATE_DIR` / `MARGINMATE_PRIVATE_DIR`
  went with it on 29/09) holds the keys, the signed PDFs, the drawings, the
  proof files and deletions.log (« Supprimer… », below) - **never inside a
  folder the site serves** (STATIC_ROOT, STATICFILES_DIRS:
  `private_files.private_dir` refuses it), and beside the espace's media/,
  never in it. **Back it up with the espace's database.**
  Losing it does not make a signed PDF unverifiable (each signature embeds its
  certificates; `verify` still says what each signature covers is intact and
  whom it names, and that its certificate « ne se rattache à aucune autorité
  connue de cette installation (clés perdues ou remplacées ?) »), but the next
  signature comes from a new authority. So the authority that issued a
  signer's certificate is **recorded when he signs** (`Signed.issuer_sha256`,
  `authority_sha256` in the `employee_signed` / `countersigned` events) and
  the proof file prints that one - never `authority_fingerprint()`, the one
  on disk today - saying so when today's differs.
- **`MARGINMATE_SIGNING_PASSPHRASE`** encrypts the private keys; unset they
  are in clear and `signing.key_warning()` is the line the owner's pages
  show. Keys written in clear are encrypted at the next signature once it is
  set. The app must not go online before it is set. Every owner's page is
  behind the login (it did not exist when this was written: single mode's
  pages said so, `ONLINE_WARNING`, gone with it); only the employee's
  signing page is meant to stay reachable without an account (its secret
  link and its code).
- **Adobe shows « validité inconnue »** for our certificates: a trust
  warning, not an alteration (`signing.ADOBE_UNKNOWN_VALIDITY`); `verify`
  checks offline against our authority and, for the timestamps, certifi's
  Mozilla list.
- The new env names (`MARGINMATE_*`, `EMAIL_*`, `DEFAULT_FROM_EMAIL`) are in
  `invoices.models.APP_ENV_PREFIXES`: a portal may never be handed the mail
  server's password.
- **Retention**: `manage.py staff_purge_signatures [--dry-run]` deletes a
  request, its events and its files together, five years after the month
  (`STAFF_SIGNATURE_RETENTION_YEARS`) - through the ONE function
  « Supprimer… » uses (below), so each leaves its line in deletions.log. Never
  run automatically; the month's own timesheet is not touched. The
  employee's page PROMISES that deletion, so the owner's « Signature »
  section says, once anything was sent, that running the command is his to
  do (`SignaturePanel.retention_note`).

**What the evidence rests on** (review of 28/09, each rule a test that
failed first):

- **The method recorded is the one of the code he TYPED.** `issue_code`
  keeps a code's method beside it (`code_method`, in 0003) and `check_code`
  copies it to `identification` when that code is verified. Written at
  issue, a code asked for by e-mail from another browser - anyone holding
  the link - after he had typed the one handed over turned the row, the
  signed event and the proof into « envoyé par e-mail ». `code_verified`
  says its method in the journal.
- **The two ways of getting a code do not undo each other.** Three an hour
  of each (`_codes_in_the_last_hour(…, method)`), and a code by e-mail is
  refused while the one the employer handed over can still be typed
  (`HANDED_OVER_CODE_WAITING`); the employer's replaces anything. Shared,
  two presses of « Recevoir un code par e-mail » voided the employer's code
  and spent his hour. The page puts typing a waiting code first and says a
  new one voids it (`waiting_code_method`), and so does the code's e-mail.
- **What he signed is where the database alone cannot rewrite it.** His
  reservations, in his own words, and the chain's head at that moment are in
  his signature's /Reason (`signing.employee_reason`, read back by
  `signed_reasons`) - inside the signed byte range, under the timestamp; the
  countersignature seals the head the same way. `verify_event_chain` checks
  that each sealed head is still in the chain before the event recording
  that signature: the events before the last signature cannot be rewritten
  unseen, even by somebody who worked every hash out again and moved the
  row's head. After it they rest on the database alone, and the proof file
  says exactly that - never « ajouté après coup ne correspond plus » as if
  the chain alone resisted a database writer. The proof prints the
  reservations from the signed PDF, the method and the certified text from
  the signed event, and names every row value that no longer matches its
  journal (`proof._row_against_journal`).
- **What `SignatureEvent.ip` stores is what is hashed**: `_clean_ip` goes
  through Django's `clean_ipv6_address`, as the column does. Python's
  `compressed` wrote an IPv4-mapped address (« ::ffff:203.0.113.7 », what a
  server on [::] reports for every IPv4 client) another way, and an
  untouched journal read « Journal altéré ».
- **Nothing technical reaches the employee's page.** `sign_for_employee`
  says `DOCUMENT_CHANGED` or `NOT_SIGNED` for whatever is the server's (a
  file gone, keys that do not open) and logs the detail; only his own
  refusals keep their words (his drawing, the box, the step, no
  timestamp). `public_views.submit` turns anything unforeseen into the same
  sentence, a 500 with the traceback in the log - Django's error page under
  DEBUG shows paths and settings. It had shown the private folder's (then STAFF_PRIVATE_DIR) absolute
  path and the passphrase variable's name.
- **A certificate's common name holds 64 BYTES of UTF-8** (X.520;
  `cryptography` raises a plain ValueError past it, which escaped every
  page as a 500): « Autorité interne de » and a 44-character name with one
  « é » are already 65. `signing._limited` cuts on the bytes, between two
  words. The stamps print the name WHOLE (`_signer_lines`: on its own line,
  smaller down to 5 pt, then over two lines) and write a letter cp1252 lacks
  as its nearest (`pdf.printable_name`: « Łukasz » is « Lukasz », never
  « ?ukasz »), as the sheet's « Salarié : » line and header do.
- **What his phone shows is what he signs, to the character.** The
  snapshot (version 2) keeps each « Motif / note » cell as the PDF prints
  it (`pdf.note_cells`: cut with « … », « ? » for what cp1252 lacks), and
  the PDF draws those same cells. The month's form refuses a note holding a
  character the sheet cannot print, naming it (`pdf.unprintable_characters`).
- **The month's page costs the same whatever the number of versions**: every
  version's events in one prefetch, handed to `verify_event_chain(request,
  events)` (`SignatureQueryCountTests`). It had cost three queries a version.

**The employer draws his signature too** (the owner, 28/09: « I cannot draw
my signature as the employer »). « Contresigner… » on the month's page opens
a pad and the countersignature posts the drawing (`signature_views.DRAWING`,
a PNG data URL). The pad is the employee's `static/js/signature_pad.js`,
now generic: any `form[data-signature-form]`, and the frame is watched by a
ResizeObserver as well as the window's `resize`. Chrome lays a closed
`<details>` out all the same (the folded canvas measured 542 × 176 and was
fitted at load, 28/09); the observer is for a browser that gives it no size
until it is opened - none the tests drive. The page loads the script only
while there is something to countersign. Each rule a test:

- **Required, and never a 500.** Nothing drawn is stopped by the script
  (« Dessinez votre signature dans le cadre avant de contresigner. ») and
  refused by the server with the same words
  (`signing.EMPLOYER_DRAWING_MISSING`) - a crafted post, or a page drawn
  before the pad existed, which posts its token alone. **The step is checked
  before the drawing** (`signature_requests.countersignable`): a page drawn
  before the employee signed, or after the request moved on, is told the
  step at the top of the month (a redirect), not to draw. A drawing refused, or no
  timestamp (503), redraws the page with the sentence beside the pad, the pad
  open, and the drawing painted back only when it was one
  (`signature_views._drawn_back`: what was no drawing is never echoed).
- **The employee's checks** - `signature_png_from_data_url` in the view,
  `clean_signature_png` inside `signing.countersign` (size, dimensions, ink,
  encoded again, every other chunk dropped), before anything is signed or
  timestamped. `countersign` returns the picture it kept (`Signed.drawing`,
  `drawing_sha256`): what is stored is exactly what was sealed.
- **One layout for both stamps** (`signing._stamp_style`): the drawing above,
  « Contresigné électroniquement par … », the moment and the ID at the foot,
  the same sizes and baselines as the employee's
  (`test_both_stamps_share_one_layout` reads both appearance streams). A long
  establishment name is still whole; over two lines it leaves the drawing
  less height, never the box.
- **No column, so no migration** (the owner had just migrated the real
  database through 0003): the picture is `employer_signature.png` in the
  request's folder (`private_files.EMPLOYER_SIGNATURE_IMAGE`), its SHA-256 in
  the COUNTERSIGNED event (`signature_requests.EMPLOYER_DRAWING`, read by
  `employer_drawing_sha256`, from the prefetched events on the owner's page)
  AND sealed in the countersignature's /Reason (« Contresignature de
  l'employeur — signature dessinée SHA-256 … — journal … »,
  `signing.employer_reason`, read back as `SignedReason.drawing`). The proof
  prints « Signature dessinée de l'employeur : SHA-256 … » from the seal,
  says it is sealed, and names an anomaly when the journal or the file on
  disk disagree with it; the owner's panel offers « Signature dessinée de
  l'employeur (PNG) » beside the employee's (`OwnerFile.sha256`).
- **« Read and seals nothing » is not « could not be read »** (`proof.
  _employer_drawing`): once the countersignature's /Reason was read, the seal
  and the journal are ALWAYS compared - a journal naming a drawing the seal
  does not hold, or losing one it does, is an « Anomalie ». Compared only when
  both were non-empty, both tamperings printed as normal (review, 28/09).
- **Each signer is refused in his own words**: `clean_signature_png(empty=)`
  - `DRAWING_EMPTY` (« … avant de signer ») for the employee,
  `EMPLOYER_DRAWING_EMPTY` (« … avant de contresigner ») for the owner, whose
  single click on the pad is a dot too thin to count as ink.
- **A request countersigned before 28/09 has no drawing** (a database in use
  before then may hold one) and reads as it did: no drawing offered, no line about it
  in the proof, its /Reason without the mark reads `drawing == ""`, its
  journal still sealed. `signing_support.countersign_without_a_drawing` makes
  one the old way, step for step, for the tests. Checked on a scratch run
  (28/09): the new `proof.py` writes such a request's proof byte for byte as
  the old one did, and describes its events and chain identically.

**The pages.** The owner's side is a section of the month's page,
« Signature » (`staff/signature_views.py`): « Envoyer pour signature » (by
e-mail when a server is configured and the employee has an address -
`Employee.email`, on his form - else only once « Je transmettrai le lien
moi-même » is ticked), « Nouveau lien », « Code à transmettre par un autre
canal que le lien », « Contresigner… » (the pad, above), « Annuler la
demande », « Corriger ce mois », « Vérifier », each version's files (checked
against their hashes, logged, the proof written again as it is downloaded)
and journal, and on every version « Supprimer… » (below). While a
request holds the month the page draws it read-only (`_month_table.html`,
from the same `month_snapshot` builder the employee's page uses). **The link
and the code are shown ONCE**, in the answer to the POST that made them -
not redirected, since carried to the next GET they would sit in the session
(the database) or a cookie; only their hashes are stored anywhere. **The
section speaks of the person by name** (« Donnez vous-même un code à DURAND
Jeanne », « pour que DURAND Jeanne récupère… », « Signé par DURAND Jeanne
(PDF) ») or in words naming nobody - never « il » beside a woman's name, and
the pills say « En attente de signature » / « Signée, à contresigner »
(`signature_views.PANEL_STATUS`; the model's labels stay the record's words,
in the proof file). A test walks every step for « salarié » outside the
journal (`test_the_section_speaks_of_her_by_name_never_as_he`).

The employee's side is `/personnel/signer/<token>/` (`staff/public_views.py`,
`sign.html`, `public_base.html`): **the only pages meant to stay reachable
without an account.** They do NOT extend base.html and are rendered without
the context processors (no navigation, no badges, no owner's message can
reach them); everything they show comes from the one request the token
reaches, drawn from its frozen snapshot. Unknown link 404, gone 410, a
refused CSRF post 403 (`CSRF_FAILURE_VIEW`, French on these paths only) -
each a plain French page. Headers: `no-store`, `noindex`, a
Content-Security-Policy allowing the site's own script and stylesheet only
(so no inline script or `style=` there), and **`Referrer-Policy: same-origin`,
never `no-referrer`**: with it Chrome posts `Origin: null` and Django's CSRF
check refused every form of the page - the unit tests passed (the test
client sends no Origin), the browser test (`test_sign_browser.py`) did not.
The drawing is `static/js/signature_pad.js` (pointer events, no dependency,
capped to the 1200 × 400 the server accepts, painted back after a refused
post). The link is built from `MARGINMATE_SITE_URL` when set - set it before
going online: otherwise it comes from the request's Host header. A verified
code is said ONCE (« Code vérifié : vous pouvez signer. », drawn while the
session is identified; `check_code` adds no notice of its own). Two CSS
traps the browser test measures at 375 px: « Annuler le dernier trait » /
« Effacer » are 44 px tap targets beside either pad (`.btn-small` made them
30; every other `.btn-small` of the owner's pages stays small with a mouse, at any width; on a touch screen every button of `<main>` is 44 px, 30/09), and
`.public-form > label` must leave `.public-check` alone - its
specificity beat the flex row and put the certification's second line under
the checkbox.

**Deleting a version: « Supprimer… », in two steps**
(`staff/signature_deletion.py`; the owner, 28/09: « I would like to be able
to delete signed time sheets (with double verifications as this can be
dangerous) »). Every version on the panel, whatever its state, ends with a
discreet « Supprimer… » - a link, not a form: nothing is deleted from the
section itself. Each rule a test (`staff/tests/test_signature_deletion.py`;
a scratch run broke each one in memory and saw its test fail):

- **Step 1** (`signature_delete`, GET then POST) says what goes - the
  version, its state and dates, every file of its folder by name, the number
  of events, the proof - and why it is dangerous: the signed sheet and its
  proof are the employer's evidence of the hours; working-time records are
  kept 1 year for the labour inspection (D3171-16), 3 for a wage claim
  (L3245-1), 5 as this app recommends; the deletion is final. Its POST
  passes only with « Je comprends que la suppression est définitive »
  ticked AND the phrase typed (`deletion_phrase`, « supprimer juin 2026 »,
  accent included; `phrase_matches` trims, merges spaces and case-folds).
  Refused, the page is drawn again as posted (200). Valid, it still deletes
  nothing: it redirects to step 2 with a token.
- **The token** (`confirmation_token`): `django.core.signing`, a salt of its
  own (`TOKEN_SALT`), `max_age` ten minutes, binding the uuid, the status,
  the document and final hashes step 1 saw, and the hours option. Step 2
  reads it on GET and POST (`read_confirmation`): missing, tampered or
  signed for anything else → `TOKEN_INVALID`; too old → `TOKEN_EXPIRED`;
  another version's → `TOKEN_OTHER`; the version moved meanwhile (signed,
  countersigned, expired) → `CHANGED` - each back to step 1 with its
  sentence, nothing deleted, never a 500. The tests move the clock by
  patching `django.core.signing.time`.
- **Step 2** (`signature_delete_confirm`): its GET is « Dernière
  vérification », a summary and ONE red button (`.btn-destroy`); only its
  POST deletes. A version already gone - the button pressed twice - is said
  at the month's section.
- **« Supprimer aussi les heures enregistrées du mois »**, unticked, is
  offered only on the month's LAST remaining version (`is_last_version`):
  the Timesheet and its days go, and the month is the typical week again,
  as if never saved. Checked again at step 2 and inside the function: a
  version sent between the steps takes the option back (the requests
  PROTECT the Timesheet - it would otherwise have been a ProtectedError).
- **ONE function deletes**, `delete_signature_request(request, how=PAGE |
  PURGE, with_hours=, expected=, ip=)`, and `staff_purge_signatures` calls
  it too (a test spies on it). One transaction: the row read again (gone,
  or not what `expected` says → refused), the request deleted (its events
  cascade), the timesheet when asked, then the tombstone - LAST, inside the
  transaction, so a line that cannot be written rolls the deletion back:
  nothing is deleted without its trace. The files go on commit
  (`transaction.on_commit`): rolled back, they stay with their rows; a
  folder that cannot be removed then (a file held open on Windows) is a
  warning beside the success (`Deleted.files_error`), not a 500. Call it
  outside any transaction of your own. **A TestCase never commits**: its
  on_commit callbacks run only inside `captureOnCommitCallbacks(execute=
  True)`, which the tests wrap round the deleting call - and they run when
  that block ends, after the page has answered.
- **The tombstone**, the espace's `private/deletions.log`, outside the
  database: one JSON line per deleted version, UTF-8, fsynced
  (`private_files.append_deletion_record`); `read_deletion_records` splits
  on « \n » only, since a U+2028 in a name is no line end. It holds when
  (Paris, with its offset), how (« page » / « purge »), the employee's
  name, the month, the version, the uuid, the state, the SHA-256 of the
  frozen, signed and countersigned documents, the files, the number of
  events, whether the hours went, and the client's address (none from the
  purge). Step 1 says the trace is kept. It keeps the name after the rest
  is gone - a record of the deletion, which the app never purges.
- **After**: the public link answers as a link that never existed (404),
  and a month the deleted version held is editable again. Version numbers
  follow the highest version REMAINING: the month's only version deleted
  and the month sent again is a version 1 again, with a new document number -
  the uuid is what the stamps and the tombstone name.

### « Consignes » (`returnables/`, `/consignes/`): the empties handed back

The kegs, crates and CO2 bottles given back to the delivery driver:
photographed and counted on the phone before the lorry leaves (a
**reprise**), then compared with the **bon** the seller's driver e-mails
(UBA's: one PDF per delivery, a « REPRISE VIDE » part) and with the refund on
the seller's invoice. Generic on purpose (the owner, 29/09: « other sellers
with other tickets »): how a bon is read is a **format de bon**, a set of
**motifs** (regexes) the owner edits and « Teste » on the page. Seeded by
`returnables/0002` in every database - the test one, the `_template`, every
espace: three types (« Fûts », « Caisses verre », « Bouteilles CO2 ») and
« UBA — bon du livreur », whose motifs were checked against the owner's real
bons (every part found, every line read, every total matched, the re-sends
and the replacements seen); tests that need an empty app call
`returnables/tests/support.py::no_defaults()`.

**Migrations - unlike Personnel, other pages read these tables.**
`returnables/0001`-`0002` are written and applied to test databases only.
Achats (the « Récupérer » card offers a format's bons), the gather
(`tasks._gather_slips`), Achats' import (the guard below), the supplier page
(`delete_refused`) and « Données » (its section, and the after-commit file
check that walks every FileField) all read the returnables tables: an espace
with the code and not the migrations breaks THEM, not just /consignes/. The
owner runs `manage.py migrate_tenants` after a backup; no agent does.

**Words on screen**: « Consignes » is also an article category of Produits
& charges and a Dépenses category - the page's subtitle says which it is
(« Les vides rendus au livreur… »). A retrieval is a **reprise**, the
seller's document a **bon** (never « ticket »: the app's ticket is a till
receipt), its reading rules a **format de bon**, a regex a **motif**, trying
one **« Tester »**, a kind of returnable a **type de consigne**. Type names
are shown exactly as typed, never lower-cased or singularised, and counts
read « Fûts 15 · Bouteilles CO2 1 » - no grammar to get wrong. Comparison
rows need no agreement either: « Fûts — compté : 15 · sur le bon : 14 → il
en manque 1 sur le bon (30,00 €) »; never « Le bon compte … », which reads as
the idiom first. Sentences built in Python write money with ONE helper,
`comparison.euros`; templates print `|money €` like every page (« Amounts
are grouped by thousands », UI conventions).

**The motif guard (`patterns.py`) - why it comes first.** `regex` is what
runs a motif, for its `timeout`. But `regex.compile` has NO timeout and
EXPANDS counted repetitions: compiling `(?:x{65535}){65535}` allocated about
50 GB and froze the owner's 16 GB PC twice (29/09). So a motif is compiled
only after the standard library's pure-Python parser (`re._parser.parse`,
which expands nothing) has shown its shape and it passed: no VERBOSE in any
form (under `(?x)` regex reads `{6 5 5 3 5}` as a count, the stdlib as
text), no `{` that is not a plain count (regex reads `a{e<=1}` as a fuzzy
constraint), no `[` inside a set, no repetition over 100, no nesting
multiplying past 1 000, no group nesting deeper than 20 - **and the same
repetition rules again on `regex`'s OWN parse** (`_check_regex_tree`: the
Source/Info/`_parse_pattern` that `regex.compile` itself runs, which expands
nothing either) - then `regex.compile` inside `except Exception`. The second
tree is not belt and braces: the review of 29/09 walked around a guard that
read only the stdlib's tree with POSIX classes - `regex` reads `[[:alpha:])(]`
as ONE set where the stdlib ends it at the first `]` and reads `)(` as group
brackets, so a 116-character motif had six nested `{100}` for `regex` and
none for the stdlib (100^6, from any form). **Whatever a check measures, it
measures on the tree that is compiled.** Its private names (`_regex_core`)
are pinned by a test, and `regex` is pinned in uv.lock. Every entry
point goes through it: the forms, « Données »'s
import, the reading, a stored motif at each use (a stricter rule, a
hand-edited archive: the page says « motif invalide : … — corrigez-le »,
never a 500), and the gather's mail motifs (`patterns.mail_matcher`, passed
as `find_matching_emails(compile=…)`). **Tests of a refusal patch
`regex.compile` with a sentinel that fails if called**; never compile a
refused motif for real to see what happens. **Budgets**, because the 0.25 s
timeout is per call: a reading has 2 s in all, a page's classification 1 s
(memoised per designation; what is left is « non classée : motif trop
lent »), « Relire les N bons » 30 s and says how many are left; every call
runs `concurrent=True`, and a timeout in a test is SIMULATED (a pattern
whose search raises TimeoutError), never a real catastrophic match.

**The one writer (`slips.store_slip`)**: the page's upload, the gather and
Achats' guard all store a bon through it. 5 MB at most, then the sha256 (the
same bytes are « déjà reçu »); the PDF's text OUTSIDE any transaction (5
pages, 200 000 characters, pdfminer's zoo of exceptions all « pas un PDF
lisible », and what pdfminer INFLATES bounded: a 5 MB file can decompress to
gigabytes, so `reading.bound_pdf_decoding` - installed by the app's
`ready()`, for the whole process, Achats' PDFs included - caps Flate, LZW
and RunLength at 64 MB per stream, and one bon's streams share 64 MB through
`inflate_budget`; past it the bon is « trop long »); the format given or the ONE active format whose start motif
matches (several is a refusal naming them); the re-send check (same format,
number, delivery date, references and lines - only for a reading that names
its bon); a reading that failed is stored anyway (file, text, the failed
check) so « Relire » can fix it once the format is - a mailed bon refused
would be lost for good. The file is `bon-<number kept to [0-9A-Za-z-]>.pdf`
(a captured « 12/34 » or « ../x » is no folder), saved inside the atomic
block with its row and lines, and deleted by the handlers OUTSIDE it when
the block fails: Django has no « on rollback ». It never calls an invoice
importer - a bon read as an invoice files the empties as positive purchase
lines, silently wrong money.

**Which bons count (`comparison.effective_slips`)**, over EVERY bon of the
formats involved, never only the ones on screen: a bon's moment is
(printed_at or received_at, pk), never the mail's date; the same non-blank
number AND delivery date is one ticket re-sent (the driver mails it again,
printed the next morning: the latest content, the earliest moment); a bon
saying « annule et remplace » supersedes every bon sharing a reference with
it, whatever order they arrived in (the original re-sent after its
replacement included); only a format with no reference motif falls back on
the delivery date.

**What is compared, per (supplier, day)**: every reprise of that supplier
that day is ONE side, counts summed (« 2 reprises ce jour-là,
additionnées » - a forgotten type saved as a second reprise is no false
« écart »), against every bon that counts of that supplier's formats delivered
that day. A line's type is NOT stored: the first type, in (position, pk)
order and active or not, one of whose motifs is found in its designation -
editing a motif reclassifies every line at once, and a type whose motif
fails stops the search for that line rather than filing it under the next
one. Status, first that applies: « fournisseur non précisé », « pas de format de
bon », « en attente du bon », « à vérifier » (a paired bon's reading failed or one of
its checks, or a line could not be classified), « écart », « conforme ». An unpaired
bon within 3 days of an unpaired reprise day is offered to the NEAREST one,
with « Mettre la reprise au … ». A bon with no line is « aucun vide repris »
only when its reading passed every check: one whose lines could not be read
(a layout the line motif no longer matches) is « à vérifier » - said empty,
it hid empties that were taken.

**The invoice check (`invoice_check.check_many`)**, read when the page is
drawn and never stored on an invoice: the seller's invoices dated from 7
days before to 45 days after the delivery (one query over the union of the
windows, then their negative lines), a reference searched as a whole token
(fewer than 4 letters or digits is not searched), bons and invoices joined
where a reference is found and each connected group compared ONCE (one
ticket's two delivery notes on two invoices, a monthly invoice refunding several bons),
each negative line going to the longest bon designation it starts with (the
bon prints the first 20 characters). Every state is its own sentence;
negative lines no bon claims are « autres avoirs », never a gap - but never a
✓ either: a bon with no line whose invoice refunds deposits is
`OTHERS_ONLY`, « à vérifier » (the bon corrected to kegs that has not
arrived, a keg returned full), and the green « rien à rembourser ✓ » needs
BOTH sides empty. A unit price is shown for a type only when every counted
line of it has one (a line without price never borrows its neighbour's).
Drawn as « Facture : <short pill> <what the sentence adds> » - the pill's
title holds the whole sentence.

**Photos (`photos.py`)**: re-encoded on the server, the original never kept -
JPEG, PNG and WebP only (HEIC is refused in French: no Pillow plugin), the
size and the pixels checked from the header BEFORE anything is decoded, and
a JPEG's `draft` scale CHOSEN from the pixel budget (`_draft_scale`: the
finest of 1, 2, 4, 8 whose decoded size fits) with the size draft really
gave checked again before `load()` - Pillow picks the scale from the SHORT
side, so a 59 600 × 3 000 panorama decoded whole (half a gigabyte); a
progressive JPEG is held to the pixel limit from its header, since libjpeg
keeps its coefficients full size whatever the scale; upright, at most 2000 px, no
EXIF left (so no GPS), a 480 px thumbnail beside it; the EXIF date is kept as
`taken_at` and shown (« Photo du … à … », else « Envoyée le … »), and a
reprise whose photos were all taken another day says so, with « Dater la
reprise du … ». A photo Pillow cannot read never refuses the reprise: the
others are saved, a warning names it. Prepared in memory before the
transaction, saved inside it, every name already saved deleted if it fails;
deleting a reprise, a photo or a bon deletes its files ON COMMIT
(`models.delete_with_files`). Tests use images of a few pixels and patch the
pixel limits down.

**The pages** (phone-first: 320-375 px, no sideways scroll, targets of 44 px
at least):

- `/consignes/`: the latest reprise in one line, then « Nouvelle reprise »
  (the day, « Repris par » and a note folded under « Reprise du … · repris
  par … · modifier » - on almost every morning nobody touches them - then
  photos, then one count per ACTIVE type, the first one, the kegs, bigger),
  then the latest reprise in full, the reprises and the bons received (20
  each, « tout afficher »), « Ajouter des bons », and the settings' links.
  The strip and the lists reload themselves on `documents-changed` (the
  gather's card sends it when it stops polling); the form is outside them,
  so a count half typed survives.
- **The counts are not a formset**: `nombre-<type pk>`, blank is 0, a whole
  number from 0 to 9 999 in ASCII digits. Every refusal the server makes has
  its twin in the HTML (the date's min/max, a count's pattern and
  maxlength) and the form never has `novalidate`: a browser never gives the
  photos back after a refused post. « Rien à enregistrer » is the only
  refusal the page cannot make first, and it has no photo to lose.
- **`static/js/returnables.js`** (nodes built with textContent only): the
  − / + steppers (drawn `hidden`, `type="button"`, `touch-action:
  manipulation` so fast taps never zoom); the photos, which are
  **`static/js/photos.js`**'s since 01/10 - shared with Achats' « Prendre
  une photo » (« A folder is a background job » and below), and
  loaded BEFORE returnables.js on every page that loads it, both deferred
  (`returnables/tests/test_views.py` checks the order): after each shot the
  filled photo input moves into a hidden box of the form and a fresh one
  takes its place (a camera input holds ONE photo), with previews and
  « Retirer » (a multiple input's files rebuilt through DataTransfer) and
  the 11th refused (`data-max-photos`); the draft of the new reprise in localStorage under the espace (12 h,
  offered back into a blank form - a restored note is named and its folded
  part opened -, « Effacer » puts back today and the offered « Repris par »,
  forgotten on `?enregistree=1`); the stale tab (a tab opened yesterday moves
  its date's max, and its value when it was « today », on `pageshow`).
  **Enter in a count moves to the next count and never sends the form**:
  Chrome on Android sends the numeric keypad's ✓ as an Enter, which saved a
  reprise with the kegs typed and nothing else; the counts say
  `enterkeyhint="next"`, the last one « done ».
- What an upload of bons said is drawn IN « Ajouter des bons » (`#bons`,
  where its redirect lands; `views._messages_by_place`, as Marges does),
  everything else at the top. A saved photo's « Retirer » and every small
  button of the app are 44 px, and « Retirer » asks twice (a `<details>`).
- Under 600 px the topbar scrolls away with the page HERE only
  (`html:has(.returnables-page)`): its three to five rows took a third of a
  phone's screen above the count being typed - one row with « Menu » since
  30/09, still let go for the keypad's sake. `position: relative` since then,
  not static: the menu's veil needs a stacking context. Every page of the app has
  `.returnables-page` on its root, and its fields are 1rem (iOS zooms the page
  in on a smaller one).
- A format's page: « Tester » is the form's FIRST submit button, hidden, so
  Enter tests and never saves; it reads a bon received, pasted text or a PDF
  (read, nothing stored, its text written back into the text box) with the
  motifs as TYPED, line by line with a tag per line - htmx answers in place,
  without JavaScript the page is drawn again. « Enregistrer » does not re-read
  the bons: « Relire les N bons de ce format » does. « Classer comme » on a
  bon's line adds `^` + its first 60 characters, escaped with readable
  spaces, to the type chosen, the whole field checked again first.
- Types: one form per type posting to its own address, no formset. A type
  counted in a reprise, a format with bons, cannot be deleted (PROTECT):
  « désactivez-le plutôt ».
- No `|safe`, no `mark_safe`, no inline script or `style=` in the app (a
  strict CSP is planned; `tests/test_ui.py` greps for them): every sentence
  the pure modules build is plain text.
- **« Récupérer les bons » has no gather of its own**: the page posts to
  `invoices:gather` - one `sources=bons-<pk>` per active format with a
  sender, from the earliest of their starts, `retour=/consignes/`, and NO
  end date (the gather ends on the day it runs: a tab kept open overnight
  posted yesterday) - so every
  rule of the gather holds (the espace's right to the mailbox, one gather
  at a time, a thread bound to the espace), and `trigger_gather` comes back
  through `common.local_return`. The page shows the running gather, or the
  latest that fetched bons for ten minutes after it ended; in an espace
  without the owner's mailbox it says `integrations.SLIPS` instead.

**The gather step** (`invoices/tasks._gather_slips`, `returnables/mail.py`):
after the mailbox's sources, each active format with a sender is searched
(code `bons-<pk>`, « Bons de consignes — <format> »; imported inside the
task, run when `source_codes` is None or names it, never for an empty set),
contained like any source - its failure is its own line, the run stays
« Terminé » - and every attachment returned is stored through the writer,
even when a cancel was asked meanwhile, before the cancel is honoured. Its
counts are bons: they stay out of `invoices_found` / `invoices_created`. A
format's start (`mail.fetch_start`) is 3 days before its newest MAILED bon
(a hand upload never moves it), else 90 days back, never more than 400. Only
a gather of bons ALONE widens the job's range to it: beside invoices the
bons' start is a log line (« recherche depuis le … ») and the range stays
the invoices' - widened, a failed Achats gather offered 90 days again to
every source, Metro included. A mail's other attachment is « pas un bon … —
ignoré », a duplicate a log line, the latest reprise's comparison the
source's NOTE - never an error, which counts as a failed source. A gather of
bons only (`ScrapeJob.slips_only`) titles its columns « Bons trouvés /
Nouveaux », and Achats never offers its period again (`workspace
._invoice_gather`). Achats' « Récupérer » card offers each mail format as a
source of its own, ticked, only where the mailbox may be used.

**Achats' guard** (`receipts.route_to_returnables`, in `import_document` after
the duplicate check and the e-invoice branch, before `document_text`): a PDF
dropped among the invoices whose text exactly ONE active format's start
motif recognises is stored as a bon (« Déposé à la main », the text read
once and handed to `store_slip(text=…)`) and the import says « Bon de
consignes : rangé dans Consignes (bon n° X) — ce n'est pas une facture. »
(or « déjà reçu dans Consignes ») through `importing.RoutedToReturnablesError`,
a `DuplicateInvoiceError`: no Invoice. Several formats recognising it is
refused the same way, naming them - filed as a purchase, it would be
silently wrong money. Anything else (no format, a PDF over 5 MB or 5 pages,
no text) is imported as before. The « Analyse IA » upload goes through it
too, before `parse_and_import`. A folder import draws a routed bon « Rangé
dans Consignes », and one several formats recognise « Erreur » (stored
nowhere, said in the batch's log); naming the shop of a kept file that turns
out to be a bon does the same. Without the guard, UBA's bon was recognised by its printed
phone number and read as a ticket - the empties filed as purchases. The
cost: while a format has a start motif (the seeded one does), every PDF
imported on Achats is read once more by pdfplumber. Not guarded: a mailbox
source with a reader of its own (`parse_and_import`) and Metro, which never
go through `import_document` - the seeded UBA invoice source does not match
a bon's sender or subject (pinned by a test).

**« Données »** (`transfer/sections/returnables.py`, the tenth section, after
« fournisseurs »): types and formats by their name as their forms compare
it, a reprise by its random `reference`, a bon by its sha256; a bon's
reading is copied, never compared nor read again at import; every motif
imported passes the guard or its record is skipped (« motif refusé : … »),
and so does every date (a reprise's and a delivery date within 01/01/2000 -
today + 7, a mail's within today + 1, else « date hors limites »: a
9999-12-31 read from an archive made every page a 500 once `timedelta` met
it - the date arithmetic is also clamped, `comparison.shifted`); photos and
PDFs under `consignes/`, deleted on commit. The details are under
« Export, import and clear ».

Tests: `returnables/tests/` - the pure modules, the models and seeds, the
views read off the rendered page (`staff/tests/page_forms.py`, the photos
and PDFs added as SimpleUploadedFile), two espaces (`test_tenants.py`), and
a 375 × 667 phone in Chrome (`test_phone_browser.py`, logged in with
`tests.runner.log_in_the_browser`). Every value in them is invented: the
owner's real bons carry his account, his driver and his deliveries.

### UI conventions

**Short texts** (the owner, 01/10/2026: « je trouve les textes du site trop
verbose en général »; every template and user-facing message was shortened
that day). A page subtitle is one sentence; a help text is one sentence or
nothing when the label says it; a warning says what is wrong and what to do,
and stops. Justifications, history and « why the page works this way »
belong in these notes, not on the screen - so many passages here that quote
a longer on-screen sentence describe what the page used to say; the rule
behind it still holds, the words are shorter. A test pins the fact a page
must show, not a paragraph around it.

`static/css/marginmate.css` holds the design tokens - colours, a 4px spacing
scale (`--s1`..`--s6`), radii. Use the tokens, not literals, and prefer an
existing class to an inline `style=`:

| Want | Use |
|---|---|
| Buttons beside a page title | `<div class="actions">` inside `.page-header` |
| A line saying what a page is for | `<p class="page-subtitle">` |
| Headline figures | `.stat-row` > `.stat` > `.stat-label` / `.stat-value` / `.stat-note` |
| A table | `<div class="table-wrap"><table data-table data-table-label="factures">` |
| A row of controls narrowing a list, that is **not** a period | `<form class="filter-row">` — `.date-range` means « du … au … » everywhere, and two forms answering to one name is how a test finds the wrong one |

**Every table gets search and sorting for free** via `static/js/datatable.js`
— add `data-table` and it grows a search box, a live "12 / 261" count and a
sort button in every header. Three things to remember:

- `data-sort="2026-03-31"` on a `<td>` when the displayed text doesn't sort
  correctly. A date shown as `31/03/2026` sorts as *text* without it, so
  every March lands together regardless of year.
- `data-child-row` on a row that explains the row above it (an expanded
  panel, a "valorisé en X" note). Without it, sorting separates the two.
- **A value living in an `<input>` is invisible to both.** `searchableText`
  reads `textContent`, so a cell that is only a text field is the empty
  string: on « Dépenses » a line already named « Loyer » could not be found
  by typing « Loyer » while one a rule named could (that one prints its name
  as text beside the field), and a click on that header sorted every row on
  the empty string, destroying the page's own ordering with no way back but
  a reload. Print the stored value as text beside the field **and** give the
  cell a `data-sort`.
- `data-table-sort-only` when the page already has its own search — the
  stock list's is server-backed and fuzzy, and a second box filtering the
  same rows by a different rule is worse than none.

`tests/test_ui.py` checks these hold, because all three fail *silently*: the
page still renders, it just quietly stops working the way every other page
does.

**A chart is a server-rendered inline SVG**, never a library: four of them
now (`recipes/views.py::_build_ingredient_pie_svg`,
`inventory/views.py::_build_price_history_svg`,
`bank/views.py::_build_spending_pie_svg` and `_build_balance_svg`), all hovered by
`static/js/charts.js`, which is the only thing Chart.js would have added. The
palette is `common.PIE_COLORS`, **one list**: two pies in one app drawn from
two lists that drifted apart read as two different legends. Every name that
reaches an SVG is escaped where it is built - the result is rendered with
`|safe`, and those names come from invoice text and from whatever somebody
typed. A pie carries **its figures in a table beside it** (`.chart-and-figures`):
one nobody can read a number off is decoration. And nothing is drawn from a
total that is not positive - a share of nothing is not 0 %. A SIGNED line
draws its zero, and zero is always on its axis. **`charts.js` builds its
tooltip from nodes and text only**: the server escapes `data-label`, but
`getAttribute` hands it back decoded, and through `innerHTML` a category
typed as markup ran when its wedge was hovered. `tests/test_ui.py` greps
charts.js for HTML-writing APIs, and `PieTooltipInBrowserTests` hovers such a
name in Chrome.

**Data for a page's script travels in `{{ value|json_script:"id" }}`**, the
view handing the template the dict itself - never a `json.dumps` string
printed `|safe`, never an island written by hand. `json.dumps` leaves « < »
as it is: the inventory form printed every product and article name that
way, and a name holding « </script> » - a line of a supplier's PDF or
e-invoice - closed the island and ran as the page's own markup in the bar's
session (security audit XSS-1, 29/09). `json_script` writes « < » as a
\u escape that `JSON.parse` reads back unchanged. `tests/test_json_islands.py`
plants such a name in every island and sweeps the templates and the views
for the pattern.

**The topbar is sticky, so the page leaves its height above what it scrolls
to** (`html { scroll-padding-top: var(--topbar-room) }` in marginmate.css:
6rem, 10rem under 860 px where the brand sits above the links, 11rem under
440 px, 13rem under 310 px - those three for the bar WITHOUT topbar.js; with
it the bar is one row under 860 px and its room 6rem, below). A fragment - Achats' `#a-voir`, the supplier page's
`#historique`, the stock list's `#a-classer` - and the tab row htmx's boost
brings to the top when an Achats tab is clicked lower down all landed under
it: « 1 changement à voir » and the supplier's name hidden (UX review,
19/09). The room was measured in Chrome width by width (57 px on one row, 82
on two, 141 on three, 170 on four, 199 on five; the CSS comments say where
each starts). A new link in the navigation can make it wrap sooner:
`invoices/tests/test_changes_to_see.py::TopbarRoomInBrowserTests` checks
nine widths. « Personnel », the eighth link, took a folding phone's 280 px
cover screen to five rows, past what 11rem leaves: the test gained 280 px
(and failed there first) and the CSS its 13rem. « Consignes », the ninth
(29/09), was measured again width by width in multi mode
(`accounts/tests/test_topbar_browser.py`, 17 widths, three-digit badges, a
long espace name, the superuser's « Admin » too): two rows above 1000 px
(82 px, under 6rem's 96), three between 861 and 960 or so (124 px, under
8rem), then 112 px from 860 px down, 141 from 465, 170 from 334 and 199 from
300 - every width inside the room already there, so no rem changed. On /consignes/ itself
the bar scrolls away under 600 px (`html:has(.returnables-page)`, room 0). A
tenth link is measured again.

**The badges are part of the measurement**, and that test's fixture carries
them for exactly that reason. Adding « Marges » (20/09) took the links from
three rows to four between **441 and 465 px** - 141 px against the 136 px
that 8.5rem gave, so an anchored heading landed five pixels under the bar -
and every width of that test passed all the same, because its database had
nothing waiting anywhere and so no badge to widen a link. With a product to
classify, a ticket to check and a till product to link, 450 px fails, and
`--topbar-room` is 10rem under 860 px.

**Under 860 px the links fold into « Menu »** (30/09, the owner: « the top
menu is too big, maybe do something that can be expanded »;
`static/js/topbar.js`, marginmate.css « topbar menu »).
- The bar is one row there, from 860 down to 280 px: the brand, the page's
  section (`navigation.SECTION_LABELS`, checked against the lit link) and
  « Menu », with a red dot while any link carries a badge - a `:has()` on the
  badges themselves, so every writer keeps it right (base.html,
  `_review_panel_refresh.html`'s out-of-band `#nav-count-products`, ui.js's
  `to-link-count`, which builds its badge as nodes). Its room is 6rem.
- The menu holds the links, the espace's name and « Se déconnecter », each
  44 px tall. It drops OVER the page under a veil (the bar's `::after`): a
  tap on the veil only closes it - through a listener put ON the header
  while the menu is open (iOS sends no click to a document listener from an
  element nothing listens on, and Chrome's touch adjustment would move the
  tap onto « Se déconnecter », the menu's last item) - and a finger
  dragged on the veil scrolls nothing: the same header listener stops the
  `touchmove` (Chrome does not honour `touch-action` on a pseudo-element).
  Escape, the focus leaving the bar and widening past 860 px close it too.
  The state is the button's `aria-expanded`, which the stylesheet reads.
- **topbar.js is the one script of `<head>` without `defer`.** The class it
  puts on `<html>` (`topbar-menu-ready`) is what folds the links: without it
  (JavaScript off, the file missing) the bar is the old one, in the rooms
  measured above. Deferred, a phone painted three rows and jumped; in the
  body, htmx's history restore would run it again. It sets itself up once.
- `accounts/tests/test_topbar_browser.py` measures both bars (the old one with
  the class taken off). A new link still counts: it changes how many rows the
  bar without the script wraps to, and it adds 44 px to the open menu, which
  on a small phone already fills most of the screen.

**On a phone a table is read as cards** (`table.phone-cards`, marginmate.css
« phone cards »; 30/09 - « columns overlapping and hard to read », the owner:
squeezed by `table-layout: fixed`, the stock list's seven columns printed over
one another). Under 860 px each row is a small grid: its first cell across as
the title (after its bulk box, `.select-col`, when it has one), every figure
under its label - `data-label` on the `<td>`, **the column's header word for
word**, drawn by CSS so the cell's own text (what datatable.js sorts and
searches by) is untouched - `.row-actions` / `.phone-card-wide` across,
`.phone-card-end` at the end of the title's line, an empty cell gone. The
header row is not drawn (no sort on a phone; a sort chosen in the session
still orders the cards). A `data-child-row` that is not itself a card (an
opened panel) runs across under its card. Used by the four tables of
Produits & charges, Achats' documents and « Documents à corriger », an
import's files and the recipes. Not a sideways scroll with the name pinned:
that put a purchases table wider than the phone inside a box scrolling
sideways. `inventory/tests/test_products_phone.py` compares every label with
its header.

**Nothing is wider than a phone** (UX review, 30/09: a ticket's check, a
supplier's page, a recipe's and the stock list were wider than 375 px, and
Chrome then widens the whole page - it zooms out sideways and the sticky bar
drifts out of view). Every `<table>` sits in a box that scrolls sideways
(`.table-wrap`, or `.table-scroll` - no frame, nothing drawn where the table
fits) or is read as cards; `tests/test_ui.py` reads every template for a bare
one. A one-column grid is `minmax(0, 1fr)`, never a bare `1fr`
(`minmax(auto, 1fr)` grew to its widest table). The « phone widths » section
lets words, fields and buttons give way under 860 px. Checked in Chrome at
320, 375 and 430 px by `tests/test_phone_width_browser.py`.

**A box that scrolls on its own is a wall on a phone** (30/09). Where a
layout stacks, the scroll box goes with the side-by-side it was made for:
`overflow: visible` and `overscroll-behavior: auto`, never only `max-height:
none` (« À classer », Three workspaces). Nor does a sticky box stay sticky
once it sits above what it was beside (the receipt's photo under 900 px). What
still scrolls inside a page on a phone is small (a job's log, the OCR text,
a pick list) or sideways only (a table). A height taken from the viewport is
written `vh` then `svh` (the screen with the address bar shown).

**A touch screen is `(pointer: coarse)`, not a width** (the « touch » section
at the end of marginmate.css, 30/09): a phone held sideways is wider than 860
px, and a narrow desktop window needs none of it. There: fields at 16 px (iOS
zooms the page in on a smaller one; .returnables-page keeps its own rule at
every width), controls 44 px tall in `<main>` - 36 in a table's row, a chip
or a segmented choice, with 8 px between two row actions -, checkboxes 20 px,
and a phone held sideways keeps no inner table scroll box. Scoped to
`<main>`: the topbar is measured on its own. Browser tests emulate touch
(`Emulation.setTouchEmulationEnabled`, as returnables' phone test does):
without it `(pointer: coarse)` does not match and none of this is seen
(`tests/test_touch_browser.py`). **Headless Chrome's default window is 800 ×
600**, under 860 px: a browser test that sets no viewport sees the phone
layout - a test about the desktop pins its window.

**Dates are always `|date:"d/m/Y"`.** `LANGUAGE_CODE` is `en-us`, so an
unformatted date renders "March 31, 2026" in an otherwise French interface.

**Amounts are grouped by thousands** (the owner, 01/10/2026: « 10000€ ->
10 000€ »): every euro amount a person reads - a page, a message, a check's
detail, a job log, a chart's label or tooltip, a PDF, a command's report -
has its whole part in groups of three, a no-break space between them
(`common.THOUSANDS_SEPARATOR`). A template prints `{{ x|money }} €` (the
`assets` library: `floatformat` with the same argument and rounding, then
grouped - `money:4` for four places), Python `common.format_money(x)` (a
`format` spec, `"+.2f"` for a signed gap) or `group_thousands` on a figure
already formatted. Only the grouping is shared: the pages keep their point
(« 1 408.18 € », en-us), the sentences that wrote a comma keep it. **Never
on a value read back**: an input's value, a `data-*` a script computes or
sorts with (a chart point's `data-value` is its tooltip, and is grouped),
an export, the « Données » archive, a file name (« Darty 11€55 … »), a
parser. datatable.js sorts a grouped amount as the number it is (`\s`
matches the no-break space). A script writing a sentence the server also
writes (`document_review.html`'s live checks) groups the same way, the
space written `String.fromCharCode(0xa0)`.

### Django's `{# … #}` comment is SINGLE-LINE ONLY

A multi-line one prints itself onto the page and executes any tag inside it.
It still returns 200, so only looking at the output catches it. Use
`{% comment %}…{% endcomment %}`;
`tests/test_views_smoke.py::assertNoUnrenderedTemplateSyntax` guards it.

### An invisible character is written as a named escape

A no-break space, a narrow no-break space, a byte order mark: write a new
one as `"\N{NARROW NO-BREAK SPACE}"`, never as a backslash-u escape nor as
the character itself. The file-writing tools and bash heredocs have turned
backslash-u escapes into the real character, which nobody reviewing the
code can see; several readers already hold their separators that way and
work, invisibly. Check a new file for invisible
characters before trusting it.

### `has_changed()` answers two different questions

Formsets use it both for "may I skip validating this blank row?" (extra rows
only) and for "should I write this saved row back?"
(`save_existing_objects`). Suppressing a field from the first silently
discards real edits in the second — that's how regrouping an ingredient via
"OU" stopped saving. `BlankRowTolerantFormMixin` gates on `empty_permitted`
for exactly this reason.

### Money is always `Decimal`

Never a float, anywhere in the invoice → cost → margin path. SQLite's own
arithmetic isn't exact decimal either — see the comments on
`StockType.current_value_ht` for why sums are done in Python.

### Test data

`tests/factories.py` — plain functions, no factory_boy. Note that
`invoices/migrations/0002_seed_suppliers` seeds METRO and UBA into every
database, the test one included.

## Known data issues (not code bugs)

136 invoice lines (€3,732.66) are attached to a product with a different
name, from the fuzzy matcher's old behaviour — e.g. `GENEPI 40D 50CL`
absorbed into `GENEPI 40D 70CL`. The matcher no longer does this, but the
existing links were not rewritten: splitting them changes historical stock
values and needs a human decision per pair.

## Privacy

Real invoice PDFs stay out of git (IBANs, addresses, prices), by explicit
choice. `.env` is never committed; `db.sqlite3*` is gitignored. So is
`private/` (single mode's signatures' keys and files). The espaces - every
bar's database, files and keys - live outside the code folder (TENANTS_ROOT
and the accounts database; `../data/` for the owner). An employee's name, typical
week and leave are personal data: « Personnel »'s tests invent all three.
