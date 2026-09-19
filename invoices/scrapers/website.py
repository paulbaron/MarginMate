"""A supplier's customer portal - the rent's, the water's, the phone's -
read with no code of its own: its settings are data (models.
WebsiteInvoiceSource, edited under Achats → Sources), and this reads them.

What it does, as a person would: open the login page, turn down the cookie
banner, find the login form (the visible password field, the text field in
front of it, the button that submits them), sign in with the credentials
the .env names, follow the link to the invoices, and download every invoice
of the period - page after page - one click at a time.

**The deciding is plain Python, the clicking is Selenium.** Which dates a
row covers (`periods`), whether a link downloads an invoice
(`looks_like_an_invoice`), whether it is one already imported
(`known_number_in`) are pure functions over what the page shows, so they
are tested without a browser; the browser part only reads the page (one
script returning every link with the text of its row) and clicks.

**A person is asked, never impersonated.** A site that wants a code sent by
SMS or a captcha gets its window shown (WebsiteInvoiceSource.show_browser)
and waits for the person at the keyboard; headless, the run stops and says
so. A site that refuses automated browsers outright ("The requested URL was
rejected") is reported as such - its invoices come by hand, or by email.

**Gentle, like the Metro scraper**: one login per run, one click every
CLICK_INTERVAL_SECONDS, one download at a time, and a row the period does
not cover, or whose invoice number is already imported, is not clicked.
"""

from __future__ import annotations

import calendar
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

PAGE_WAIT_SECONDS = 20  # a page, or the list of invoices on it, appearing
LOGIN_WAIT_SECONDS = 25  # the site answering a sign-in
PERSON_WAIT_SECONDS = 300  # a code sent by SMS, a captcha: typed by a person
# A login page's words alone (no form yet) count as a check only after this:
# a form drawn a moment after its page was taken for one.
WORDS_GRACE_SECONDS = 4
# Signed in - the password field gone - a page may still draw a code prompt.
SETTLE_SECONDS = 3
DOWNLOAD_TIMEOUT_SECONDS = 45  # a download under way, to finish
# A click that has started nothing by then - no file, no download under way,
# no tab - started nothing: 45 seconds went by for every invoice of a list.
DOWNLOAD_START_SECONDS = 8
CLICK_INTERVAL_SECONDS = 2.0  # never faster than a person clicking down a list
POLL_SECONDS = 0.5
MAX_PAGES = 20
MAX_DOWNLOADS = 200
DEBUG_DIR = "_debug"


class WebsiteError(RuntimeError):
    """For the operator: what went wrong on the site, in words."""


class NeedsAPerson(WebsiteError):
    """The site asks for something only a person can give (a code, a captcha)."""


class RefusedByTheSite(WebsiteError):
    """The site turns automated browsers away."""


@dataclass
class WebsiteRecipe:
    """A WebsiteInvoiceSource, as plain values: what the scraper reads."""

    name: str
    login_url: str
    username_env: str
    password_env: str
    invoices_url: str = ""
    navigation: list[str] = field(default_factory=list)
    username_selector: str = ""
    password_selector: str = ""
    submit_selector: str = ""
    link_selector: str = ""
    next_selector: str = ""
    show_browser: bool = False

    @classmethod
    def from_source(cls, source, name: str = "") -> "WebsiteRecipe":
        return cls(
            name=name or str(source.invoice_type),
            login_url=source.login_url,
            username_env=source.username_env,
            password_env=source.password_env,
            invoices_url=source.invoices_url,
            navigation=[line.strip() for line in (source.navigation or "").splitlines() if line.strip()],
            username_selector=source.username_selector,
            password_selector=source.password_selector,
            submit_selector=source.submit_selector,
            link_selector=source.link_selector,
            next_selector=source.next_selector,
            show_browser=source.show_browser,
        )


# --------------------------------------------------------------- credentials


def credentials(recipe: WebsiteRecipe, env_file=None, environ=None) -> tuple[str, str]:
    """The login and password the recipe's .env variables hold - read from
    the .env file itself at each run, so a line added there counts without
    restarting the server, then from the environment."""
    values = {}
    if env_file is not None and os.path.exists(env_file):
        from dotenv import dotenv_values

        values = {key: value for key, value in dotenv_values(env_file).items() if value}
    environ = os.environ if environ is None else environ
    login = (values.get(recipe.username_env) or environ.get(recipe.username_env) or "").strip()
    password = values.get(recipe.password_env) or environ.get(recipe.password_env) or ""
    missing = [name for name, value in ((recipe.username_env, login), (recipe.password_env, password)) if not value]
    if missing:
        raise WebsiteError(
            f"{' et '.join(missing)} {'est absente' if len(missing) == 1 else 'sont absentes'} du fichier .env : "
            f"ajoutez {'la ligne' if len(missing) == 1 else 'les lignes'} NOM=valeur, puis relancez."
        )
    return login, password


# ------------------------------------------------------------------- dates


MONTHS = {
    "janvier": 1, "janv": 1, "jan": 1, "january": 1,
    "fevrier": 2, "fevr": 2, "fev": 2, "february": 2, "feb": 2,
    "mars": 3, "march": 3, "mar": 3,
    "avril": 4, "avr": 4, "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juin": 6, "june": 6, "jun": 6,
    "juillet": 7, "juil": 7, "july": 7, "jul": 7,
    "aout": 8, "august": 8, "aug": 8,
    "septembre": 9, "sept": 9, "sep": 9, "september": 9,
    "octobre": 10, "oct": 10, "october": 10,
    "novembre": 11, "nov": 11, "november": 11,
    "decembre": 12, "dec": 12, "december": 12,
}
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
DAY_MONTH_YEAR_RE = re.compile(r"(?<!\d)(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})(?!\d)")
YEAR_MONTH_DAY_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
DAY_NAMED_MONTH_RE = re.compile(rf"(?<!\d)(\d{{1,2}})(?:er)?\s+({_MONTH_NAMES})\.?\s+(\d{{4}})(?!\d)")
NAMED_MONTH_YEAR_RE = re.compile(rf"(?<![a-z])({_MONTH_NAMES})\.?\s+(\d{{4}})(?!\d)")
MONTH_YEAR_RE = re.compile(r"(?<![\d/.-])(\d{1,2})[/.-](\d{4})(?!\d)")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def _day(year: int, month: int, day: int) -> date | None:
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month(year: int, month: int) -> tuple[date, date] | None:
    if not 1 <= month <= 12 or not 2000 <= year <= 2100:
        return None
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def periods(text: str) -> list[tuple[date, date]]:
    """What a row of an invoice list says about when: each date it prints
    as that day, each month it names ("Facture de mars 2026", "03/2026") as
    that month. A day's figures are not read again as a month."""
    folded = _fold(text)
    found: list[tuple[date, date]] = []
    taken: list[tuple[int, int]] = []

    def free(match) -> bool:
        return not any(match.start() < end and start < match.end() for start, end in taken)

    for pattern, build in (
        (YEAR_MONTH_DAY_RE, lambda m: _day(int(m[1]), int(m[2]), int(m[3]))),
        (DAY_MONTH_YEAR_RE, lambda m: _day(int(m[3]), int(m[2]), int(m[1]))),
        (DAY_NAMED_MONTH_RE, lambda m: _day(int(m[3]), MONTHS[m[2]], int(m[1]))),
    ):
        for match in pattern.finditer(folded):
            if not free(match):
                continue
            day = build(match)
            if day is not None and 2000 <= day.year <= 2100:
                found.append((day, day))
                taken.append(match.span())
    for pattern, build in (
        (NAMED_MONTH_YEAR_RE, lambda m: _month(int(m[2]), MONTHS[m[1]])),
        (MONTH_YEAR_RE, lambda m: _month(int(m[2]), int(m[1]))),
    ):
        for match in pattern.finditer(folded):
            if not free(match):
                continue
            month = build(match)
            if month is not None:
                found.append(month)
                taken.append(match.span())
    return found


def in_window(row: str, start: date, end: date) -> bool | None:
    """Whether a row belongs to the period: True when any of its dates or
    months meets it, False when it prints some and none does, None when it
    prints none - downloaded all the same, since nothing says it is out."""
    spans = periods(row)
    if not spans:
        return None
    return any(first <= end and last >= start for first, last in spans)


# ----------------------------------------------------------------- links


PDF_HREF_RE = re.compile(r"\.pdf(?:$|[?#&])|[/?&=_.-](?:pdf|download|telecharg\w*)(?:$|[/?&=#._-])", re.I)
DOWNLOAD_WORDS_RE = re.compile(r"t[ée]l[ée]charg|\bpdf\b|download", re.I)
# A link opening one invoice (Free Mobile's « Voir ma facture »): one invoice,
# singular - "Voir mes factures" is the way to the list.
OPEN_WORDS_RE = re.compile(
    r"\b(?:voir|afficher|consulter|ouvrir)\s+(?:ma|la|votre|cette)\s+(?:facture|quittance|avis d.[ée]ch[ée]ance)(?![a-z])",
    re.I,
)
INVOICES_LINK_RE = re.compile(r"factur", re.I)
AMOUNT_RE = re.compile(r"\d[\d\s.]*[,.]\d{2}\s*(?:€|eur)|€\s*\d", re.I)


@dataclass
class Candidate:
    """A link or button on the page, with the text of the row it sits in,
    and the indexes of the links or buttons it holds (a menu clickable as a
    whole holds its entries)."""

    index: int
    text: str = ""
    href: str = ""
    row: str = ""
    download: bool = False
    label: str = ""
    holds: tuple = ()


def looks_like_an_invoice(candidate: Candidate) -> bool:
    """A link that downloads an invoice: it leads to a PDF, says
    "Télécharger" or "PDF" (in words, or by its icon: READ_LINKS_JS puts an
    icon's name in `label`), opens one ("Voir ma facture"), or carries the
    download attribute - and it sits in a row that prints a date or an
    amount. The footer's terms and conditions are a PDF too, and print
    neither."""
    if candidate.href.lower().startswith(("mailto:", "tel:", "javascript:void")):
        return False
    words = f"{candidate.text} {candidate.label}"
    says = bool(DOWNLOAD_WORDS_RE.search(words) or OPEN_WORDS_RE.search(words))
    leads = bool(PDF_HREF_RE.search(candidate.href)) or candidate.download
    return (says or leads) and (bool(periods(candidate.row)) or bool(AMOUNT_RE.search(candidate.row)))


def leads_to_invoices(candidate: Candidate) -> bool:
    """The link to follow to the invoices when none is named: it speaks of
    invoices, and is not one. A home page listing the latest invoices, each
    a link named after its month (as Freebox's does), had one clicked as if
    it were the menu - opening a PDF instead of the list."""
    words = f"{candidate.text} {candidate.label}"
    return (
        bool(INVOICES_LINK_RE.search(words))
        and not DOWNLOAD_WORDS_RE.search(words)
        and not OPEN_WORDS_RE.search(words)
        and not looks_like_an_invoice(candidate)
    )


def link_to_follow(candidates: list[Candidate], target: str | None = None) -> Candidate | None:
    """The link to click on the way to the invoices: the first one speaking
    of invoices (`leads_to_invoices`), or naming `target` - and of those, one
    not holding another that does. A side menu drawn as one clickable block
    around its entries (Eau de Paris') comes first in the page and says
    every entry's text at once; clicked, it only closed itself, and the
    invoices were never reached. A card around the invoices' link holding a
    "Voir" button is still the way. Nesting is read from the page, never
    from the words: two links side by side are both links."""
    if target is None:
        matches = [c for c in candidates if leads_to_invoices(c)]
    else:
        matches = [c for c in candidates if _fold(target) in _fold(f"{c.text} {c.label}")]
    matching = {c.index for c in matches}
    innermost = [c for c in matches if not matching.intersection(c.holds)]
    return (innermost or matches or [None])[0]


def names_a_file(href: str) -> bool:
    """An address naming a file - not one only pointing back into the page
    ("#", buttons drawn by a script), not a script."""
    return href.startswith("http") and "#" not in href


def invoice_key(candidate: Candidate):
    """What one invoice link is, across the pages of a run: its file, or its
    words where it names none."""
    return candidate.href if names_a_file(candidate.href) else (candidate.text, candidate.row)


def known_number_in(row: str, known_numbers) -> str | None:
    """The number of an invoice already imported, when the row prints one:
    that invoice is not downloaded again. Short numbers name nothing (a day,
    a quantity)."""
    tokens = set(re.findall(r"[A-Za-z0-9][A-Za-z0-9/_-]*[A-Za-z0-9]", row))
    for number in known_numbers:
        if len(number) >= 5 and number in tokens:
            return number
    return None


def choose(candidates, start: date, end: date, known_numbers=(), selector_given: bool = False):
    """(candidate, decision) for every link worth saying something about:
    "à télécharger", "hors période", "déjà importée <n°>". A given selector
    names the links itself; otherwise they are recognised
    (looks_like_an_invoice). Two links to one file are one - an address
    only pointing back into the page ("#", as buttons drawn by a script
    have) names no file: taken for one, it left every invoice but the
    first."""
    invoices = [c for c in candidates if selector_given or looks_like_an_invoice(c)]
    # One link a row: a card offering « Voir ma facture » and a download
    # button had its invoice fetched twice. The one that downloads first.
    best: dict[str, Candidate] = {}
    for candidate in invoices:
        if candidate.row and (candidate.row not in best or _strength(candidate) > _strength(best[candidate.row])):
            best[candidate.row] = candidate
    seen = set()
    decided = []
    for candidate in invoices:
        if candidate.row and best[candidate.row] is not candidate:
            continue
        key = invoice_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        known = known_number_in(candidate.row, known_numbers)
        if known:
            decided.append((candidate, f"déjà importée ({known})"))
        elif in_window(candidate.row, start, end) is False:
            decided.append((candidate, "hors période"))
        else:
            decided.append((candidate, "à télécharger"))
    return decided


def _strength(candidate: Candidate) -> int:
    """How surely a link fetches the file: it leads to one, it says it
    downloads, it opens the invoice (which may be a page to read)."""
    if candidate.download or PDF_HREF_RE.search(candidate.href):
        return 2
    if DOWNLOAD_WORDS_RE.search(f"{candidate.text} {candidate.label}"):
        return 1
    return 0


# ---------------------------------------------------------- page reading

REFUSED_RE = re.compile(
    r"requested url was rejected|access denied|acc[eè]s refus[ée]|request blocked|you have been blocked", re.I
)
VERIFY_RE = re.compile(
    r"code (?:de )?(?:v[ée]rification|s[ée]curit[ée]|confirmation)|code re[çc]u|code envoy[ée]"
    r"|(?:saisissez|entrez|saisir|entrer|renseignez) (?:le|votre|ce) code|un code (?:vous )?a [ée]t[ée] envoy[ée]"
    # Not the legal notice every page protected by reCAPTCHA prints.
    r"|(?<!prot.g. par re)(?<!protected by re)captcha"
    r"|je ne suis pas un robot|not a robot|authentification (?:forte|[àa] deux)"
    # A slider to drag, a page making sure one is not a robot (TotalEnergies').
    # Not any "faites glisser": an upload zone says it too ("vos fichiers ici").
    r"|(?:non|pas) (?:pas )?(?:[àa] )?un robot|(?:faites glisser|glissez) (?:le curseur|vers la droite)"
    r"|slide to (?:verify|continue)"
    r"|verify (?:that )?you are (?:a )?human",
    re.I,
)
NEXT_RE = re.compile(r"^(?:suivant(?:e)?|page suivante|voir plus|afficher plus|charger plus|plus de factures|next|›|»|>)$", re.I)
REFUSE_COOKIES_RE = re.compile(
    r"tout refuser|je refuse|refuser|interdire|continuer sans accepter|rejeter|refuse all|reject all|decline", re.I
)

# Every visible link or button, marked so it can be clicked by index, with
# the text of the row it sits in (its table row, list item or card).
READ_LINKS_JS = r"""
const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
const selector = arguments[0] || 'a, button, [role=button], [role=link], input[type=button], input[type=submit]';
const clean = text => (text || '').replace(/\s+/g, ' ').trim();
const MAX_ROW = 400;
// What an icon says, by its name: a button holding only
// <i class="icon icon-download-2-line"> (Free Mobile's) says "download".
const iconSays = el => ([el, ...el.querySelectorAll('i, svg, span, use, img')]
  .map(icon => [icon.getAttribute('class'), icon.getAttribute('data-icon'), icon.getAttribute('href'),
                icon.getAttribute('xlink:href'), icon.getAttribute('alt')].join(' '))
  .join(' ').match(/[\w-]*(?:download|t[ée]l[ée]charg|pdf)[\w-]*/gi) || []).join(' ');
const downloadish = other => /t[ée]l[ée]charg|pdf|download/i.test(
  [other.innerText, other.getAttribute('href'), other.getAttribute('aria-label'), other.getAttribute('title'),
   iconSays(other)].join(' '));
// The marks of an earlier read go first: a page drawn in place (no reload)
// kept them on its hidden menu, whose links then came before the new ones
// bearing the same numbers - and were clicked instead.
document.querySelectorAll('[data-mm-link]').forEach(el => el.removeAttribute('data-mm-link'));
const out = [];
const shown = Array.from(document.querySelectorAll(selector)).filter(visible);
let index = 0;
for (const el of shown) {
  // The links a block clickable as a whole holds - a menu's entries: it is
  // not one of them (link_to_follow).
  const holds = el.querySelector(selector)
    ? shown.map((other, at) => (other !== el && el.contains(other)) ? at : -1).filter(at => at >= 0) : [];
  let row = el.closest('tr, li, [role=row], article');
  if (!row) {
    row = el.parentElement;
    // Up to the block printing more than the link - and, for a button
    // with little or no text of its own, a figure (its month, its
    // amount): the block holding only its neighbour « Voir ma facture »
    // printed no date, and the invoice was never recognised.
    while (row && row.parentElement && (clean(row.innerText).length < clean(el.innerText).length + 12
        || (!/\d/.test(row.innerText) && clean(row.parentElement.innerText).length <= MAX_ROW))) row = row.parentElement;
    // Climbed past a row into what holds the rows - a footer's link reaches
    // the whole page, invoices and all: the link sits in no row, and the
    // dates around it are other invoices'. A block holding table rows, or
    // download links to other files, or far more text than a row, is that.
    if (row && (clean(row.innerText).length > MAX_ROW || row.querySelector('tr, li, [role=row], article')
        || Array.from(row.querySelectorAll('a, button')).some(other =>
             other !== el && visible(other) && downloadish(other) && (other.href || '') !== (el.href || '')))) row = null;
  }
  el.setAttribute('data-mm-link', String(index));
  out.push({
    index: index,
    text: clean(el.innerText || el.value).slice(0, 160),
    href: el.href || el.getAttribute('href') || '',
    row: clean(row ? row.innerText : '').slice(0, 500),
    download: el.hasAttribute('download'),
    label: clean([el.getAttribute('aria-label'), el.getAttribute('title'), iconSays(el)].filter(Boolean).join(' ')).slice(0, 160),
    holds: holds,
  });
  index++;
}
return out;
"""

# The login form: the visible password field, the text field in front of
# it, the button that submits them - or what the given selectors name.
FIND_LOGIN_JS = r"""
const [userSel, passSel, submitSel] = arguments;
const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
const pick = sel => { if (!sel) return null; const el = document.querySelector(sel); return visible(el) ? el : null; };
const password = pick(passSel) || Array.from(document.querySelectorAll('input[type=password]')).find(visible) || null;
const scope = (password && (password.form || password.closest('form'))) || document;
const says = el => [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.getAttribute('autocomplete'),
  ...Array.from(el.labels || []).map(label => label.innerText)].join(' ');
const textual = Array.from(scope.querySelectorAll('input')).filter(el =>
  visible(el) && !el.readOnly && !el.disabled && !el.closest('[role=search]') && !/recherch|search/i.test(says(el)) &&
  ['', 'text', 'email', 'tel', 'number'].includes((el.getAttribute('type') || '').toLowerCase()));
// With no password beside it, a text field is the identifier only if it says
// so: on a check page, the answer box or the site's search box got the login.
const identifier = el => ['email', 'tel'].includes((el.getAttribute('type') || '').toLowerCase()) ||
  /login|user|mail|identifi|compte|account|client|num[ée]ro|ligne|adresse/i.test(says(el));
let user = pick(userSel);
if (!user) {
  user = password
    ? textual.filter(el => el.compareDocumentPosition(password) & Node.DOCUMENT_POSITION_FOLLOWING).pop() || null
    : textual.find(identifier) || null;
}
let submit = pick(submitSel);
if (!submit) {
  const buttons = Array.from(scope.querySelectorAll('button, input[type=submit], [role=button]')).filter(visible);
  submit = buttons.find(b => (b.getAttribute('type') || '').toLowerCase() === 'submit')
        || buttons.find(b => /connex|connect|valider|login|suivant|continuer|entrer/i.test(b.innerText || b.value || ''))
        || null;
}
return [user, password, submit];
"""

# Buttons in the page and in the open shadow roots (cookie banners live
# there), with their text.
READ_BUTTONS_JS = r"""
const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
const found = [];
const walk = root => {
  for (const el of root.querySelectorAll('button, a, [role=button], input[type=button]')) {
    if (visible(el)) found.push([el, (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim()]);
  }
  for (const host of root.querySelectorAll('*')) if (host.shadowRoot) walk(host.shadowRoot);
};
walk(document);
return found;
"""

# Something on screen only a person answers: a field for a one-time code, or
# a captcha one can see. An invisible one - a badge scoring the visitor, a
# challenge frame parked off screen until needed - asks nothing.
VERIFICATION_DOM_JS = r"""
const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
const onScreen = el => {
  const box = el.getBoundingClientRect(), style = getComputedStyle(el);
  return box.width >= 30 && box.height >= 30 && box.bottom > 0 && box.right > 0
    && style.visibility !== 'hidden' && style.display !== 'none' && parseFloat(style.opacity || '1') > 0;
};
if (Array.from(document.querySelectorAll('input[autocomplete=one-time-code]')).some(visible)) return true;
// A field named for a one-time code (of whatever type, a password's too).
if (Array.from(document.querySelectorAll('input')).some(el => visible(el) &&
    /otp|one.?time|(^|[-_])sms([-_]|$)|sms.?code|code.?sms|verif\w*.?code|code.?verif/i.test(
      [el.name, el.id, el.getAttribute('autocomplete')].join(' ')))) return true;
return Array.from(document.querySelectorAll('iframe')).some(f =>
  /captcha|recaptcha|hcaptcha|turnstile|challenge/i.test(f.src || '') && !/size=invisible/i.test(f.src || '') && onScreen(f));
"""

# Something to answer on a page after signing in: an empty field to type in,
# or a slider. Words about a code on an account's pages ("code de sécurité",
# "authentification forte") are not a question without one.
ANSWERABLE_JS = r"""
const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
// Not a search box, a chat or a newsletter field: an account's pages have
// them, and one beside "protégé par…" was taken for a code to type.
const searching = el => !!el.closest('[role=search]') ||
  /recherch|search|question|message|chat|newsletter/i.test(
    [el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
     el.form && [el.form.id, el.form.name, el.form.getAttribute('action'), el.form.className].join(' ')].join(' '));
const typed = Array.from(document.querySelectorAll('input')).some(el =>
  visible(el) && !el.readOnly && !el.disabled && !el.value && el.getBoundingClientRect().width >= 30 && !searching(el) &&
  ['', 'text', 'tel', 'number'].includes((el.getAttribute('type') || '').toLowerCase()));
return typed || Array.from(document.querySelectorAll('[role=slider], input[type=range]')).some(visible);
"""

SLIDER_JS = r"""
const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
return Array.from(document.querySelectorAll('[role=slider], input[type=range]')).some(visible);
"""

# The document a tab shows, read by the tab itself (an address held in the
# page's memory is nobody else's to fetch), as base64.
READ_DOCUMENT_JS = r"""
const done = arguments[arguments.length - 1];
fetch(location.href, {credentials: 'include'}).then(r => r.arrayBuffer()).then(buffer => {
  const bytes = new Uint8Array(buffer);
  let text = '';
  for (let at = 0; at < bytes.length; at += 32768) text += String.fromCharCode.apply(null, bytes.subarray(at, at + 32768));
  done(btoa(text));
}).catch(() => done(null));
"""

ERROR_TEXT_JS = r"""
const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
return Array.from(document.querySelectorAll('[role=alert], .error, .alert, [class*=error], [class*=erreur], .invalid-feedback'))
  .filter(visible).map(el => (el.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 3).join(' | ');
"""


def _browser_gone(exc: Exception) -> bool:
    """A browser closed or dead - not a page that is not what was expected."""
    from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException

    if isinstance(exc, (InvalidSessionIdException, NoSuchWindowException)):
        return True
    text = str(exc).lower()
    return any(
        sign in text
        for sign in (
            "chrome not reachable", "session deleted", "disconnected: not connected", "no such session",
            "target window already closed", "web view not found", "invalid session id",
        )
    )


def build_chrome(download_dir: str, headless: bool):
    """Chrome downloading PDFs into `download_dir` instead of showing them."""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": os.path.abspath(download_dir),
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
            # This profile visits the configured portals only. Chrome held
            # every download of a site after its first ("download several
            # files?", asked of nobody), and blocked the tab a script's click
            # opened - Free Mobile's « Voir ma facture », Eau de Paris' PDF.
            "profile.default_content_setting_values.automatic_downloads": 1,
            "profile.default_content_setting_values.popups": 1,
        },
    )
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    # Headless Chrome blocks downloads unless told otherwise (see metro.py) -
    # in every tab: the page's own setting left a tab opened later without.
    folder = os.path.abspath(download_dir)
    driver.execute_cdp_cmd("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": folder})
    try:
        driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": folder})
    except Exception:  # noqa: BLE001 - an older Chrome: the page's setting is there
        pass
    return driver


class _Visit:
    """One run on one site: the browser, what it is told to do, and a way
    to say what went wrong with the page to show for it."""

    def __init__(self, recipe, download_dir, log, should_cancel, driver_factory, headless):
        self.recipe = recipe
        self.download_dir = download_dir
        self.log = log
        self.should_cancel = should_cancel
        self.headless = headless and not recipe.show_browser
        self.driver = (driver_factory or build_chrome)(download_dir, self.headless)

    # -------------------------------------------------------------- helpers

    def text(self) -> str:
        try:
            return self.driver.execute_script("return document.body ? document.body.innerText : ''") or ""
        except Exception:  # noqa: BLE001 - a page between two loads
            return ""

    def wait(self, condition, seconds: float, message: str = ""):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                result = condition()
            except Exception:  # noqa: BLE001 - the page is changing under us
                result = None
            if result:
                return result
            time.sleep(POLL_SECONDS)
        if message:
            raise WebsiteError(message)
        return None

    def refuse_cookies(self) -> None:
        """Turn the cookie banner down: only the choice that refuses them is
        clicked. A banner offering none is left alone."""
        try:
            for element, text in self.driver.execute_script(READ_BUTTONS_JS) or []:
                if text and len(text) < 60 and REFUSE_COOKIES_RE.search(_fold(text)):
                    self.driver.execute_script("arguments[0].click();", element)
                    time.sleep(1)
                    return
        except Exception:  # noqa: BLE001 - a banner is never worth failing a run over
            return

    def refused(self) -> bool:
        return bool(REFUSED_RE.search(self.text()[:2000]))

    def asks_for_a_person(self, words_alone: bool = False) -> bool:
        """Whether the page asks for something only a person gives: a field
        for a code or a captcha on screen - or words asking for one, on a
        page that is not the login form. A login page may explain that a
        code will be asked (Free Mobile's says a first sign-in needs one
        received by SMS): read as a check, it stopped every run before its
        form was filled. `words_alone`: the words are enough (no form to
        fill at all - TotalEnergies' slider); otherwise they need something
        to answer beside them."""
        if self.check_on_screen():
            return True
        words = bool(VERIFY_RE.search(self.text()[:3000]))
        try:
            # A slider asking to be dragged, over a login form still drawn.
            if words and self.driver.execute_script(SLIDER_JS):
                return True
            if self.password_visible():
                return False
        except Exception:  # noqa: BLE001 - between two pages
            return False
        if not words:
            return False
        if words_alone:
            return True
        try:
            return bool(self.driver.execute_script(ANSWERABLE_JS))
        except Exception:  # noqa: BLE001
            return False

    def check_on_screen(self) -> bool:
        """A field for a one-time code or a captcha a person can see - a
        check whatever the page says around it."""
        try:
            return bool(self.driver.execute_script(VERIFICATION_DOM_JS))
        except Exception:  # noqa: BLE001
            return False

    def cancelled(self) -> bool:
        return bool(callable(self.should_cancel) and self.should_cancel())

    def alive(self) -> bool:
        """Whether the browser is still there - closed by hand, its page
        reads as empty, and an empty page asks for nothing."""
        try:
            self.driver.current_url  # noqa: B018 - asked for the answer's sake
            return True
        except Exception as exc:  # noqa: BLE001
            return not _browser_gone(exc)

    def password_visible(self) -> bool:
        return bool(self.driver.execute_script(
            "return Array.from(document.querySelectorAll('input[type=password]'))"
            ".some(el => el.offsetWidth || el.offsetHeight);"
        ))

    def diagnose(self, context: str) -> None:
        """A screenshot and the page's HTML, kept beside the downloads, and
        the paths said in the log - how a site that changed is fixed."""
        try:
            folder = os.path.join(self.download_dir, DEBUG_DIR)
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            picture = os.path.join(folder, f"{stamp}.png")
            page = os.path.join(folder, f"{stamp}.html")
            self.driver.save_screenshot(picture)
            with open(page, "w", encoding="utf-8") as handle:
                handle.write(self.driver.page_source)
            self.log(f"{context} - page enregistrée : {picture} et {page} (adresse : {self.driver.current_url})")
        except Exception as exc:  # noqa: BLE001
            self.log(f"{context} - la page n'a pas pu être enregistrée : {exc}")

    def quit(self) -> None:
        try:
            self.driver.quit()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- sign in

    def sign_in(self, login: str, password: str) -> None:
        from selenium.common.exceptions import StaleElementReferenceException
        from selenium.webdriver.common.keys import Keys

        recipe = self.recipe
        self.log(f"{recipe.name} : connexion sur {recipe.login_url}")
        self.driver.get(recipe.login_url)
        time.sleep(1)
        if self.refused():
            raise RefusedByTheSite(
                f"{recipe.name} refuse les navigateurs automatisés (« {self.text().strip()[:120]} »). Ses factures "
                "sont à récupérer à la main, ou par email si le site les envoie."
            )
        self.refuse_cookies()

        def fields():
            found = self.driver.execute_script(
                FIND_LOGIN_JS, recipe.username_selector, recipe.password_selector, recipe.submit_selector
            )
            return found if found and (found[0] or found[1]) else None

        def form_or_check(since: float, wanted=fields):
            # The form on screen is filled: whatever its page says about
            # codes, it is what the scraper is there for. A check can stand
            # where the form will be (TotalEnergies' slider): looked for
            # where the form is not, and handed to a person - never
            # answered by the scraper. Words alone count once the form has
            # had time to be drawn.
            found_now = wanted()
            if found_now:
                return found_now
            if self.check_on_screen():
                return "check"
            if time.monotonic() - since >= WORDS_GRACE_SECONDS and self.asks_for_a_person(words_alone=True):
                return "check"
            return None

        loaded = time.monotonic()
        found = self.wait(
            lambda: form_or_check(loaded), PAGE_WAIT_SECONDS,
            f"{recipe.name} : aucun formulaire de connexion trouvé sur {recipe.login_url}.",
        )
        if found == "check":
            self._hand_over(before_login=True, form=fields)
            self.refuse_cookies()
            found = self.wait(
                fields, PAGE_WAIT_SECONDS, f"{recipe.name} : aucun formulaire de connexion après la vérification."
            )

        def fill(which: int, value: str) -> bool:
            """Typed into the field as it is now: a form redrawn while it is
            filled (Free Mobile's) left the one found a moment before
            pointing at nothing."""
            for _attempt in range(3):
                current = fields()
                field_now = current[which] if current else None
                if field_now is None:
                    return False
                try:
                    field_now.clear()
                    field_now.send_keys(value)
                    return True
                except StaleElementReferenceException:
                    time.sleep(POLL_SECONDS)
            raise WebsiteError(f"{recipe.name} : le formulaire de connexion change sans cesse - connexion impossible.")

        def submit_now(fallback: int) -> None:
            """The button as it is now, or Enter in the field."""
            for _attempt in range(3):
                current = fields()
                try:
                    if current and current[2] is not None:
                        self.driver.execute_script("arguments[0].click();", current[2])
                    elif current and current[fallback] is not None:
                        current[fallback].send_keys(Keys.ENTER)
                    return
                except StaleElementReferenceException:
                    time.sleep(POLL_SECONDS)
            raise WebsiteError(f"{recipe.name} : le formulaire de connexion change sans cesse - connexion impossible.")

        def password_form():
            current = fields()
            return current if current and current[1] is not None else None

        _user_field, password_field, _submit = found
        if self.cancelled():
            raise WebsiteError(f"{recipe.name} : récupération annulée avant la connexion.")
        fill(0, login)
        if password_field is None:
            # Two steps: the identifier first, the password on the next page
            # - or a check in between, a person's to answer.
            submit_now(0)
            sent = time.monotonic()
            step = self.wait(
                lambda: form_or_check(sent, password_form),
                PAGE_WAIT_SECONDS,
                f"{recipe.name} : le champ du mot de passe n'est jamais apparu après l'identifiant.",
            )
            if step == "check":
                self._hand_over(before_login=True, form=password_form)
                self.wait(
                    password_form,
                    PAGE_WAIT_SECONDS,
                    f"{recipe.name} : le champ du mot de passe n'est jamais apparu après la vérification.",
                )
        if self.cancelled():
            raise WebsiteError(f"{recipe.name} : récupération annulée avant l'envoi du mot de passe.")
        fill(1, password)
        submit_now(1)
        self._wait_signed_in()

    def _wait_signed_in(self) -> None:
        recipe = self.recipe
        deadline = time.monotonic() + LOGIN_WAIT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            if self.refused():
                raise RefusedByTheSite(f"{recipe.name} a refusé la connexion d'un navigateur automatisé.")
            if self.asks_for_a_person():
                self._hand_over()
                return
            try:
                gone = not self.password_visible()
            except Exception:  # noqa: BLE001 - between two pages
                continue
            if gone:
                # The form gone is not yet signed in: a page may draw its
                # code prompt a moment later - taken for signed in, the run
                # ended with no invoice and no error.
                if self._check_appears(SETTLE_SECONDS):
                    self._hand_over()
                    return
                self.log(f"{recipe.name} : connecté.")
                return
        try:
            error = self.driver.execute_script(ERROR_TEXT_JS) or ""
        except Exception:  # noqa: BLE001
            error = ""
        raise WebsiteError(
            f"{recipe.name} : la connexion n'a pas abouti"
            + (f" (« {error[:200]} »)" if error else "")
            + f". Vérifiez {recipe.username_env} et {recipe.password_env} dans le fichier .env."
        )

    def _check_appears(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.asks_for_a_person():
                return True
            time.sleep(POLL_SECONDS)
        return False

    def _hand_over(self, before_login: bool = False, form=None) -> None:
        """A code sent by SMS, a captcha, a slider: only a person answers it.
        Before the login form, done once the check is gone or the form is
        there (`form`); after it, once signed in. A cancel is heard while
        waiting."""
        recipe = self.recipe
        if self.headless:
            raise NeedsAPerson(
                f"{recipe.name} demande une vérification (code reçu par SMS, captcha, curseur à glisser…)"
                + (" avant même la connexion" if before_login else "")
                + ". Cochez « Navigateur visible » pour cette source et relancez : la fenêtre s'ouvrira, faites "
                "la vérification, la récupération reprendra seule."
            )
        self.log(
            f"{recipe.name} demande une vérification : faites-la dans la fenêtre du navigateur "
            f"(la récupération attend {PERSON_WAIT_SECONDS // 60} minutes)."
        )
        closed, stopped = [], []

        def done() -> bool:
            if self.cancelled():
                stopped.append(True)
                return True
            if not self.alive():
                closed.append(True)
                return True
            if before_login:
                return bool(form and form()) or not self.asks_for_a_person(words_alone=True)
            return not self.asks_for_a_person() and not self.password_visible()

        self.wait(done, PERSON_WAIT_SECONDS, f"{recipe.name} : la vérification n'a pas été faite à temps.")
        if stopped:
            raise WebsiteError(f"{recipe.name} : récupération annulée pendant la vérification.")
        if closed:
            raise WebsiteError(
                f"{recipe.name} : la fenêtre du navigateur a été fermée avant la fin - rien n'a été récupéré. "
                "Relancez et laissez-la ouverte : elle se ferme seule à la fin."
            )
        self.log(f"{recipe.name} : vérification faite" + ("." if before_login else ", connecté."))

    # ------------------------------------------------------- the invoices

    def go_to_invoices(self) -> None:
        recipe = self.recipe
        if recipe.invoices_url:
            self.driver.get(recipe.invoices_url)
            time.sleep(1)
        targets = list(recipe.navigation)
        if not recipe.invoices_url and not targets:
            targets = [None]  # the first link that speaks of invoices
        for target in targets:
            self.refuse_cookies()
            link = self.wait(lambda target=target: self._link_to(target), PAGE_WAIT_SECONDS)
            if link is None:
                if target is None:
                    self.log(f"{recipe.name} : pas de lien « Factures » - les factures sont cherchées sur cette page.")
                    return
                shown = ", ".join(sorted({c.text for c in self.links() if c.text})[:25])
                raise WebsiteError(f"{recipe.name} : aucun lien « {target} » sur la page. Liens visibles : {shown}")
            self.log(f"{recipe.name} : lien « {link[1]} ».")
            self._click_link(link[0])
            time.sleep(2)

    def _link_to(self, target):
        candidate = link_to_follow(self.links(), target)
        return None if candidate is None else (candidate.index, candidate.text or candidate.label)

    def links(self, selector: str = "") -> list[Candidate]:
        rows = self.driver.execute_script(READ_LINKS_JS, selector) or []
        return [Candidate(**row) for row in rows]

    def _element(self, index: int):
        from selenium.webdriver.common.by import By

        found = self.driver.find_elements(By.CSS_SELECTOR, f'[data-mm-link="{index}"]')
        return found[0] if found else None

    def _click_link(self, index: int) -> None:
        element = self._element(index)
        if element is None:
            raise WebsiteError(f"{self.recipe.name} : le lien a disparu de la page avant d'être cliqué.")
        self.driver.execute_script("arguments[0].click();", element)

    def settled_links(self) -> list[Candidate]:
        """The links once the list stops growing: a page drawing its rows
        after it loads is read when it is done."""
        previous, stable = -1, 0
        deadline = time.monotonic() + PAGE_WAIT_SECONDS
        links: list[Candidate] = []
        while time.monotonic() < deadline:
            links = self.links(self.recipe.link_selector)
            stable = stable + 1 if len(links) == previous else 0
            if stable >= 2 and links:
                break
            previous = len(links)
            time.sleep(1)
        return links

    def next_page(self) -> bool:
        if self.recipe.next_selector:
            candidates = self.links(self.recipe.next_selector)
            target = candidates[0].index if candidates else None
        else:
            target = next((c.index for c in self.links() if NEXT_RE.match((c.text or c.label).strip())), None)
        if target is None:
            return False
        self._click_link(target)
        time.sleep(2)
        return True

    def pages(self):
        """The invoices' pages, one after another, as their links."""
        seen: set = set()
        for _page in range(MAX_PAGES):
            links = self.settled_links()
            fresh = [c for c in links if (c.href, c.text, c.row) not in seen]
            if not fresh:
                return
            seen.update((c.href, c.text, c.row) for c in links)
            yield links
            if not self.next_page():
                return

    # ------------------------------------------------------------ download

    def start_downloads(self) -> None:
        """What the folder held before this run - each file with its size and
        time: a file that lands during it, whichever click brought it, is
        this run's, under a new name or written over an old one. Eau de
        Paris names each file after its invoice, and Chrome - downloading as
        told through its DevTools - writes over a file of the same name: a
        run after the first found every name already there, and said
        nothing had arrived while the browser showed each download done."""
        self.baseline = self._snapshot()
        self.taken: set[str] = set()

    def _snapshot(self) -> dict:
        found = {}
        for name in os.listdir(self.download_dir):
            try:
                status = os.stat(os.path.join(self.download_dir, name))
            except OSError:
                continue
            found[name] = (status.st_size, status.st_mtime_ns)
        return found

    def download(self, candidate: Candidate) -> list[str]:
        """One invoice's file - and any other that landed meanwhile.

        A link naming its file is fetched with the browser's session: there
        is no click to wait on. Otherwise it is clicked as a person clicks,
        and waited for: a tab it opened is read for the document it shows,
        and a click that has started nothing - no file, no download under
        way, no tab - after DOWNLOAD_START_SECONDS started nothing."""
        if names_a_file(candidate.href):
            fetched = self._fetch(candidate.href)
            if fetched:
                return self._take([fetched])
        element = self._element(candidate.index)
        if element is None:
            return []
        windows = set(self.driver.window_handles)
        main = self.driver.current_window_handle
        self._press(element)
        started = time.monotonic()
        read_tabs = False
        try:
            while True:
                time.sleep(POLL_SECONDS)
                landed = self._new_pdfs()
                if landed:
                    return self._take(landed)
                elapsed = time.monotonic() - started
                opened = set(self.driver.window_handles) - windows
                if opened and not read_tabs and elapsed >= 2:
                    # A tab showing the invoice (a download would have landed
                    # by now): the document it shows is read.
                    read_tabs = True
                    grabbed = self._read_tabs(opened, main)
                    if grabbed:
                        return self._take([grabbed])
                if elapsed >= DOWNLOAD_TIMEOUT_SECONDS:
                    return []
                if elapsed >= DOWNLOAD_START_SECONDS and not self._downloading() and (not opened or read_tabs):
                    return []
        finally:
            for handle in set(self.driver.window_handles) - windows:
                try:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
                except Exception:  # noqa: BLE001
                    pass
            self.driver.switch_to.window(main)

    def late_downloads(self) -> list[str]:
        """Files that landed after their click was given up: waited for while
        one is under way, and taken."""
        deadline = time.monotonic() + DOWNLOAD_TIMEOUT_SECONDS
        while self._downloading() and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        return self._take(self._new_pdfs())

    def _press(self, element) -> None:
        """A person's click - what lets a page open a tab or save a file; the
        script's where something covers the element."""
        try:
            element.click()
        except Exception:  # noqa: BLE001 - covered, off screen, not interactable
            self.driver.execute_script("arguments[0].click();", element)

    def _fresh(self):
        return {
            name for name, state in self._snapshot().items()
            if self.baseline.get(name) != state and name not in self.taken
        }

    def _downloading(self) -> bool:
        return any(name.endswith((".crdownload", ".tmp")) for name in self._fresh())

    def _new_pdfs(self) -> list[str]:
        """New files that are PDFs, whatever their names. Chrome also drops a
        stray "downloads.htm" that appears and vanishes (as on Metro's
        site): counted as the download, it failed the run."""
        found = []
        for name in sorted(self._fresh()):
            if name.endswith((".crdownload", ".tmp")) or name == DEBUG_DIR:
                continue
            try:
                with open(os.path.join(self.download_dir, name), "rb") as handle:
                    if handle.read(5) == b"%PDF-":
                        found.append(name)
            except OSError:
                continue  # gone already, or still being written
        return found

    def _take(self, names) -> list[str]:
        """The files, each moved at once to a name of its own: the next
        invoice of the same name - or the next run's - would be written over
        it, and imported after the run, two paths held the second."""
        paths = []
        for name in names:
            stem = name[:-4] if name.lower().endswith(".pdf") else name
            kept = f"{datetime.now():%Y%m%d%H%M%S%f}_{stem}.pdf"
            path = os.path.join(self.download_dir, name)
            try:
                os.replace(path, os.path.join(self.download_dir, kept))
            except OSError:
                # Still held by the browser: kept as it is.
                kept = name
            self.taken.add(kept)
            paths.append(os.path.join(self.download_dir, kept))
        return paths

    def _read_tabs(self, opened, main) -> str | None:
        """The document a tab shows, when it is one: its address fetched with
        the session, or - held in the page's memory (a "blob:" address) -
        read by the tab itself."""
        try:
            for handle in opened:
                try:
                    self.driver.switch_to.window(handle)
                    address = self.driver.current_url or ""
                    if address.startswith("http"):
                        fetched = self._fetch(address)
                    elif address.startswith("blob:"):
                        fetched = self._save_from_page()
                    else:
                        fetched = None
                    if fetched:
                        return fetched
                except Exception:  # noqa: BLE001 - a tab closing itself
                    continue
            return None
        finally:
            try:
                self.driver.switch_to.window(main)
            except Exception:  # noqa: BLE001
                pass

    def _save_from_page(self) -> str | None:
        import base64

        self.driver.set_script_timeout(DOWNLOAD_TIMEOUT_SECONDS)
        encoded = self.driver.execute_async_script(READ_DOCUMENT_JS)
        if not encoded:
            return None
        content = base64.b64decode(encoded)
        if not content.startswith(b"%PDF-"):
            return None
        name = f"{datetime.now():%Y%m%d%H%M%S%f}_facture.pdf"
        with open(os.path.join(self.download_dir, name), "wb") as handle:
            handle.write(content)
        return name

    def sweep(self) -> None:
        """What arrived and is not a PDF goes: a stray page, a partial file."""
        for name in os.listdir(self.download_dir):
            path = os.path.join(self.download_dir, name)
            if name == DEBUG_DIR or os.path.isdir(path) or name.lower().endswith(".pdf"):
                continue
            try:
                os.remove(path)
            except OSError:
                pass

    def _fetch(self, url: str) -> str | None:
        import requests

        session = requests.Session()
        for cookie in self.driver.get_cookies():
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"))
        agent = self.driver.execute_script("return navigator.userAgent")
        try:
            response = session.get(url, headers={"User-Agent": agent}, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        except requests.RequestException:
            return None
        if response.status_code != 200 or not response.content.startswith(b"%PDF-"):
            return None
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]) or "facture"
        name = f"{datetime.now():%Y%m%d%H%M%S%f}_{name}"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        with open(os.path.join(self.download_dir, name), "wb") as handle:
            handle.write(response.content)
        return name


def _visit(recipe, download_dir, start, end, known_numbers, log, should_cancel, driver_factory, headless, env_file, fetch):
    os.makedirs(download_dir, exist_ok=True)
    login, password = credentials(recipe, env_file=env_file)
    try:
        visit = _Visit(recipe, download_dir, log, should_cancel, driver_factory, headless)
    except Exception as exc:  # noqa: BLE001 - raised raw, it stopped the whole gather, not this site
        raise WebsiteError(
            f"{recipe.name} : le navigateur n'a pas pu démarrer ({exc.__class__.__name__} - {str(exc).strip()[:200]})."
        ) from exc
    try:
        try:
            visit.sign_in(login, password)
            visit.go_to_invoices()
            return fetch(visit)
        except WebsiteError:
            visit.diagnose(f"{recipe.name} : échec")
            raise
        except Exception as exc:  # noqa: BLE001 - said in words, with the page to show for it
            visit.diagnose(f"{recipe.name} : erreur inattendue")
            raise WebsiteError(f"{recipe.name} : {exc.__class__.__name__} - {str(exc).strip()[:300]}") from exc
    finally:
        visit.quit()
        visit.sweep()


def fetch_website_invoices(
    recipe: WebsiteRecipe,
    download_dir: str,
    start: date,
    end: date,
    *,
    known_numbers=(),
    log=print,
    on_progress=None,
    should_cancel=lambda: False,
    driver_factory=None,
    headless: bool = True,
    env_file=None,
) -> list[str]:
    """Sign in, and download every invoice of [start, end] not already
    imported. Returns the files, PDFs only. Raises WebsiteError, for the
    operator."""

    def fetch(visit: _Visit) -> list[str]:
        files: list[str] = []
        recognised = attempted = 0
        seen: set = set()
        visit.start_downloads()
        for links in visit.pages():
            # One invoice once a run: a list growing under « Voir plus » (Free
            # Mobile's shows five, then eight) was downloaded again from its
            # first row, at every page.
            fresh = [
                (candidate, decision)
                for candidate, decision in choose(links, start, end, known_numbers, bool(recipe.link_selector))
                if invoice_key(candidate) not in seen
            ]
            if recognised and not fresh:
                break
            for candidate, decision in fresh:
                seen.add(invoice_key(candidate))
                recognised += 1
                if decision != "à télécharger":
                    continue
                if should_cancel() or len(files) >= MAX_DOWNLOADS:
                    return files
                attempted += 1
                landed = visit.download(candidate)
                what = candidate.row[:80] or candidate.text
                if not landed:
                    log(f"{recipe.name} : rien n'est arrivé pour « {what} ».")
                else:
                    files.extend(landed)
                    log(f"{recipe.name} : téléchargée « {what} »" + (f" (+{len(landed) - 1} arrivée(s) en retard)." if len(landed) > 1 else "."))
                    if on_progress:
                        on_progress(len(files), None)
                time.sleep(CLICK_INTERVAL_SECONDS)
        late = visit.late_downloads()
        if late:
            files.extend(late)
            log(f"{recipe.name} : {len(late)} facture(s) arrivée(s) après avoir été attendue(s).")
        if attempted and not files:
            # Clicked, and nothing came: said as "nothing to download", Eau de
            # Paris' three invoices passed for a quiet month.
            raise WebsiteError(
                f"{recipe.name} : aucune des {attempted} factures cliquées n'est arrivée - le site ne les a pas "
                "données au navigateur. La page est enregistrée."
            )
        if not files:
            log(f"{recipe.name} : aucune facture à télécharger sur la période.")
        if not recognised:
            # Not one invoice link recognised - even out of the period: this is
            # not the list, or its links are not understood. A failure, on its
            # own line (the period is offered again), with the page kept by
            # _visit and its links named - what the site is set up from
            # ("Liens à suivre", the selectors). Said as "0 found", it passed
            # for a quiet month, and left nothing to go on.
            shown = ", ".join(sorted({c.text or c.label for c in visit.links() if c.text or c.label})[:40])
            raise WebsiteError(
                f"{recipe.name} : aucun lien de facture reconnu sur la page lue. Liens visibles : {shown}"
            )
        return files

    return _visit(recipe, download_dir, start, end, known_numbers, log, should_cancel, driver_factory, headless, env_file, fetch)


def list_website_invoices(
    recipe: WebsiteRecipe,
    download_dir: str,
    start: date,
    end: date,
    *,
    known_numbers=(),
    log=print,
    driver_factory=None,
    headless: bool = True,
    env_file=None,
) -> list[dict]:
    """The same visit, downloading nothing: what each invoice link found is,
    and what a run would do with it - how a new site is set up."""

    def fetch(visit: _Visit) -> list[dict]:
        rows = []
        seen: set = set()
        for links in visit.pages():
            fresh = [
                (candidate, decision)
                for candidate, decision in choose(links, start, end, known_numbers, bool(recipe.link_selector))
                if invoice_key(candidate) not in seen
            ]
            if rows and not fresh:
                break
            for candidate, decision in fresh:
                seen.add(invoice_key(candidate))
                spans = periods(candidate.row)
                rows.append(
                    {
                        "row": candidate.row[:200],
                        "link": candidate.text or candidate.label or candidate.href[:80],
                        "date": f"{spans[0][0]:%d/%m/%Y}" if spans else "",
                        "decision": decision,
                    }
                )
        log(f"{recipe.name} : {len(rows)} facture(s) trouvée(s).")
        if not rows:
            shown = ", ".join(sorted({c.text for c in visit.links() if c.text})[:40])
            log(f"{recipe.name} : aucun lien de facture reconnu sur cette page. Liens visibles : {shown}")
        # A test is how a site is set up: the page it read is kept, found or
        # not, to set the links to follow and the selectors from.
        visit.diagnose(f"{recipe.name} : page lue")
        return rows

    return _visit(recipe, download_dir, start, end, known_numbers, log, lambda: False, driver_factory, headless, env_file, fetch)
