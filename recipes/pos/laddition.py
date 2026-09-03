"""Sales out of L'Addition, via the "Z digital" report.

https://reporting.laddition.com/v2/z-digital produces a PDF of everything
sold between two dates. The back office refuses a range longer than two
years, so a longer history has to be pulled in consecutive windows and
stitched together - see date_windows.

Two quite different jobs use the same download:

* **Seeding the catalogue.** A multi-year report lists every item the till
  has ever rung up. That's how the Recipe list gets names that match the
  till EXACTLY, which is what makes sales import reliable - recipes.sales
  deliberately refuses to fuzzy-match, since a mis-linked sale silently
  moves stock consumption from one drink to another.

* **Measuring a stock-take period.** Here the report must cover exactly the
  window between two counts (see inventory/variance.py). Pull it per period
  and the two-year limit never comes up.

That second point constrains the whole design: a Z report gives ONE total
per product for the whole range, not a figure per day. So the range asked
for has to be the range you want to account for - a report spanning half of
one stock-take period and half of the next can't be split after the fact
without inventing numbers. download_for_period exists to make the aligned
case the easy one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# The back office's own cap. Two calendar years, expressed in days so leap
# years can't push a window one day over and get it rejected.
MAX_RANGE_DAYS = 730


def date_windows(start: date, end: date, max_days: int = MAX_RANGE_DAYS):
    """Split [start, end] into consecutive windows no longer than max_days.

    Windows are inclusive at both ends and never overlap, so summing the
    reports gives each sale exactly once - an overlap would double-count
    every product in the shared days, and a gap would silently lose them.
    """
    if start > end:
        return
    step = timedelta(days=max_days - 1)  # inclusive bounds: day 1 to day 730
    window_start = start
    while window_start <= end:
        window_end = min(window_start + step, end)
        yield window_start, window_end
        window_start = window_end + timedelta(days=1)


@dataclass(frozen=True)
class SoldItem:
    """One line of a Z digital report: a till item and how many were sold
    over the report's whole range."""

    name: str
    quantity: int
    revenue_ttc: object = None  # Decimal when the report prints one

    def __str__(self):
        return f"{self.quantity} x {self.name}"
