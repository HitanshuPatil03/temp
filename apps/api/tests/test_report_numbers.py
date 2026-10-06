"""Indian digit grouping in exported reports.

The expected strings here are not hand-written. Every one was produced by
``Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 })`` in Node — the same
formatter ``apps/web/src/lib/format.ts`` uses — so this file is the contract
between the two halves of the stack rather than one half's opinion.

That contract is the point. An officer verifies a figure in the browser and
approves it; the ``.docx`` that reaches the Ministry is rendered in Python. Until
this landed the two used different conventions, so ``19,30,00,000 t`` on screen
was signed off and ``193,000,000 t`` was delivered.
"""

from __future__ import annotations

import pytest

from mrip.reports.grouping import format_indian

#: (value, what Intl.NumberFormat("en-IN") returns for it)
#:
#: Chosen to cover the places the Indian system diverges from the Western one.
#: Below six digits the two agree, which is exactly why a test using only small
#: numbers would have passed against the old code.
EN_IN = [
    (0, "0"),
    (1, "1"),
    (100, "100"),
    (999, "999"),
    # The last three digits group together, so up to five digits nothing differs.
    (1_000, "1,000"),
    (1_500, "1,500"),
    (9_999, "9,999"),
    (12_345, "12,345"),
    # Six digits is where the systems part: 1,00,000 against 100,000.
    (100_000, "1,00,000"),
    # A CIL annual production figure in canonical tonnes.
    (193_000_000, "19,30,00,000"),
    (704_200_000, "70,42,00,000"),
    (1_234_567_890, "1,23,45,67,890"),
    (-193_000_000, "-19,30,00,000"),
]


@pytest.mark.parametrize(("value", "expected"), EN_IN)
def test_grouping_matches_the_browser(value: int, expected: str) -> None:
    assert format_indian(value) == expected


#: (value, digits, what Intl.NumberFormat returns) at the rounding tie.
#:
#: Python rounds halves to even and Intl rounds them away from zero, so these
#: are the values where a naive implementation silently disagrees with the
#: screen. 2.675 is here because Intl rounds the shortest decimal
#: representation, not the stored double (which is 2.67499...).
EN_IN_TIES = [
    (0.5, 0, "1"),
    (1.5, 0, "2"),
    (2.5, 0, "3"),
    (3.5, 0, "4"),
    (-0.5, 0, "-1"),
    (-1.5, 0, "-2"),
    (0.125, 2, "0.13"),
    (0.135, 2, "0.14"),
    (2.675, 2, "2.68"),
]


@pytest.mark.parametrize(("value", "digits", "expected"), EN_IN_TIES)
def test_ties_round_the_way_the_browser_rounds(
    value: float, digits: int, expected: str
) -> None:
    """A one-tonne disagreement between the screen and the signed document is the
    worst kind to explain, because both numbers are defensible."""
    assert format_indian(value, digits=digits) == expected


def test_python_default_rounding_would_have_disagreed() -> None:
    """Why the Decimal detour exists, stated so nobody removes it as ceremony."""
    assert format_indian(2.5) == "3"
    assert f"{2.5:.0f}" == "2"


def test_a_figure_that_rounds_to_zero_is_not_printed_as_minus_zero() -> None:
    """``-0`` in a production table reads as a defect even when it is not."""
    assert format_indian(-0.4) == "0"
    assert format_indian(-0.0) == "0"


def test_decimals_are_fixed_width_not_best_effort() -> None:
    """A figures column has to line up, and two tonnages printed to different
    precisions invite the question of which one was rounded."""
    assert format_indian(1234.5, digits=2) == "1,234.50"
    assert format_indian(1_00_000, digits=2) == "1,00,000.00"


def test_grouping_is_wrong_under_western_rules() -> None:
    """Guard the guard.

    If someone "simplifies" this back to ``f"{value:,.0f}"`` every test above
    still passes for values under six digits, so this states the difference
    directly: the two conventions genuinely disagree on a real CIL figure.
    """
    assert format_indian(193_000_000) != f"{193_000_000:,.0f}"
    assert f"{193_000_000:,.0f}" == "193,000,000"


def test_the_whole_export_path_uses_it(store, make_fact) -> None:
    """Not the helper in isolation — the figure as a reader receives it.

    Rendering through `generate` and the Markdown writer is what proves the call
    sites were actually changed; a correct formatter nobody calls is the state
    this started in.
    """
    from mrip.auth.scope import Scope
    from mrip.reports.generate import generate
    from mrip.reports.render import render_markdown
    from mrip.reports.template import default_template
    from mrip.schemas import FactStatus

    store.insert_facts(
        [
            make_fact(
                value=193_000_000.0, status=FactStatus.VALIDATED, unit_ambiguous=False
            )
        ]
    )
    manifest = generate(
        store,
        Scope.unrestricted("test"),
        default_template(),
        entity="secl",
        period="FY2024-25",
    )
    assert manifest.figures, "the fixture must pin a figure for this to prove anything"

    markdown = render_markdown(default_template(), manifest)

    assert "19,30,00,000" in markdown
    assert "193,000,000" not in markdown
