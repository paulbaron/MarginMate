"""Small shared form helpers.

Project-level rather than per-app because the problem below has now bitten
three different formsets across three different apps.
"""

import re
from datetime import timedelta

from django import forms
from django.db import models
from django.utils import timezone


class BlankRowTolerantFormMixin:
    """Makes a formset row count as blank unless the user actually filled
    something in, ignoring fields that only carry defaults or bookkeeping.

    Django only skips validating an extra formset row when its
    ``has_changed()`` is False, and ``has_changed()`` is True as soon as ANY
    field differs from its initial. That breaks two things at once:

    * A row removed client-side leaves a GAP in the posted indices (0, 1, 3
      with TOTAL_FORMS=4) - index 2 is absent from the POST entirely. A field
      declared with an ``initial`` then reads as "changed" (initial 0 vs
      nothing submitted), so the invisible row gets validated and fails with
      "this field is required" on a row the user cannot see or fix.
    * A row left at its pre-filled default (a VAT rate of 20%) reads as
      changed too, so an untouched trailing row blocks the save.

    Both are invisible from a happy-path test, because the browser's own
    payloads - non-contiguous indices, defaults echoed back - are not what a
    hand-written test naturally posts. See recipes/tests/test_forms.py.

    Subclasses list the field names that don't count as user input.
    """

    #: Fields that carry a default or bookkeeping value rather than a real
    #: user entry, and so must not by themselves make a row "filled in".
    bookkeeping_fields: tuple[str, ...] = ()

    def has_changed(self) -> bool:
        # Django asks this question for two different reasons, and only one
        # of them wants the bookkeeping fields ignored:
        #
        #   1. "May I skip validating this blank row?" - asked only of rows
        #      that are allowed to be blank (empty_permitted), i.e. the extra
        #      ones. That's the question this mixin exists to answer.
        #   2. "Has this SAVED row changed, so should I write it back?" -
        #      asked by BaseModelFormSet.save_existing_objects of every
        #      initial row, which never has empty_permitted set.
        #
        # Answering (2) with the bookkeeping fields stripped out silently
        # discards real edits: regrouping an existing ingredient via the "OU"
        # button changes nothing BUT `group`, so the save became a no-op and
        # the recipe reopened with the old grouping. Hence the guard.
        if not self.empty_permitted:
            return super().has_changed()
        return bool(set(self.changed_data) - set(self.bookkeeping_fields))


class BlankRowTolerantForm(BlankRowTolerantFormMixin, forms.Form):
    pass


class BlankRowTolerantModelForm(BlankRowTolerantFormMixin, forms.ModelForm):
    pass


class JobLogMixin(models.Model):
    """Everything a background job needs beyond its own fields.

    Both job models append timestamped lines to one text field, and both are
    driven by a daemon thread that cannot be relied on to reach its own
    `finally`: the dev server's autoreloader kills it outright on any code
    change. The job is then left RUNNING for ever, which blocks every future
    run behind "already in progress" - and its Cancel button does nothing,
    because there is no thread left to notice. That combination is a deadlock
    with no way out from the UI, and it is what stopped a three-year sales
    import from ever starting again.

    `last_heartbeat` is how a run says "still here" during the long silent
    stretches; anything quiet for STALE_AFTER is presumed dead and reaped.
    """

    #: Deliberately generous: one export of three years of tickets takes
    #: about a minute to generate and fetch, and declaring a working job dead
    #: is worse than waiting a little longer.
    STALE_AFTER = timedelta(minutes=10)

    last_heartbeat = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    #: "[+  12.3s] " - real information while a job runs, noise on the one
    #: line shown as a live status, where the spinner already says "running".
    _ELAPSED_PREFIX = re.compile(r"^\[\+\s*[\d.]+s\]\s*")

    def beat(self) -> None:
        """Say "still alive" without writing a log line - the download has
        long silent stretches, and a line every two seconds would bury the
        log it shares."""
        self.last_heartbeat = timezone.now()
        self.save(update_fields=["last_heartbeat"])

    @property
    def is_active(self) -> bool:
        return self.status in (self.Status.PENDING, self.Status.RUNNING)

    @property
    def is_stale(self) -> bool:
        """Nominally running, but nothing has been heard from it."""
        if not self.is_active:
            return False
        since = self.last_heartbeat or self.started_at
        return timezone.now() - since > self.STALE_AFTER

    @classmethod
    def reap_stale(cls) -> int:
        """Mark abandoned runs as failed, so they stop blocking new ones."""
        stale = [
            job
            for job in cls.objects.filter(status__in=[cls.Status.PENDING, cls.Status.RUNNING])
            if job.is_stale
        ]
        for job in stale:
            job.status = cls.Status.FAILED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "finished_at"])
            job.append_log(
                "Interrompue : plus aucune nouvelle de cette exécution. "
                "Le serveur a probablement redémarré pendant qu'elle tournait."
            )
        return len(stale)

    @property
    def log_lines(self) -> int:
        return len([line for line in (self.log or "").splitlines() if line.strip()])

    @property
    def last_log_line(self) -> str:
        for line in reversed((self.log or "").splitlines()):
            if line.strip():
                return self._ELAPSED_PREFIX.sub("", line).strip()
        return ""
