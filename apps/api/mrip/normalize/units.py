"""Unit normalization for Indian coal-sector reporting.

Mining reports mix Indian numbering (lakh, crore) with SI prefixes, and abbreviate
aggressively: ``MT``, ``LT``, ``Mm3``, ``M.Cum``, ``MTPA``. A figure is only
comparable across documents once it is in a canonical base unit, so every fact in
MRIP stores *both* the canonical value and the raw representation the document
printed.

The one genuinely hard case is ``MT``. Internationally it reads "metric tonne";
in CIL/CMPDI production reporting it almost always means "million tonnes". We
resolve it to the domain convention but always set ``ambiguous``, so the UI can
show a caution badge and a reviewer can override. Guessing silently is exactly
the failure mode this module exists to prevent.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Dimension",
    "MTConvention",
    "NormalizedUnit",
    "Quantity",
    "UnknownUnitError",
    "normalize_quantity",
    "normalize_unit",
]


class Dimension(StrEnum):
    """Physical dimension of a measurement, used to block nonsense comparisons."""

    MASS = "mass"
    VOLUME = "volume"
    LENGTH = "length"
    AREA = "area"
    MONEY = "money"
    RATIO = "ratio"
    COUNT = "count"
    CALORIFIC = "calorific"
    MASS_RATE = "mass_rate"
    VOLUME_RATE = "volume_rate"


#: Canonical base unit per dimension. Every stored value is expressed in these.
CANONICAL: dict[Dimension, str] = {
    Dimension.MASS: "t",
    Dimension.VOLUME: "m3",
    Dimension.LENGTH: "m",
    Dimension.AREA: "ha",
    Dimension.MONEY: "inr",
    Dimension.RATIO: "fraction",
    Dimension.COUNT: "count",
    Dimension.CALORIFIC: "kcal/kg",
    Dimension.MASS_RATE: "t/yr",
    Dimension.VOLUME_RATE: "m3/yr",
}


class MTConvention(StrEnum):
    """How to read a bare ``MT``.

    ``MILLION_TONNES`` matches CIL/CMPDI production reporting and is the default.
    """

    MILLION_TONNES = "million_tonnes"
    METRIC_TONNE = "metric_tonne"


class UnknownUnitError(ValueError):
    """Raised when a unit string cannot be resolved to a canonical form."""


#: Scale words. Indian numbering sits alongside SI so both parse in one pass.
_MULTIPLIERS: dict[str, float] = {
    "hundred": 1e2,
    "thousand": 1e3,
    "k": 1e3,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "million": 1e6,
    "mn": 1e6,
    "mio": 1e6,
    "crore": 1e7,
    "crores": 1e7,
    "cr": 1e7,
    "billion": 1e9,
    "bn": 1e9,
    # ---- prefixes in the singular and in the forms CIL actually prints ----
    #
    # The CIL production statements head their tables "COAL PRODUCTION (Figs in
    # Mill Te)". The tokenizer splits that into ["mill", "te"], and because
    # unrecognised tokens are tolerated (below), "mill" was dropped on the floor
    # and the value came out in **tonnes**: 193.00 Mt stored as 193 t. A factor
    # of a million, on the headline series this platform exists to extract, in
    # the direction that makes CIL look like a rounding error.
    #
    # "mil"/"mill" are ambiguous in general — "mil" is also a thousandth of an
    # inch and a Scandinavian mile — but in a coal production table headed "Figs
    # in Mill Te" there is exactly one reading, and the table says so.
    #
    # Bare "M" is deliberately **not** here. It is the metre, and putting it in
    # this table shadowed that (multipliers are tested first) so "Depth in m"
    # stopped resolving. The "M Te" spelling is handled as a composite instead,
    # where the mass unit beside it is what makes the reading unambiguous.
    "mil": 1e6,
    "mill": 1e6,
    "mils": 1e6,
    "mills": 1e6,
}

#: Base units: token -> (dimension, factor to canonical).
_BASE_UNITS: dict[str, tuple[Dimension, float]] = {
    # mass
    "t": (Dimension.MASS, 1.0),
    "te": (Dimension.MASS, 1.0),
    "ton": (Dimension.MASS, 1.0),
    "tons": (Dimension.MASS, 1.0),
    "tonne": (Dimension.MASS, 1.0),
    "tonnes": (Dimension.MASS, 1.0),
    "tonnage": (Dimension.MASS, 1.0),
    "kg": (Dimension.MASS, 1e-3),
    "quintal": (Dimension.MASS, 0.1),
    "quintals": (Dimension.MASS, 0.1),
    # volume
    "m3": (Dimension.VOLUME, 1.0),
    "cum": (Dimension.VOLUME, 1.0),
    "cbm": (Dimension.VOLUME, 1.0),
    "cubicmetre": (Dimension.VOLUME, 1.0),
    "cubicmetres": (Dimension.VOLUME, 1.0),
    "cubicmeter": (Dimension.VOLUME, 1.0),
    "cubicmeters": (Dimension.VOLUME, 1.0),
    "litre": (Dimension.VOLUME, 1e-3),
    "litres": (Dimension.VOLUME, 1e-3),
    # length
    "m": (Dimension.LENGTH, 1.0),
    "metre": (Dimension.LENGTH, 1.0),
    "metres": (Dimension.LENGTH, 1.0),
    "meter": (Dimension.LENGTH, 1.0),
    "meters": (Dimension.LENGTH, 1.0),
    "km": (Dimension.LENGTH, 1e3),
    "cm": (Dimension.LENGTH, 1e-2),
    "mm": (Dimension.LENGTH, 1e-3),
    # area
    "ha": (Dimension.AREA, 1.0),
    "hectare": (Dimension.AREA, 1.0),
    "hectares": (Dimension.AREA, 1.0),
    "acre": (Dimension.AREA, 0.404686),
    "acres": (Dimension.AREA, 0.404686),
    "km2": (Dimension.AREA, 100.0),
    "sqkm": (Dimension.AREA, 100.0),
    "m2": (Dimension.AREA, 1e-4),
    "sqm": (Dimension.AREA, 1e-4),
    # money
    "inr": (Dimension.MONEY, 1.0),
    "rs": (Dimension.MONEY, 1.0),
    "rupee": (Dimension.MONEY, 1.0),
    "rupees": (Dimension.MONEY, 1.0),
    # ratio
    "%": (Dimension.RATIO, 0.01),
    "percent": (Dimension.RATIO, 0.01),
    "pct": (Dimension.RATIO, 0.01),
    "fraction": (Dimension.RATIO, 1.0),
    "ratio": (Dimension.RATIO, 1.0),
    # count
    "count": (Dimension.COUNT, 1.0),
    "nos": (Dimension.COUNT, 1.0),
    "no": (Dimension.COUNT, 1.0),
    "number": (Dimension.COUNT, 1.0),
    "units": (Dimension.COUNT, 1.0),
    # calorific value (coal grade)
    "kcal/kg": (Dimension.CALORIFIC, 1.0),
    "kcalkg": (Dimension.CALORIFIC, 1.0),
}

#: Rate dimension implied by a denominator, plus factor to a per-year canonical.
_PERIOD_DENOMINATORS: dict[str, float] = {
    "yr": 1.0,
    "year": 1.0,
    "annum": 1.0,
    "a": 1.0,
    "pa": 1.0,
    "month": 12.0,
    "mth": 12.0,
    "day": 365.0,
    "d": 365.0,
    "hr": 8760.0,
    "hour": 8760.0,
}

_RATE_DIMENSION: dict[Dimension, Dimension] = {
    Dimension.MASS: Dimension.MASS_RATE,
    Dimension.VOLUME: Dimension.VOLUME_RATE,
}

#: Sector abbreviations that do not decompose cleanly, resolved before tokenising.
#: value = (dimension, factor to canonical, ambiguous, note)
_COMPOSITES: dict[str, tuple[Dimension, float, bool, str | None]] = {
    "mt": (
        Dimension.MASS,
        1e6,
        True,
        "'MT' read as million tonnes (CIL/CMPDI convention); "
        "may mean metric tonne in non-Indian sources",
    ),
    "mmt": (Dimension.MASS, 1e6, False, None),
    "mnt": (Dimension.MASS, 1e6, False, None),
    "lt": (Dimension.MASS, 1e5, False, None),
    "mtpa": (Dimension.MASS_RATE, 1e6, False, None),
    "mtpy": (Dimension.MASS_RATE, 1e6, False, None),
    "tpd": (Dimension.MASS_RATE, 365.0, False, None),
    "tpa": (Dimension.MASS_RATE, 1.0, False, None),
    "mm3": (Dimension.VOLUME, 1e6, False, None),
    "mcum": (Dimension.VOLUME, 1e6, False, None),
    "mcm": (Dimension.VOLUME, 1e6, False, None),
    "lcum": (Dimension.VOLUME, 1e5, False, None),
    "gcv": (Dimension.CALORIFIC, 1.0, False, None),
    # "M Te" / "M.Te", the spelling CIL uses beside "Mill Te" in the same series
    # of statements. Handled here rather than by a bare "M" multiplier, because
    # the mass unit sitting next to it is exactly what rules out the metre.
    "mte": (Dimension.MASS, 1e6, False, None),
    "mt.": (Dimension.MASS, 1e6, False, None),
    "mtonnes": (Dimension.MASS, 1e6, False, None),
}

#: Words that appear in a unit caption without bearing a dimension. The list is
#: closed on purpose: a token outside it is **refused**, not ignored.
#:
#: Tolerating unknown tokens is how "Figs in Miil Te" — OCR for "Mill Te" — came
#: out as plain tonnes. "miil" was dropped on the floor, the multiplier went with
#: it, and 2.7 million tonnes was stored as 2.7 tonnes with a clean receipt and
#: full confidence. A factor of a million, in the direction that makes CIL look
#: like a village quarry, on the one series this platform exists to report.
#:
#: So an unreadable word in the multiplier slot now raises. The caller counts it
#: as `ambiguous_unit` and shows the cell to a reviewer, which is the difference
#: between a number we declined to read and a number we got wrong.
_STOPWORDS = frozenset(
    {
        # grammatical
        "in",
        "of",
        "the",
        "as",
        "at",
        "all",
        "and",
        "amount",
        "figure",
        "figures",
        "figs",
        "fig",
        "qty",
        "quantity",
        "value",
        "values",
        "total",
        "nos",
        "no",
        "unit",
        "units",
        "terms",
        "wise",
        # what these captions are measuring — descriptive, not dimensional
        "coal",
        "raw",
        "washed",
        "production",
        "produced",
        "raising",
        "offtake",
        "despatch",
        "despatches",
        "dispatch",
        "dispatches",
        "sales",
        "sale",
        "supply",
        "supplies",
        "stock",
        "stocks",
        "overburden",
        "ob",
        "removal",
        "capacity",
        "target",
        "actual",
        "actuals",
        "provisional",
        "estimated",
        "revenue",
        "turnover",
        "expenditure",
        "profit",
        "loss",
        "manpower",
        "employees",
        "area",
        "areas",
        "mine",
        "mines",
        "land",
        "power",
        "consumption",
        "depth",
        "thickness",
        "reserves",
        "resource",
        "resources",
    }
)

_SUPERSCRIPTS = str.maketrans({"²": "2", "³": "3", "¹": "1"})


@dataclass(frozen=True, slots=True)
class NormalizedUnit:
    """A unit string resolved to a canonical base unit."""

    canonical_unit: str
    dimension: Dimension
    factor: float
    raw_unit: str
    ambiguous: bool = False
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Quantity:
    """A measurement carrying both its canonical and as-printed form.

    ``value``/``unit`` are what MRIP compares and aggregates on.
    ``raw_value``/``raw_unit`` are what the source document actually said, kept so
    an evidence receipt can quote the document rather than our rewrite of it.
    """

    value: float
    unit: str
    dimension: Dimension
    raw_value: float
    raw_unit: str
    ambiguous: bool = False
    note: str | None = None

    @property
    def display(self) -> str:
        """Human-readable canonical form, e.g. ``'3.20e+05 t'``."""
        return f"{self.value:,.4g} {self.unit}"


def _clean(raw: str) -> str:
    """Fold a unit string to comparable lowercase ASCII-ish text."""
    text = unicodedata.normalize("NFKC", raw).strip().lower()
    text = text.translate(_SUPERSCRIPTS)
    text = text.replace("₹", " rs ").replace("$", " usd ")
    # 'per' and '/' both denote a rate; unify on '/'
    text = re.sub(r"\bper\b", "/", text)
    # drop bracketed asides: "production (in MT)" -> "production  in MT"
    text = re.sub(r"[()\[\]]", " ", text)
    # collapse separators, but keep '/', '%' and digits
    text = re.sub(r"[.,;:_+\-–—]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    """Split cleaned text into unit-bearing tokens, dropping noise."""
    parts = re.findall(r"%|[a-z]+\d*|\d+", text)
    return [p for p in parts if p not in _STOPWORDS]


def _squash(text: str) -> str:
    """Collapse cleaned text to a single alphanumeric run.

    Lets ``"M.Cum"``, ``"M Cum"`` and ``"mcum"`` all reach the same table entry,
    and keeps ``kcal/kg`` intact as one base unit rather than a rate.
    """
    return re.sub(r"[^a-z0-9%]", "", text)


def _resolve_side(
    tokens: list[str], context: str
) -> tuple[Dimension, float, bool, str | None]:
    """Resolve one side of a rate expression to (dimension, factor, ambiguous, note).

    Args:
        tokens: Cleaned tokens for this side of the expression.
        context: The original unit string, quoted back in any refusal so the
            caller is told what was rejected rather than just that something was.
    """
    multiplier = 1.0
    base: tuple[Dimension, float] | None = None
    ambiguous = False
    note: str | None = None

    for token in tokens:
        # A composite only stands alone (MTPA, Mm3); never mix it with a multiplier.
        if base is None and multiplier == 1.0 and token in _COMPOSITES:
            dim, factor, amb, cnote = _COMPOSITES[token]
            return dim, factor, amb, cnote
        if token in _MULTIPLIERS:
            multiplier *= _MULTIPLIERS[token]
            continue
        if token in _BASE_UNITS and base is None:
            base = _BASE_UNITS[token]
            continue
        if base is None:
            # The multiplier slot. A word here that we cannot read may *be* the
            # multiplier, and dropping it silently turns "Mill Te" into tonnes —
            # see the note on `_STOPWORDS`. Refusing costs one skipped cell;
            # tolerating cost a factor of a million.
            raise UnknownUnitError(
                f"{token!r} in {context!r} is neither a unit nor a known "
                "multiplier, and it stands where a scale word would "
                "(lakh / million / crore); refusing rather than reading "
                f"{context!r} as though it had no scale"
            )
        # Past the base unit, a stray word is trailing prose ("tonnes raised"),
        # and Indian tables put the scale *before* the unit, so nothing
        # dimensional can be hiding here.

    if base is None:
        raise UnknownUnitError(f"no recognisable unit in {context!r}")

    dimension, base_factor = base
    return dimension, multiplier * base_factor, ambiguous, note


def normalize_unit(
    raw_unit: str,
    *,
    mt_convention: MTConvention = MTConvention.MILLION_TONNES,
) -> NormalizedUnit:
    """Resolve a raw unit string to its canonical base unit.

    Args:
        raw_unit: Unit text as printed, e.g. ``"lakh tonnes"``, ``"Mm³"``, ``"MTPA"``.
        mt_convention: How to read a bare ``MT``. Defaults to the CIL/CMPDI reading.

    Returns:
        The resolved unit, with ``factor`` to multiply a raw value by.

    Raises:
        UnknownUnitError: If no base unit can be identified.

    Examples:
        >>> normalize_unit("lakh tonnes").factor
        100000.0
        >>> normalize_unit("%").canonical_unit
        'fraction'
        >>> u = normalize_unit("MT")
        >>> u.factor, u.ambiguous
        (1000000.0, True)
        >>> normalize_unit("M.Cum").dimension.value
        'volume'
        >>> normalize_unit("kcal/kg").dimension.value
        'calorific'
    """
    if not raw_unit or not raw_unit.strip():
        raise UnknownUnitError("empty unit string")

    raw = raw_unit.strip()
    text = _clean(raw_unit)
    squashed = _squash(text)

    def _built(
        dimension: Dimension, factor: float, ambiguous: bool, note: str | None
    ) -> NormalizedUnit:
        return NormalizedUnit(
            canonical_unit=CANONICAL[dimension],
            dimension=dimension,
            factor=factor,
            raw_unit=raw,
            ambiguous=ambiguous,
            note=note,
        )

    # A bare 'MT' is the one case the caller's convention may override.
    if squashed == "mt" and mt_convention is MTConvention.METRIC_TONNE:
        return _built(
            Dimension.MASS, 1.0, True, "'MT' read as metric tonne by explicit convention"
        )

    # Whole-string match first: 'kcal/kg' is one unit, not a rate, and 'M.Cum' must
    # not decompose into metres.
    if squashed in _BASE_UNITS:
        dimension, factor = _BASE_UNITS[squashed]
        return _built(dimension, factor, False, None)
    if squashed in _COMPOSITES:
        return _built(*_COMPOSITES[squashed])

    numerator_text, _, denominator_text = text.partition("/")

    num_tokens = _tokens(numerator_text)
    den_tokens = _tokens(denominator_text)

    if not num_tokens:
        raise UnknownUnitError(f"no recognisable unit in {raw_unit!r}")

    dimension, factor, ambiguous, note = _resolve_side(num_tokens, raw_unit)

    # kcal/kg is a single base unit, not a rate — the fast path above matched it.
    if den_tokens and dimension is not Dimension.CALORIFIC:
        denominator = next((t for t in den_tokens if t in _PERIOD_DENOMINATORS), None)
        if denominator is None:
            if any(t in _BASE_UNITS for t in den_tokens):
                raise UnknownUnitError(
                    f"unsupported compound unit {raw_unit!r}: "
                    "only per-period rates are normalized"
                )
        else:
            rate_dimension = _RATE_DIMENSION.get(dimension)
            if rate_dimension is None:
                raise UnknownUnitError(
                    f"{dimension.value} has no per-period canonical form ({raw_unit!r})"
                )
            dimension = rate_dimension
            factor *= _PERIOD_DENOMINATORS[denominator]

    return _built(dimension, factor, ambiguous, note)


def normalize_quantity(
    raw_value: float,
    raw_unit: str,
    *,
    mt_convention: MTConvention = MTConvention.MILLION_TONNES,
) -> Quantity:
    """Convert a raw value + unit into a canonical :class:`Quantity`.

    Examples:
        >>> q = normalize_quantity(3.2, "lakh tonnes")
        >>> q.value, q.unit
        (320000.0, 't')
        >>> normalize_quantity(45.0, "%").value
        0.45
        >>> normalize_quantity(704.2, "MT").value
        704200000.0
    """
    unit = normalize_unit(raw_unit, mt_convention=mt_convention)
    return Quantity(
        value=raw_value * unit.factor,
        unit=unit.canonical_unit,
        dimension=unit.dimension,
        raw_value=raw_value,
        raw_unit=unit.raw_unit,
        ambiguous=unit.ambiguous,
        note=unit.note,
    )
