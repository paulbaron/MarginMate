"""returnables/patterns.py: the guard in front of `regex.compile`, the
budgets, and the numbers and dates a slip's patterns capture.

MACHINE SAFETY. `regex.compile` of `(?:x{65535}){65535}` allocated about
50 GB and froze the owner's PC twice (29/09). Every refusal of the guard is
tested with `regex.compile` replaced by `NeverCompile`, which records a call
instead of compiling: a pattern the guard lets through by mistake is then a
failed assertion, not a frozen machine. No test here compiles a large
repetition or runs a real pathological match - timeouts are simulated with
a stand-in pattern whose search raises TimeoutError.
"""

from datetime import date, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import regex
from django.test import SimpleTestCase
from django.utils import timezone

from returnables import patterns
from returnables.patterns import (
    FORMAT_FIELDS,
    TYPE_FIELD,
    Budget,
    PatternError,
    aware_datetime,
    captured,
    check_lines,
    check_sender_pattern,
    compile_field,
    compile_format,
    compile_pattern,
    find_all,
    mail_matcher,
    read_amount,
    read_date,
    read_quantity,
    read_time,
    search,
)
from returnables.tests import texts

#: The patterns whose compilation froze the machine, or would have (spec §9).
DANGEROUS = (
    (r"(?:x{65535}){65535}", "répétition trop grande"),
    (r"((a{1000}){1000}){1000}", "répétition trop grande"),
    (r"(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}", "le mode (?x) n'est pas accepté dans un motif"),
    (r"(?x:a{1 0 0})", "le mode (?x) n'est pas accepté dans un motif"),
    (r"a{e<=1}", "accolade : écrivez \\{ pour une accolade littérale"),
    (r"(?:(?:(?:x{100,}){100,}){100,}){100,}", "répétition trop grande"),
)


class NeverCompile:
    """Stands in for regex.compile: records the call and refuses to compile."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        raise AssertionError("regex.compile called on a motif the guard had to refuse")


class GuardedTestCase(SimpleTestCase):
    """regex.compile is the sentinel for every test of the class; the cache
    of checked patterns starts empty."""

    def setUp(self):
        super().setUp()
        patterns._checked.cache_clear()
        self.addCleanup(patterns._checked.cache_clear)
        self.never = NeverCompile()
        patcher = mock.patch.object(regex, "compile", new=self.never)
        patcher.start()
        self.addCleanup(patcher.stop)

    def refused(self, pattern, **kwargs) -> str:
        kwargs.setdefault("field_label", "Motif de ligne")
        with self.assertRaises(PatternError) as caught:
            compile_pattern(pattern, **kwargs)
        self.assertEqual(self.never.calls, [], f"{pattern!r} reached regex.compile")
        return caught.exception.message


class TheDangerousPatternsAreRefusedBeforeCompilingTests(GuardedTestCase):
    def test_the_six_patterns_of_the_freeze_are_refused_without_compiling(self):
        for pattern, reason in DANGEROUS:
            with self.subTest(pattern=pattern):
                message = self.refused(pattern)
                self.assertTrue(message.startswith("Motif de ligne : "), message)
                self.assertIn(reason, message)

    def test_they_are_refused_in_a_one_per_line_field_too(self):
        # First line: the lines are checked in order, and a harmless second
        # one would be compiled - by the sentinel.
        for pattern, reason in DANGEROUS:
            with self.subTest(pattern=pattern):
                with self.assertRaises(PatternError) as caught:
                    check_lines(f"\n{pattern}\nBL No", field_label="Motif de date")
                self.assertIn("Motif de date (ligne 2) : ", caught.exception.message)
                self.assertIn(reason, caught.exception.message)
        self.assertEqual(self.never.calls, [])

    def test_a_repetition_over_a_hundred_is_refused(self):
        for pattern in ("a{101}", "a{101,}", "a{0,101}", "a{,101}", "a{5,200}?", "a{150}+"):
            with self.subTest(pattern=pattern):
                self.assertIn("répétition trop grande : 100 fois au plus", self.refused(pattern))

    def test_nested_repetitions_multiplying_past_a_thousand_are_refused(self):
        for pattern in ("(?:(?:a{10}){10}){11}", "(?:a{50}){21}", "(?:(?:a+){100}){11}", "(?>(?:a{40}){30})"):
            with self.subTest(pattern=pattern):
                self.assertIn("dépassent 1000 en tout", self.refused(pattern))

    def test_a_repetition_inside_a_lookaround_or_a_condition_is_counted(self):
        for pattern in (
            "(?=(?:a{50}){50})b",
            "(?<!(?:a{50}){50})b",
            "(a)?(?(1)(?:b{50}){50}|c)",
            "(a)?(?(1)c|(?:b{50}){50})",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn("répétition trop grande", self.refused(pattern))

    def test_groups_nested_deeper_than_twenty_are_refused(self):
        pattern = "(" * 21 + "a" + ")" * 21
        self.assertIn("plus de 20 groupes", self.refused(pattern))

    def test_verbose_in_every_form_is_refused(self):
        for pattern in ("(?x)abc", "(?ix)abc", "(?x:abc)", "a(?x:b c)d", "(?i:(?x:a))"):
            with self.subTest(pattern=pattern):
                self.assertIn("le mode (?x) n'est pas accepté dans un motif", self.refused(pattern))

    def test_a_brace_that_is_not_a_count_is_refused_with_its_position(self):
        for pattern, position in (
            ("a{e<=1}", 2),
            ("ab{1 0}", 3),
            ("a{}", 2),
            ("a{,}", 2),
            ("{", 1),
            ("x{2,5}{e}", 7),
            ("[a]{i<=1}", 4),
            (r"\N{LATIN SMALL LETTER A}", 3),
            ("(?:ab){s<=2}", 7),
        ):
            with self.subTest(pattern=pattern):
                message = self.refused(pattern)
                self.assertIn(r"accolade : écrivez \{ pour une accolade littérale", message)
                self.assertIn(f"(position {position})", message)

    def test_too_long_and_blank_patterns_are_refused(self):
        self.assertEqual(self.refused("a" * 301), "Motif de ligne : 300 caractères au plus (301 ici).")
        self.assertEqual(self.refused("a" * 201, max_length=200), "Motif de ligne : 200 caractères au plus (201 ici).")
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                self.assertEqual(self.refused(blank), "Motif de ligne : le motif est vide.")


def _posix_bypass(count: int) -> str:
    """The review's pattern (29/09): flat for the standard parser (each
    `[[:alpha:]` set ends at its first `]`, then `)(` closes a group and opens
    the next), nested six deep for `regex` (`[[:alpha:])(]` is ONE set). At
    count 100 that was 100^6 repetitions reaching regex.compile."""
    set_ = "[[:alpha:])(]"
    return "(?:" * 5 + f"x{{{count}}}{set_}" + f"){{{count}}}{set_}" * 4 + f"){{{count}}}"


class TheTwoParsersCannotDisagreeTests(GuardedTestCase):
    """The guard read the STANDARD parser's tree, and `regex` compiles its
    own: every disagreement between the two was a way around it (review,
    29/09). Now a `[` inside a set is refused, comments are skipped as both
    parsers skip them, and the repetitions are measured again on `regex`'s
    own parse - which expands nothing, so these tests parse the dangerous
    patterns for real and still compile nothing."""

    def test_the_posix_class_bypass_of_the_review_is_refused(self):
        pattern = _posix_bypass(100)
        self.assertEqual(
            pattern,
            "(?:(?:(?:(?:(?:x{100}[[:alpha:])(]){100}[[:alpha:])(]){100}[[:alpha:])(]){100}"
            "[[:alpha:])(]){100}[[:alpha:])(]){100}",
        )
        self.assertIn("crochet [ dans un ensemble", self.refused(pattern))

    def test_every_nested_set_is_refused_wherever_the_bracket_sits(self):
        # Wrapped in a group so that BOTH parsers read them as balanced (on
        # its own `[^[:alpha:])(]x` is already refused by the standard
        # parser, for its stray parenthesis) and the scan is what refuses.
        for pattern in (
            "[[:alpha:]]x",
            "(?:[^[:alpha:])(]x)",
            "(?:[a[:alpha:])(]x)",
            "(?:[a[:digit:])(]x)",
            "a[b[c]d",
            "[]a[]",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn("crochet [ dans un ensemble [...] : écrivez \\[", self.refused(pattern))

    def test_a_comment_hides_no_brace(self):
        message = self.refused("(?#[)abc{e<=1}d(?#])")
        self.assertIn(r"accolade : écrivez \{ pour une accolade littérale", message)

    def test_regex_s_own_tree_stops_the_bypass_even_without_the_character_scan(self):
        # The second line of defence on its own: with the scan switched off,
        # the review's pattern still never reaches the compiler, because the
        # tree regex builds shows the six nested repetitions.
        with mock.patch.object(patterns, "_scan_problem", return_value=None):
            message = self.refused(_posix_bypass(100))
        self.assertIn("répétition trop grande", message)

    def test_the_regex_tree_counts_repetitions_like_the_standard_one(self):
        for pattern in ("x{101}", "x{0,101}", "x{101,}", "(?:(?:x{20}){20}){20}", "(?:(?:a+){100}){11}"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(patterns._Refused) as caught:
                    patterns._check_regex_tree(pattern)
                self.assertIn("répétition trop grande", caught.exception.reason)
        for pattern in ("(?:x{10}){10}", "x{100}", "a+b*c?", texts.UBA_PATTERNS["line_pattern"]):
            with self.subTest(pattern=pattern):
                patterns._check_regex_tree(pattern)
        self.assertEqual(self.never.calls, [])

    def test_approximate_matching_and_group_calls_found_by_regex_are_refused(self):
        for pattern in ("(?:abc){e<=1}", "a(?R)?b", "(a)(?1)", "(?P<n>a)(?&n)"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(patterns._Refused) as caught:
                    patterns._check_regex_tree(pattern)
                self.assertIn("recherche approchée ou appel de groupe", caught.exception.reason)
        self.assertEqual(self.never.calls, [])

    def test_the_private_names_of_regex_the_guard_relies_on_still_exist(self):
        # A `regex` upgrade that moves them must fail HERE, not open the guard.
        core = patterns._regex_core
        for name in (
            "Source",
            "Info",
            "_parse_pattern",
            "_UnscopedFlagSet",
            "GreedyRepeat",
            "LazyRepeat",
            "PossessiveRepeat",
            "Fuzzy",
            "CallGroup",
            "CallRef",
            "RegexBase",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(core, name), name)
        self.assertTrue(issubclass(core.LazyRepeat, core.GreedyRepeat))
        self.assertTrue(issubclass(core.PossessiveRepeat, core.GreedyRepeat))
        repeats = []
        stack = [patterns._regex_tree("ab{2,5}c+")]
        while stack:
            node = stack.pop()
            if isinstance(node, core.GreedyRepeat):
                repeats.append((node.min_count, node.max_count))
            stack.extend(patterns._rx_children(node))
        self.assertEqual(sorted(repeats, key=str), sorted([(2, 5), (1, None)], key=str))


class EscapedBracketsAndCommentsAreAcceptedTests(SimpleTestCase):
    """What the new refusals must not catch - compiled for real (tiny)."""

    def test_an_escaped_bracket_in_a_set_and_a_comment_are_fine(self):
        patterns._checked.cache_clear()
        for pattern, line in (
            (r"[\[]x", "[x"),
            (r"[a\[]+", "a[a"),
            ("(?#une note)FUT", "FUT 30 L"),
            (r"(?#[\)]{)FUT", "FUT"),
        ):
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(search(compile_pattern(pattern, field_label="Motif"), line, Budget(1)))


class SyntaxErrorsAreFrenchWithTheirPositionTests(GuardedTestCase):
    def test_each_stdlib_error_is_translated(self):
        cases = (
            ("abc(de", "parenthèse non fermée (position 4)"),
            ("abc)", "parenthèse fermante sans parenthèse ouvrante (position 4)"),
            ("*a", "rien à répéter avant ce signe"),
            (r"a\q", r"échappement inconnu \q (position 2)"),
            ("[ab", "crochet non fermé (position 1)"),
            ("a**", "deux signes de répétition à la suite (position 3)"),
            ("(?P<x>a)(?P<x>b)", "nom de groupe utilisé deux fois"),
            ("(?P=y)", "nom de groupe inconnu"),
            ("(?P<1>a)", "nom de groupe invalide"),
            ("b{3,2}", "répétition {min,max} dont le min dépasse le max"),
            ("[z-a]", "intervalle de caractères à l'envers"),
            ("(?Q)", "construction (?…) inconnue"),
            ("a(?i)b", "une option comme (?i) se met au tout début du motif"),
            (r"(a)\2", "référence à un groupe qui n'existe pas"),
        )
        for pattern, reason in cases:
            with self.subTest(pattern=pattern):
                message = self.refused(pattern)
                self.assertIn(reason, message)
                self.assertTrue(message.startswith("Motif de ligne : "))

    def test_the_spec_example_reads_as_written(self):
        # « Motif de ligne : parenthèse non fermée (position 14) »: the 14th character is the unclosed one.
        self.assertEqual(self.refused("^(?P<qte>\\d+)(abc"), "Motif de ligne : parenthèse non fermée (position 14).")

    def test_any_other_parser_exception_is_a_refusal_not_a_crash(self):
        for error in (RecursionError("deep"), OverflowError("big"), KeyError("x")):
            with self.subTest(error=type(error).__name__):
                patterns._checked.cache_clear()
                with mock.patch.object(patterns.sre_parser, "parse", side_effect=error):
                    self.assertEqual(self.refused("abc"), "Motif de ligne : motif invalide.")


class TheCompilerItselfIsContainedTests(SimpleTestCase):
    def setUp(self):
        patterns._checked.cache_clear()
        self.addCleanup(patterns._checked.cache_clear)

    def test_whatever_regex_compile_raises_is_a_pattern_error(self):
        for error in (RecursionError("deep"), OverflowError("big"), MemoryError(), regex.error("bad")):
            with self.subTest(error=type(error).__name__):
                patterns._checked.cache_clear()
                with mock.patch.object(regex, "compile", side_effect=error):
                    with self.assertRaises(PatternError) as caught:
                        compile_pattern("abc", field_label="Motif de ligne")
                self.assertEqual(caught.exception.message, "Motif de ligne : motif invalide.")

    def test_a_pattern_is_compiled_once_ignorecase_and_multiline(self):
        real = regex.compile
        with mock.patch.object(regex, "compile", wraps=real) as spy:
            first = compile_pattern("^Deconsigne", field_label="Motif de total A")
            second = compile_pattern("  ^Deconsigne  ", field_label="Motif de total B")
        self.assertIs(first, second)
        spy.assert_called_once_with("^Deconsigne", patterns.FLAGS)
        self.assertTrue(first.flags & regex.IGNORECASE and first.flags & regex.MULTILINE)
        self.assertTrue(first.search("x\ndeconsigne : 3"))

    def test_the_cache_is_bounded(self):
        self.assertEqual(patterns._checked.cache_info().maxsize, 256)

    def test_a_refused_pattern_is_not_cached(self):
        for _attempt in range(2):
            with self.assertRaises(PatternError):
                compile_pattern("(ab", field_label="Motif")
        self.assertEqual(patterns._checked.cache_info().currsize, 0)


class AcceptedPatternsTests(SimpleTestCase):
    """These compile for real: each is a few characters."""

    def test_literal_braces_and_counts_are_accepted(self):
        for pattern, line in (
            (r"\{", "a{b"),
            (r"[{]", "a{b"),
            (r"x{2,5}", "axxxb"),
            (r"x{3}", "xxx"),
            (r"x{2,}", "xx"),
            (r"x{,5}y", "y"),
            (r"[]{]", "]"),
            (r"[^]{]", "a"),
            (r"[\]{]", "{"),
            (r"(?:ab){100}", "ab" * 100),
            (r"(?:a{10}){100}", "a" * 1000),
        ):
            with self.subTest(pattern=pattern):
                compiled = compile_pattern(pattern, field_label="Motif")
                self.assertTrue(compiled.search(line))

    def test_the_seeded_patterns_pass_every_rule(self):
        compiled = compile_format(texts.UBA_RULES, FORMAT_FIELDS)
        self.assertEqual(set(compiled), {field.attr for field in FORMAT_FIELDS})
        self.assertEqual(len(compiled["date_patterns"]), 2)
        for _name, _position, slip_patterns in texts.SEED_TYPES:
            self.assertEqual(len(compile_field(TYPE_FIELD, slip_patterns)), 1)

    def test_case_does_not_matter(self):
        pattern = compile_pattern(texts.SEED_TYPES[0][2], field_label="Motifs des bons")
        for designation in (texts.KEG, "fût 20 l", "FUTS", "Fut"):
            with self.subTest(designation=designation):
                self.assertTrue(pattern.search(designation))
        self.assertIsNone(pattern.search("REFUTATION"))

    def test_a_pattern_matching_an_empty_line_is_refused(self):
        for pattern in (".*", "^$", "a?", r"\s*", "(?P<designation>.*)(?P<quantite>\\d*)", "x|"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(PatternError) as caught:
                    compile_pattern(pattern, field_label="Début de la partie")
                self.assertIn("le motif accepte une ligne vide", caught.exception.message)

    def test_a_missing_required_group_is_refused_naming_them_all(self):
        with self.assertRaises(PatternError) as caught:
            compile_pattern(
                r"^(?P<designation>.+) (\d+)$",
                field_label="Motif de ligne",
                required_groups=("designation", "quantite"),
            )
        self.assertEqual(
            caught.exception.message,
            "Motif de ligne : le motif doit contenir (?P<designation>…) et (?P<quantite>…).",
        )
        with self.assertRaises(PatternError) as caught:
            compile_field(patterns.FIELD_BY_ATTR["total_patterns"], r"Total : (\d+)")
        self.assertIn("le motif doit contenir (?P<total>…)", caught.exception.message)


class FieldsTests(SimpleTestCase):
    def test_a_blank_field_is_no_pattern(self):
        for field in FORMAT_FIELDS + (TYPE_FIELD,):
            with self.subTest(field=field.attr):
                self.assertEqual(compile_field(field, ""), [])
                self.assertEqual(compile_field(field, None), [])
                self.assertEqual(compile_field(field, " \n \n"), [])

    def test_a_one_per_line_field_holds_ten_patterns_at_most(self):
        field = patterns.FIELD_BY_ATTR["date_patterns"]
        ten = "\n".join(rf"D{number}\s+(?P<date>\S+)" for number in range(10))
        self.assertEqual(len(compile_field(field, ten)), 10)
        with self.assertRaises(PatternError) as caught:
            compile_field(field, ten + "\nD10 (?P<date>x)")
        self.assertEqual(caught.exception.message, "Motif de date : 10 motifs au plus, un par ligne (11 ici).")

    def test_a_type_holds_fifty_patterns_at_most(self):
        fifty = "\n".join(f"MOTIF{number}" for number in range(50))
        self.assertEqual(len(compile_field(TYPE_FIELD, fifty)), 50)
        with self.assertRaises(PatternError) as caught:
            compile_field(TYPE_FIELD, fifty + "\nMOTIF50")
        self.assertIn("50 motifs au plus", caught.exception.message)

    def test_blank_lines_are_skipped_and_a_refusal_names_its_line(self):
        self.assertEqual(len(check_lines("\nA\n\n  \nB\n", field_label="Motifs")), 2)
        with self.assertRaises(PatternError) as caught:
            check_lines("A\n\nB(", field_label="Motifs des bons")
        self.assertTrue(caught.exception.message.startswith("Motifs des bons (ligne 3) : parenthèse non fermée"))
        with self.assertRaises(PatternError) as caught:
            check_lines("\nB(", field_label="Motifs des bons")
        self.assertTrue(caught.exception.message.startswith("Motifs des bons : parenthèse non fermée"))

    def test_each_field_has_its_own_length(self):
        line = patterns.FIELD_BY_ATTR["line_pattern"]
        long_line = "^(?P<designation>A+) (?P<quantite>\\d+)" + "B" * 460
        self.assertLessEqual(len(long_line), 500)
        self.assertEqual(len(compile_field(line, long_line)), 1)
        with self.assertRaises(PatternError):
            compile_field(line, long_line + "B" * 10)
        with self.assertRaises(PatternError):
            compile_field(patterns.FIELD_BY_ATTR["attachment_pattern"], "a" * 201)

    def test_compile_format_reads_any_object_and_stops_at_the_first_refusal(self):
        self.assertEqual(
            compile_format(SimpleNamespace(line_pattern=texts.UBA_PATTERNS["line_pattern"]))["date_patterns"], []
        )
        with self.assertRaises(PatternError) as caught:
            compile_format(texts.uba_rules(total_patterns="Deconsigne (?P<total>"))
        self.assertTrue(caught.exception.message.startswith("Motif de total : parenthèse non fermée"))

    def test_the_mail_patterns_do_not_stop_a_reading(self):
        self.assertIn("line_pattern", compile_format(texts.uba_rules(sender_pattern="(")))

    def test_a_sender_pattern_designates_an_address_or_a_domain(self):
        check_sender_pattern(r"livreur@exemple\.invalid")
        check_sender_pattern(r"@exemple\.invalid$")
        for pattern in ("livreur", r".+@.+", r"@", r"quelquun@"):
            with self.subTest(pattern=pattern):
                with self.assertRaises(PatternError) as caught:
                    check_sender_pattern(pattern)
                self.assertEqual(
                    caught.exception.message, "Le motif d'expéditeur doit désigner une adresse ou un domaine."
                )


class SlowPattern:
    """A compiled pattern whose every search runs out of time - a real
    catastrophic match is never run on the owner's machine."""

    pattern = "(a+)+$"
    groupindex = {}

    def __init__(self):
        self.calls = []

    def search(self, text, **kwargs):
        self.calls.append((text, kwargs))
        raise TimeoutError("regex timed out")

    def finditer(self, text, **kwargs):
        self.calls.append((text, kwargs))

        def matches():
            raise TimeoutError("regex timed out")
            yield  # pragma: no cover

        return matches()


class RecordingPattern:
    """A compiled pattern that answers `found` and records how it was called."""

    pattern = "BL"

    def __init__(self, found=None):
        self.found = found
        self.calls = []

    def search(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return self.found


class BudgetTests(SimpleTestCase):
    def test_a_timeout_is_a_pattern_error_naming_the_pattern(self):
        slow = SlowPattern()
        with self.assertRaises(PatternError) as caught:
            search(slow, "aaaa", Budget(2))
        self.assertEqual(caught.exception.message, "Le motif « (a+)+$ » est trop lent : simplifiez-le.")

    def test_find_all_catches_the_timeout_raised_while_iterating(self):
        with self.assertRaises(PatternError) as caught:
            find_all(SlowPattern(), "aaaa", Budget(2))
        self.assertIn("est trop lent", caught.exception.message)

    def test_each_call_gets_the_smaller_of_the_timeout_and_what_is_left(self):
        pattern = RecordingPattern()
        search(pattern, "x", Budget(10))
        self.assertEqual(pattern.calls[-1][1], {"timeout": patterns.PATTERN_TIMEOUT, "concurrent": True})
        search(pattern, "x", Budget(0.1))
        self.assertLess(pattern.calls[-1][1]["timeout"], 0.11)
        self.assertGreater(pattern.calls[-1][1]["timeout"], 0)

    def test_a_spent_budget_refuses_without_matching(self):
        pattern = RecordingPattern()
        budget = Budget(0)
        self.assertTrue(budget.spent)
        self.assertEqual(budget.remaining(), 0)
        for call in (search, find_all):
            with self.subTest(call=call.__name__):
                with self.assertRaises(PatternError):
                    call(pattern, "x", budget)
        self.assertEqual(pattern.calls, [])

    def test_find_all_stops_at_its_limit(self):
        pattern = compile_pattern("a", field_label="Motif")
        self.assertEqual(len(find_all(pattern, "a" * 80, Budget(2))), 50)
        self.assertEqual(len(find_all(pattern, "a" * 80, Budget(2), limit=3)), 3)
        self.assertEqual(find_all(pattern, "bbb", Budget(2)), [])

    def test_the_budgets_are_the_specs(self):
        self.assertEqual(
            (patterns.PATTERN_TIMEOUT, patterns.READING_SECONDS, patterns.PAGE_SECONDS, patterns.REREAD_SECONDS),
            (0.25, 2.0, 1.0, 30.0),
        )


class MailMatcherTests(SimpleTestCase):
    def test_it_matches_like_a_compiled_pattern_ignoring_case(self):
        matcher = mail_matcher(r"livreur@exemple\.invalid")
        self.assertTrue(matcher.search("Le Livreur <LIVREUR@EXEMPLE.INVALID>"))
        self.assertIsNone(matcher.search("autre@exemple.invalid"))
        self.assertIsNone(matcher.search(None))
        self.assertEqual(matcher.pattern, r"livreur@exemple\.invalid")

    def test_it_looks_at_the_first_500_characters_only(self):
        recording = RecordingPattern()
        patterns.MailMatcher(recording).search("x" * 2000)
        self.assertEqual(len(recording.calls[0][0]), 500)
        self.assertEqual(recording.calls[0][1], {"timeout": patterns.PATTERN_TIMEOUT, "concurrent": True})

    def test_a_timeout_is_no_match_and_is_logged(self):
        said = []
        with self.assertLogs("returnables.patterns", level="WARNING") as logs:
            self.assertIsNone(patterns.MailMatcher(SlowPattern(), log=said.append).search("Livraison du 14/05/2025"))
        self.assertIn("motif trop lent sur un mail, laissé pour la prochaine recherche", said[0])
        self.assertIn("motif trop lent sur un mail, laissé pour la prochaine recherche", logs.output[0])

    def test_a_refused_pattern_is_refused_before_compiling(self):
        never = NeverCompile()
        patterns._checked.cache_clear()
        with mock.patch.object(regex, "compile", new=never):
            with self.assertRaises(PatternError):
                mail_matcher(r"(?x)(?:x{6 5 5 3 5}){6 5 5 3 5}")
        self.assertEqual(never.calls, [])


class ReadAmountTests(SimpleTestCase):
    def test_it_is_the_app_s_one_reader(self):
        """Moved to common.py for « Combler les écarts »' amount field and
        re-exported here: one reading of « 1 234,50 » for the whole app, so
        the two cannot drift. tests/test_common_numbers.py reads it there."""
        import common

        self.assertIs(read_amount, common.read_amount)
        self.assertIs(patterns.read_amount, common.read_amount)
        self.assertIs(patterns.read_number, common.read_number)

    def test_what_is_read(self):
        cases = {
            "90.00": "90.00",
            "90,00": "90.00",
            "90": "90.00",
            "0": "0.00",
            "-0.00": "0.00",
            "-175.00": "-175.00",
            "\N{MINUS SIGN}175.00": "-175.00",
            "175.00-": "-175.00",
            " 12.5 ": "12.50",
            "1 234,56": "1234.56",
            "1\N{NO-BREAK SPACE}234,56": "1234.56",
            "1\N{NARROW NO-BREAK SPACE}234,56": "1234.56",
            "1'234.56": "1234.56",
            "1.234,56": "1234.56",
            "1,234.56": "1234.56",
            "1.234.567,89": "1234567.89",
            "4,000": "4.00",
            "1.000": "1.00",
            "1.000.000": "1000000.00",
            "1,000,000": "1000000.00",
            "9999999999.99": "9999999999.99",
            "-9999999999.99": "-9999999999.99",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(read_amount(text), Decimal(expected))
                self.assertEqual(str(read_amount(text)), expected)

    def test_what_is_not_read(self):
        for text in (
            None,
            "",
            "  ",
            "-",
            "abc",
            "12.",
            ".5",
            "+5",
            "1e5",
            "-175.00-",
            "--5",
            "1.00.0",
            "12,34.56",
            "1,234.5,6",
            "1.2345,6",
            "0.125",
            "90.001",
            "10000000000.00",
            "-10000000000",
            "\N{ARABIC-INDIC DIGIT THREE}",
            "\N{SUPERSCRIPT TWO}",
            "1" * 41,
            12,
        ):
            with self.subTest(text=text):
                self.assertIsNone(read_amount(text))

    def test_a_unit_price_keeps_four_decimals_and_its_own_column(self):
        self.assertEqual(read_amount("0.125", places=4), Decimal("0.1250"))
        self.assertEqual(read_amount("0,3333", places=4), Decimal("0.3333"))
        self.assertIsNone(read_amount("0.12345", places=4))
        self.assertEqual(read_amount("99999999.9999", places=4), Decimal("99999999.9999"))
        # Decimal(12, 4) holds 8 integer digits: 10^8 would raise on every read of the row.
        self.assertIsNone(read_amount("100000000", places=4))


class ReadQuantityTests(SimpleTestCase):
    def test_what_is_read(self):
        for text, expected in (
            ("3", 3),
            ("-1", -1),
            ("0", 0),
            ("-0", 0),
            ("4,000", 4),
            ("1 000", 1000),
            ("99999", 99_999),
            ("-99999", -99_999),
            ("12-", -12),
        ):
            with self.subTest(text=text):
                self.assertEqual(read_quantity(text), expected)

    def test_what_is_not_read(self):
        for text in (None, "", "x", "4,5", "0.5", "100000", "-100000", "1" * 30, "1,00,0"):
            with self.subTest(text=text):
                self.assertIsNone(read_quantity(text))


class ReadDateTests(SimpleTestCase):
    TODAY = date(2026, 9, 29)

    def read(self, text):
        return read_date(text, today=self.TODAY)

    def test_the_shapes_read(self):
        for text in ("14/05/2025", "14/05/25", "14.05.2025", "14-05-2025", "2025-05-14", " 14/05/2025 ", "14/5/2025"):
            with self.subTest(text=text):
                self.assertEqual(self.read(text), date(2025, 5, 14))

    def test_impossible_dates_and_other_shapes_are_not_read(self):
        for text in (
            None,
            "",
            "31/02/2025",
            "29/02/2025",
            "00/05/2025",
            "14/13/2025",
            "2025/05/14",
            "14/05/2025 08:15",
            "le 14/05/2025",
            "14/05-2025",
            "14/05/202",
            "140/05/2025",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.read(text))
        self.assertEqual(self.read("29/02/2024"), date(2024, 2, 29))

    def test_the_bounds(self):
        self.assertEqual(self.read("01/01/2000"), date(2000, 1, 1))
        self.assertIsNone(self.read("31/12/1999"))
        self.assertEqual(self.read("06/10/2026"), self.TODAY + timedelta(days=7))
        self.assertIsNone(self.read("07/10/2026"))

    def test_today_is_the_local_day_by_default(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        self.assertEqual(read_date(f"{tomorrow:%d/%m/%Y}"), tomorrow)
        self.assertIsNone(read_date(f"{timezone.localdate() + timedelta(days=8):%d/%m/%Y}"))


class ReadTimeTests(SimpleTestCase):
    def test_what_is_read(self):
        for text, expected in (
            ("08:15", time(8, 15)),
            ("08:15:02", time(8, 15, 2)),
            ("8:15", time(8, 15)),
            ("00:00", time(0, 0)),
            ("23:59:59", time(23, 59, 59)),
        ):
            with self.subTest(text=text):
                self.assertEqual(read_time(text), expected)

    def test_what_is_not_read(self):
        for text in (None, "", "24:00", "12:60", "12:00:60", "0815", "8h15", "12:5"):
            with self.subTest(text=text):
                self.assertIsNone(read_time(text))

    def test_a_printed_moment_is_aware_in_paris_and_never_raises(self):
        summer = aware_datetime(date(2025, 5, 14), time(8, 15, 2))
        self.assertEqual(summer.utcoffset(), timedelta(hours=2))
        self.assertEqual(summer.tzinfo, timezone.get_default_timezone())
        self.assertEqual(aware_datetime(date(2025, 1, 14)).utcoffset(), timedelta(hours=1))
        self.assertEqual(aware_datetime(date(2025, 1, 14)).time(), time(0, 0))
        # 02:30 does not exist on 30/03/2025 in Paris (DST gap): fold 0, no exception.
        gap = aware_datetime(date(2025, 3, 30), time(2, 30))
        self.assertEqual((gap.fold, gap.hour, gap.minute), (0, 2, 30))


class CapturedTests(SimpleTestCase):
    def test_only_a_group_that_took_part_and_is_not_blank_counts(self):
        pattern = compile_pattern(r"N(?P<numero>\s*\d*\s*)(?:x(?P<prix>\d+))?", field_label="Motif")
        self.assertEqual(captured(pattern.search("N 12 "), "numero"), "12")
        self.assertIsNone(captured(pattern.search("N  "), "numero"))
        self.assertIsNone(captured(pattern.search("N 12"), "prix"))
        self.assertEqual(captured(pattern.search("N12x5"), "prix"), "5")
        self.assertIsNone(captured(pattern.search("N12"), "reference"))
        self.assertIsNone(captured(None, "numero"))
