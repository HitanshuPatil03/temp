"""Fiscal and calendar period normalization for Indian coal-sector reporting.

The Indian fiscal year runs 1 April to 31 March, so ``FY2024-25`` is the range
``[2024-04-01, 2025-03-31]``. Mining reports write it a dozen ways — ``FY 2024-25``,
``F.Y. 24-25``, ``2024-2025``, ``FY25`` — and freely mix in bare calendar years,
quarters and "as on" snapshot dates.

The rule this module enforces, taken straight from the research report: a fiscal
year is stored as an explicit date range and is *never* silently mixed with
calendar-year data. :func:`comparable` is the guard that makes that structural
rather than a matter of discipline — a calendar-2024 figure and an FY2024-25
figure describe different nine-month overlaps and comparing them is a reporting
error, not a rounding one.
"""

from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

__all__ = [
    "Period",
    "PeriodKind",
    "UnknownPeriodError",
    "comparable",
    "fiscal_year_label",
    "fiscal_year_of",
    "normalize_period",
]

#: Month the Indian fiscal year starts in.
FISCAL_START_MONTH = 4


class PeriodKind(StrEnum):
    """What kind of time span a period label denotes."""

    FISCAL_YEAR = "fiscal_year"
    FISCAL_QUARTER = "fiscal_quarter"
    FISCAL_HALF = "fiscal_half"
    CALENDAR_YEAR = "calendar_year"
    MONTH = "month"
    #: A run of whole months inside one fiscal year — "APR'23 - AUG'23". Every
    #: CIL monthly performance statement reports each figure twice, once for the
    #: month and once for the year so far, side by side under headings that differ
    #: only in this. Reading the cumulative column as a monthly figure would
    #: overstate production fivefold in August and twelvefold in March, so the two
    #: are different kinds rather than a flag on one.
    MONTH_RANGE = "month_range"
    AS_ON = "as_on"


class UnknownPeriodError(ValueError):
    """Raised when a period label cannot be resolved to a date range."""


#: Fiscal quarter -> (start month offset from April, length in months).
_QUARTERS: dict[int, tuple[int, int]] = {1: (0, 3), 2: (3, 3), 3: (6, 3), 4: (9, 3)}
#: Fiscal half -> (start month offset from April, length in months).
_HALVES: dict[int, tuple[int, int]] = {1: (0, 6), 2: (6, 6)}

_MONTHS: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

#: Two-digit years at or below this map to 20xx, above to 19xx.
_CENTURY_PIVOT = 70


@dataclass(frozen=True, slots=True)
class Period:
    """A time span resolved to explicit dates.

    ``fiscal_year`` is populated only for genuinely fiscal periods. A bare
    calendar year leaves it ``None`` — that absence is what stops downstream code
    treating ``2024`` as ``FY2024-25``.
    """

    kind: PeriodKind
    start: date
    end: date
    label: str
    raw: str
    fiscal_year: str | None = None
    ambiguous: bool = False
    note: str | None = None

    @property
    def is_fiscal(self) -> bool:
        """Whether this period is anchored to the fiscal calendar."""
        return self.fiscal_year is not None

    @property
    def days(self) -> int:
        """Inclusive length of the span in days."""
        return (self.end - self.start).days + 1


def _expand_year(token: str) -> int:
    """Expand a 2- or 4-digit year token to a full year."""
    year = int(token)
    if len(token) <= 2:
        return 2000 + year if year <= _CENTURY_PIVOT else 1900 + year
    return year


def fiscal_year_label(start_year: int) -> str:
    """Canonical label for the fiscal year beginning in ``start_year``.

    Examples:
        >>> fiscal_year_label(2024)
        'FY2024-25'
        >>> fiscal_year_label(1999)
        'FY1999-00'
    """
    return f"FY{start_year}-{(start_year + 1) % 100:02d}"


def fiscal_year_of(day: date) -> int:
    """Return the starting year of the fiscal year containing ``day``.

    Examples:
        >>> fiscal_year_of(date(2025, 3, 31))
        2024
        >>> fiscal_year_of(date(2025, 4, 1))
        2025
    """
    return day.year if day.month >= FISCAL_START_MONTH else day.year - 1


def _fiscal_span(start_year: int, month_offset: int, months: int) -> tuple[date, date]:
    """Date span of ``months`` starting ``month_offset`` months into a fiscal year."""
    start_abs = (start_year * 12) + (FISCAL_START_MONTH - 1) + month_offset
    end_abs = start_abs + months - 1  # inclusive final month
    start = date(start_abs // 12, (start_abs % 12) + 1, 1)
    end_year, end_month = end_abs // 12, (end_abs % 12) + 1
    return start, date(end_year, end_month, monthrange(end_year, end_month)[1])


def _fiscal_year_period(
    start_year: int, raw: str, *, ambiguous: bool = False, note: str | None = None
) -> Period:
    start, end = _fiscal_span(start_year, 0, 12)
    label = fiscal_year_label(start_year)
    return Period(
        kind=PeriodKind.FISCAL_YEAR,
        start=start,
        end=end,
        label=label,
        raw=raw,
        fiscal_year=label,
        ambiguous=ambiguous,
        note=note,
    )


#: ``apr 2024``, ``apr-24``, ``AUG'23``, ``AUG’23``, ``aug.23``. The separator is
#: optional because CIL prints the apostrophe form with no space, and the month
#: name is verified against :data:`_MONTHS` by the caller rather than by the
#: pattern — a regex cannot tell "mar" from "may" from "mad".
_MONTH_YEAR = re.compile(r"\b([a-z]{3,9})\s*['./ -]?\s*(\d{4}|\d{2})\b")

#: ``APR'23 - AUG'23`` and the variants these statements print, including the one
#: where the dash came out of OCR as a full stop: ``APR'23 . AUG'23``.
_MONTH_RANGE = re.compile(
    r"\b([a-z]{3,9})\s*['./ -]?\s*(\d{4}|\d{2})\b"
    r"\s*(?:-|to|\.|–)\s*"
    r"\b([a-z]{3,9})\s*['./ -]?\s*(\d{4}|\d{2})\b"
)


def _month_span(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _month_range(text: str, raw: str) -> Period | None:
    """A progressive span of whole months, as ``APR'23 - AUG'23``.

    Returns ``None`` when the text is not a month range at all, because this is
    tried speculatively before the other shapes.

    **Raises** when it *is* a month range but not a coherent one — running
    backwards, or crossing a fiscal year boundary. Falling through to the
    single-month branch there would read "APR'23 - AUG'23" as "Apr 2023" and
    attach five months of cumulative production to one month, which is the error
    this whole distinction exists to prevent. A label we cannot read is a refusal;
    a label we read as the wrong span is a wrong number.
    """
    match = _MONTH_RANGE.search(text)
    if match is None:
        return None
    first_name, first_year, second_name, second_year = match.groups()
    if first_name not in _MONTHS or second_name not in _MONTHS:
        return None

    start_year, start_month = _expand_year(first_year), _MONTHS[first_name]
    end_year, end_month = _expand_year(second_year), _MONTHS[second_name]
    start, _ = _month_span(start_year, start_month)
    _, end = _month_span(end_year, end_month)
    if end < start:
        raise UnknownPeriodError(
            f"{raw!r} names a month range that runs backwards "
            f"({start.isoformat()} to {end.isoformat()})"
        )

    fiscal = fiscal_year_of(start)
    if fiscal_year_of(end) != fiscal:
        raise UnknownPeriodError(
            f"{raw!r} spans two fiscal years; a cumulative column in an Indian "
            "coal statement is always year-to-date within one"
        )

    label_start = f"{start:%b %Y}"
    label_end = f"{end:%b %Y}"
    if label_start == label_end:
        # "AUG'23 - AUG'23" is a month, not a range, and calling it a range would
        # make it incomparable with the same month written the ordinary way.
        return Period(
            kind=PeriodKind.MONTH,
            start=start,
            end=end,
            label=label_start,
            raw=raw,
            fiscal_year=fiscal_year_label(fiscal),
        )
    return Period(
        kind=PeriodKind.MONTH_RANGE,
        start=start,
        end=end,
        label=f"{label_start} - {label_end}",
        raw=raw,
        fiscal_year=fiscal_year_label(fiscal),
        note="cumulative: a run of months within one fiscal year, not a single month",
    )


def normalize_period(raw: str) -> Period:
    """Resolve a period label to an explicit date range.

    Args:
        raw: Period text as printed, e.g. ``"FY2024-25"``, ``"Q3 FY24-25"``,
            ``"as on 31.03.2025"``, ``"April 2024"``, ``"2024"``.

    Returns:
        The resolved period. Fiscal periods carry ``fiscal_year``; calendar years
        deliberately do not.

    Raises:
        UnknownPeriodError: If no period can be identified.

    Examples:
        >>> p = normalize_period("FY2024-25")
        >>> p.start, p.end
        (datetime.date(2024, 4, 1), datetime.date(2025, 3, 31))
        >>> normalize_period("2024").is_fiscal
        False
        >>> normalize_period("Q3 FY2024-25").start
        datetime.date(2024, 10, 1)
    """
    if not raw or not raw.strip():
        raise UnknownPeriodError("empty period string")

    text = re.sub(r"\s+", " ", raw.strip().lower())
    # Fold 'f.y.' -> 'fy' and normalise dash variants.
    text = text.replace("f.y.", "fy").replace("f. y.", "fy")
    text = re.sub(r"[–—]", "-", text)
    # Curly apostrophes to straight ones, so AUG’23 and AUG'23 are one case.
    text = text.replace("’", "'").replace("ʼ", "'")

    # --- progressive month range: "APR'23 - AUG'23" ----------------------------
    #
    # Placed before the fiscal-span branch below, which would otherwise see
    # "23 - 23" in "apr'23 - aug'23" and refuse it as a year span that does not
    # advance. This is the shape of the right-hand half of every CIL monthly
    # performance statement, so getting it wrong is not a corner case.
    if (progressive := _month_range(text, raw.strip())) is not None:
        return progressive

    # --- "as on 31.03.2025" / "as at 31-03-2025" -------------------------------
    as_on = re.search(
        r"\bas\s+(?:on|at|of)\b\D{0,4}(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", text
    )
    if as_on:
        day, month, year_token = as_on.groups()
        snapshot = date(_expand_year(year_token), int(month), int(day))
        return Period(
            kind=PeriodKind.AS_ON,
            start=snapshot,
            end=snapshot,
            label=f"as on {snapshot.isoformat()}",
            raw=raw.strip(),
            fiscal_year=fiscal_year_label(fiscal_year_of(snapshot)),
            note="point-in-time snapshot, not a flow over a period",
        )

    # --- quarter / half prefix -------------------------------------------------
    sub_period: tuple[PeriodKind, int] | None = None
    if quarter := re.search(r"\bq([1-4])\b", text):
        sub_period = (PeriodKind.FISCAL_QUARTER, int(quarter.group(1)))
    elif half := re.search(r"\bh([12])\b", text):
        sub_period = (PeriodKind.FISCAL_HALF, int(half.group(1)))

    # --- explicit fiscal span: 2024-25, fy24-25, 2024-2025 ---------------------
    span = re.search(r"(?<!\d)(\d{4}|\d{2})\s*-\s*(\d{4}|\d{2})(?!\d)", text)
    if span:
        first = _expand_year(span.group(1))
        second = _expand_year(span.group(2))
        if second != first + 1:
            raise UnknownPeriodError(
                f"{raw!r} spans {first}->{second}, which is not a single fiscal year"
            )
        if sub_period:
            kind, index = sub_period
            offset, months = (
                _QUARTERS[index] if kind is PeriodKind.FISCAL_QUARTER else _HALVES[index]
            )
            start, end = _fiscal_span(first, offset, months)
            prefix = "Q" if kind is PeriodKind.FISCAL_QUARTER else "H"
            return Period(
                kind=kind,
                start=start,
                end=end,
                label=f"{prefix}{index} {fiscal_year_label(first)}",
                raw=raw.strip(),
                fiscal_year=fiscal_year_label(first),
            )
        return _fiscal_year_period(first, raw.strip())

    # --- month + year: "April 2024", "apr-24", "AUG'23" ------------------------
    #
    # Every match is considered, not only the first. The pattern happily matches
    # "annexure 23" or "figures 2024", and returning at the first match meant a
    # caption with a stray word before the month resolved to nothing. The month
    # table is the arbiter; the regex only proposes.
    for month_match in _MONTH_YEAR.finditer(text):
        name = month_match.group(1)
        if name not in _MONTHS:
            continue
        month = _MONTHS[name]
        year = _expand_year(month_match.group(2))
        start, end = _month_span(year, month)
        return Period(
            kind=PeriodKind.MONTH,
            start=start,
            end=end,
            label=f"{start:%b %Y}",
            raw=raw.strip(),
            fiscal_year=fiscal_year_label(fiscal_year_of(start)),
        )

    # --- single year -----------------------------------------------------------
    single = re.search(r"(?<!\d)(\d{4})(?!\d)", text)
    if single:
        year = int(single.group(1))
        # 'FY2025' in Indian usage means the year *ending* March 2025, i.e. FY2024-25.
        if re.search(r"\bfy\s*\d{4}(?!\d)", text):
            return _fiscal_year_period(
                year - 1,
                raw.strip(),
                ambiguous=True,
                note=(
                    f"'{raw.strip()}' read as the fiscal year ending March {year} "
                    f"({fiscal_year_label(year - 1)}); confirm against the source if "
                    f"the document means {fiscal_year_label(year)}"
                ),
            )
        return Period(
            kind=PeriodKind.CALENDAR_YEAR,
            start=date(year, 1, 1),
            end=date(year, 12, 31),
            label=str(year),
            raw=raw.strip(),
            fiscal_year=None,
            note="calendar year — not comparable with fiscal-year figures",
        )

    # --- two-digit fiscal shorthand: 'fy25' ------------------------------------
    short = re.search(r"\bfy\s*(\d{2})\b", text)
    if short:
        year = _expand_year(short.group(1))
        return _fiscal_year_period(
            year - 1,
            raw.strip(),
            ambiguous=True,
            note=f"'{raw.strip()}' read as the fiscal year ending March {year}",
        )

    raise UnknownPeriodError(f"no recognisable period in {raw!r}")


def comparable(left: Period, right: Period) -> tuple[bool, str | None]:
    """Whether two periods may be compared directly.

    This is the structural guard against the report's named failure mode: mixing
    fiscal and calendar figures. Returns ``(True, None)`` when comparison is safe,
    otherwise ``(False, reason)`` so the caller can surface the reason instead of
    quietly producing a wrong delta.

    Examples:
        >>> a = normalize_period("FY2023-24")
        >>> b = normalize_period("FY2024-25")
        >>> comparable(a, b)
        (True, None)
        >>> ok, why = comparable(b, normalize_period("2024"))
        >>> ok
        False
    """
    if left.is_fiscal != right.is_fiscal:
        return False, (
            f"{left.label} and {right.label} mix fiscal and calendar periods; "
            "they cover different spans and are not directly comparable"
        )
    if left.kind is not right.kind:
        return False, (
            f"{left.label} is a {left.kind.value} but {right.label} is a "
            f"{right.kind.value}; compare like with like"
        )
    if left.kind is PeriodKind.AS_ON:
        return False, "point-in-time snapshots describe stock, not flow over a period"
    return True, None
