"""« Motifs »: the regexes somebody types to read a seller's bon - checked
before they are ever compiled, and run with a time limit.

**Why the check comes first (29/09).** The PyPI `regex` module is what
runs a motif, because it takes a `timeout`. But `regex.compile` itself has
no timeout and EXPANDS counted repetitions: compiling `(?:x{65535}){65535}`
allocated about 50 GB and froze the owner's 16 GB PC twice. So a motif is
compiled only after the standard library's pure-Python parser has shown its
shape (`re._parser.parse` builds a tree and expands nothing), and that tree
passed three rules:

- no VERBOSE mode, in any form: under (?x) `regex` reads `{6 5 5 3 5}` as a
  count while the standard parser reads it as text, so the tree would lie;
- no `{` that is not a plain numeric count: `regex` reads such a brace as a
  fuzzy constraint (`a{e<=1}`), which the standard parser reads as text;
- no `[` inside a set: `regex` reads `[[:alpha:])(]` as ONE set (a POSIX
  class) where the standard parser ends the set at the first `]` and reads
  `)(` as group brackets - the trees then disagree on the nesting;
- no repetition past 100, no nesting of repetitions multiplying past 1 000,
  no group nesting deeper than 20.

**And the repetitions are measured again on `regex`'s own tree** (review,
29/09): a check that reads the standard parser's tree is only as good as the
two parsers' agreement, and a POSIX class walked around it with a motif whose
groups nested six deep for `regex` (100^6) while the standard tree saw
siblings. `regex` parses exactly as `regex.compile` would (_check_regex_tree)
and parsing expands nothing, so every rule holds on the tree that is actually
compiled; approximate matching and calls to a group found there are refused.

Only then `regex.compile`, inside `except Exception` (a RecursionError or an
OverflowError is not a regex.error). Tests of these refusals patch
`regex.compile` with a sentinel that fails if it is called: never compile a
refused motif for real to "see what happens".

**Matching** goes through `search` / `find_all` with a `Budget` (a deadline):
each call gets `timeout=min(MOTIF_TIMEOUT, what is left)` and
`concurrent=True` (the GIL is released while it runs), and a timeout or a
spent budget is a MotifError « … est trop lent », never a hang. `regex`'s
finditer is lazy, so find_all builds its list inside the try.

**Numbers and dates** read from a bon are bounded to the columns they go
into (`read_amount`, `read_quantity`, `read_date`, `read_time`): a figure
wider than the column is not read - saved, it would raise on every later
read of the row (SQLite quantizes decimals on the way out).

Pure: no model, no request. `reading.py` reads a bon with these; the forms
and « Données » check a motif with `compile_field` / `compile_motif`.
"""

from __future__ import annotations

import functools
import logging
import re
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from re import _constants as sre_constants
from re import _parser as sre_parser

import regex
from django.utils import timezone
from regex import _regex_core

logger = logging.getLogger(__name__)

#: Seconds one match may run.
MOTIF_TIMEOUT = 0.25
#: A reading (read_slip_text, « Tester »), a page's classification, and
#: « Relire » over a whole format: the time each may spend matching.
READING_SECONDS = 2.0
PAGE_SECONDS = 1.0
REREAD_SECONDS = 30.0

#: A motif's own length, unless its field says otherwise.
MAX_MOTIF_LENGTH = 300
#: A repetition's count ({n}, {n,m}, {n,}) - its min and its bounded max.
MAX_REPEAT = 100
#: What the counts of repetitions nested in one another may multiply to.
MAX_REPEAT_PRODUCT = 1_000
#: Groups inside groups.
MAX_GROUP_DEPTH = 20
#: A « one per line » field of a format, and a type's motifs.
MAX_MOTIFS = 10
MAX_TYPE_MOTIFS = 50
#: A mail header is matched on this many characters at most.
MAIL_TEXT_LIMIT = 500

#: How every motif is compiled: case never matters, ^ and $ are a line's ends.
FLAGS = regex.IGNORECASE | regex.MULTILINE

#: A quantity read from a bon (SlipLine.quantity).
MAX_QUANTITY = 99_999
#: The oldest date a reading accepts, and how far past today.
OLDEST_DATE = date(2000, 1, 1)
FUTURE_DAYS = 7

#: The address a sender motif must NOT match (it would match everybody).
SENDER_PROBE = "quelquun@example.invalid"

_REPEATS = (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT, sre_constants.POSSESSIVE_REPEAT)
_UNBOUNDED = sre_constants.MAXREPEAT

#: What may follow a `{` that is a count: {3} {3,} {3,5} {,5}.
_COUNT_TAIL = re.compile(r"[0-9]+(?:,[0-9]*)?\}|,[0-9]+\}")


class MotifError(ValueError):
    """A motif refused, or too slow: `message` is the French sentence, the
    field's name first (« Motif de ligne : parenthèse non fermée
    (position 14) »)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class _Refused(Exception):
    """Inside the cached check: the reason, without the field's name (the
    cache is shared by every field, the name is added outside it)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# -- The standard parser's errors, in French ------------------------------------------------------------------------

#: (start of the stdlib message, what the page says). The first that the
#: message starts with wins; anything else is « motif invalide ».
_PARSE_ERRORS = (
    ("missing ), unterminated subpattern", "parenthèse non fermée"),
    ("missing ), unterminated comment", "commentaire (?#…) non fermé"),
    ("unbalanced parenthesis", "parenthèse fermante sans parenthèse ouvrante"),
    ("nothing to repeat", "rien à répéter avant ce signe (écrivez \\* \\+ \\? pour le caractère lui-même)"),
    ("multiple repeat", "deux signes de répétition à la suite"),
    ("bad escape", "échappement inconnu"),
    ("unterminated character set", "crochet non fermé"),
    ("bad character range", "intervalle de caractères à l'envers"),
    ("unknown group name", "nom de groupe inconnu"),
    ("redefinition of group name", "nom de groupe utilisé deux fois"),
    ("missing group name", "nom de groupe manquant"),
    ("bad character in group name", "nom de groupe invalide (lettres, chiffres et _ seulement)"),
    ("missing >, unterminated name", "nom de groupe non fermé par >"),
    ("min repeat greater than max repeat", "répétition {min,max} dont le min dépasse le max"),
    ("the repetition number is too large", "répétition trop grande"),
    ("unknown extension", "construction (?…) inconnue"),
    ("unknown flag", "option (?…) inconnue"),
    ("missing -, : or )", "option (?…) mal fermée"),
    ("missing :", "option (?…) mal fermée"),
    ("global flags not at the start", "une option comme (?i) se met au tout début du motif"),
    ("invalid group reference", "référence à un groupe qui n'existe pas"),
    ("cannot refer to an open group", "référence à un groupe encore ouvert"),
    ("look-behind requires fixed-width pattern", "un regard en arrière (?<=…) doit avoir une longueur fixe"),
)


def _translate(message: str, position) -> str:
    message = message or ""
    reason = "motif invalide"
    for start, french in _PARSE_ERRORS:
        if message.startswith(start):
            reason = french
            if start == "bad escape":
                reason += " " + message[len(start):].strip()
            break
    if isinstance(position, int) and position >= 0:
        # 1-based: « position 14 » is the fourteenth character of the motif.
        reason += f" (position {position + 1})"
    return reason


# -- The check ------------------------------------------------------------------------------------------------------


def _scan_problem(motif: str) -> tuple[str, int] | None:
    """What the character scan refuses, with its index, or None:

    - `"brace"`: a `{` that is not a plain count. Escapes (`\\{`) and the
      inside of `[...]` sets are skipped - `[{]` is a brace, literally; a
      `]` right after `[` or `[^` belongs to the set.
    - `"set"`: an unescaped `[` INSIDE a set. `regex` reads `[[:alpha:])(]`
      as one set (a POSIX class, then `)` and `(`) where the standard parser
      reads a set ending at the first `]` followed by `)(`: the two trees
      then disagree on where groups open and close, and a guard reading one
      of them was walked around by exactly that (review, 29/09). Refused
      outright, whatever follows (`[[:`, `[^[:`, `[a[:`, or any nested set).

    A `(?#…)` comment is skipped the way both parsers skip it (to the first
    unescaped `)`): a `[` inside one opened a « set » here that swallowed
    the rest of the motif, braces included."""
    index, length = 0, len(motif)
    while index < length:
        char = motif[index]
        if char == "\\":
            index += 2
            continue
        if motif.startswith("(?#", index):
            index += 3
            while index < length and motif[index] != ")":
                index += 2 if motif[index] == "\\" else 1
            index += 1
            continue
        if char == "[":
            inside = index + 1
            if inside < length and motif[inside] == "^":
                inside += 1
            if inside < length and motif[inside] == "]":
                inside += 1
            while inside < length and motif[inside] != "]":
                if motif[inside] == "\\":
                    inside += 2
                    continue
                if motif[inside] == "[":
                    return "set", inside
                inside += 1
            index = inside + 1
            continue
        if char == "{" and not _COUNT_TAIL.match(motif, index + 1):
            return "brace", index
        index += 1
    return None


# -- The same rules on `regex`'s own tree ----------------------------------------------------------------------------
#
# The standard parser's tree is not what `regex` compiles: the two parsers
# disagree on some syntax, and every disagreement is a way around a check
# that reads the wrong tree. So the repetitions are ALSO measured on the tree
# `regex` itself builds, exactly as `regex.compile` builds it (the same
# Source, Info and _parse_pattern, the same retry on a global flag), before
# anything is compiled. Parsing expands nothing - counts are plain integers
# until compile - so this is as safe as the standard parse. These are private
# names of the pinned `regex` version; tests/test_patterns pins that they
# still exist, so an upgrade that moves them fails a test, not the guard.

#: LazyRepeat and PossessiveRepeat derive from GreedyRepeat.
_RX_REPEAT = _regex_core.GreedyRepeat
#: Approximate matching and calls to a group (recursion) are refused.
_RX_REFUSED = (_regex_core.Fuzzy, _regex_core.CallGroup, _regex_core.CallRef)
_RX_NODE = _regex_core.RegexBase
#: More nodes than a 500-character motif can make: a cycle, or worse.
MAX_TREE_NODES = 20_000


def _regex_tree(motif: str):
    """`regex`'s parse of `motif`, as `regex.compile(motif, FLAGS)` would
    parse it (regex/_main.py _compile), and nothing more."""
    flags = FLAGS | regex.VERSION0
    for _attempt in range(3):
        source = _regex_core.Source(motif)
        info = _regex_core.Info(flags, source.char_type, {})
        source.ignore_space = bool(info.flags & regex.VERBOSE)
        try:
            parsed = _regex_core._parse_pattern(source, info)
        except _regex_core._UnscopedFlagSet:
            flags = info.global_flags
            continue
        if not source.at_end():
            raise _Refused("parenthèse fermante sans parenthèse ouvrante")
        return parsed
    raise _Refused("motif invalide")


def _rx_children(node):
    for value in vars(node).values():
        if isinstance(value, _RX_NODE):
            yield value
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, _RX_NODE):
                    yield item


def _check_regex_tree(motif: str) -> None:
    """Steps 5's rules on `regex`'s own tree - _Refused when broken."""
    try:
        parsed = _regex_tree(motif)
    except _Refused:
        raise
    except Exception:  # regex.error, RecursionError on a deep motif, ...
        raise _Refused("motif invalide") from None
    stack = [(parsed, 1)]
    visited = 0
    while stack:
        node, product = stack.pop()
        visited += 1
        if visited > MAX_TREE_NODES:
            raise _Refused("motif trop compliqué")
        if isinstance(node, _RX_REFUSED):
            raise _Refused("recherche approchée ou appel de groupe : non accepté dans un motif")
        if isinstance(node, _RX_REPEAT):
            low, high = node.min_count, node.max_count
            if low > MAX_REPEAT or (high is not None and high > MAX_REPEAT):
                raise _Refused(f"répétition trop grande : {MAX_REPEAT} fois au plus")
            product *= high if high is not None else max(low, 1)
            if product > MAX_REPEAT_PRODUCT:
                raise _Refused(
                    f"répétition trop grande : des répétitions imbriquées dépassent {MAX_REPEAT_PRODUCT} en tout"
                )
        stack.extend((child, product) for child in _rx_children(node))


def _items(node):
    return getattr(node, "data", node) or ()


def _walk(parsed):
    """Every node of the parse tree, with the product of the repetition
    counts above it and its depth in groups - iteratively: a deep motif
    must not be a RecursionError here."""
    stack = [(parsed, 1, 0)]
    while stack:
        node, product, depth = stack.pop()
        for op, av in _items(node):
            yield op, av, product, depth
            if op in _REPEATS:
                low, high, item = av
                factor = high if high != _UNBOUNDED else max(low, 1)
                stack.append((item, min(product * factor, MAX_REPEAT_PRODUCT + 1), depth))
            elif op is sre_constants.SUBPATTERN:
                stack.append((av[3], product, depth + 1))
            elif op is sre_constants.BRANCH:
                for branch in av[1]:
                    stack.append((branch, product, depth))
            elif op is sre_constants.ATOMIC_GROUP:
                stack.append((av, product, depth + 1))
            elif op in (sre_constants.ASSERT, sre_constants.ASSERT_NOT):
                stack.append((av[1], product, depth + 1))
            elif op is sre_constants.GROUPREF_EXISTS:
                stack.append((av[1], product, depth + 1))
                if av[2] is not None:
                    stack.append((av[2], product, depth + 1))


def _groups_sentence(names) -> str:
    shown = [f"(?P<{name}>…)" for name in names]
    if len(shown) == 1:
        return shown[0]
    return ", ".join(shown[:-1]) + " et " + shown[-1]


@functools.lru_cache(maxsize=256)
def _checked(motif: str, required_groups: tuple, max_length: int):
    """Steps 2 to 7 of the check, cached: only a motif that passed is kept
    (lru_cache does not keep an exception). It holds compiled patterns and
    no espace's data, so it needs no tenant key."""
    # 2. The standard parser shows the motif's shape and expands nothing.
    try:
        parsed = sre_parser.parse(motif, re.IGNORECASE | re.MULTILINE)
    except re.error as error:
        raise _Refused(_translate(error.msg, error.pos)) from None
    except Exception:
        raise _Refused("motif invalide") from None

    # 3. VERBOSE in any form: the global (?x), or a scoped (?x:…).
    nodes = list(_walk(parsed))
    verbose = bool(parsed.state.flags & re.VERBOSE) or any(
        op is sre_constants.SUBPATTERN and av[1] & re.VERBOSE for op, av, _product, _depth in nodes
    )
    if verbose:
        raise _Refused("le mode (?x) n'est pas accepté dans un motif")

    # 4. Every `{` is a plain count (this also refuses regex's fuzzy {e<=1}),
    # and no `[` inside a set (where the two parsers read sets differently).
    problem = _scan_problem(motif)
    if problem is not None and problem[0] == "brace":
        raise _Refused(f"accolade : écrivez \\{{ pour une accolade littérale (position {problem[1] + 1})")
    if problem is not None:
        raise _Refused(f"crochet [ dans un ensemble [...] : écrivez \\[ (position {problem[1] + 1})")

    # 5. Repetitions and nesting, bounded.
    for op, av, product, depth in nodes:
        if depth > MAX_GROUP_DEPTH:
            raise _Refused(f"répétition trop grande : plus de {MAX_GROUP_DEPTH} groupes les uns dans les autres")
        if op in _REPEATS:
            low, high, _item = av
            if low > MAX_REPEAT or (high != _UNBOUNDED and high > MAX_REPEAT):
                raise _Refused(f"répétition trop grande : {MAX_REPEAT} fois au plus")
            factor = high if high != _UNBOUNDED else max(low, 1)
            if product * factor > MAX_REPEAT_PRODUCT:
                raise _Refused(
                    f"répétition trop grande : des répétitions imbriquées dépassent {MAX_REPEAT_PRODUCT} en tout"
                )

    # 5b. The same rules on the tree `regex` itself builds - the one it
    # compiles (see _check_regex_tree): a disagreement between the two
    # parsers can no longer hide a repetition.
    _check_regex_tree(motif)

    # 6. Only now, compiled - anything it raises is a refusal, not a 500.
    try:
        pattern = regex.compile(motif, FLAGS)
    except Exception as error:
        raise _Refused(_translate(getattr(error, "msg", ""), None)) from None

    # 7. A motif that finds something on an empty line finds it everywhere.
    try:
        empty = pattern.search("", timeout=MOTIF_TIMEOUT, concurrent=True)
    except TimeoutError:
        raise _Refused("le motif est trop lent") from None
    if empty is not None:
        raise _Refused("le motif accepte une ligne vide : il trouverait quelque chose sur n'importe quelle ligne")
    if any(name not in pattern.groupindex for name in required_groups):
        raise _Refused(f"le motif doit contenir {_groups_sentence(required_groups)}")
    return pattern


def compile_motif(text, *, field_label: str, required_groups=(), max_length: int = MAX_MOTIF_LENGTH):
    """The motif `text` (stripped), checked then compiled with
    IGNORECASE | MULTILINE - or MotifError, a French sentence starting with
    `field_label`. A blank motif is refused here: whether a field may be
    blank is its caller's to decide, before calling (`compile_field` does)."""
    motif = (text or "").strip()
    if not motif:
        raise MotifError(f"{field_label} : le motif est vide.")
    if len(motif) > max_length:
        raise MotifError(f"{field_label} : {max_length} caractères au plus ({len(motif)} ici).")
    try:
        return _checked(motif, tuple(required_groups), max_length)
    except _Refused as refused:
        raise MotifError(f"{field_label} : {refused.reason}.") from None


def check_lines(text, *, field_label: str, required_groups=(), max_length: int = MAX_MOTIF_LENGTH,
                max_lines: int = MAX_MOTIFS) -> list:
    """A « one per line » field: every non-blank line is a motif, checked
    like `compile_motif`; returns the compiled list, in order ([] when
    blank). A refusal names its line when there are several."""
    lines = [(number, line.strip()) for number, line in enumerate((text or "").splitlines(), start=1)]
    motifs = [(number, line) for number, line in lines if line]
    if len(motifs) > max_lines:
        raise MotifError(f"{field_label} : {max_lines} motifs au plus, un par ligne ({len(motifs)} ici).")
    several = len(motifs) > 1
    return [
        compile_motif(
            line,
            field_label=f"{field_label} (ligne {number})" if several else field_label,
            required_groups=required_groups,
            max_length=max_length,
        )
        for number, line in motifs
    ]


def check_sender_motif(text) -> None:
    """A format's sender motif (when set) must designate an address or a
    domain: it holds a literal « @ » and does not match anybody's address."""
    pattern = compile_motif(text, field_label=FIELD_BY_ATTR["sender_pattern"].label)
    message = "Le motif d'expéditeur doit désigner une adresse ou un domaine."
    if "@" not in text:
        raise MotifError(message)
    try:
        matched = pattern.search(SENDER_PROBE, timeout=MOTIF_TIMEOUT, concurrent=True)
    except TimeoutError:
        raise MotifError(f"{FIELD_BY_ATTR['sender_pattern'].label} : le motif est trop lent.") from None
    if matched is not None:
        raise MotifError(message)


# -- The motifs of a format and of a type ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MotifField:
    """One motif field of a format (or a type's `slip_patterns`): what its
    motifs must contain and how long they may be. `several`: one motif per
    line, at most `max_lines`. `required`: the form refuses it blank (not
    enforced by compile_field, which returns [] for a blank field)."""

    attr: str
    label: str
    groups: tuple = ()
    max_length: int = MAX_MOTIF_LENGTH
    several: bool = False
    max_lines: int = MAX_MOTIFS
    required: bool = False


FORMAT_FIELDS = (
    MotifField("sender_pattern", "Motif d'expéditeur"),
    MotifField("subject_pattern", "Motif d'objet"),
    MotifField("attachment_pattern", "Motif de pièce jointe", max_length=200),
    MotifField("section_start", "Début de la partie"),
    MotifField("section_end", "Fin de la partie"),
    MotifField("line_pattern", "Motif de ligne", groups=("designation", "quantite"), max_length=500, required=True),
    MotifField("date_patterns", "Motif de date", groups=("date",), several=True, required=True),
    MotifField("printed_patterns", "Motif d'impression", groups=("date",), several=True),
    MotifField("number_patterns", "Motif de numéro", groups=("numero",), several=True),
    MotifField("reference_patterns", "Motif de référence", groups=("reference",), several=True),
    MotifField("replaces_pattern", "Motif « annule et remplace »"),
    MotifField("total_patterns", "Motif de total", groups=("total",), several=True),
    MotifField("remarks_start", "Début des remarques"),
    MotifField("remarks_end", "Fin des remarques"),
)
FIELD_BY_ATTR = {field.attr: field for field in FORMAT_FIELDS}
#: The fields a reading uses (not the mail ones: a bad sender motif must not
#: stop a bon from being read).
READING_FIELDS = tuple(
    field for field in FORMAT_FIELDS if field.attr not in ("sender_pattern", "subject_pattern", "attachment_pattern")
)
TYPE_FIELD = MotifField("slip_patterns", "Motifs des bons", several=True, max_lines=MAX_TYPE_MOTIFS)


def compile_field(field: MotifField, value) -> list:
    """The compiled motifs of one field's value: [] when blank, a list of
    one for a single-motif field, MotifError at the first refusal."""
    if not (value or "").strip():
        return []
    if field.several:
        return check_lines(
            value, field_label=field.label, required_groups=field.groups,
            max_length=field.max_length, max_lines=field.max_lines,
        )
    return [compile_motif(value, field_label=field.label, required_groups=field.groups, max_length=field.max_length)]


def compile_format(fmt, fields=READING_FIELDS) -> dict:
    """{attr: [compiled motifs]} for `fmt` - any object carrying the
    attributes (a SlipFormat, a form's cleaned data as a namespace, a
    test's SimpleNamespace); a missing attribute is blank. MotifError at
    the first field refused."""
    return {field.attr: compile_field(field, getattr(fmt, field.attr, "") or "") for field in fields}


# -- Matching, on a budget ------------------------------------------------------------------------------------------


class Budget:
    """A deadline: what a reading, a page's classification or « Relire »
    may spend matching, in seconds, all calls together."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.deadline = clock.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self.deadline - clock.monotonic())

    @property
    def spent(self) -> bool:
        return self.remaining() <= 0


def shown_motif(pattern) -> str:
    text = getattr(pattern, "pattern", str(pattern))
    return text if len(text) <= 60 else text[:59] + "…"


def _too_slow(pattern) -> MotifError:
    return MotifError(f"Le motif « {shown_motif(pattern)} » est trop lent : simplifiez-le.")


def _timeout(pattern, budget: Budget) -> float:
    remaining = budget.remaining()
    if remaining <= 0:
        raise _too_slow(pattern)
    return min(MOTIF_TIMEOUT, remaining)


def search(pattern, text: str, budget: Budget):
    """pattern.search(text) within the budget: the match or None, or
    MotifError « … est trop lent »."""
    timeout = _timeout(pattern, budget)
    try:
        return pattern.search(text, timeout=timeout, concurrent=True)
    except TimeoutError:
        raise _too_slow(pattern) from None


def find_all(pattern, text: str, budget: Budget, limit: int = 50) -> list:
    """Every match of `pattern` in `text` (at most `limit`), within the
    budget. The list is built inside the try: finditer is lazy, and its
    TimeoutError comes while iterating."""
    timeout = _timeout(pattern, budget)
    found = []
    try:
        for match in pattern.finditer(text, timeout=timeout, concurrent=True):
            found.append(match)
            if len(found) >= limit:
                break
    except TimeoutError:
        raise _too_slow(pattern) from None
    return found


def captured(match, name: str) -> str | None:
    """A group's value, stripped - None when the motif has no such group,
    when the group took no part in the match, or when it is blank: a blank
    capture is « not read », and the next line or motif is tried."""
    if match is None or name not in match.re.groupindex:
        return None
    value = match.group(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


class MailMatcher:
    """What `mail_matcher` returns: `.search(text)` like a compiled
    pattern's, on the first MAIL_TEXT_LIMIT characters, with a timeout. A
    timeout is « no match » (a header anybody can write must not hang the
    gather), logged."""

    def __init__(self, pattern, log=None):
        self._pattern = pattern
        self._log = log
        self.pattern = pattern.pattern

    def search(self, text):
        try:
            return self._pattern.search((text or "")[:MAIL_TEXT_LIMIT], timeout=MOTIF_TIMEOUT, concurrent=True)
        except TimeoutError:
            message = f"motif trop lent sur un mail : ignoré ({shown_motif(self._pattern)})"
            logger.warning(message)
            if self._log is not None:
                self._log(message)
            return None


def mail_matcher(text, *, log=None) -> MailMatcher:
    """The `compile` a format's mail motifs are handed to
    find_matching_emails with: the motif checked like any other (MotifError
    otherwise), case-insensitive, timed."""
    return MailMatcher(compile_motif(text, field_label="Motif de mail"), log)


# -- Numbers, dates, times ------------------------------------------------------------------------------------------

_DIGITS = re.compile(r"[0-9]+")


def _grouped(text: str, separator: str) -> bool:
    """Thousands groups: 1 to 3 digits, then groups of exactly 3."""
    return re.fullmatch(r"[0-9]{1,3}(?:" + re.escape(separator) + r"[0-9]{3})+", text) is not None


def _read_number(text) -> Decimal | None:
    """A number as a bon prints it, unbounded: spaces, no-break spaces and
    « ' » dropped; a leading « - » or « − » or a trailing « - » is the sign;
    with both « . » and « , » the rightmost is the decimal separator and the
    others separate thousands; one of them once is the decimal separator
    (« 4,000 » is 4); one of them several times separates thousands."""
    if not isinstance(text, str):
        return None
    digits = "".join(char for char in text.strip() if not char.isspace() and char != "'")
    if not digits or len(digits) > 40:
        return None
    negative = False
    if digits[0] in ("-", "\N{MINUS SIGN}"):
        negative, digits = True, digits[1:]
    if digits.endswith("-"):
        if negative:
            return None
        negative, digits = True, digits[:-1]
    if not digits:
        return None
    dots, commas = digits.count("."), digits.count(",")
    if dots and commas:
        decimal_mark = "." if digits.rfind(".") > digits.rfind(",") else ","
        thousands = "," if decimal_mark == "." else "."
        if digits.count(decimal_mark) != 1:
            return None
        whole, fraction = digits.split(decimal_mark)
        if not _grouped(whole, thousands):
            return None
        whole = whole.replace(thousands, "")
    elif dots + commas == 1:
        whole, fraction = re.split(r"[.,]", digits)
        if not fraction:
            return None
    elif dots + commas > 1:
        separator = "." if dots else ","
        if not _grouped(digits, separator):
            return None
        whole, fraction = digits.replace(separator, ""), ""
    else:
        whole, fraction = digits, ""
    if not _DIGITS.fullmatch(whole) or (fraction and not _DIGITS.fullmatch(fraction)):
        return None
    value = Decimal(f"{whole}.{fraction}" if fraction else whole)
    return -value if negative else value


def read_amount(text, places: int = 2, *, digits: int = 12) -> Decimal | None:
    """An amount, exact to `places` decimals (a unit price: places=4), that
    fits a DecimalField(`digits`, `places`): |x| < 10^(digits - places) -
    10^10 for an amount. More decimals than `places` (other than zeros), or
    wider than the column: None, « nombre hors limites » - never rounded."""
    number = _read_number(text)
    if number is None:
        return None
    if abs(number) >= Decimal(10) ** (digits - places):
        return None
    try:
        quantized = number.quantize(Decimal(1).scaleb(-places))
    except InvalidOperation:
        return None
    if quantized != number:
        return None
    return abs(quantized) if quantized == 0 else quantized


def read_quantity(text) -> int | None:
    """A whole quantity (« 4,000 » is 4), |q| ≤ 99 999, else None."""
    number = _read_number(text)
    if number is None or number != number.to_integral_value():
        return None
    quantity = int(number)
    if abs(quantity) > MAX_QUANTITY:
        return None
    return quantity


_DAY_FIRST = re.compile(r"([0-9]{1,2})([/.\-])([0-9]{1,2})\2([0-9]{4}|[0-9]{2})")
_YEAR_FIRST = re.compile(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})")
_TIME = re.compile(r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?")


def read_date(text, *, today: date | None = None) -> date | None:
    """dd/mm/yyyy, dd/mm/yy (20yy), dd.mm.yyyy, dd-mm-yyyy or yyyy-mm-dd,
    the whole text; an impossible date, or one outside [2000-01-01,
    today + 7 days], is None."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    match = _DAY_FIRST.fullmatch(text)
    try:
        if match:
            year = int(match.group(4))
            if len(match.group(4)) == 2:
                year += 2000
            day = date(year, int(match.group(3)), int(match.group(1)))
        else:
            match = _YEAR_FIRST.fullmatch(text)
            if not match:
                return None
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    today = today or timezone.localdate()
    if day < OLDEST_DATE or day > today + timedelta(days=FUTURE_DAYS):
        return None
    return day


def read_time(text) -> time | None:
    """HH:MM or HH:MM:SS within a day, else None."""
    if not isinstance(text, str):
        return None
    match = _TIME.fullmatch(text.strip())
    if not match:
        return None
    hour, minute, second = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    return time(hour, minute, second)


def aware_datetime(day: date, hour: time | None = None) -> datetime:
    """`day` at `hour` (midnight when None) in settings.TIME_ZONE. zoneinfo
    never raises: a time in a DST gap or fold takes fold=0."""
    return timezone.make_aware(datetime.combine(day, hour or time(0, 0)), timezone.get_default_timezone())
