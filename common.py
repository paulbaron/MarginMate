"""Small shared form helpers.

Project-level rather than per-app because the problem below has now bitten
three different formsets across three different apps.
"""

import re

from django import forms


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


class JobLogMixin:
    """Shared reading of a background job's `log` field.

    Both job models append timestamped lines to one text field. The console
    partial (templates/_job_console.html) shows the last line on its own as
    "what's happening right now" and folds the rest away, so both models have
    to answer the same two questions about it.
    """

    #: "[+  12.3s] " - real information while a job runs, noise on the one
    #: line shown as a live status, where the spinner already says "running".
    _ELAPSED_PREFIX = re.compile(r"^\[\+\s*[\d.]+s\]\s*")

    @property
    def log_lines(self) -> int:
        return len([line for line in (self.log or "").splitlines() if line.strip()])

    @property
    def last_log_line(self) -> str:
        for line in reversed((self.log or "").splitlines()):
            if line.strip():
                return self._ELAPSED_PREFIX.sub("", line).strip()
        return ""
