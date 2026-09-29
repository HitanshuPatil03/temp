"""Tests for the normalization layer.

These are the highest-value tests in the project. The research report's thesis is
that MRIP's differentiation lives in extraction and normalization correctness, not
in the UI or the LLM — so every unit, period and entity rule gets pinned here, and
KPI claims in the pitch are reproducible from this file.
"""

from __future__ import annotations

from datetime import date

import pytest

from mrip.normalize.entities import EntityKind, resolve_entity
from mrip.normalize.periods import (
    PeriodKind,
    UnknownPeriodError,
    comparable,
    fiscal_year_label,
    fiscal_year_of,
    normalize_period,
)
from mrip.normalize.units import (
    Dimension,
    MTConvention,
    UnknownUnitError,
    normalize_quantity,
    normalize_unit,
)

# --------------------------------------------------------------------------- units


@pytest.mark.parametrize(
    ("raw_value", "raw_unit", "expected_value", "expected_unit"),
    [
        # Indian numbering
        (3.2, "lakh tonnes", 3.2e5, "t"),
        (77.2, "LT", 77.2e5, "t"),
        (1234.5, "Rs crore", 1234.5e7, "inr"),
        (1234.5, "Rs. in crore", 1234.5e7, "inr"),
        (5.0, "thousand tonnes", 5_000.0, "t"),
        # SI / mixed
        (1.5, "million tonnes", 1.5e6, "t"),
        (250.0, "Te", 250.0, "t"),
        (250.0, "tonnes", 250.0, "t"),
        (500.0, "kg", 0.5, "t"),
        (12.4, "km", 12_400.0, "m"),
        (9.0, "sq km", 900.0, "ha"),
        (58.6, "ha", 58.6, "ha"),
        (10.0, "acres", 4.04686, "ha"),
        # volume — the 'M.Cum' family must not decompose into metres
        (83.1, "Mm3", 83.1e6, "m3"),
        (83.1, "Mm³", 83.1e6, "m3"),
        (2.5, "M.Cum", 2.5e6, "m3"),
        (2.5, "M Cum", 2.5e6, "m3"),
        (2.5, "mcum", 2.5e6, "m3"),
        # ratio
        (45.0, "%", 0.45, "fraction"),
        (45.0, "percent", 0.45, "fraction"),
        # calorific — one unit, not a rate
        (4500.0, "kcal/kg", 4500.0, "kcal/kg"),
        (4500.0, "GCV", 4500.0, "kcal/kg"),
        # rates
        (160.0, "MTPA", 160e6, "t/yr"),
        (500.0, "tonnes/year", 500.0, "t/yr"),
        (500.0, "tonnes per annum", 500.0, "t/yr"),
        (9.8, "TPD", 9.8 * 365, "t/yr"),
        # counts
        (3.0, "Nos.", 3.0, "count"),
    ],
)
def test_unit_conversions(raw_value, raw_unit, expected_value, expected_unit):
    quantity = normalize_quantity(raw_value, raw_unit)
    assert quantity.unit == expected_unit
    assert quantity.value == pytest.approx(expected_value, rel=1e-6)


def test_raw_representation_is_preserved():
    """The receipt must quote the document, not our conversion of it."""
    quantity = normalize_quantity(3.2, "lakh tonnes")
    assert (quantity.raw_value, quantity.raw_unit) == (3.2, "lakh tonnes")
    assert quantity.value == pytest.approx(3.2e5)


def test_mt_is_flagged_ambiguous_but_resolved_to_domain_convention():
    """'MT' means million tonnes in CIL reporting — but we never hide the ambiguity."""
    quantity = normalize_quantity(704.2, "MT")
    assert quantity.value == pytest.approx(704.2e6)
    assert quantity.ambiguous is True
    assert quantity.note and "million tonnes" in quantity.note


def test_mt_convention_can_be_overridden():
    quantity = normalize_quantity(250, "MT", mt_convention=MTConvention.METRIC_TONNE)
    assert quantity.value == pytest.approx(250.0)
    assert quantity.ambiguous is True


def test_unambiguous_units_are_not_flagged():
    assert normalize_quantity(1.5, "million tonnes").ambiguous is False
    assert normalize_quantity(3.2, "lakh tonnes").ambiguous is False


def test_unit_parsed_from_surrounding_header_text():
    """Real table headers read 'Production (in MT)', not a bare unit."""
    quantity = normalize_quantity(42.0, "Production (in MT)")
    assert quantity.dimension is Dimension.MASS
    assert quantity.value == pytest.approx(42e6)


def test_dimension_is_tracked_to_block_nonsense_maths():
    assert normalize_unit("tonnes").dimension is Dimension.MASS
    assert normalize_unit("Mm3").dimension is Dimension.VOLUME
    assert normalize_unit("ha").dimension is Dimension.AREA
    assert normalize_unit("%").dimension is Dimension.RATIO
    assert normalize_unit("MTPA").dimension is Dimension.MASS_RATE


@pytest.mark.parametrize("raw_unit", ["", "   ", "zzz", "wibbles", "tonnes/hectare"])
def test_unresolvable_units_are_refused_not_guessed(raw_unit):
    with pytest.raises(UnknownUnitError):
        normalize_unit(raw_unit)


@pytest.mark.parametrize("raw_unit", ["Figs in Mill Te", "Mill Te", "M Te", "M.Te"])
def test_the_scale_word_cil_actually_prints_is_read(raw_unit):
    """CIL heads its production tables "(Figs in Mill Te)", not "million tonnes"."""
    assert normalize_unit(raw_unit).factor == pytest.approx(1e6)


@pytest.mark.parametrize("raw_unit", ["Miil Te", "MiII Te", "xyzzy Te", "Figs in Nn Te"])
def test_an_unreadable_scale_word_is_refused_not_dropped(raw_unit):
    """A word we cannot read, standing where the scale word stands, is a refusal.

    This is the narrowest and most expensive bug this module has had. Unrecognised
    tokens used to be ignored, so ``Miil`` — a scan's rendering of ``Mill`` — was
    dropped, the million went with it, and 2.7 million tonnes was stored as 2.7
    **tonnes**: wrong by a factor of a million, on the headline series, with a
    clean receipt and full confidence, in the direction that makes Coal India look
    like a village quarry.

    Refusing costs one cell, shown to a reviewer as ``ambiguous_unit``. It also
    *enables* the repair: :func:`mrip.facts.performance_statement._block_unit`
    tries the caption as printed and only OCR-repairs it when that raises. If this
    returned tonnes instead of raising, the repair would never be reached and the
    layout reader would silently inherit the same error.
    """
    with pytest.raises(UnknownUnitError):
        normalize_unit(raw_unit)


def test_a_bare_m_beside_no_unit_is_still_the_metre():
    """The cost of the fix above must not be the metre.

    ``m`` was briefly added to the multiplier table to catch "M Te", and because
    multipliers are tested first it shadowed the base unit: "Depth in m" stopped
    resolving to a length. "M Te" is handled as a composite instead, where the
    mass unit beside it is what rules the metre out.
    """
    assert normalize_unit("Depth in m").dimension is Dimension.LENGTH
    assert normalize_unit("m").factor == pytest.approx(1.0)


def test_prose_after_the_unit_is_not_treated_as_a_scale():
    """Indian tables put the scale before the unit, so nothing dimensional can be
    hiding after it — and "tonnes raised" must not be refused for the trailing word."""
    assert normalize_unit("tonnes raised").dimension is Dimension.MASS
    assert normalize_unit("lakh tonnes despatched").factor == pytest.approx(1e5)


# ------------------------------------------------------------------------- periods


@pytest.mark.parametrize(
    "raw",
    ["FY2024-25", "FY 2024-25", "F.Y. 24-25", "2024-25", "2024-2025", "fy2024-25"],
)
def test_fiscal_year_spellings_all_resolve_identically(raw):
    period = normalize_period(raw)
    assert period.kind is PeriodKind.FISCAL_YEAR
    assert (period.start, period.end) == (date(2024, 4, 1), date(2025, 3, 31))
    assert period.fiscal_year == "FY2024-25"


@pytest.mark.parametrize(
    ("raw", "start", "end"),
    [
        ("Q1 FY2024-25", date(2024, 4, 1), date(2024, 6, 30)),
        ("Q2 FY2024-25", date(2024, 7, 1), date(2024, 9, 30)),
        ("Q3 FY2024-25", date(2024, 10, 1), date(2024, 12, 31)),
        ("Q4 FY2024-25", date(2025, 1, 1), date(2025, 3, 31)),
        ("H1 FY2024-25", date(2024, 4, 1), date(2024, 9, 30)),
        ("H2 FY2024-25", date(2024, 10, 1), date(2025, 3, 31)),
    ],
)
def test_indian_fiscal_quarters_and_halves(raw, start, end):
    period = normalize_period(raw)
    assert (period.start, period.end) == (start, end)
    assert period.fiscal_year == "FY2024-25"


def test_leap_year_february_end_is_correct():
    period = normalize_period("Feb 2024")
    assert period.end == date(2024, 2, 29)


@pytest.mark.parametrize(
    ("raw", "start", "end"),
    [
        ("April 2024", date(2024, 4, 1), date(2024, 4, 30)),
        ("apr-24", date(2024, 4, 1), date(2024, 4, 30)),
        ("December 2024", date(2024, 12, 1), date(2024, 12, 31)),
    ],
)
def test_month_periods(raw, start, end):
    period = normalize_period(raw)
    assert period.kind is PeriodKind.MONTH
    assert (period.start, period.end) == (start, end)


def test_calendar_year_is_not_marked_fiscal():
    """The core guard: a bare year must never acquire a fiscal_year silently."""
    period = normalize_period("2024")
    assert period.kind is PeriodKind.CALENDAR_YEAR
    assert period.fiscal_year is None
    assert period.is_fiscal is False
    assert (period.start, period.end) == (date(2024, 1, 1), date(2024, 12, 31))


def test_as_on_snapshot_is_a_point_in_time():
    period = normalize_period("as on 31.03.2025")
    assert period.kind is PeriodKind.AS_ON
    assert period.start == period.end == date(2025, 3, 31)
    assert period.fiscal_year == "FY2024-25"


def test_fy_with_single_year_is_resolved_but_flagged():
    """'FY2025' means the year ending March 2025 — plausible, so flag it."""
    period = normalize_period("FY2025")
    assert period.fiscal_year == "FY2024-25"
    assert period.ambiguous is True


def test_multi_year_span_is_refused():
    """'2024-26' is not a fiscal year; guessing which one would be wrong."""
    with pytest.raises(UnknownPeriodError):
        normalize_period("2024-26")


@pytest.mark.parametrize("raw", ["", "   ", "sometime", "n/a"])
def test_unresolvable_periods_are_refused(raw):
    with pytest.raises(UnknownPeriodError):
        normalize_period(raw)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2025, 3, 31), 2024),
        (date(2025, 4, 1), 2025),
        (date(2024, 12, 31), 2024),
        (date(2024, 1, 1), 2023),
    ],
)
def test_fiscal_year_of_boundary_dates(day, expected):
    assert fiscal_year_of(day) == expected


def test_fiscal_year_label_handles_century_rollover():
    assert fiscal_year_label(2024) == "FY2024-25"
    assert fiscal_year_label(1999) == "FY1999-00"
    assert fiscal_year_label(2009) == "FY2009-10"


def test_fiscal_year_spans_are_complete():
    period = normalize_period("FY2024-25")
    assert period.days == 365
    assert normalize_period("FY2023-24").days == 366  # Feb 2024 leap day


# --- the guard the report specifically asks for -----------------------------


def test_fiscal_years_are_comparable_with_each_other():
    ok, reason = comparable(normalize_period("FY2023-24"), normalize_period("FY2024-25"))
    assert ok is True
    assert reason is None


def test_fiscal_and_calendar_periods_are_never_comparable():
    ok, reason = comparable(normalize_period("FY2024-25"), normalize_period("2024"))
    assert ok is False
    assert reason and "fiscal and calendar" in reason


def test_different_period_kinds_are_not_comparable():
    ok, _ = comparable(normalize_period("FY2024-25"), normalize_period("Q3 FY2024-25"))
    assert ok is False


def test_snapshots_are_not_comparable_as_flows():
    snapshot = normalize_period("as on 31.03.2025")
    ok, reason = comparable(snapshot, snapshot)
    assert ok is False
    assert reason and "stock" in reason


# ------------------------------------------------------------------------ entities


@pytest.mark.parametrize(
    ("raw", "entity_id"),
    [
        ("SECL", "secl"),
        ("S.E.C.L.", "secl"),
        ("South Eastern Coalfields Ltd.", "secl"),
        ("South Eastern Coal Fields Limited", "secl"),
        ("MCL", "mcl"),
        ("Mahanadi Coalfields Limited", "mcl"),
        ("CMPDI", "cmpdi"),
        ("CMPDIL", "cmpdi"),
        ("Central Mine Planning & Design Institute Limited", "cmpdi"),
        ("Coal India Limited", "cil"),
        ("BCCL", "bccl"),
        ("North Eastern Coalfields", "nec"),
        ("Ministry of Coal", "moc"),
    ],
)
def test_entity_aliases_resolve_exactly(raw, entity_id):
    match = resolve_entity(raw)
    assert match is not None
    assert match.entity.entity_id == entity_id
    assert match.exact is True
    assert match.needs_review is False


def test_sccl_is_not_a_cil_subsidiary():
    """Rolling SCCL into CIL totals is a real reporting error — model it explicitly."""
    match = resolve_entity("Singareni Collieries")
    assert match is not None
    assert match.entity.kind is EntityKind.EXTERNAL
    assert match.entity.is_cil_group is False
    assert match.entity.note and "NOT a CIL subsidiary" in match.entity.note


def test_nlc_is_not_a_cil_subsidiary():
    match = resolve_entity("Neyveli Lignite Corporation")
    assert match is not None
    assert match.entity.is_cil_group is False


def test_all_eight_coal_producing_subsidiaries_resolve():
    codes = ["ECL", "BCCL", "CCL", "NCL", "WCL", "SECL", "MCL"]
    for code in codes:
        match = resolve_entity(code)
        assert match is not None, code
        assert match.entity.kind is EntityKind.SUBSIDIARY, code
        assert match.entity.parent == "cil", code


def test_ocr_damaged_name_is_matched_but_flagged_for_review():
    match = resolve_entity("Mahanadi Coalfeilds Limited")
    assert match is not None
    assert match.entity.entity_id == "mcl"
    assert match.exact is False
    assert match.needs_review is True


@pytest.mark.parametrize("raw", ["", "   ", "Atlantis Coal Corp", "Acme Mining Inc"])
def test_unknown_organisations_are_refused_not_guessed(raw):
    """Mis-attributing production is worse than declining to resolve a name."""
    assert resolve_entity(raw) is None
