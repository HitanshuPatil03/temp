"""How a report stamps itself with a time.

Timestamps are stored in UTC, which is right — a single instant, no ambiguity,
comparable across hosts. They are *read* by officers in India, and that is where
the two diverge.

IST is UTC+05:30, so the UTC date and the Indian date differ for the five and a
half hours after midnight local. The PowerPoint title slide carried
``generated_at.date()``, a bare UTC date with nothing to reveal the offset: a
report built at 00:30 IST on 1 April was stamped **31 March**. The one night of
the year when that matters most is exactly that one, because 31 March is the
fiscal-year boundary — a FY2026-27 report dated the last day of FY2025-26, in a
deck going to a Ministry review. And the hours after midnight are when deadline
work happens.

The ISO timestamps elsewhere printed their offset, so they were not *wrong*, but
they asked an officer who generated a report at 00:30 to recognise themselves in
``2026-03-31T19:00:00+00:00``. Rendering in IST makes the stamp agree with the
wall clock in the room.

**A fixed offset, not a named zone.** ``ZoneInfo("Asia/Kolkata")`` needs the
system tz database, which a slim container image may not carry, and the failure
would be an exception while writing a report rather than anything visible in
tests. India has observed no daylight saving since 1945, so +05:30 is a constant
and the lookup buys nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = ["IST", "report_date", "report_timestamp"]

#: India Standard Time. Fixed; see the module docstring for why this is not a
#: ``ZoneInfo``.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def report_timestamp(moment: datetime) -> str:
    """An unambiguous timestamp that also matches the reader's wall clock.

    Keeps the offset, so the instant is still recoverable from the document —
    dropping it would trade one ambiguity for another.

    >>> from datetime import UTC
    >>> report_timestamp(datetime(2026, 3, 31, 19, 0, tzinfo=UTC))
    '2026-04-01T00:30:00+05:30'
    """
    return moment.astimezone(IST).isoformat(timespec="seconds")


def report_date(moment: datetime) -> str:
    """The calendar date in India, for the one place a bare date is printed.

    >>> from datetime import UTC
    >>> report_date(datetime(2026, 3, 31, 19, 0, tzinfo=UTC))
    '2026-04-01'
    >>> report_date(datetime(2026, 3, 31, 12, 0, tzinfo=UTC))
    '2026-03-31'
    """
    return moment.astimezone(IST).date().isoformat()
