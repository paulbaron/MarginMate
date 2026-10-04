"""« Motifs »: the regexes somebody types to read a seller's slip - checked
before they are ever compiled, and run with a time limit.

**Why the check comes first (29/09).** The PyPI `regex` module is what
runs a pattern, because it takes a `timeout`. But `regex.compile` itself has
no timeout and EXPANDS counted repetitions: compiling `(?:x{65535}){65535}`
allocated about 50 GB and froze the owner's 16 GB PC twice. So a pattern is
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
two parsers' agreement, and a POSIX class walked around it with a pattern whose
groups nested six deep for `regex` (100^6) while the standard tree saw
siblings. `regex` parses exactly as `regex.compile` would (_check_regex_tree)
and parsing expands nothing, so every rule holds on the tree that is actually
compiled; approximate matching and calls to a group found there are refused.

Only then `regex.compile`, inside `except Exception` (a RecursionError or an
OverflowError is not a regex.error). Tests of these refusals patch
`regex.compile` with a sentinel that fails if it is called: never compile a
refused pattern for real to "see what happens".

**Matching** goes through `search` / `find_all` with a `Budget` (a deadline):
each call gets `timeout=min(PATTERN_TIMEOUT, what is left)` and
`concurrent=True` (the GIL is released while it runs), and a timeout or a
spent budget is a PatternError « … est trop lent », never a hang. `regex`'s
finditer is lazy, so find_all builds its list inside the try.

**Numbers and dates** read from a slip are bounded to the columns they go
into (`read_amount`, `read_quantity`, `read_date`, `read_time`): a figure
wider than the column is not read - saved, it would raise on every later
read of the row (SQLite quantizes decimals on the way out).

Pure: no model, no request. `reading.py` reads a slip with these; the forms
and « Données » check a pattern with `compile_field` / `compile_pattern`.
"""

from __future__ import annotations

import functools
import logging
import re
import time as clock
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from re import _constants as sre_constants  # ty: ignore[unresolved-import]  # the motif guard reads it on purpose
from re import _parser as sre_parser  # ty: ignore[unresolved-import]  # the motif guard reads it on purpose

import regex
from django.utils import timezone
from regex import _regex_core

from common import read_amount, read_number  # noqa: F401 - read_amount is this module's interface too

logger = logging.getLogger(__name__)

#: Seconds one match may run.
PATTERN_TIMEOUT = 0.25
#: A reading (read_slip_text, « Tester »), a page's classification, and
#: « Relire » over a whole format: the time each may spend matching.
READING_SECONDS = 2.0
PAGE_SECONDS = 1.0
REREAD_SECONDS = 30.0

#: A pattern's own length, unless its field says otherwise.
MAX_PATTERN_LENGTH = 300
#: A repetition's count ({n}, {n,m}, {n,}) - its min and its bounded max.
MAX_REPEAT = 100
#: What the counts of repetitions nested in one another may multiply to.
MAX_REPEAT_PRODUCT = 1_000
#: Groups inside groups.
MAX_GROUP_DEPTH = 20
#: A « one per line » field of a format, and a type's patterns.
MAX_PATTERNS = 10
MAX_TYPE_PATTERNS = 50
#: A mail header is matched on this many characters at most.
MAIL_TEXT_LIMIT = 500

#: How every pattern is compiled: case never matters, ^ and $ are a line's ends.
FLAGS = regex.IGNORECASE | regex.MULTILINE

#: A quantity read from a slip (SlipLine.quantity).
MAX_QUANTITY = 99_999
#: The oldest date a reading accepts, and how far past today.
OLDEST_DATE = date(2000, 1, 1)
FUTURE_DAYS = 7

#: The address a sender pattern must NOT match (it would match everybody).
SENDER_PROBE = "quelquun@example.invalid"

_REPEATS = (sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT, sre_constants.POSSESSIVE_REPEAT)
_UNBOUNDED = sre_constants.MAXREPEAT

#: What may follow a `{` that is a count: {3} {3,} {3,5} {,5}.
_COUNT_TAIL = re.compile(r"[0-9]+(?:,[0-9]*)?\}|,[0-9]+\}")


class PatternError(ValueError):
    """A pattern refused, or too slow: `message` is the French sentence, the
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
                reason += " " + message[len(start) :].strip()
            break
    if isinstance(position, int) and position >= 0:
        # 1-based: « position 14 » is the fourteenth character of the pattern.
        reason += f" (position {position + 1})"
    return reason


# -- The check ------------------------------------------------------------------------------------------------------


def _scan_problem(pattern: str) -> tuple[str, int] | None:
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
    the rest of the pattern, braces included."""
    index, length = 0, len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if pattern.startswith("(?#", index):
            index += 3
            while index < length and pattern[index] != ")":
                index += 2 if pattern[index] == "\\" else 1
            index += 1
            continue
        if char == "[":
            inside = index + 1
            if inside < length and pattern[inside] == "^":
                inside += 1
            if inside < length and pattern[inside] == "]":
                inside += 1
            while inside < length and pattern[inside] != "]":
                if pattern[inside] == "\\":
                    inside += 2
                    continue
                if pattern[inside] == "[":
                    return "set", inside
                inside += 1
            index = inside + 1
            continue
        if char == "{" and not _COUNT_TAIL.match(pattern, index + 1):
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
#: More nodes than a 500-character pattern can make: a cycle, or worse.
MAX_TREE_NODES = 20_000


def _regex_tree(pattern: str):
    """`regex`'s parse of `pattern`, as `regex.compile(pattern, FLAGS)` would
    parse it (regex/_main.py _compile), and nothing more."""
    flags = FLAGS | regex.VERSION0
    for _attempt in range(3):
        source = _regex_core.Source(pattern)
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


def _check_regex_tree(pattern: str) -> None:
    """Steps 5's rules on `regex`'s own tree - _Refused when broken."""
    try:
        parsed = _regex_tree(pattern)
    except _Refused:
        raise
    except Exception:  # noqa: BLE001 - regex.error, RecursionError on a deep pattern, ...: a refusal, never a 500
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
    counts above it and its depth in groups - iteratively: a deep pattern
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
def _checked(pattern: str, required_groups: tuple, max_length: int):
    """Steps 2 to 7 of the check, cached: only a pattern that passed is kept
    (lru_cache does not keep an exception). It holds compiled patterns and
    no tenant's data, so it needs no tenant key."""
    _check_shape(pattern)

    # 6. Only now, compiled - anything it raises is a refusal, not a 500.
    try:
        compiled = regex.compile(pattern, FLAGS)
    except Exception as error:  # noqa: BLE001 - anything compile raises is a refusal, not a 500
        raise _Refused(_translate(getattr(error, "msg", ""), None)) from None

    # 7. A pattern that finds something on an empty line finds it everywhere.
    try:
        empty = compiled.search("", timeout=PATTERN_TIMEOUT, concurrent=True)
    except TimeoutError:
        raise _Refused("le motif est trop lent") from None
    if empty is not None:
        raise _Refused("le motif accepte une ligne vide : il trouverait quelque chose sur n'importe quelle ligne")
    if any(name not in compiled.groupindex for name in required_groups):
        raise _Refused(f"le motif doit contenir {_groups_sentence(required_groups)}")
    return compiled


def _check_shape(pattern: str) -> None:
    """Steps 2 to 5b: the pattern's shape, read without compiling it -
    _Refused when a rule is broken."""
    # 2. The standard parser shows the pattern's shape and expands nothing.
    try:
        parsed = sre_parser.parse(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as error:
        raise _Refused(_translate(error.msg, error.pos)) from None
    except Exception:  # noqa: BLE001 - whatever the parser raises is a refusal in French, never a 500
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
    problem = _scan_problem(pattern)
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
    _check_regex_tree(pattern)


def compile_pattern(text, *, field_label: str, required_groups=(), max_length: int = MAX_PATTERN_LENGTH):
    """The pattern `text` (stripped), checked then compiled with
    IGNORECASE | MULTILINE - or PatternError, a French sentence starting with
    `field_label`. A blank pattern is refused here: whether a field may be
    blank is its caller's to decide, before calling (`compile_field` does)."""
    pattern = (text or "").strip()
    if not pattern:
        raise PatternError(f"{field_label} : le motif est vide.")
    if len(pattern) > max_length:
        raise PatternError(f"{field_label} : {max_length} caractères au plus ({len(pattern)} ici).")
    try:
        return _checked(pattern, tuple(required_groups), max_length)
    except _Refused as refused:
        raise PatternError(f"{field_label} : {refused.reason}.") from None


def check_lines(
    text, *, field_label: str, required_groups=(), max_length: int = MAX_PATTERN_LENGTH, max_lines: int = MAX_PATTERNS
) -> list:
    """A « one per line » field: every non-blank line is a pattern, checked
    like `compile_pattern`; returns the compiled list, in order ([] when
    blank). A refusal names its line when there are several."""
    lines = [(number, line.strip()) for number, line in enumerate((text or "").splitlines(), start=1)]
    pattern_lines = [(number, line) for number, line in lines if line]
    if len(pattern_lines) > max_lines:
        raise PatternError(f"{field_label} : {max_lines} motifs au plus, un par ligne ({len(pattern_lines)} ici).")
    several = len(pattern_lines) > 1
    return [
        compile_pattern(
            line,
            field_label=f"{field_label} (ligne {number})" if several else field_label,
            required_groups=required_groups,
            max_length=max_length,
        )
        for number, line in pattern_lines
    ]


def check_sender_pattern(text) -> None:
    """A format's sender pattern (when set) must designate an address or a
    domain: it holds a literal « @ » and does not match anybody's address."""
    pattern = compile_pattern(text, field_label=FIELD_BY_ATTR["sender_pattern"].label)
    message = "Le motif d'expéditeur doit désigner une adresse ou un domaine."
    if "@" not in text:
        raise PatternError(message)
    try:
        matched = pattern.search(SENDER_PROBE, timeout=PATTERN_TIMEOUT, concurrent=True)
    except TimeoutError:
        raise PatternError(f"{FIELD_BY_ATTR['sender_pattern'].label} : le motif est trop lent.") from None
    if matched is not None:
        raise PatternError(message)


# -- The patterns of a format and of a type -------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternField:
    """One pattern field of a format (or a type's `slip_patterns`): what its
    patterns must contain and how long they may be. `several`: one pattern per
    line, at most `max_lines`. `required`: the form refuses it blank (not
    enforced by compile_field, which returns [] for a blank field)."""

    attr: str
    label: str
    groups: tuple = ()
    max_length: int = MAX_PATTERN_LENGTH
    several: bool = False
    max_lines: int = MAX_PATTERNS
    required: bool = False


FORMAT_FIELDS = (
    PatternField("sender_pattern", "Motif d'expéditeur"),
    PatternField("subject_pattern", "Motif d'objet"),
    PatternField("attachment_pattern", "Motif de pièce jointe", max_length=200),
    PatternField("section_start", "Début de la partie"),
    PatternField("section_end", "Fin de la partie"),
    PatternField("line_pattern", "Motif de ligne", groups=("designation", "quantite"), max_length=500, required=True),
    PatternField("date_patterns", "Motif de date", groups=("date",), several=True, required=True),
    PatternField("printed_patterns", "Motif d'impression", groups=("date",), several=True),
    PatternField("number_patterns", "Motif de numéro", groups=("numero",), several=True),
    PatternField("reference_patterns", "Motif de référence", groups=("reference",), several=True),
    PatternField("replaces_pattern", "Motif « annule et remplace »"),
    PatternField("total_patterns", "Motif de total", groups=("total",), several=True),
    PatternField("remarks_start", "Début des remarques"),
    PatternField("remarks_end", "Fin des remarques"),
)
FIELD_BY_ATTR = {field.attr: field for field in FORMAT_FIELDS}
#: The fields a reading uses (not the mail ones: a bad sender pattern must not
#: stop a slip from being read).
READING_FIELDS = tuple(
    field for field in FORMAT_FIELDS if field.attr not in ("sender_pattern", "subject_pattern", "attachment_pattern")
)
TYPE_FIELD = PatternField("slip_patterns", "Motifs des bons", several=True, max_lines=MAX_TYPE_PATTERNS)


def compile_field(field: PatternField, value) -> list:
    """The compiled patterns of one field's value: [] when blank, a list of
    one for a single-pattern field, PatternError at the first refusal."""
    if not (value or "").strip():
        return []
    if field.several:
        return check_lines(
            value,
            field_label=field.label,
            required_groups=field.groups,
            max_length=field.max_length,
            max_lines=field.max_lines,
        )
    return [compile_pattern(value, field_label=field.label, required_groups=field.groups, max_length=field.max_length)]


def compile_format(fmt, fields=READING_FIELDS) -> dict:
    """{attr: [compiled patterns]} for `fmt` - any object carrying the
    attributes (a SlipFormat, a form's cleaned data as a namespace, a
    test's SimpleNamespace); a missing attribute is blank. PatternError at
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


def shown_pattern(pattern) -> str:
    text = getattr(pattern, "pattern", str(pattern))
    return text if len(text) <= 60 else text[:59] + "…"


def _too_slow(pattern) -> PatternError:
    return PatternError(f"Le motif « {shown_pattern(pattern)} » est trop lent : simplifiez-le.")


def _timeout(pattern, budget: Budget) -> float:
    remaining = budget.remaining()
    if remaining <= 0:
        raise _too_slow(pattern)
    return min(PATTERN_TIMEOUT, remaining)


def search(pattern, text: str, budget: Budget):
    """pattern.search(text) within the budget: the match or None, or
    PatternError « … est trop lent »."""
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
    """A group's value, stripped - None when the pattern has no such group,
    when the group took no part in the match, or when it is blank: a blank
    capture is « not read », and the next line or pattern is tried."""
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
    gather), logged and counted (`timed_out`): find_matching_emails takes
    the search for an incomplete one - a mail left out for its pattern's
    time is not one that does not match."""

    def __init__(self, pattern, log=None):
        self._pattern = pattern
        self._log = log
        self.pattern = pattern.pattern
        self.timed_out = 0

    def limits(self) -> tuple[int, float]:
        """(characters matched, seconds a match may take)."""
        return MAIL_TEXT_LIMIT, PATTERN_TIMEOUT

    def search(self, text):
        text_limit, timeout = self.limits()
        try:
            return self._pattern.search((text or "")[:text_limit], timeout=timeout, concurrent=True)
        except TimeoutError:
            self.timed_out += 1
            message = (
                f"motif trop lent sur un mail, laissé pour la prochaine recherche ({shown_pattern(self._pattern)})"
            )
            logger.warning(message)
            if self._log is not None:
                self._log(message)
            return None


def mail_matcher(text, *, log=None) -> MailMatcher:
    """The `compile` a format's mail patterns are handed to
    find_matching_emails with: the pattern checked like any other (PatternError
    otherwise), case-insensitive, timed."""
    return MailMatcher(compile_pattern(text, field_label="Motif de mail"), log)


#: An invoice source's mail pattern: its own length (the model's column),
#: the body it is matched on, and the time one match may take.
INVOICE_MAIL_PATTERN_LENGTH = 500
INVOICE_MAIL_TEXT_LIMIT = 200_000
INVOICE_MAIL_TIMEOUT = 1.0


@functools.lru_cache(maxsize=256)
def _invoice_mail_compiled(pattern: str):
    _check_shape(pattern)
    try:
        # No flag: an invoice source's patterns were always matched like
        # `re.compile(pattern)` - case-sensitive unless they say (?i), and a
        # blank-matching « .* » is accepted.
        return regex.compile(pattern)
    except Exception as error:  # noqa: BLE001 - anything compile raises is a refusal, not a 500
        raise _Refused(_translate(getattr(error, "msg", ""), None)) from None


def check_invoice_mail_pattern(text, *, field_label: str = "Motif de mail"):
    """An invoice source's pattern (EmailInvoiceSource) checked by the motif
    guard and compiled with `re`'s meaning - or PatternError. It used to be
    a bare `re.compile`: no shape check, no timeout, on a body anybody on the
    internet writes (security audit 04/10/2026)."""
    pattern = (text or "").strip()
    if not pattern:
        raise PatternError(f"{field_label} : le motif est vide.")
    if len(pattern) > INVOICE_MAIL_PATTERN_LENGTH:
        raise PatternError(f"{field_label} : {INVOICE_MAIL_PATTERN_LENGTH} caractères au plus ({len(pattern)} ici).")
    try:
        return _invoice_mail_compiled(pattern)
    except _Refused as refused:
        raise PatternError(f"{field_label} : {refused.reason}.") from None


class InvoiceMailMatcher(MailMatcher):
    """`.search(text)` for an invoice source: the first
    INVOICE_MAIL_TEXT_LIMIT characters (a body, not only a header), timed; a
    timeout is « no match », logged and counted as MailMatcher's."""

    def limits(self) -> tuple[int, float]:
        return INVOICE_MAIL_TEXT_LIMIT, INVOICE_MAIL_TIMEOUT


def invoice_mail_matcher(text, *, field_label: str = "Motif de mail", log=None) -> InvoiceMailMatcher:
    """The `compile` an invoice source's patterns are handed to
    find_matching_emails with (the gather and « Tester »). `field_label`:
    which of the source's patterns it is, named by a refusal."""
    return InvoiceMailMatcher(check_invoice_mail_pattern(text, field_label=field_label), log)


# -- Numbers, dates, times ------------------------------------------------------------------------------------------


def read_quantity(text) -> int | None:
    """A whole quantity (« 4,000 » is 4), |q| ≤ 99 999, else None."""
    number = read_number(text)
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
