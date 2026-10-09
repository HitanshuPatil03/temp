"""One span, one label — the invariant three subsystems depend on.

``period_label`` is a measurement's identity in this system. The conflict radar
groups on it, :func:`mrip.query.exact.resolve_figure` matches on it, and a report
manifest pins figures by it. So two period strings that resolve to the **same date
span** must resolve to the **same label**, or one measurement becomes two:

- two sources disagreeing about that span are never grouped, so the disagreement
  stays invisible and both figures stay validated;
- an exact-figure query returns one of them and silently ignores the other;
- :meth:`mrip.db.repositories.facts.FactRepository.entity_metric_series` groups
  every full-year fact within a fiscal year into one bar, so two spellings of the
  same year land together. That no longer *doubles* the bar — the aggregate picks
  a representative value rather than adding — but one canonical label per span is
  still what keeps the radar and the exact path from being fooled.

This file asserts the invariant over the whole vocabulary rather than over the one
spelling that happened to break it (``FY2024-25`` versus ``April 2024 to March
2025``). A normalizer that resolves a span correctly but names it inconsistently is
a normalizer that has not done its job.
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from mrip.normalize.periods import Period, UnknownPeriodError, normalize_period

#: Every shape the parser accepts, spelled several ways each. Deliberately
#: includes the pairs that *should* collide (a fiscal year written as a month
#: range) and the ones that must not (a half-year inside the same fiscal year).
SPELLINGS: tuple[str, ...] = (
    # Fiscal years, four ways.
    "FY2024-25",
    "FY 2024-25",
    "2024-25",
    "fy24-25",
    "April 2024 to March 2025",
    "Apr 2024 - Mar 2025",
    "APR'24 - MAR'25",
    # Quarters.
    "Q1 FY2024-25",
    "Q2 FY2024-25",
    "Q3 FY2024-25",
    "Q4 FY2024-25",
    # Halves.
    "H1 FY2024-25",
    "H2 FY2024-25",
    # Single months, several ways.
    "Apr 2024",
    "April 2024",
    "APR'24",
    "Aug 2024",
    "AUG'24",
    "Mar 2025",
    # Cumulatives that exactly cover a named period — these are the collisions:
    # a September year-to-date column *is* H1, a June one *is* Q1.
    "Apr 2024 - Sep 2024",
    "APR'24 - SEP'24",
    "Apr 2024 - Jun 2024",
    "APR'24 - JUN'24",
    "Oct 2024 - Mar 2025",
    "Oct 2024 - Dec 2024",
    # ...and ones that cover no named period, so keep their own label.
    "Apr 2024 - Aug 2024",
    "Apr 2024 - Feb 2025",
    # A degenerate range that is really one month.
    "Aug 2024 - Aug 2024",
    # Calendar years, which are a different span from the fiscal year.
    "2024",
    "CY2024",
)


def _resolved() -> list[Period]:
    periods: list[Period] = []
    for raw in SPELLINGS:
        try:
            periods.append(normalize_period(raw))
        except UnknownPeriodError:  # pragma: no cover - a spelling this parser declines
            continue
    return periods


def test_the_vocabulary_actually_resolves() -> None:
    """A guard on the guard: if most of these stopped parsing, the invariant below
    would hold vacuously and prove nothing."""
    resolved = _resolved()
    assert len(resolved) >= len(SPELLINGS) - 2, (
        f"only {len(resolved)} of {len(SPELLINGS)} spellings resolved; this file's "
        "other tests would pass without checking much"
    )


def test_the_same_span_always_gets_the_same_label() -> None:
    """The invariant. One span, one name."""
    by_span: dict[tuple[object, object], set[str]] = defaultdict(set)
    sources: dict[tuple[object, object], set[str]] = defaultdict(set)
    for period in _resolved():
        key = (period.start, period.end)
        by_span[key].add(period.label)
        sources[key].add(period.raw)

    clashes = {span: labels for span, labels in by_span.items() if len(labels) > 1}
    assert clashes == {}, "\n".join(
        f"{span[0]}..{span[1]} is labelled {sorted(labels)} "
        f"(from {sorted(sources[span])})"
        for span, labels in clashes.items()
    )


def test_the_same_label_always_means_the_same_span() -> None:
    """The converse, which matters just as much.

    Two different spans sharing a label would make a stored figure ambiguous: a
    report pinned to "FY2024-25" would be re-resolvable to either, and the diff
    endpoint would report a change that never happened.
    """
    by_label: dict[str, set[tuple[object, object]]] = defaultdict(set)
    for period in _resolved():
        by_label[period.label].add((period.start, period.end))

    clashes = {label: spans for label, spans in by_label.items() if len(spans) > 1}
    assert clashes == {}, "\n".join(
        f"{label!r} means {sorted(spans)}" for label, spans in clashes.items()
    )


def test_a_cumulative_is_never_relabelled_as_its_fiscal_year() -> None:
    """The boundary the fiscal-year canonicalisation must not cross.

    Every CIL monthly statement reports each figure twice — once for the month,
    once for the year so far. Giving a part-year cumulative the fiscal year's label
    would merge a half-year into the year, which overstates production by however
    much of the year has elapsed.
    """
    for raw in ("Apr 2024 - Sep 2024", "Apr 2024 - Aug 2024", "Apr 2024 - Feb 2025"):
        period = normalize_period(raw)
        assert period.fiscal_year == "FY2024-25"
        assert period.label != "FY2024-25", (
            f"{raw!r} is a cumulative within FY2024-25, not the year itself"
        )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("FY2024-25", "April 2024 to March 2025"),
        ("FY2024-25", "APR'24 - MAR'25"),
        ("2024-25", "Apr 2024 - Mar 2025"),
        ("H1 FY2024-25", "Apr 2024 - Sep 2024"),
        ("H2 FY2024-25", "Oct 2024 - Mar 2025"),
        ("Q1 FY2024-25", "APR'24 - JUN'24"),
        ("Q3 FY2024-25", "Oct 2024 - Dec 2024"),
        ("Aug 2024", "Aug 2024 - Aug 2024"),
    ],
)
def test_two_spellings_of_one_period_are_interchangeable(left: str, right: str) -> None:
    """Named pairs, so a failure says which spelling broke rather than only that
    some span has two labels."""
    a, b = normalize_period(left), normalize_period(right)

    assert (a.start, a.end) == (b.start, b.end)
    assert a.label == b.label
    assert a.fiscal_year == b.fiscal_year


def test_no_two_distinct_periods_share_a_dedup_key() -> None:
    """``Fact.dedup_key`` is ``(entity, metric, unit, period_start, period_end)``
    and the conflict radar groups by ``period_label``. Those two definitions of
    "the same measurement" only agree while span and label are one-to-one, which is
    what the tests above establish — this one states the consequence directly, so a
    future change to either definition fails here with the reason attached."""
    spans = {(p.start, p.end): p.label for p in _resolved()}
    labels = {p.label: (p.start, p.end) for p in _resolved()}

    assert len(spans) == len(labels), (
        "span↔label is no longer a bijection, so grouping by span and grouping by "
        "label would put different facts in different groups"
    )
    for span, label in spans.items():
        assert labels[label] == span


def test_distinct_quarters_and_halves_do_not_collapse() -> None:
    """A sanity check that the vocabulary above contains genuinely different
    spans — otherwise the bijection tests could pass on a degenerate set."""
    quarters = {normalize_period(f"Q{index} FY2024-25").label for index in range(1, 5)}
    halves = {normalize_period(f"H{index} FY2024-25").label for index in (1, 2)}

    assert len(quarters) == 4
    assert len(halves) == 2
    assert quarters.isdisjoint(halves)
