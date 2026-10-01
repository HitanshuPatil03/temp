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
