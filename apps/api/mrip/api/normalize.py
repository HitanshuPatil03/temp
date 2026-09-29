"""Normalization endpoints.

These expose the deterministic normalizers directly, which is deliberate. The
extraction and normalization layer is where MRIP's correctness claims live, so
it is worth being able to interrogate it without ingesting a document — during
a demo, in a bug report, or from the frontend's unit-checker.

Every handler surfaces refusals as ``422`` with the reason. An unresolvable unit
or period is a real answer, not an error to paper over with a guess.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from mrip.api.deps import current_principal
from mrip.normalize.entities import resolve_entity
from mrip.normalize.periods import UnknownPeriodError, comparable, normalize_period
from mrip.normalize.units import MTConvention, UnknownUnitError, normalize_quantity

# Authenticated, though nothing here reads a row: these endpoints describe how
# the platform interprets figures, which is internal reasoning rather than public
# reference data. Declared once on the router — every handler in this module is a
# pure function, so there is no per-route scope to thread.
router = APIRouter(
    prefix="/normalize",
    tags=["normalize"],
    dependencies=[Depends(current_principal)],
)


@router.get("/quantity")
def normalize_quantity_endpoint(
    value: float,
    unit: str = Query(description="Unit exactly as printed, e.g. 'lakh tonnes'"),
    mt_convention: MTConvention = MTConvention.MILLION_TONNES,
) -> dict[str, Any]:
    """Convert a printed value and unit to canonical form.

    The response keeps the raw pair alongside the canonical one so a caller can
    show what the document said as well as what it means.
    """
    try:
        quantity = normalize_quantity(value, unit, mt_convention=mt_convention)
    except UnknownUnitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "value": quantity.value,
        "unit": quantity.unit,
        "dimension": quantity.dimension.value,
        "raw_value": quantity.raw_value,
        "raw_unit": quantity.raw_unit,
        "ambiguous": quantity.ambiguous,
        "note": quantity.note,
    }


@router.get("/period")
def normalize_period_endpoint(
    raw: str = Query(
        description="Period as printed, e.g. 'FY2024-25' or 'as on 31.03.2025'"
    ),
) -> dict[str, Any]:
    """Resolve a period label to an explicit date span."""
    try:
        period = normalize_period(raw)
    except UnknownPeriodError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "label": period.label,
        "kind": period.kind.value,
        "start": period.start,
        "end": period.end,
        "days": period.days,
        "fiscal_year": period.fiscal_year,
        "is_fiscal": period.is_fiscal,
        "raw": period.raw,
        "ambiguous": period.ambiguous,
        "note": period.note,
    }


@router.get("/period/comparable")
def periods_comparable(
    left: str = Query(description="First period, as printed"),
    right: str = Query(description="Second period, as printed"),
) -> dict[str, Any]:
    """Whether two periods may be placed side by side.

    This is the guard that stops a fiscal-year figure being charted against a
    calendar-year one. The frontend calls it before drawing a comparison.
    """
    try:
        first, second = normalize_period(left), normalize_period(right)
    except UnknownPeriodError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    ok, reason = comparable(first, second)
    return {
        "comparable": ok,
        "reason": reason,
        "left": first.label,
        "right": second.label,
    }


@router.get("/entity")
def resolve_entity_endpoint(
    q: str = Query(description="Organisation name or code, e.g. 'SECL'"),
) -> dict[str, Any]:
    """Resolve an organisation name to a canonical entity.

    Returns ``404`` for an unrecognised name rather than a best guess:
    mis-attributing production to the wrong subsidiary is worse than declining.
    """
    match = resolve_entity(q)
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=f"{q!r} does not resolve to a known entity",
        )
    return {
        "entity_id": match.entity.entity_id,
        "code": match.entity.code,
        "name": match.entity.name,
        "kind": match.entity.kind.value,
        "parent": match.entity.parent,
        "is_cil_group": match.entity.is_cil_group,
        "state": match.entity.state,
        "headquarters": match.entity.headquarters,
        "note": match.entity.note,
        "matched_on": match.raw,
        "exact": match.exact,
        "score": match.score,
        "needs_review": match.needs_review,
    }
