"""Indian digit grouping, for the figures that leave this system.

A CMPDI officer reads **19,30,00,000**, not 193,000,000. The web app has said so
since it was written — ``apps/web/src/lib/format.ts`` formats every figure with
``Intl.NumberFormat("en-IN")`` and explains why in its header — and the export
path did not. It used Python's ``{:,.0f}``, which groups in thousands.

The consequence was narrow and bad: an officer verified a figure on screen as
``19,30,00,000 t``, approved it, and the ``.docx`` that went to the Ministry read
``193,000,000 t``. The same number, in the convention of a different country,
in the artefact that carries the official weight. Nothing was wrong with the
*value* — which is why no test caught it — but the document an officer signs
should be in the notation they checked.

Python has no built-in for this. ``locale`` with ``en_IN`` would do it and is the
wrong tool: the locale is process-global, so a worker rendering one report would
change how every other thread formats, and ``en_IN`` is not installed on a bare
container anyway. The rule is small enough to implement exactly: the last three
digits group together, everything above them in pairs.

**Rounding has to match too, and it is not Python's default.** ``f"{x:.0f}"``
rounds halves to even, so 2.5 becomes 2; ``Intl`` rounds halves away from zero,
so 2.5 becomes 3. Reaching a tie needs a figure ending in exactly .5 — ordinary
in tonnages converted from lakh — and the result would have been a one-tonne
disagreement between the screen and the document, which is the worst kind of
discrepancy to explain to an auditor because both numbers are defensible.
``Intl`` also rounds the *shortest decimal representation* rather than the binary
value (2.675 formats as 2.68, though the stored double is 2.67499…), which is why
this goes through ``Decimal(str(value))``: ``str`` of a float is that same
shortest representation.

Named ``grouping`` rather than ``numbers`` on purpose: ``numbers`` is a standard
library module, and ``decimal`` imports it. Shadowing it works while the package
is imported normally, and breaks the moment anything puts this directory on
``sys.path`` — a direct script run, or a tool that does — with a circular-import
error that names ``decimal`` and gives no hint that a filename is the cause.

``tests/test_report_numbers.py`` pins the output against values taken from
``Intl.NumberFormat`` itself, so the two sides of the stack cannot drift apart
without a failure.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

__all__ = ["format_indian"]


def format_indian(value: float, *, digits: int = 0) -> str:
    """Group ``value`` in the Indian system, e.g. ``19,30,00,000``.

    ``digits`` is a fixed number of decimal places, not a maximum: a figures
    table wants a column that lines up, and a report that prints two tonnages to
    different precisions invites the question of which one was rounded.

    Ties round away from zero, matching ``Intl.NumberFormat``'s default rather
    than Python's — see the module docstring.

    >>> format_indian(193_000_000)
    '19,30,00,000'
    >>> format_indian(100_000)
    '1,00,000'
    >>> format_indian(999)
    '999'
    >>> format_indian(-193_000_000)
    '-19,30,00,000'
    >>> format_indian(2.5)
    '3'
    >>> format_indian(1234.5, digits=2)
    '1,234.50'
    """
    # str(value), not value: Decimal(float) would carry the full binary
    # expansion, and 2.675 would round down where the browser rounds up.
    quantum = Decimal(1).scaleb(-digits)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)

    text = f"{abs(rounded):.{digits}f}"
    integer, _, fraction = text.partition(".")

    if len(integer) > 3:
        head, tail = integer[:-3], integer[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        integer = ",".join([*groups, tail])

    # Checked after rounding, so a value that rounds to zero does not come back
    # as "-0" — which looks like a defect in a report even though it is not.
    sign = "-" if rounded < 0 else ""
    return f"{sign}{integer}.{fraction}" if fraction else f"{sign}{integer}"
