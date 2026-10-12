"""Report repository — persistence for pinned-evidence manifests.

The manifest is the product here. :func:`mrip.reports.generate.generate` resolves
every figure through the same resolver the query path uses and returns a
:class:`~mrip.schemas.ReportManifest`; this stores it verbatim so that re-opening
the report years later reproduces the figures *as approved*, not as the corpus
has since become (ARCHITECTURE §8.5, §11.3).

Two rules are enforced here rather than left to the caller:

**The lifecycle is a table of legal transitions, not an ordering.** ``draft →
published`` raises instead of quietly skipping review, the same way the document
lifecycle refuses ``received → ready``.

**A published report is immutable.** Not by convention — :meth:`transition`
refuses to move anything out of ``published``, and nothing in this class can
rewrite a stored manifest. A report that can be edited after it was sent to the
Ministry is not a record of what was sent.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from mrip.auth.scope import Scope
from mrip.db.tables import reports
from mrip.schemas import ReportManifest, ReportState, Role

__all__ = [
    "LEGAL_TRANSITIONS",
    "TRANSITION_ROLE",
    "IllegalReportTransitionError",
    "IncompleteReportSignOffError",
    "InsufficientRoleForTransitionError",
    "ReportRepository",
    "SelfApprovalError",
]


class IncompleteReportSignOffError(RuntimeError):
    """An approval or publication of a report that can never be rendered.

    ``missing_required`` is fixed when a manifest is generated and the stored
    manifest is never rewritten — ``transition`` touches only the state and the
    sign-off columns. So a report that was incomplete at generation is
    incomplete for good, and the writers refuse to render it: §11.2 forbids
    emitting a report with a blank where a required number belongs.

    The lifecycle did not check, so an incomplete report walked ``draft →
    in_review → approved → published`` on three 200s and then returned 409 on
    every download. An approver had put their name to something nobody could
    produce, and ``published`` — which this repository treats as *the record* —
    pointed at nothing renderable.
    """


class IllegalReportTransitionError(RuntimeError):
    """A state change the publish lifecycle does not allow (§11.3)."""


class SelfApprovalError(RuntimeError):
    """The approver is the same person who generated the report.

    Separation of duties. The lifecycle exists so that the officer who runs a
    report and the approver who signs it off are two people — for a draft
    parliamentary answer that is the entire point of the approval step, not a
    formality. Enforced rather than documented, because a control nobody checks
    is a control that is not there.
    """


class InsufficientRoleForTransitionError(RuntimeError):
    """The caller's role is below what this particular move requires.

    Separate from a plain route-level role guard because the answer depends on
    *which* move: the message has to name both sides. "Publishing needs the
    approver role; you are an officer" is actionable, where a bare "forbidden"
    on an endpoint that handles four different decisions is not.
    """


#: The publish lifecycle, as the set of moves each state permits. Written as data
#: so the legal graph can be read at a glance and tested directly, rather than
#: being implied by a chain of ``if`` statements.
#:
#: ``published`` maps to nothing: once a report is published it is frozen. A
#: correction is a new report with its own manifest, which is what keeps the
#: published record a record.
LEGAL_TRANSITIONS: dict[ReportState, frozenset[ReportState]] = {
    ReportState.DRAFT: frozenset({ReportState.IN_REVIEW}),
    # Back to draft is allowed: a reviewer who finds a problem sends it back
    # rather than approving something they do not believe.
    ReportState.IN_REVIEW: frozenset({ReportState.APPROVED, ReportState.DRAFT}),
    ReportState.APPROVED: frozenset({ReportState.PUBLISHED, ReportState.IN_REVIEW}),
    ReportState.PUBLISHED: frozenset(),
}


#: The rank each *transition* needs, keyed by the move rather than by the route.
#:
#: One gate for the whole endpoint was the previous rule — approver for every
#: move — and it made the lifecycle unusable by the role that drives it. An
#: officer could generate a report and then do nothing with it: submitting their
#: own draft for review returned 403, so every report had to be walked forward
#: by an approver from the moment it existed, while the UI showed the officer a
#: button that could only fail. That is as much a failure of separation of
#: duties as self-approval is, approached from the other end — the preparer has
#: to be able to hand work over.
#:
#: Keyed on ``(from, to)`` and not on the target alone, because ``in_review`` is
#: reached by two unrelated acts: an officer submitting a draft, and an approver
#: withdrawing an approval. Those must not share a rank — and a guard that read
#: the state *before* taking the row lock could be raced from one into the
#: other, which is why this is checked inside :meth:`ReportRepository.transition`
#: rather than at the route.
#:
#: Mirrors :data:`LEGAL_TRANSITIONS` exactly; a test asserts the two agree, so a
#: new edge cannot be added to the graph without deciding who may walk it.
TRANSITION_ROLE: dict[tuple[ReportState, ReportState], Role] = {
    # Submitting your own work is handing it over, not deciding it.
    (ReportState.DRAFT, ReportState.IN_REVIEW): Role.OFFICER,
    # A reviewer who does not believe a report sends it back rather than
    # approving it; that is the whole point of the role.
    (ReportState.IN_REVIEW, ReportState.DRAFT): Role.REVIEWER,
    # The accountable signature. Four-eyes applies here and nowhere else.
    (ReportState.IN_REVIEW, ReportState.APPROVED): Role.APPROVER,
    # Withdrawing an approval undoes a signature someone is on the record for,
    # so it is an approver's act and not a reviewer's.
    (ReportState.APPROVED, ReportState.IN_REVIEW): Role.APPROVER,
    (ReportState.APPROVED, ReportState.PUBLISHED): Role.APPROVER,
}


class ReportRepository:
    """Stored report manifests, scoped by the entity the report is about."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    # ----------------------------------------------------------------- writes

    def create(
        self,
        manifest: ReportManifest,
        *,
        entity_id: str,
        period_label: str,
        generated_by: str | None = None,
    ) -> ReportManifest:
        """Store a freshly generated manifest as a draft.

        ``entity_id`` is passed rather than read out of the manifest because it is
        the scope key: it decides who can see this report at all, and that is not
        a detail to infer from the first figure — an incomplete manifest may have
        no figures to infer it from.
        """
        self._conn.execute(
            sa.insert(reports).values(
                report_id=manifest.report_id,
                template_id=manifest.template_id,
                template_version=manifest.template_version,
                title=manifest.title,
                entity_id=entity_id,
                period_label=period_label,
                state=manifest.state.value,
                manifest=manifest.model_dump(mode="json"),
                generated_at=manifest.generated_at,
                generated_by=generated_by,
            )
        )
        return manifest

    def transition(
        self,
        report_id: str,
        target: ReportState,
        scope: Scope,
        *,
        actor_user_id: str | None = None,
        actor_role: Role | None = None,
        require_separate_approver: bool = True,
    ) -> ReportManifest:
        """Move a report through the publish lifecycle, or refuse.

        Raises :class:`IllegalReportTransitionError` when the move is not in
        :data:`LEGAL_TRANSITIONS` — including every move out of ``published``.
        Raises :class:`SelfApprovalError` when the approver is the account that
        generated the report and ``require_separate_approver`` is set.
        Raises :class:`InsufficientRoleForTransitionError` when ``actor_role`` is
        below what this particular move needs — see :data:`TRANSITION_ROLE`.
        Raises ``LookupError`` when the report does not exist or is out of scope;
        the caller turns that into the 404 that does not confirm its existence.
        """
        row = self._row(report_id, scope, for_update=True)
        if row is None:
            raise LookupError(report_id)

        current = ReportState(row["state"])
        if target not in LEGAL_TRANSITIONS[current]:
            allowed = sorted(state.value for state in LEGAL_TRANSITIONS[current])
            raise IllegalReportTransitionError(
                f"A {current.value} report cannot become {target.value}. "
                f"Allowed from here: {allowed or 'nothing — published is final'}."
            )

        # Checked here, under the row lock taken above, and not at the route.
        # The required rank depends on where the report *is*, so a guard that
        # read the state before locking could be raced: an officer cleared for
        # `draft -> in_review` would carry that clearance into an
        # `approved -> in_review` withdrawal if an approver moved the report in
        # between. Reading the state and acting on it have to be one step.
        # A sign-off is a claim that this report is fit to go out, so it cannot
        # be made about one that will not render. Both ends are guarded, not
        # just `approved`: a report approved before this check existed must
        # still not reach `published`.
        if target in (ReportState.APPROVED, ReportState.PUBLISHED):
            manifest = self._hydrate(row)
            if not manifest.complete:
                missing = ", ".join(item.label for item in manifest.missing_required)
                raise IncompleteReportSignOffError(
                    f"This report cannot become {target.value}: required figures "
                    f"could not be pinned ({missing}). Resolve the conflict or "
                    "validate the figure, then regenerate — a manifest's missing "
                    "fields are fixed when it is generated."
                )

        required = TRANSITION_ROLE[(current, target)]
        if actor_role is not None and actor_role.rank < required.rank:
            article = "an" if actor_role.value[0] in "aeiou" else "a"
            raise InsufficientRoleForTransitionError(
                f"Moving a report from {current.value} to {target.value} needs the "
                f"{required.value} role; you are {article} {actor_role.value}."
            )

        # Separation of duties, checked at the sign-off and nowhere else. Moving
        # a report *into* review, or sending it back, is routine handling; the
        # approval is the decision someone is accountable for, so that is the one
        # move the generator may not make alone. Roles are a linear rank, so an
        # approver also satisfies "officer" and can generate — which is exactly
        # how one account could otherwise run a parliamentary answer and sign it
        # off with nobody else ever reading it.
        if (
            target is ReportState.APPROVED
            and require_separate_approver
            and actor_user_id is not None
            and row["generated_by"] == actor_user_id
        ):
            raise SelfApprovalError(
                "You generated this report, so you cannot also approve it. "
                "Another approver must sign it off — that separation is what the "
                "approval step is for. A deployment with a single approver can "
                "set MRIP_REQUIRE_SEPARATE_APPROVER=false, deliberately and on "
                "the record."
            )

        values: dict[str, Any] = {"state": target.value, "updated_at": sa.func.now()}
        if target is ReportState.APPROVED:
            values["approved_by"] = actor_user_id
            values["approved_at"] = sa.func.now()
        elif target is ReportState.PUBLISHED:
            values["published_at"] = sa.func.now()
        elif target in (ReportState.DRAFT, ReportState.IN_REVIEW):
            # Sending a report back withdraws the approval with it. Leaving the
            # approver's name on a report that is being reworked would credit
            # them with a version they never saw.
            values["approved_by"] = None
            values["approved_at"] = None

        self._conn.execute(
            sa.update(reports).where(reports.c.report_id == report_id).values(**values)
        )
        stored = self._row(report_id, scope)
        assert stored is not None  # updated inside this transaction
        return self._hydrate(stored)

    # ------------------------------------------------------------------ reads

    def get(self, report_id: str, scope: Scope) -> ReportManifest | None:
        row = self._row(report_id, scope)
        return self._hydrate(row) if row else None

    def list(
        self,
        scope: Scope,
        *,
        state: ReportState | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReportManifest]:
        """Reports the caller may see, newest first."""
        query = sa.select(reports).where(scope.clause(reports.c.entity_id))
        if state is not None:
            query = query.where(reports.c.state == state.value)
        rows = (
            self._conn.execute(
                query.order_by(reports.c.generated_at.desc()).limit(limit).offset(offset)
            )
            .mappings()
            .all()
        )
        return [self._hydrate(dict(row)) for row in rows]

    def count_by_state(self, scope: Scope) -> dict[str, int]:
        """How many reports sit in each state — the dashboard's report tile."""
        rows = self._conn.execute(
            sa.select(reports.c.state, sa.func.count())
            .where(scope.clause(reports.c.entity_id))
            .group_by(reports.c.state)
        ).all()
        counts = {state.value: 0 for state in ReportState}
        for state_value, count in rows:
            counts[str(state_value)] = int(count)
        return counts

    # ----------------------------------------------------------------- internals

    def _row(
        self, report_id: str, scope: Scope, *, for_update: bool = False
    ) -> dict[str, Any] | None:
        """Fetch one report, optionally locking it for the rest of the transaction.

        ``for_update`` is what makes :meth:`transition` safe against a second
        approver acting on the same report. Without it the legality check reads a
        state that the ``UPDATE`` never re-checks, so two requests that were each
        legal against the state they saw can both commit: one publishes while the
        other sends the report back, and a *published* report ends up in review —
        the one move ``LEGAL_TRANSITIONS`` exists to forbid.

        With the lock the second transaction blocks, and PostgreSQL re-evaluates
        the row after acquiring it, so the check runs against the state that
        actually committed and refuses on its own. That is why this needs no new
        error type: the loser gets the same ``IllegalReportTransitionError`` a
        sequential caller would.

        Plain reads must not take it — a dashboard listing reports has no business
        blocking a sign-off.
        """
        query = sa.select(reports).where(
            reports.c.report_id == report_id,
            scope.clause(reports.c.entity_id),
        )
        if for_update:
            query = query.with_for_update()
        row = self._conn.execute(query).mappings().first()
        return dict(row) if row else None

    def _hydrate(self, row: dict[str, Any]) -> ReportManifest:
        """Rebuild the manifest from storage.

        The stored JSON is the manifest as it was generated; ``state`` is read
        from its own column because that is the field the lifecycle moves, and
        the copy inside the JSON is a snapshot of the state at generation.
        """
        manifest = ReportManifest.model_validate(row["manifest"])
        return manifest.model_copy(update={"state": ReportState(row["state"])})
