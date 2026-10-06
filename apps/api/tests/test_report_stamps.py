"""Report timestamps read in the timezone the reader is standing in.

The boundary case drives all of this: IST is UTC+05:30, so for five and a half
hours after local midnight the UTC date is yesterday's. One of those nights is
31 March into 1 April — the fiscal-year boundary, and the kind of night a
deadline gets met on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mrip.reports.stamps import IST, report_date, report_timestamp


def test_ist_is_five_and_a_half_hours_ahead() -> None:
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)


def test_the_night_the_fiscal_year_turns_over() -> None:
    """The defect this module exists for.

    00:30 IST on 1 April 2026 is 19:00 UTC on 31 March. A bare UTC date put
    **31 March** on a report built in the small hours of 1 April — dating a
    FY2026-27 report to the last day of FY2025-26.
    """
    just_after_midnight_ist = datetime(2026, 3, 31, 19, 0, tzinfo=UTC)

    assert report_date(just_after_midnight_ist) == "2026-04-01"
    # The old behaviour, stated so the regression is unmistakable.
    assert just_after_midnight_ist.date().isoformat() == "2026-03-31"


def test_the_last_moment_of_the_fiscal_year_stays_in_it() -> None:
    """The mirror case: 23:59 IST on 31 March must not roll forward either."""
    end_of_fy = datetime(2026, 3, 31, 18, 29, tzinfo=UTC)  # 23:59 IST

    assert report_date(end_of_fy) == "2026-03-31"


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # Midnight UTC is 05:30 the same morning in India.
        (datetime(2026, 7, 1, 0, 0, tzinfo=UTC), "2026-07-01T05:30:00+05:30"),
        # 18:30 UTC is exactly midnight IST, so the date advances.
        (datetime(2026, 7, 1, 18, 30, tzinfo=UTC), "2026-07-02T00:00:00+05:30"),
        (datetime(2026, 7, 1, 18, 29, tzinfo=UTC), "2026-07-01T23:59:00+05:30"),
    ],
)
def test_timestamps_render_in_ist(utc: datetime, expected: str) -> None:
    assert report_timestamp(utc) == expected


def test_the_offset_is_kept_so_the_instant_is_still_recoverable() -> None:
    """Converting to local time must not throw away which instant it was.

    A report is a record. "2026-04-01T00:30:00" alone would be a worse stamp
    than the UTC one it replaced, not a better one.
    """
    moment = datetime(2026, 3, 31, 19, 0, tzinfo=UTC)

    rendered = report_timestamp(moment)

    assert rendered.endswith("+05:30")
    assert datetime.fromisoformat(rendered) == moment


def test_a_naive_datetime_is_not_silently_assumed_to_be_utc() -> None:
    """``astimezone`` treats a naive datetime as *system* local time, so on a host
    set to anything but UTC it would convert from the wrong base and be quietly
    wrong. Every timestamp in this system is stored tz-aware, so this asserts the
    assumption rather than defending against it — if a naive value ever reaches
    here, the right fix is at the source."""
    from mrip.db.tables import reports as reports_table

    # `TIMESTAMP WITH TIME ZONE` on every timestamp column is what makes the
    # assumption safe, so check the schema rather than trusting a convention.
    assert reports_table.c.generated_at.type.timezone is True


def test_the_markdown_footer_carries_the_indian_timestamp(store, make_fact) -> None:
    """End to end, because a correct helper nobody calls is where this started."""
    from mrip.auth.scope import Scope
    from mrip.reports.generate import generate
    from mrip.reports.render import render_markdown
    from mrip.reports.template import default_template
    from mrip.schemas import FactStatus

    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    manifest = generate(
        store,
        Scope.unrestricted("test"),
        default_template(),
        entity="secl",
        period="FY2024-25",
    )

    markdown = render_markdown(default_template(), manifest)

    assert "+05:30" in markdown
    assert "+00:00" not in markdown
