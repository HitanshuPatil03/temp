"""Entity normalization for Coal India Limited and its subsidiaries.

Mining reports name the same organisation half a dozen ways: ``SECL``, ``S.E.C.L.``,
``South Eastern Coalfields Ltd.``, ``South Eastern Coal Fields Limited``. Without a
canonical entity id, cross-document comparison silently fails — or worse, silently
succeeds against the wrong company.

The design rule here is **conservatism over coverage**. An exact or alias match is
trusted. A fuzzy match is returned but flagged ``needs_review``, because attributing
one subsidiary's production to another is a far more damaging error than declining to
resolve a name. Anything below the similarity floor is refused outright.

One distinction matters especially: **SCCL (Singareni Collieries) is not a CIL
subsidiary.** It is a separate Government of Telangana / Government of India joint
venture. Rolling it into CIL totals is a real reporting error, so it is modelled
explicitly as ``EXTERNAL``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum

__all__ = [
    "ENTITIES",
    "Entity",
    "EntityKind",
    "EntityMatch",
    "canonical_id",
    "resolve_entity",
]

#: Fuzzy matches below this similarity are refused rather than guessed.
FUZZY_FLOOR = 0.87


class EntityKind(StrEnum):
    """Role of an organisation in the coal-sector hierarchy."""

    HOLDING = "holding"
    SUBSIDIARY = "subsidiary"
    INSTITUTE = "institute"
    UNIT = "unit"
    EXTERNAL = "external"
    MINISTRY = "ministry"


@dataclass(frozen=True, slots=True)
class Entity:
    """A canonical organisation in the CIL reporting hierarchy."""

    entity_id: str
    code: str
    name: str
    kind: EntityKind
    parent: str | None = None
    headquarters: str | None = None
    state: str | None = None
    aliases: tuple[str, ...] = field(default=())
    note: str | None = None

    @property
    def is_cil_group(self) -> bool:
        """Whether this entity's figures roll up into Coal India Limited totals."""
        return self.entity_id == "cil" or self.parent == "cil"


ENTITIES: tuple[Entity, ...] = (
    Entity(
        entity_id="cil",
        code="CIL",
        name="Coal India Limited",
        kind=EntityKind.HOLDING,
        headquarters="Kolkata",
        state="West Bengal",
        aliases=("coal india", "coalindia", "cil holding", "coal india ltd"),
    ),
    Entity(
        entity_id="ecl",
        code="ECL",
        name="Eastern Coalfields Limited",
        kind=EntityKind.SUBSIDIARY,
        parent="cil",
        headquarters="Sanctoria",
        state="West Bengal",
        aliases=("eastern coalfields", "eastern coal fields"),
    ),
    Entity(
        entity_id="bccl",
        code="BCCL",
        name="Bharat Coking Coal Limited",
        kind=EntityKind.SUBSIDIARY,
        parent="cil",
        headquarters="Dhanbad",
        state="Jharkhand",
        aliases=("bharat coking coal", "bharat cocking coal"),
    ),
    Entity(
        entity_id="ccl",
        code="CCL",
        name="Central Coalfields Limited",
        kind=EntityKind.SUBSIDIARY,
        parent="cil",
        headquarters="Ranchi",
        state="Jharkhand",
        aliases=("central coalfields", "central coal fields"),
    ),
    Entity(
        entity_id="ncl",
        code="NCL",
        name="Northern Coalfields Limited",
        kind=EntityKind.SUBSIDIARY,
        parent="cil",
        headquarters="Singrauli",
        state="Madhya Pradesh",
        aliases=("northern coalfields", "northern coal fields"),
    ),
    Entity(
        entity_id="wcl",
        code="WCL",
        name="Western Coalfields Limited",
        kind=EntityKind.SUBSIDIARY,
        parent="cil",
        headquarters="Nagpur",
        state="Maharashtra",
        aliases=("western coalfields", "western coal fields"),
    ),
    Entity(
        entity_id="secl",
        code="SECL",
        name="South Eastern Coalfields Limited",
        kind=EntityKind.SUBSIDIARY,
        parent="cil",
        headquarters="Bilaspur",
        state="Chhattisgarh",
        aliases=(
            "south eastern coalfields",
            "south eastern coal fields",
            "se coalfields",
        ),
    ),
    Entity(
        entity_id="mcl",
        code="MCL",
        name="Mahanadi Coalfields Limited",
        kind=EntityKind.SUBSIDIARY,
        parent="cil",
        headquarters="Sambalpur",
        state="Odisha",
        aliases=("mahanadi coalfields", "mahanadi coal fields"),
    ),
    Entity(
        entity_id="cmpdi",
        code="CMPDI",
        name="Central Mine Planning and Design Institute Limited",
        kind=EntityKind.INSTITUTE,
        parent="cil",
        headquarters="Ranchi",
        state="Jharkhand",
        aliases=(
            "central mine planning and design institute",
            "central mine planning & design institute",
            "cmpdil",
            "cmpd institute",
        ),
    ),
    Entity(
        entity_id="nec",
        code="NEC",
        name="North Eastern Coalfields",
        kind=EntityKind.UNIT,
        parent="cil",
        headquarters="Margherita",
        state="Assam",
        aliases=("north eastern coalfields", "north eastern coal fields"),
        note="A directly-managed CIL unit, not a separate subsidiary company",
    ),
    Entity(
        entity_id="sccl",
        code="SCCL",
        name="Singareni Collieries Company Limited",
        kind=EntityKind.EXTERNAL,
        parent=None,
        headquarters="Kothagudem",
        state="Telangana",
        aliases=("singareni collieries", "singareni"),
        note=(
            "Government of Telangana / Government of India joint venture — NOT a CIL "
            "subsidiary. Must not be included in CIL group totals."
        ),
    ),
    Entity(
        entity_id="nlcil",
        code="NLCIL",
        name="NLC India Limited",
        kind=EntityKind.EXTERNAL,
        parent=None,
        headquarters="Neyveli",
        state="Tamil Nadu",
        aliases=("nlc india", "neyveli lignite", "neyveli lignite corporation", "nlc"),
        note="Lignite PSU under the Ministry of Coal — not a CIL subsidiary",
    ),
    Entity(
        entity_id="moc",
        code="MoC",
        name="Ministry of Coal",
        kind=EntityKind.MINISTRY,
        parent=None,
        headquarters="New Delhi",
        aliases=("ministry of coal", "moc", "coal ministry"),
    ),
)

_BY_ID: dict[str, Entity] = {e.entity_id: e for e in ENTITIES}

_SUFFIXES = re.compile(
    r"\b(?:limited|ltd|pvt|private|company|co|corporation|corpn|the)\b", re.IGNORECASE
)


def _fold(text: str) -> str:
    """Fold a name for matching: lowercase, drop punctuation and legal suffixes."""
    folded = text.lower().strip()
    folded = re.sub(r"[^a-z0-9&\s]", " ", folded)
    folded = folded.replace("&", " and ")
    folded = _SUFFIXES.sub(" ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def _build_lookup() -> dict[str, str]:
    """Exact-match table from every known surface form to an entity id."""
    lookup: dict[str, str] = {}
    for entity in ENTITIES:
        forms = {entity.code, entity.name, entity.entity_id, *entity.aliases}
        for form in forms:
            folded = _fold(form)
            if folded:
                lookup[folded] = entity.entity_id
            # 'S.E.C.L.' folds to 's e c l'; also index the de-spaced run.
            squashed = folded.replace(" ", "")
            if squashed:
                lookup.setdefault(squashed, entity.entity_id)
    return lookup


_LOOKUP: dict[str, str] = _build_lookup()


@dataclass(frozen=True, slots=True)
class EntityMatch:
    """Result of resolving a raw organisation name."""

    entity: Entity
    raw: str
    score: float
    exact: bool

    @property
    def needs_review(self) -> bool:
        """Whether a human should confirm this match before facts are trusted."""
        return not self.exact


def canonical_id(raw: str) -> str | None:
    """Resolve a name to a canonical entity id, or ``None`` if unresolved.

    Examples:
        >>> canonical_id("S.E.C.L.")
        'secl'
        >>> canonical_id("South Eastern Coalfields Ltd.")
        'secl'
        >>> canonical_id("Atlantis Coal Corp") is None
        True
    """
    match = resolve_entity(raw)
    return match.entity.entity_id if match else None


def resolve_entity(raw: str, *, floor: float = FUZZY_FLOOR) -> EntityMatch | None:
    """Resolve a raw organisation name to a canonical :class:`Entity`.

    Exact and alias hits return ``exact=True``. Close-but-inexact names (OCR damage,
    unusual spellings) return ``exact=False`` and should be routed to review rather
    than trusted. Names below ``floor`` similarity return ``None``.

    Args:
        raw: Organisation name as printed in the source document.
        floor: Minimum similarity for a fuzzy match to be offered at all.

    Returns:
        The match, or ``None`` when the name cannot be resolved safely.

    Examples:
        >>> resolve_entity("MCL").entity.name
        'Mahanadi Coalfields Limited'
        >>> resolve_entity("Mahanadi Coalfeilds Limited").needs_review
        True
        >>> resolve_entity("SCCL").entity.is_cil_group
        False
    """
    if not raw or not raw.strip():
        return None

    folded = _fold(raw)
    if not folded:
        return None

    for key in (folded, folded.replace(" ", "")):
        if key in _LOOKUP:
            return EntityMatch(
                entity=_BY_ID[_LOOKUP[key]], raw=raw.strip(), score=1.0, exact=True
            )

    best_id: str | None = None
    best_score = 0.0
    for key, entity_id in _LOOKUP.items():
        # Comparing a long name against a 3-letter code produces noise; skip those.
        if abs(len(key) - len(folded)) > max(len(key), len(folded)) * 0.5:
            continue
        score = SequenceMatcher(None, folded, key).ratio()
        if score > best_score:
            best_score, best_id = score, entity_id

    if best_id is None or best_score < floor:
        return None
    return EntityMatch(
        entity=_BY_ID[best_id], raw=raw.strip(), score=best_score, exact=False
    )
