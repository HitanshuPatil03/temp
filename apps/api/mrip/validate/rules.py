"""Validation rules.

Rules that read a document's figures and say what looks wrong. They do not
*correct* anything — a rule that silently adjusted a number would be this
platform inventing one — and they do not reject. Every finding routes a fact to
human review with a sentence explaining what was noticed.

The rules are domain rules, not statistics. Each encodes something that is true
of coal operations, so a finding can be explained to the officer whose figure it
is:

- **A negative production figure is impossible.** A minus sign that survived
  extraction is a parse error, not a report of negative output.
- **Production above rated capacity is suspect**, not impossible: a mine can
  exceed its sanctioned capacity, and some do, but by 30% is a number worth a
  second look before it reaches a Ministry report.
- **A year-on-year swing beyond half** is either a real event (a new mine, a
  monsoon flooding) or a decimal-point error. Both deserve a reviewer; only one
  of them survives the look.
- **Offtake far above production, with no stock to draw on,** is arithmetic that
  does not close.

Each rule states its own threshold in its docstring, because a reviewer asked to
act on a finding will immediately ask "compared with what?"
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mrip import log
from mrip.auth.scope import Scope
from mrip.schemas import Document, Fact, FactStatus

if TYPE_CHECKING:
    from mrip.db import Store

__all__ = ["RULES", "Finding", "Rule", "run_rules"]

logger = log.get_logger("mrip.validate")

#: A mine may exceed its sanctioned capacity; 30% over is where "may" becomes
#: "check this".
CAPACITY_TOLERANCE = 1.30

#: Year-on-year change beyond this is flagged. Half is deliberately loose: coal
#: output does move by a third when a new mine opens, and a tighter bound would
#: flag so much that the flag would stop meaning anything.
YOY_LIMIT = 0.50

#: Metrics that cannot be negative, whatever the document appears to say.
_NON_NEGATIVE = frozenset(
    {
        "coal_production",
        "coal_offtake",
        "washed_coal_production",
        "overburden_removal",
        "coal_stock",
        "rated_capacity",
        "geological_reserves",
        "extractable_reserves",
        "manpower",
        "land_acquired",
        "gcv",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    """Something a rule noticed about one fact."""

    rule: str
    fact_id: str
    message: str
    #: What the rule compared against, so the reviewer can check the comparison
    #: rather than only the verdict.
    context: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "fact_id": self.fact_id,
            "message": self.message,
            "context": self.context,
        }


Rule = Callable[[Sequence[Fact], "Store"], list[Finding]]


def negative_values(facts: Sequence[Fact], _store: Store) -> list[Finding]:
    """A physical quantity below zero is a parse error, not a measurement."""
    findings = []
    for fact in facts:
        if fact.metric in _NON_NEGATIVE and fact.value < 0:
            findings.append(
                Finding(
                    rule="negative_value",
                    fact_id=fact.fact_id,
                    message=(
                        f"{fact.metric.replace('_', ' ')} of {fact.raw_value} "
                        f"{fact.raw_unit} is negative, which is not physically "
                        "possible. Most likely a minus sign or a bracketed "
                        "accounting negative was read as part of the figure."
                    ),
                    context={"value": fact.value, "raw": fact.raw_value},
                )
            )
    return findings


def implausible_magnitude(facts: Sequence[Fact], _store: Store) -> list[Finding]:
    """A production figure larger than India's entire annual output.

    The sanity check that catches a unit misread: 193 *million* tonnes read as
    193 million tonnes is right; read as 193 million tonnes when the table said
    lakh tonnes is 100× wrong, and the result exceeds national production. About
    1.0 billion tonnes is the whole country's annual coal output, so anything
    above 2 billion in one row is not a figure, it is an error.
    """
    limit = 2e9  # tonnes
    findings = []
    for fact in facts:
        if (
            fact.metric in {"coal_production", "coal_offtake"}
            and fact.unit == "t"
            and fact.value > limit
        ):
            findings.append(
                Finding(
                    rule="implausible_magnitude",
                    fact_id=fact.fact_id,
                    message=(
                        f"{fact.value / 1e6:,.0f} million tonnes exceeds India's "
                        "entire annual coal production. The unit on this table "
                        f"({fact.raw_unit!r}) was probably read too generously."
                    ),
                    context={"value_tonnes": fact.value, "limit": limit},
                )
            )
    return findings


def production_above_capacity(facts: Sequence[Fact], store: Store) -> list[Finding]:
    """Production more than 30% above the same mine's rated capacity.

    Compared only against a capacity figure for the **same entity**, because a
    subsidiary's capacity says nothing about one mine's. When no capacity is
    known the rule stays silent rather than guessing one.
    """
    capacity: dict[str, float] = {}
    for fact in facts:
        if fact.metric == "rated_capacity" and fact.unit == "t":
            key = f"{fact.entity_id}|{fact.mine_or_block or ''}"
            capacity[key] = max(capacity.get(key, 0.0), fact.value)

    findings = []
    for fact in facts:
        if fact.metric != "coal_production" or fact.unit != "t":
            continue
        key = f"{fact.entity_id}|{fact.mine_or_block or ''}"
        rated = capacity.get(key)
        if rated and fact.value > rated * CAPACITY_TOLERANCE:
            findings.append(
                Finding(
                    rule="production_above_capacity",
                    fact_id=fact.fact_id,
                    message=(
                        f"Production of {fact.value / 1e6:,.2f} Mt is "
                        f"{fact.value / rated:.0%} of the rated capacity recorded in "
                        "this document. Exceeding capacity happens, but by this "
                        "margin it is worth confirming before it is reported."
                    ),
                    context={"production": fact.value, "rated_capacity": rated},
                )
            )
    return findings


def year_on_year_swing(facts: Sequence[Fact], store: Store) -> list[Finding]:
    """A change of more than half against the same series in the corpus.

    Compares against **validated** facts already stored for the same entity,
    metric and unit in an adjacent fiscal year — so the comparison is with what
    the platform has previously accepted, not with another figure from the same
    unreviewed document.
    """
    findings = []
    scope = Scope.unrestricted("validation: compares a figure with its own history")

    for fact in facts:
        if not fact.fiscal_year or fact.metric not in {
            "coal_production",
            "coal_offtake",
        }:
            continue

        history = [
            other
            for other in store.facts.query(
                scope,
                entity_id=fact.entity_id,
                metric=fact.metric,
                limit=50,
            )
            if other.unit == fact.unit
            and other.fiscal_year
            and other.fiscal_year != fact.fiscal_year
            and other.status is FactStatus.VALIDATED
        ]
        if not history:
            continue

        previous = max(history, key=lambda item: item.fiscal_year or "")
        if previous.value <= 0:
            continue
        change = (fact.value - previous.value) / previous.value
        if abs(change) > YOY_LIMIT:
            findings.append(
                Finding(
                    rule="year_on_year_swing",
                    fact_id=fact.fact_id,
                    message=(
                        f"{change:+.0%} against {previous.fiscal_year} "
                        f"({previous.value / 1e6:,.2f} Mt → {fact.value / 1e6:,.2f} Mt). "
                        "Either something real happened, or a decimal point moved."
                    ),
                    context={
                        "previous_fiscal_year": previous.fiscal_year,
                        "previous_value": previous.value,
                        "change": round(change, 4),
                    },
                )
            )
    return findings


def offtake_exceeds_production(facts: Sequence[Fact], _store: Store) -> list[Finding]:
    """Offtake far above production for the same entity and period.

    Offtake *can* exceed production — the difference comes out of pithead stock —
    so this fires only beyond 20%, which is more stock than a mine normally
    holds.
    """
    production: dict[str, float] = {}
    for fact in facts:
        if fact.metric == "coal_production" and fact.unit == "t":
            production[f"{fact.entity_id}|{fact.fiscal_year}"] = fact.value

    findings = []
    for fact in facts:
        if fact.metric != "coal_offtake" or fact.unit != "t":
            continue
        produced = production.get(f"{fact.entity_id}|{fact.fiscal_year}")
        if produced and fact.value > produced * 1.20:
            findings.append(
                Finding(
                    rule="offtake_exceeds_production",
                    fact_id=fact.fact_id,
                    message=(
                        f"Offtake of {fact.value / 1e6:,.2f} Mt is "
                        f"{fact.value / produced:.0%} of the production reported in "
                        "the same document for the same period. The difference has "
                        "to come from pithead stock; check that both figures are "
                        "for the same scope."
                    ),
                    context={"offtake": fact.value, "production": produced},
                )
            )
    return findings


#: The rule set, in the order it runs. Order does not change the outcome — the
#: rules are independent — but it does decide the order a reviewer reads them.
RULES: tuple[tuple[str, Rule], ...] = (
    ("negative_value", negative_values),
    ("implausible_magnitude", implausible_magnitude),
    ("production_above_capacity", production_above_capacity),
    ("offtake_exceeds_production", offtake_exceeds_production),
    ("year_on_year_swing", year_on_year_swing),
)


def run_rules(store: Store, document: Document) -> list[Finding]:
    """Run every rule over a document's facts and route findings to review.

    A flagged fact becomes ``needs_review`` with the rule's sentence recorded
    against it. Nothing is deleted and no value is changed: the figure the
    document printed stays exactly as it was printed, with a note saying why
    somebody should look at it.
    """
    scope = Scope.unrestricted("validation: acts on its own document's facts")
    facts = store.facts.query(
        scope, document_id=document.document_id, include_inactive=False, limit=10_000
    )
    if not facts:
        return []

    findings: list[Finding] = []
    for name, rule in RULES:
        try:
            findings.extend(rule(facts, store))
        except Exception:
            # A broken rule must not fail the document. The other rules still
            # run, and the exception is logged with the rule that raised it.
            logger.exception("validation rule failed", rule=name)

    if findings:
        store.facts.flag_for_review(
            [finding.fact_id for finding in findings],
            note="; ".join(dict.fromkeys(finding.message for finding in findings[:3]))[
                :1000
            ],
        )

    logger.info(
        "validation complete",
        document_id=document.document_id,
        facts=len(facts),
        findings=len(findings),
    )
    return findings
