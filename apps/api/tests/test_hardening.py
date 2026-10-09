"""Tests for the production-hardening pass.

Each test here pins a specific defect the audit found, so the fix cannot quietly
regress. They are grouped by the subsystem they protect; the thread connecting
them is that every one guards a path where a wrong number or a dead end would
reach a real user at Coal India.
"""

from __future__ import annotations

from datetime import date

import pytest

from mrip.auth.scope import Scope
from mrip.db import Store
from mrip.facts.cells import parse_number
from mrip.schemas import FactStatus

SCOPE = Scope.unrestricted("test suite")


# ---------------------------------------------------- comparison overcount


def test_series_takes_the_annual_fact_not_the_sum_of_periods(
    store: Store, make_fact
) -> None:
    """The headline bug: a comparison chart must not sum monthly + cumulative +
    annual facts that all carry the same fiscal year. On the CIL monthly-statement
    corpus that reports 7–8× the real figure, straight onto a Ministry slide.

    The series takes the one validated full-fiscal-year figure, which is the same
    number the exact-figure path returns — chart and answer cannot disagree.
    """
    store.insert_facts(
        [
            # The authoritative annual figure (a full fiscal year).
            make_fact(
                entity_id="secl",
                value=193.0e6,
                status=FactStatus.VALIDATED,
                period_start=date(2024, 4, 1),
                period_end=date(2025, 3, 31),
            ),
            # A single month — must not be added on top.
            make_fact(
                entity_id="secl",
                value=16.0e6,
                status=FactStatus.VALIDATED,
                period_label="Apr 2024",
                period_start=date(2024, 4, 1),
                period_end=date(2024, 4, 30),
            ),
            # A part-year cumulative — must not be added either.
            make_fact(
                entity_id="secl",
                value=95.0e6,
                status=FactStatus.VALIDATED,
                period_label="Apr-Sep 2024",
                period_start=date(2024, 4, 1),
                period_end=date(2024, 9, 30),
            ),
        ]
    )

    series = store.entity_metric_series("coal_production", SCOPE, unit="t")

    assert len(series) == 1
    assert series[0]["value"] == 193.0e6  # not 304e6
    assert series[0]["fact_count"] == 1


def test_two_sources_agreeing_do_not_double_the_bar(
    store: Store, make_fact, make_document
) -> None:
    """Corroboration is not production.

    An original filing and a revised one can state the *same* annual figure. A
    conflict needs ``count(distinct value) > 1``, so two facts that agree are
    not in conflict and both stay validated — which made this the one overcount
    the period-canonicalisation and full-year-span guards both let through,
    because neither is about duplicates.

    It was live on the development corpus: the dashboard reported MCL producing
    421 Mt against documents saying 210.5, and every entity with a second
    corroborating source was exactly twice its real figure. The aggregate now
    picks a representative value instead of adding them.
    """
    second_source = make_document("revised-annual-statement.pdf")
    store.register_document(second_source)
    annual = {
        "entity_id": "mcl",
        "value": 210.5e6,
        "status": FactStatus.VALIDATED,
        "period_start": date(2024, 4, 1),
        "period_end": date(2025, 3, 31),
    }
    store.insert_facts(
        [
            make_fact(**annual),
            make_fact(**annual, document_id=second_source.document_id),
        ]
    )

    series = store.entity_metric_series("coal_production", SCOPE, unit="t")

    assert len(series) == 1
    assert series[0]["value"] == 210.5e6, "two agreeing sources were added together"
    # Still reported, because "two documents say this" is worth knowing — it is
    # the count that must not become the multiplier.
    assert series[0]["fact_count"] == 2


def test_the_chart_and_the_exact_answer_cannot_disagree(
    store: Store, make_fact, make_document
) -> None:
    """The property the series docstring claims, asserted rather than assumed.

    Both paths are supposed to return the one validated full-year figure. While
    the series summed, they diverged by a factor of however many sources
    happened to corroborate — and the chart was the one a Ministry slide took
    its number from.
    """
    from mrip.query.exact import resolve_figure

    second_source = make_document("corroborating-statement.pdf")
    store.register_document(second_source)
    annual = {
        "entity_id": "mcl",
        "value": 210.5e6,
        "status": FactStatus.VALIDATED,
        "unit_ambiguous": False,
        "period_start": date(2024, 4, 1),
        "period_end": date(2025, 3, 31),
    }
    store.insert_facts(
        [
            make_fact(**annual),
            make_fact(**annual, document_id=second_source.document_id),
        ]
    )

    charted = store.entity_metric_series("coal_production", SCOPE, unit="t")[0]
    exact = resolve_figure(
        store,
        SCOPE,
        entity_id="mcl",
        metric="coal_production",
        period_label="FY2024-25",
        fiscal_year="FY2024-25",
    )

    assert exact.ok, f"the exact path refused: {exact.reason} {exact.message}"
    assert exact.fact is not None
    assert charted["value"] == exact.fact.value


def test_series_excludes_unvalidated_facts(store: Store, make_fact) -> None:
    """A figure still in review or conflicted is one the exact path refuses, so
    it must not appear on a comparison chart either."""
    store.insert_facts(
        [
            make_fact(
                entity_id="mcl",
                value=150.0e6,
                status=FactStatus.NEEDS_REVIEW,
                period_start=date(2024, 4, 1),
                period_end=date(2025, 3, 31),
            ),
            make_fact(
                entity_id="mcl",
                value=160.0e6,
                status=FactStatus.CONFLICTED,
                period_start=date(2024, 4, 1),
                period_end=date(2025, 3, 31),
            ),
        ]
    )

    assert store.entity_metric_series("coal_production", SCOPE, unit="t") == []


def test_series_omits_a_subsidiary_with_only_monthly_facts(
    store: Store, make_fact
) -> None:
    """Honest absence over a plausible wrong bar: a subsidiary with no validated
    annual total does not appear, rather than appearing with a summed-months
    number the system never validated as a year."""
    store.insert_facts(
        [
            make_fact(
                entity_id="wcl",
                value=5.0e6,
                status=FactStatus.VALIDATED,
                period_label=f"M{month} 2024",
                period_start=date(2024, month, 1),
                period_end=date(2024, month, 28),
            )
            for month in (4, 5, 6)
        ]
    )

    assert store.entity_metric_series("coal_production", SCOPE, unit="t") == []


def test_one_fiscal_year_has_exactly_one_label() -> None:
    """A span and its label must be one-to-one, because the system treats the
    label as the measurement's identity in three places.

    ``Apr 2024 - Mar 2025`` and ``FY2024-25`` are the same twelve months. A
    document writing "April 2024 to March 2025" and one writing "FY 2024-25"
    therefore describe the *same measurement*, and if the normalizer gives them
    different labels:

    - the conflict radar groups on ``period_label``, so two sources disagreeing
      about that year are never put in the same group and the disagreement stays
      invisible;
    - ``resolve_figure`` matches on ``period_label``, so asking for FY2024-25
      returns one of them and silently ignores the other;
    - ``entity_metric_series`` groups on ``(entity, fiscal_year, unit)``, so two
      full-year facts for one year land in one bar. It no longer adds them — see
      the duplicate-source test above — but canonical labels are what keep the
      radar and ``resolve_figure`` from being fooled in the first place.

    The last one is the same overcount class as the test at the top of this file,
    reached through a different door.
    """
    from mrip.normalize.periods import normalize_period

    canonical = normalize_period("FY2024-25")
    spelled_out = normalize_period("April 2024 to March 2025")
    abbreviated = normalize_period("Apr 2024 - Mar 2025")

    # Same twelve months, by construction.
    for other in (spelled_out, abbreviated):
        assert (other.start, other.end) == (canonical.start, canonical.end)
        assert other.fiscal_year == canonical.fiscal_year
        # ...so the same label, or the three bullets above all come true.
        assert other.label == canonical.label, (
            f"{other.label!r} and {canonical.label!r} are the same span but would "
            "be treated as two different measurements"
        )


def test_a_part_year_is_never_relabelled_as_the_whole_year() -> None:
    """The boundary the canonicalisation must not cross.

    Every CIL monthly statement carries a year-to-date column beside the month's
    own figure. Those runs get the canonical name of whatever fiscal period they
    exactly cover — ``Apr 2024 - Sep 2024`` is ``H1 FY2024-25``, because it is —
    but a part-year must never take the *year's* label. Doing so would merge a
    half into the year, overstating production by however much of the year has
    elapsed, which is the overcount this whole area exists to prevent.

    A run that covers no named period keeps its month-range label, because it has
    no other honest name: five months is nobody's quarter.
    """
    from mrip.normalize.periods import PeriodKind, normalize_period

    half = normalize_period("Apr 2024 - Sep 2024")
    assert half.label == "H1 FY2024-25"  # it is H1, exactly
    assert half.fiscal_year == "FY2024-25"
    assert half.label != "FY2024-25", "a half must not take the year's name"
    assert half.kind is PeriodKind.FISCAL_HALF

    quarter = normalize_period("Apr 2024 - Jun 2024")
    assert quarter.label == "Q1 FY2024-25"
    assert quarter.label != "FY2024-25"

    # Five months. Not a quarter, not a half, not the year — so it keeps the only
    # name it has, and stays distinguishable from all three.
    five = normalize_period("Apr 2024 - Aug 2024")
    assert five.label == "Apr 2024 - Aug 2024"
    assert five.kind is PeriodKind.MONTH_RANGE
    assert five.fiscal_year == "FY2024-25"


def test_a_canonicalised_range_says_how_it_was_written() -> None:
    """Relabelling must not lose the fact that the source spelled out months.

    The label is the measurement's identity and has to be canonical, but a
    reviewer reading the fact back needs to know the document said
    "APR'24 - SEP'24" rather than "H1". That lives on ``note``, so the identity
    stays clean without the provenance being thrown away.
    """
    from mrip.normalize.periods import normalize_period

    half = normalize_period("APR'24 - SEP'24")

    assert half.label == "H1 FY2024-25"
    assert half.raw == "APR'24 - SEP'24"
    assert half.note is not None
    assert "run of months" in half.note


# ------------------------------------------------------- parse_number


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,23,456.78", 123456.78),  # Indian grouping
        ("(1,234)", -1234.0),  # accounting negative
        ("193.00", 193.0),
        ("45%", 45.0),
        ("63.5", 63.5),
    ],
)
def test_parse_number_accepts_real_figures(text: str, expected: float) -> None:
    assert parse_number(text) == expected


@pytest.mark.parametrize("text", ["63 5", "6 3.5", "1 2 3", "12 34"])
def test_parse_number_refuses_split_digits(text: str) -> None:
    """An OCR'd '63.5' arriving as '63 5' must refuse (→ review), never become
    635.0 — a lost decimal must not silently become a tenfold error."""
    assert parse_number(text) is None


# ----------------------------------------------------------- review queue


def _needs_review_fact(store: Store, make_fact, **kw):
    store.insert_facts([make_fact(status=FactStatus.NEEDS_REVIEW, **kw)])
    return store.query_facts(SCOPE, status=FactStatus.NEEDS_REVIEW)[0]


def test_a_reviewer_can_validate_a_needs_review_fact(store: Store, make_fact) -> None:
    """The dead end the audit found: a low-confidence fact had no path to
    validated except through a conflict. Now it does."""
    fact = _needs_review_fact(store, make_fact)

    updated = store.review_fact(fact.fact_id, "validate", SCOPE)

    assert updated is not None
    assert updated.status is FactStatus.VALIDATED
    assert updated.review_state.value == "approved"


def test_a_reviewer_can_correct_a_misread_figure(store: Store, make_fact) -> None:
    fact = _needs_review_fact(store, make_fact, value=635.0e6)

    updated = store.review_fact(
        fact.fact_id, "correct", SCOPE, corrected_value=63.5e6, note="OCR lost a dot"
    )

    assert updated is not None
    assert updated.value == 63.5e6
    assert updated.status is FactStatus.VALIDATED
    assert updated.review_state.value == "corrected"
    # The raw receipt is untouched — the evidence still quotes the page.
    assert updated.raw_value == 193.0


def test_correct_without_a_value_is_refused(store: Store, make_fact) -> None:
    fact = _needs_review_fact(store, make_fact)
    assert store.review_fact(fact.fact_id, "correct", SCOPE) is None


def test_a_reviewer_can_reject_a_fact(store: Store, make_fact) -> None:
    fact = _needs_review_fact(store, make_fact)

    updated = store.review_fact(fact.fact_id, "reject", SCOPE, note="not a real figure")

    assert updated is not None
    assert updated.status is FactStatus.REJECTED
    assert updated.review_state.value == "rejected"


def test_review_does_not_reopen_a_settled_fact(store: Store, make_fact) -> None:
    """A validated fact is a decision; a second review must not silently flip it."""
    store.insert_facts([make_fact(status=FactStatus.VALIDATED)])
    fact = store.query_facts(SCOPE, status=FactStatus.VALIDATED)[0]

    assert store.review_fact(fact.fact_id, "reject", SCOPE) is None


def test_review_is_scoped(store: Store, make_fact) -> None:
    """A reviewer adjudicates only facts for the subsidiaries they cover."""
    fact = _needs_review_fact(store, make_fact, entity_id="mcl")

    assert store.review_fact(fact.fact_id, "validate", Scope.of("secl")) is None
    assert store.review_fact(fact.fact_id, "validate", Scope.of("mcl")) is not None
