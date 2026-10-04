"""The « Consignes » alert (returnables/notify.py): what a slip just created
says - conforming, a gap, no pickup, to check - once per batch and once per
result, and never a failure for the slip.

`events.emit` is replaced where the mapping is tested (what it is handed is
the subject); one test lets it queue a real dispatch. No delivery thread is
ever started (`notifications.events.threading.Thread` patched), nothing is
sent. "Today" is the invented delivery day (`notify._today` patched). Every
slip, pickup, number and count is invented (support.py, texts.py).
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from invoices.importing import RoutedToReturnablesError
from invoices.receipts import route_to_returnables
from invoices.tests.pdf_files import write_pdf
from notifications.models import Dispatch, EventRule
from returnables import mail, notify, slips
from returnables.comparison import Board
from returnables.models import ReturnableType, Slip
from returnables.slips import store_slip, store_uploads
from returnables.tests import texts
from returnables.tests.support import (
    DELIVERY_DAY,
    KEG_LINE,
    make_pickup,
    make_slip,
    seeded_format,
    seeded_type,
    slip_text,
    tiny_pdf,
)
from returnables.tests.test_gather import mail_of
from returnables.tests.test_slips import Upload
from tests.support import NoNetworkTestCase

DAY = DELIVERY_DAY  # a Tuesday: « mar. 10/02 »
EVENT = "returnables-comparison"
ALL_OUTCOMES = ["match", "differs", "no_pickup", "to_check"]
D = Decimal
NBSP = "\N{NO-BREAK SPACE}"
DOT = "\N{MIDDLE DOT}"
ARROW = "\N{RIGHTWARDS ARROW}"
GAP_OF_ONE = f"Fûts — compté : 15 {DOT} sur le bon : 14 {ARROW} il en manque 1 sur le bon (30,00{NBSP}€)"


def keg_line(quantity, unit=D("30.00")):  # noqa: B008 - a Decimal is immutable, built once on purpose
    return (texts.KEG, quantity, unit, quantity * unit)


def slip_pdf(number="4243", reference="555001") -> bytes:
    """An invented slip of the seeded format, delivered DAY, as a PDF."""
    return tiny_pdf(slip_text(number=number, references=(reference,), lines=(KEG_LINE,)).split("\n"))


def _moment(hour):
    return timezone.make_aware(datetime.combine(DAY, datetime.min.time()).replace(hour=hour))


def target_of(slip) -> str:
    return reverse("returnables:slip_detail", args=[slip.pk])


class AlertCase(NoNetworkTestCase):
    """A rule following every outcome, today = DAY, `emit` recorded."""

    def setUp(self):
        super().setUp()
        seeded_type(texts.KEGS)
        self.rule = EventRule.objects.create(event=EVENT, outcomes=ALL_OUTCOMES, recipient_ids=[1])
        self.enterContext(mock.patch("returnables.notify._today", return_value=DAY))
        self.emit = self.enterContext(mock.patch("returnables.notify.events.emit"))

    def alerts(self) -> list:
        """[(outcome, title, body, target)] in the order emitted."""
        return [
            (call.args[1], call.kwargs["title"], call.kwargs["body"], call.kwargs["target"])
            for call in self.emit.call_args_list
        ]

    def keys(self) -> list:
        return [call.kwargs["content_key"] for call in self.emit.call_args_list]

    def alert_of(self, *slips) -> tuple:
        """The one alert `slips` (one batch) make."""
        self.emit.reset_mock()
        notify.notify_slips(list(slips))
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1, alerts)
        self.assertEqual(self.emit.call_args.args[0], EVENT)
        return alerts[0]


class PairedTests(AlertCase):
    def test_a_conforming_slip(self):
        make_pickup(date=DAY)
        slip = make_slip(delivery_date=DAY, lines=[keg_line(15)], number="4243")
        self.assertEqual(
            self.alert_of(slip),
            ("match", "Consignes : conforme", "Reprise du 10/02/2026 : conforme (bon n° 4243).", target_of(slip)),
        )

    def test_a_gap(self):
        make_pickup(date=DAY)
        slip = make_slip(delivery_date=DAY, lines=[keg_line(14)])
        self.assertEqual(
            self.alert_of(slip),
            ("differs", "Consignes : écart", f"Reprise du 10/02/2026 : écart — {GAP_OF_ONE}.", target_of(slip)),
        )

    def test_a_paired_slip_whose_check_failed_is_to_check(self):
        make_pickup(date=DAY)
        failed = {"label": "Total des lignes = total imprimé", "passed": False, "detail": "lignes : 0,00"}
        slip = make_slip(delivery_date=DAY, lines=[keg_line(15)], checks=[failed], number="4244")
        outcome, title, body, _target = self.alert_of(slip)
        self.assertEqual((outcome, title), ("to_check", "Consignes : à vérifier"))
        self.assertTrue(body.startswith("Reprise du 10/02/2026 : à vérifier — Le bon n° 4244 : contrôle"), body)


class HintTests(AlertCase):
    """A pickup counted the night before and dated that night: compared as
    if it were on the slip's day, and the alert says where to move it."""

    def test_a_gap_with_a_misdated_pickup(self):
        make_pickup(date=DAY - timedelta(days=2))
        slip = make_slip(delivery_date=DAY, lines=[keg_line(14)])
        self.assertEqual(
            self.alert_of(slip),
            (
                "differs",
                "Consignes : écart",
                f"Reprise du dim. 08/02, à mettre au mar. 10/02 : écart — {GAP_OF_ONE}.",
                target_of(slip),
            ),
        )

    def test_a_misdated_pickup_that_matches(self):
        make_pickup(date=DAY - timedelta(days=1))
        slip = make_slip(delivery_date=DAY, lines=[keg_line(15)], number="4243")
        self.assertEqual(
            self.alert_of(slip)[:3],
            (
                "match",
                "Consignes : conforme",
                "Reprise du lun. 09/02, à mettre au mar. 10/02 : conforme (bon n° 4243).",
            ),
        )

    def test_a_slip_listing_no_empty_beside_a_misdated_pickup_differs(self):
        make_pickup(date=DAY - timedelta(days=1))
        slip = make_slip(delivery_date=DAY, lines=[])
        outcome, _title, body, _target = self.alert_of(slip)
        self.assertEqual(outcome, "differs")
        self.assertTrue(body.startswith("Reprise du lun. 09/02, à mettre au mar. 10/02 : écart — Fûts"), body)
        self.assertIn("il en manque 15 sur le bon", body)

    def test_a_slip_whose_lines_were_not_read_beside_a_misdated_pickup_is_to_check(self):
        make_pickup(date=DAY - timedelta(days=1))
        unread = {"label": "Aucune ligne ignorée", "passed": False, "detail": "2 lignes non lues"}
        slip = make_slip(delivery_date=DAY, lines=[], checks=[unread], number="4245")
        outcome, _title, body, _target = self.alert_of(slip)
        self.assertEqual(outcome, "to_check")
        self.assertEqual(
            body,
            "Reprise du lun. 09/02, à mettre au mar. 10/02 : à vérifier — "
            "Le bon n° 4245 a 2 lignes non lues : comparaison incomplète.",
        )


class UnpairedTests(AlertCase):
    def test_no_pickup_at_all(self):
        slip = make_slip(delivery_date=DAY, number="4243")
        self.assertEqual(
            self.alert_of(slip),
            (
                "no_pickup",
                "Consignes : aucune reprise saisie",
                "Aucune reprise saisie pour la livraison du mar. 10/02 (bon n° 4243).",
                target_of(slip),
            ),
        )

    def test_a_pickup_with_repris_par_blank_is_to_check(self):
        make_pickup(date=DAY - timedelta(days=1), supplier=None)
        make_pickup(date=DAY + timedelta(days=3), supplier=None)
        slip = make_slip(delivery_date=DAY)
        self.assertEqual(
            self.alert_of(slip)[:3],
            (
                "to_check",
                "Consignes : à vérifier",
                "Une reprise sans « Repris par » le 09/02 : précisez le fournisseur.",
            ),
        )

    def test_a_blank_pickup_too_far_away_says_nothing(self):
        make_pickup(date=DAY - timedelta(days=4), supplier=None)
        slip = make_slip(delivery_date=DAY)
        self.assertEqual(self.alert_of(slip)[0], "no_pickup")

    def test_nothing_taken_back_and_nothing_counted_is_conforming(self):
        slip = make_slip(delivery_date=DAY, lines=[], number="4243")
        self.assertEqual(
            self.alert_of(slip)[:3],
            ("match", "Consignes : conforme", "Bon n° 4243 : aucun vide repris, aucune reprise saisie."),
        )

    def test_a_slip_listing_nothing_beside_a_pickup_with_repris_par_blank_is_to_check(self):
        # The driver left the empties off the slip and « Repris par » was left
        # blank: never « conforme », never « aucune reprise saisie ».
        make_pickup(date=DAY, supplier=None)
        slip = make_slip(delivery_date=DAY, lines=[], number="4243")
        self.assertEqual(
            self.alert_of(slip)[:3],
            (
                "to_check",
                "Consignes : à vérifier",
                "Une reprise sans « Repris par » le 10/02 : précisez le fournisseur.",
            ),
        )


class DayOfSeveralSlipsTests(AlertCase):
    """Two deliveries the same day and no pickup entered: the day is decided
    on every slip that counts that day, never on the latest one alone."""

    def test_the_latest_slip_listing_nothing_does_not_hide_the_one_that_does(self):
        listing = make_slip(delivery_date=DAY, lines=[keg_line(15)], number="1", printed_at=_moment(7))
        empty = make_slip(delivery_date=DAY, lines=[], number="2", printed_at=_moment(15))
        self.assertEqual(
            self.alert_of(listing, empty),
            (
                "no_pickup",
                "Consignes : aucune reprise saisie",
                "Aucune reprise saisie pour la livraison du mar. 10/02 (bon n° 1).",
                target_of(listing),
            ),
        )

    def test_two_batches_never_say_conforme_after_aucune_reprise_saisie(self):
        listing = make_slip(delivery_date=DAY, lines=[keg_line(15)], number="1", printed_at=_moment(7))
        notify.notify_slips([listing])
        empty = make_slip(delivery_date=DAY, lines=[], number="2", printed_at=_moment(15))
        notify.notify_slips([empty])
        self.assertEqual([alert[0] for alert in self.alerts()], ["no_pickup", "no_pickup"])
        # The same facts: the same result, sent once.
        self.assertEqual(len(set(self.keys())), 1)

    def assert_one_blank_result(self):
        blank = "Une reprise sans « Repris par » le 09/02 : précisez le fournisseur."
        self.assertEqual(
            [alert[:3] for alert in self.alerts()],
            [("to_check", "Consignes : à vérifier", blank)] * 2,
        )
        # The sentence names no slip: a later slip of the day is the same
        # result, sent once.
        self.assertEqual(len(set(self.keys())), 1)

    def test_a_blank_pickup_is_said_once_when_an_empty_slip_follows_a_listing_one(self):
        make_pickup(date=DAY - timedelta(days=1), supplier=None)
        listing = make_slip(delivery_date=DAY, lines=[keg_line(15)], number="1", printed_at=_moment(7))
        notify.notify_slips([listing])
        empty = make_slip(delivery_date=DAY, lines=[], number="2", printed_at=_moment(15))
        notify.notify_slips([empty])
        self.assert_one_blank_result()
        # The alert still opens the slip it was made for.
        self.assertEqual([alert[3] for alert in self.alerts()], [target_of(listing), target_of(empty)])

    def test_a_blank_pickup_is_said_once_when_a_listing_slip_follows_an_empty_one(self):
        make_pickup(date=DAY - timedelta(days=1), supplier=None)
        empty = make_slip(delivery_date=DAY, lines=[], number="1", printed_at=_moment(7))
        notify.notify_slips([empty])
        listing = make_slip(delivery_date=DAY, lines=[keg_line(15)], number="2", printed_at=_moment(15))
        notify.notify_slips([listing])
        self.assert_one_blank_result()

    def test_an_earlier_slip_whose_lines_were_not_read_makes_the_day_to_check(self):
        unread = {"label": "Aucune ligne ignorée", "passed": False, "detail": "2 lignes non lues"}
        failed = make_slip(delivery_date=DAY, lines=[], checks=[unread], number="1", printed_at=_moment(7))
        empty = make_slip(delivery_date=DAY, lines=[], number="2", printed_at=_moment(15))
        self.assertEqual(
            self.alert_of(failed, empty)[:3],
            (
                "to_check",
                "Consignes : à vérifier",
                "Le bon n° 1 a 2 lignes non lues : comparaison incomplète.",
            ),
        )

    def test_conforme_only_when_every_slip_of_the_day_lists_nothing(self):
        one = make_slip(delivery_date=DAY, lines=[], number="1", printed_at=_moment(7))
        two = make_slip(delivery_date=DAY, lines=[], number="2", printed_at=_moment(15))
        self.assertEqual(
            self.alert_of(one, two)[:3],
            ("match", "Consignes : conforme", "Bon n° 1, bon n° 2 : aucun vide repris, aucune reprise saisie."),
        )

    def test_an_unreadable_slip_and_one_without_its_date_are_to_check_with_why(self):
        unreadable = make_slip(
            lines=[], read_error="Motif de ligne : le motif est vide.", delivery_date=None, mail_date=DAY
        )
        self.assertEqual(
            self.alert_of(unreadable)[:3],
            ("to_check", "Consignes : à vérifier", "Lecture impossible : Motif de ligne : le motif est vide."),
        )
        undated = make_slip(delivery_date=None, mail_date=DAY - timedelta(days=1))
        self.assertEqual(
            self.alert_of(undated)[:3],
            (
                "to_check",
                "Consignes : à vérifier",
                "Date de livraison non lue : ce bon ne peut être rapproché d'aucune reprise.",
            ),
        )


class SupersededTests(AlertCase):
    def test_a_resend_says_nothing_the_slip_that_counts_speaks_for_both(self):
        first = make_slip(number="1", references=["690001"], delivery_date=DAY, printed_at=_moment(7))
        last = make_slip(number="1", references=["690001"], delivery_date=DAY, printed_at=_moment(9))
        notify.notify_slips([first])
        self.assertEqual(self.alerts(), [])
        self.assertEqual(self.alert_of(first, last)[3], target_of(last))

    def test_an_original_arriving_after_its_replacement_says_nothing(self):
        make_slip(number="2", references=["690002"], delivery_date=DAY, replaces=True, printed_at=_moment(9))
        original = make_slip(number="1", references=["690002"], delivery_date=DAY, printed_at=_moment(7))
        notify.notify_slips([original])
        self.assertEqual(self.alerts(), [])


class BatchTests(AlertCase):
    def test_two_slips_of_one_day_in_one_batch_are_one_evaluation(self):
        make_pickup(date=DAY, counts={texts.KEGS: 30})
        one = make_slip(delivery_date=DAY, lines=[keg_line(15)], number="4243")
        two = make_slip(delivery_date=DAY, lines=[keg_line(15)], number="4244")
        with (
            mock.patch("returnables.notify.Board.load", wraps=Board.load) as load,
            mock.patch.object(Board, "slip_state", autospec=True, side_effect=Board.slip_state) as state,
        ):
            outcome, _title, body, target = self.alert_of(one, two)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(state.call_count, 1)
        self.assertEqual(outcome, "match")
        self.assertEqual(body, "Reprise du 10/02/2026 : conforme (bon n° 4243, bon n° 4244).")
        self.assertEqual(target, target_of(two))

    def test_one_alert_per_supplier_and_day(self):
        one = make_slip(delivery_date=DAY)
        other_day = make_slip(delivery_date=DAY - timedelta(days=1))
        notify.notify_slips([one, other_day])
        self.assertEqual([alert[3] for alert in self.alerts()], [target_of(other_day), target_of(one)])

    def test_only_the_recent_days_alert(self):
        slips = {offset: make_slip(delivery_date=DAY + timedelta(days=offset)) for offset in (-4, -3, 0, 1, 2)}
        notify.notify_slips(list(slips.values()))
        self.assertEqual([alert[3] for alert in self.alerts()], [target_of(slips[offset]) for offset in (-3, 0, 1)])

    def test_a_slip_without_its_date_never_stands_for_the_dated_slip_of_its_day(self):
        make_pickup(date=DAY)
        dated = make_slip(delivery_date=DAY, lines=[keg_line(14)], printed_at=_moment(8))
        undated = make_slip(delivery_date=None, mail_date=DAY)  # received now: later than 08:00
        notify.notify_slips([dated, undated])
        self.assertEqual(
            self.alerts(),
            [
                ("differs", "Consignes : écart", f"Reprise du 10/02/2026 : écart — {GAP_OF_ONE}.", target_of(dated)),
                (
                    "to_check",
                    "Consignes : à vérifier",
                    "Date de livraison non lue : ce bon ne peut être rapproché d'aucune reprise.",
                    target_of(undated),
                ),
            ],
        )

    def test_an_undated_slip_is_dated_by_its_mail_then_by_its_arrival(self):
        old_mail = make_slip(delivery_date=None, mail_date=DAY - timedelta(days=10))
        arrived = make_slip(delivery_date=None)  # received now, far from the invented "today"
        notify.notify_slips([old_mail, arrived])
        self.assertEqual(self.alerts(), [])

    def test_an_empty_batch_costs_nothing(self):
        with self.assertNumQueries(0):
            notify.notify_slips([])
            notify.notify_slips([None])
        self.emit.assert_not_called()


class WantedTests(AlertCase):
    def test_no_rule_no_slip_is_read(self):
        slip = make_slip(delivery_date=DAY)
        for rule_state in ("inactive", "absent"):
            with self.subTest(rule_state=rule_state):
                if rule_state == "inactive":
                    EventRule.objects.filter(pk=self.rule.pk).update(is_active=False)
                else:
                    EventRule.objects.all().delete()
                with (
                    mock.patch("returnables.notify.Board.load") as load,
                    mock.patch("returnables.notify._recent_groups") as groups,
                ):
                    notify.notify_slips([slip])
                load.assert_not_called()
                groups.assert_not_called()
                self.emit.assert_not_called()

    def test_wanted_is_asked_before_anything_else(self):
        slip = make_slip(delivery_date=DAY)
        calls = []
        with (
            mock.patch("returnables.notify.events.wanted", side_effect=lambda event: calls.append("wanted") or True),
            mock.patch("returnables.notify._recent_groups", side_effect=lambda *a: calls.append("groups") or []),
        ):
            notify.notify_slips([slip])
        self.assertEqual(calls, ["wanted", "groups"])


class NeverRaisesTests(AlertCase):
    def test_a_failure_is_logged_and_dropped(self):
        slip = make_slip(delivery_date=DAY)
        with (
            mock.patch("returnables.notify.Board.load", side_effect=RuntimeError("panne")),
            self.assertLogs("returnables.notify", "ERROR") as logged,
        ):
            self.assertIsNone(notify.notify_slips([slip]))
        self.assertIn("Consignes : alerte du bon non préparée", logged.output[0])
        self.emit.assert_not_called()


class ContentKeyTests(AlertCase):
    def key_of(self, slip) -> str:
        self.emit.reset_mock()
        notify.notify_slips([slip])
        self.assertEqual(self.emit.call_count, 1)
        return self.keys()[0]

    def test_the_substance_not_the_words(self):
        pickup = make_pickup(date=DAY)
        slip = make_slip(delivery_date=DAY, lines=[keg_line(14)])
        key = self.key_of(slip)
        self.assertRegex(key, r"^consignes:[0-9a-f]{24}$")
        self.assertEqual(self.key_of(slip), key)
        body = self.emit.call_args.kwargs["body"]
        # Reworded: a type renamed changes the sentence, not the result.
        ReturnableType.objects.filter(name=texts.KEGS).update(name="Fûts inox")
        self.assertEqual(self.key_of(slip), key)
        self.assertNotEqual(self.emit.call_args.kwargs["body"], body)
        # A count changed is another result.
        pickup.counts.update(quantity=14)
        changed = self.key_of(slip)
        self.assertNotEqual(changed, key)
        self.assertEqual(self.emit.call_args.args[1], "match")

    def test_another_slip_on_the_day_is_another_result(self):
        make_pickup(date=DAY, counts={texts.KEGS: 30})
        one = make_slip(delivery_date=DAY, lines=[keg_line(15)])
        key = self.key_of(one)
        two = make_slip(delivery_date=DAY, lines=[keg_line(15)])
        self.assertNotEqual(self.key_of(two), key)

    def test_a_blank_pickup_on_another_day_is_another_result(self):
        pickup = make_pickup(date=DAY - timedelta(days=1), supplier=None)
        slip = make_slip(delivery_date=DAY)
        key = self.key_of(slip)
        self.assertEqual(self.emit.call_args.args[1], "to_check")
        type(pickup).objects.filter(pk=pickup.pk).update(date=DAY - timedelta(days=2))
        self.assertNotEqual(self.key_of(slip), key)
        self.assertIn("le 08/02", self.emit.call_args.kwargs["body"])

    def test_two_slips_without_their_date_are_two_results(self):
        one = make_slip(delivery_date=None, mail_date=DAY)
        two = make_slip(delivery_date=None, mail_date=DAY - timedelta(days=1))
        self.assertNotEqual(self.key_of(one), self.key_of(two))


class DispatchTests(NoNetworkTestCase):
    """Through the real `emit`: one dispatch per result, however often the
    slip is read."""

    def setUp(self):
        super().setUp()
        seeded_type(texts.KEGS)
        self.rule = EventRule.objects.create(event=EVENT, outcomes=ALL_OUTCOMES, recipient_ids=[1])
        self.enterContext(mock.patch("returnables.notify._today", return_value=DAY))
        self.thread = self.enterContext(mock.patch("notifications.events.threading.Thread"))
        self.thread.return_value.is_alive.return_value = False

    def test_one_dispatch_and_the_same_result_is_sent_once(self):
        make_pickup(date=DAY)
        slip = make_slip(delivery_date=DAY, lines=[keg_line(14)])
        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=True):
                notify.notify_slips([slip])
        dispatch = Dispatch.objects.get()
        self.assertEqual(
            (dispatch.event, dispatch.outcome, dispatch.title, dispatch.target),
            (EVENT, "differs", "Consignes : écart", target_of(slip)),
        )
        self.assertEqual(dispatch.body, f"Reprise du 10/02/2026 : écart — {GAP_OF_ONE}.")
        self.assertTrue(re.fullmatch(rf"event:{self.rule.pk}:consignes:[0-9a-f]{{24}}", dispatch.dedupe_key))

    def test_an_outcome_the_rule_does_not_follow_makes_nothing(self):
        EventRule.objects.filter(pk=self.rule.pk).update(outcomes=["differs"])
        make_pickup(date=DAY)
        slip = make_slip(delivery_date=DAY, lines=[keg_line(15)])
        with self.captureOnCommitCallbacks(execute=True):
            notify.notify_slips([slip])
        self.assertFalse(Dispatch.objects.exists())


# -- The three batches -----------------------------------------------------------------------------------------------


class CallSiteTests(NoNetworkTestCase):
    """`mail.store_matches`, `slips.store_uploads` and Achats' guard hand
    their CREATED slips to `notify_slips` once per batch; `store_slip`
    never does."""

    def setUp(self):
        super().setUp()
        self.notify = self.enterContext(mock.patch("returnables.notify.notify_slips"))

    def handed(self) -> list:
        self.assertEqual(self.notify.call_count, 1)
        return list(self.notify.call_args.args[0])

    def test_store_slip_alone_never_alerts(self):
        result = store_slip(slip_pdf(), filename="a.pdf", origin=Slip.Origin.UPLOAD)
        self.assertEqual(result.kind, slips.CREATED)
        self.notify.assert_not_called()

    def test_an_upload_alerts_once_with_the_slips_it_created(self):
        store_slip(slip_pdf("4250", "555050"), filename="deja.pdf", origin=Slip.Origin.UPLOAD)
        summary = store_uploads(
            [
                Upload("a.pdf", slip_pdf("4243", "555001")),
                Upload("deja.pdf", slip_pdf("4250", "555050")),
                Upload("b.pdf", slip_pdf("4244", "555002")),
                Upload("rien.pdf", b"pas un PDF"),
            ]
        )
        created = [result.slip for _name, result in summary.results if result.kind == slips.CREATED]
        self.assertEqual(len(created), 2)
        self.assertEqual(self.handed(), created)

    def test_an_upload_creating_nothing_hands_nothing(self):
        store_uploads([Upload("rien.pdf", b"pas un PDF")])
        self.assertEqual(self.handed(), [])

    def test_a_gather_alerts_once_after_its_mails_and_before_its_note(self):
        order = []
        self.notify.side_effect = lambda created: order.append(("notify", len(created)))
        with mock.patch("returnables.mail._note", side_effect=lambda log: order.append(("note",)) or ""):
            found, imported, _note = mail.store_matches(
                seeded_format(),
                [mail_of(slip_pdf("4243", "555001"), slip_pdf("4244", "555002")), mail_of(slip_pdf("4243", "555001"))],
                lambda line: None,
            )
        self.assertEqual((found, imported), (3, 2))
        self.assertEqual(order, [("notify", 2), ("note",)])
        self.assertEqual(self.handed(), list(Slip.objects.order_by("pk")))

    def test_a_gather_stopped_by_an_error_still_alerts_what_it_stored(self):
        real = slips.store_slip
        calls = []

        def store_then_fail(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("panne")
            return real(*args, **kwargs)

        with mock.patch("returnables.slips.store_slip", side_effect=store_then_fail), self.assertRaises(RuntimeError):
            mail.store_matches(
                seeded_format(), [mail_of(slip_pdf("4243", "555001"), slip_pdf("4244", "555002"))], lambda line: None
            )
        self.assertEqual(self.handed(), [Slip.objects.get()])

    def test_achats_guard_alerts_the_slip_it_created_and_only_then(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        lines = slip_text(number="4243", references=("555001",), lines=(KEG_LINE,)).split("\n")
        path = write_pdf(os.path.join(folder, "document.pdf"), lines)
        with self.assertRaises(RoutedToReturnablesError):
            route_to_returnables(path, "T0000000042.pdf")
        self.assertEqual(self.handed(), [Slip.objects.get()])
        with self.assertRaises(RoutedToReturnablesError):
            route_to_returnables(path, "T0000000042.pdf")  # already there: nothing new
        self.assertEqual(self.notify.call_count, 1)


class AlertNeverCostsTheSlipTests(NoNetworkTestCase):
    """An alert that fails changes nothing the batch says."""

    def setUp(self):
        super().setUp()
        EventRule.objects.create(event=EVENT, outcomes=ALL_OUTCOMES)
        self.enterContext(mock.patch("returnables.notify._today", return_value=DAY))
        self.enterContext(mock.patch("returnables.notify._evaluate", side_effect=RuntimeError("panne")))
        self.enterContext(self.assertLogs("returnables.notify", "ERROR"))

    def test_an_upload_says_the_same(self):
        summary = store_uploads([Upload("a.pdf", slip_pdf())])
        self.assertEqual([result.kind for _name, result in summary.results], [slips.CREATED])
        self.assertNotIn("erreur inattendue", summary.message)
        self.assertEqual(Slip.objects.count(), 1)

    def test_a_gather_says_the_same(self):
        found, imported, _note = mail.store_matches(seeded_format(), [mail_of(slip_pdf())], lambda line: None)
        self.assertEqual((found, imported, Slip.objects.count()), (1, 1, 1))


# -- The links that open in a new tab ----------------------------------------------------------------------------


class NewTabLinksTests(NoNetworkTestCase):
    """A Home Screen app has no Back button: the slip's PDF and a pickup's
    photo open in a new tab."""

    def test_the_slip_s_pdf(self):
        slip = make_slip()
        page = self.client.get(reverse("returnables:slip_detail", args=[slip.pk]))
        self.assertContains(
            page, f'<a href="{slip.file.url}" target="_blank" rel="noopener">Ouvrir le PDF</a>', html=True
        )

    def test_a_pickup_s_photo(self):
        pickup = make_pickup(photos=1)
        photo = pickup.photos.get()
        page = self.client.get(reverse("returnables:pickup_detail", args=[pickup.pk]))
        self.assertContains(page, f'href="{photo.image.url}" target="_blank" rel="noopener"')
