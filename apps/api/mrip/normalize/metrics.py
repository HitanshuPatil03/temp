"""The metric lexicon.

What counts as a *figure worth extracting*, and what to call it. A closed,
curated vocabulary rather than an open one, for the same reason the entity
resolver has a closed list of subsidiaries: this platform reports to the Ministry
of Coal, and "we found a number next to some words" is not a reportable fact.

Each metric declares the **dimension** it must have. That is the check that
catches a misread table: if the column under "Production (MT)" parses as a
percentage, the dimension does not match and the candidate is refused rather
than stored as a production figure of 0.45 tonnes. A lexicon without dimensions
would happily accept it.

Matching is deterministic and explainable — exact match on a folded string, then
alias, then a contains-check on distinctive phrases. There is no fuzzy score
here. A label that does not match is *reported as unmatched*, and the reviewer
sees the literal text that was skipped, which is a better failure than a
confident wrong metric.

The vocabulary comes from what CIL and CMPDI actually publish: the production
and despatch series in the CIL annual report, the overburden and stripping-ratio
figures in mine plans, the capacity and capex lines in CMPDI project reports.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Final

from mrip.normalize.units import Dimension

__all__ = [
    "METRICS",
    "Metric",
    "all_metric_keys",
    "resolve_metric",
]


@dataclass(frozen=True, slots=True)
class Metric:
    """One reportable quantity."""

    key: str
    label: str
    dimension: Dimension
    #: Phrases that name this metric in a table's row or column header. Folded
    #: the same way the input is, so case and punctuation do not matter.
    aliases: tuple[str, ...] = ()
    #: Longer distinctive phrases matched by containment, for headers that carry
    #: extra words ("Raw coal production during the year").
    phrases: tuple[str, ...] = ()
    #: What this metric means, shown in the UI beside the figure.
    definition: str = ""
    notes: list[str] = field(default_factory=list)


METRICS: Final[tuple[Metric, ...]] = (
    Metric(
        key="coal_production",
        label="Coal production",
        dimension=Dimension.MASS,
        aliases=(
            "production",
            "coal production",
            "raw coal production",
            "coal output",
            "output",
            "prod",
        ),
        phrases=("coal production", "raw coal production", "production of coal"),
        definition="Raw coal raised, before washing.",
    ),
    Metric(
        key="coal_offtake",
        label="Coal offtake",
        dimension=Dimension.MASS,
        aliases=(
            "offtake",
            "off-take",
            "coal offtake",
            "despatch",
            "dispatch",
            "coal despatch",
            "coal dispatch",
            "despatches",
            "dispatches",
        ),
        phrases=("coal offtake", "coal despatch", "coal dispatch"),
        definition="Coal moved out to consumers. Distinct from production: the "
        "difference is the change in pithead stock.",
    ),
    Metric(
        key="washed_coal_production",
        label="Washed coal production",
        dimension=Dimension.MASS,
        aliases=("washed coal", "washed coal production", "washery production"),
        phrases=("washed coal",),
        definition="Output of coal washeries.",
    ),
    Metric(
        key="overburden_removal",
        label="Overburden removal",
        dimension=Dimension.VOLUME,
        aliases=(
            "ob removal",
            "overburden",
            "overburden removal",
            "ob",
            "over burden removal",
        ),
        phrases=("overburden removal", "ob removal"),
        definition="Waste rock moved to expose coal, measured in bank cubic metres.",
    ),
    Metric(
        key="stripping_ratio",
        label="Stripping ratio",
        dimension=Dimension.RATIO,
        aliases=("stripping ratio", "sr", "strip ratio"),
        phrases=("stripping ratio",),
        definition="Cubic metres of overburden removed per tonne of coal.",
    ),
    Metric(
        key="coal_stock",
        label="Pithead coal stock",
        dimension=Dimension.MASS,
        aliases=("stock", "coal stock", "pithead stock", "closing stock"),
        phrases=("coal stock", "pithead stock"),
        definition="Coal held at the mine at period end.",
    ),
    Metric(
        key="rated_capacity",
        label="Rated capacity",
        dimension=Dimension.MASS,
        aliases=("capacity", "rated capacity", "normative capacity", "mine capacity"),
        phrases=("rated capacity", "normative capacity"),
        definition="Sanctioned annual capacity of a mine or project.",
    ),
    Metric(
        key="geological_reserves",
        label="Geological reserves",
        dimension=Dimension.MASS,
        aliases=("geological reserves", "reserves", "gr"),
        phrases=("geological reserve",),
        definition="In-place coal resource, as assessed.",
    ),
    Metric(
        key="extractable_reserves",
        label="Extractable reserves",
        dimension=Dimension.MASS,
        aliases=("extractable reserves", "mineable reserves", "extractable reserve"),
        phrases=("extractable reserve", "mineable reserve"),
        definition="Portion of the resource recoverable under the approved plan.",
    ),
    Metric(
        key="capital_expenditure",
        label="Capital expenditure",
        dimension=Dimension.MONEY,
        aliases=("capex", "capital expenditure", "capital outlay", "capital cost"),
        phrases=("capital expenditure", "capital outlay"),
        definition="Capital spent in the period.",
    ),
    Metric(
        key="revenue",
        label="Revenue from operations",
        dimension=Dimension.MONEY,
        aliases=(
            "revenue",
            "turnover",
            "sales",
            "revenue from operations",
            "gross sales",
            "net sales",
        ),
        phrases=("revenue from operations", "gross sales", "net sales"),
        definition="Sales value for the period.",
    ),
    Metric(
        key="profit_before_tax",
        label="Profit before tax",
        dimension=Dimension.MONEY,
        aliases=("pbt", "profit before tax", "profit before taxation"),
        phrases=("profit before tax",),
        definition="Earnings before tax for the period.",
    ),
    Metric(
        key="manpower",
        label="Manpower",
        dimension=Dimension.COUNT,
        aliases=(
            "manpower",
            "employees",
            "employee strength",
            "headcount",
            "man power",
            "total manpower",
        ),
        phrases=("employee strength", "total manpower"),
        definition="People on the rolls at period end.",
    ),
    Metric(
        key="productivity_oms",
        label="Output per manshift",
        dimension=Dimension.MASS,
        aliases=("oms", "output per manshift", "productivity"),
        phrases=("output per manshift",),
        definition="Tonnes produced per person per shift.",
    ),
    Metric(
        key="land_acquired",
        label="Land acquired",
        dimension=Dimension.AREA,
        aliases=("land acquired", "land acquisition", "area acquired"),
        phrases=("land acquired", "land acquisition"),
        definition="Land taken into possession for a project.",
    ),
    Metric(
        key="gcv",
        label="Gross calorific value",
        dimension=Dimension.CALORIFIC,
        aliases=("gcv", "calorific value", "gross calorific value"),
        phrases=("calorific value",),
        definition="Energy content of the coal, which determines its grade.",
    ),
)

_BY_KEY: Final[dict[str, Metric]] = {metric.key: metric for metric in METRICS}

#: Words that appear in a header without changing which metric it names. Removed
#: before matching so "Production (MT)" and "Production during the year" both
#: reach "production".
_NOISE: Final[frozenset[str]] = frozenset(
    {
        "actual",
        "annual",
        "during",
        "figures",
        "for",
        "in",
        "no",
        "of",
        "period",
        "target",
        "the",
        "total",
        "year",
        "yearly",
        "ytd",
    }
)

_PUNCTUATION = re.compile(r"[^a-z0-9\s]+")
_SPACES = re.compile(r"\s+")


def _fold(raw: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    The parenthesised unit is dropped first: "Production (Million Tonnes)" names
    the same metric as "Production", and the unit is resolved separately — by the
    unit normalizer, which is the only thing entitled to interpret it.
    """
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = _PUNCTUATION.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def _strip_noise(folded: str) -> str:
    return " ".join(word for word in folded.split() if word not in _NOISE)


def resolve_metric(raw: str) -> Metric | None:
    """Resolve a row or column label to a metric, or ``None``.

    Three passes, most specific first:

    1. the folded label matches an alias exactly;
    2. the label with filler words removed matches an alias exactly;
    3. a distinctive phrase appears inside the label.

    Containment is tried **last and only against long phrases**, because
    "production" appears inside "production target" and inside "production cost",
    and a contains-first rule would label a cost figure as production. Returning
    ``None`` is a legitimate and common answer.
    """
    folded = _fold(raw)
    if not folded:
        return None

    for metric in METRICS:
        if folded in metric.aliases:
            return metric

    stripped = _strip_noise(folded)
    if stripped:
        for metric in METRICS:
            if stripped in metric.aliases:
                return metric

    for metric in METRICS:
        for phrase in metric.phrases:
            if phrase in folded:
                return metric
    return None


def metric_by_key(key: str) -> Metric | None:
    return _BY_KEY.get(key)


def all_metric_keys() -> list[str]:
    return sorted(_BY_KEY)
