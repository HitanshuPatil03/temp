"""Report endpoints (ARCHITECTURE §11).

The route surface is small because the hard guarantees live below it:
:mod:`mrip.reports.generate` resolves every figure through the same resolver the
query path uses, and :class:`~mrip.db.repositories.reports.ReportRepository`
enforces the publish lifecycle. What this module adds is who may do what, and
the audit trail that says who did.

Three things are deliberate:

**Generation needs the officer role, approval needs the approver role.** §9's
roles exist so that the person who runs a report and the person who signs it off
can be different people, which is the entire point of an approval step.

**An incomplete report is a 200, not a 500.** A required figure with no
validated fact is an *answer* — "this is what is missing and why" — and the
manifest carries the structured refusal reasons. Rendering it is what fails
(§11.2), not generating it.

**Download never asks a model for a figure.** The writers are on the figure path
and the architecture test enforces it; narrative sections render a stated note
when the model is off rather than silently dropping out.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from mrip.api.deps import ScopeDep, SourceIpDep, StoreDep, require_role
from mrip.auth.principal import Principal
from mrip.db.repositories.reports import IllegalReportTransitionError, SelfApprovalError
from mrip.llm import client_from_settings
from mrip.reports.generate import diff as diff_manifest
from mrip.reports.generate import generate
from mrip.reports.narrate import narrate
from mrip.reports.render import ReportIncompleteError, render_markdown
from mrip.reports.template import (
    BUILTIN_TEMPLATES,
    ReportTemplate,
    builtin_template,
    normalize_context,
)
from mrip.reports.writers import CONTENT_TYPES, render_docx, render_pptx, render_xlsx
from mrip.schemas import FigureDelta, ReportManifest, ReportState, Role

router = APIRouter(prefix="/reports", tags=["reports"])

OfficerDep = Annotated[Principal, Depends(require_role(Role.OFFICER))]
ApproverDep = Annotated[Principal, Depends(require_role(Role.APPROVER))]


class GenerateRequest(BaseModel):
    """What to render, for whom, and for when."""

    entity: str = Field(description="Entity name, code or alias — resolved canonically")
    period: str = Field(description="Period as written, e.g. 'FY2024-25'")
    template_id: str = Field(
        default="production-summary",
        description="Which template to render. See GET /reports/templates.",
    )


class TransitionRequest(BaseModel):
    """A move through the publish lifecycle."""

    state: ReportState
    note: str | None = Field(default=None, description="Recorded with the decision.")


@router.post("", response_model=ReportManifest, status_code=201)
def create_report(
    body: GenerateRequest,
    store: StoreDep,
    scope: ScopeDep,
    officer: OfficerDep,
    request: Request,
    caller_ip: SourceIpDep,
) -> ReportManifest:
    """Generate a report and store its pinned-evidence manifest.

    Returns 201 with the manifest whether or not it is complete: a manifest
    naming the figures it could not pin, and the reason for each, is a more
    useful answer than an error. ``complete`` says which it is, and the render
    endpoints are what refuse.
    """
    if body.template_id not in BUILTIN_TEMPLATES:
        known = ", ".join(sorted(BUILTIN_TEMPLATES))
        raise HTTPException(
            status_code=404,
            detail=f"No template {body.template_id!r}. Available: {known}.",
        )
    template = builtin_template(body.template_id)

    try:
        context = normalize_context(entity=body.entity, period=body.period)
    except ValueError as bad:
        # 422 with the reason, not 400 with "invalid input": the normalizer
        # declined to guess what the caller meant, and saying so is the product.
        raise HTTPException(status_code=422, detail=str(bad)) from bad

    if not scope.allows(context["entity"]):
        # 404 rather than 403, as everywhere else: confirming that a report could
        # be generated for an entity the caller cannot see is itself disclosure.
        raise HTTPException(
            status_code=404, detail=f"No entity {body.entity!r} in your scope."
        )

    manifest = generate(store, scope, template, entity=body.entity, period=body.period)
    store.reports.create(
        manifest,
        entity_id=context["entity"],
        period_label=context["period"],
        generated_by=officer.user_id,
    )
    store.audit.record(
        "report.generated",
        actor_user_id=officer.user_id,
        actor_username=officer.username,
        subject_type="report",
        subject_id=manifest.report_id,
        detail={
            "template_id": manifest.template_id,
            "template_version": manifest.template_version,
            "entity_id": context["entity"],
            "period_label": context["period"],
            "figures": len(manifest.figures),
            "missing_required": [item.label for item in manifest.missing_required],
            "complete": manifest.complete,
        },
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )
    return manifest


class TemplateSummary(BaseModel):
    """What a template needs, so a caller can choose before generating."""

    id: str
    version: int
    title: str
    required: list[str] = Field(description="Metrics that must resolve, or no render")
    optional: list[str] = Field(description="Metrics omitted when absent")
    sections: list[str]
    has_narrative: bool = Field(
        description="Whether any section is prose, and therefore needs the model"
    )


@router.get("/templates", response_model=list[TemplateSummary])
def list_templates() -> list[TemplateSummary]:
    """The installed templates and what each requires.

    Declared before ``/{report_id}`` so the literal path wins over the
    parameterised one — otherwise this resolves as a report with the id
    ``"templates"`` and 404s.
    """
    summaries: list[TemplateSummary] = []
    for template_id in sorted(BUILTIN_TEMPLATES):
        template = builtin_template(template_id)
        summaries.append(
            TemplateSummary(
                id=template.id,
                version=template.version,
                title=template.title,
                required=[field.metric for field in template.required],
                optional=[field.metric for field in template.optional],
                sections=[section.title for section in template.sections],
                has_narrative=any(
                    section.kind == "narrative" for section in template.sections
                ),
            )
        )
    return summaries


@router.get("", response_model=list[ReportManifest])
def list_reports(
    store: StoreDep,
    scope: ScopeDep,
    state: ReportState | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ReportManifest]:
    """Reports the caller may see, newest first."""
    return store.reports.list(scope, state=state, limit=limit, offset=offset)


@router.get("/{report_id}", response_model=ReportManifest)
def get_report(report_id: str, store: StoreDep, scope: ScopeDep) -> ReportManifest:
    """One stored manifest — the figures exactly as they were pinned."""
    return _load(store, report_id, scope)


@router.get("/{report_id}/diff", response_model=list[FigureDelta])
def diff_report(report_id: str, store: StoreDep, scope: ScopeDep) -> list[FigureDelta]:
    """What has moved since this report was generated (§11.3).

    The answer to "the Ministry is asking why last year's number has changed",
    which is otherwise a week of archaeology: each pinned figure is re-resolved
    against the corpus as it stands now, and the deltas name both the new value
    and the document version that moved it.
    """
    manifest = _load(store, report_id, scope)
    return diff_manifest(store, scope, manifest)


@router.post("/{report_id}/transition", response_model=ReportManifest)
def transition_report(
    report_id: str,
    body: TransitionRequest,
    store: StoreDep,
    scope: ScopeDep,
    request: Request,
    caller_ip: SourceIpDep,
    approver: ApproverDep,
) -> ReportManifest:
    """Move a report through ``draft → in_review → approved → published``.

    Takes the approver role for every move, including sending one back: pushing
    a report out of review is as much a decision as approving it. A move the
    lifecycle forbids — anything out of ``published``, or skipping review — is a
    409 naming what *is* allowed from here.
    """
    try:
        manifest = store.reports.transition(
            report_id,
            body.state,
            scope,
            actor_user_id=approver.user_id,
            require_separate_approver=store.settings.require_separate_approver,
        )
    except LookupError as missing:
        raise HTTPException(
            status_code=404, detail=f"No report {report_id!r}"
        ) from missing
    except SelfApprovalError as self_approval:
        # 409, not 403: the caller holds the right role, the report is simply
        # not theirs to sign off. A 403 would read as "you lack permission",
        # which would send an approver to an administrator instead of to a
        # colleague.
        raise HTTPException(status_code=409, detail=str(self_approval)) from self_approval
    except IllegalReportTransitionError as illegal:
        raise HTTPException(status_code=409, detail=str(illegal)) from illegal

    store.audit.record(
        f"report.{body.state.value}",
        actor_user_id=approver.user_id,
        actor_username=approver.username,
        subject_type="report",
        subject_id=report_id,
        detail={"state": body.state.value, "note": body.note},
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )
    return manifest


@router.get("/{report_id}/download")
def download_report(
    report_id: str,
    store: StoreDep,
    scope: ScopeDep,
    request: Request,
    caller_ip: SourceIpDep,
    fmt: str = Query(default="md", pattern="^(md|docx|xlsx|pptx)$"),
) -> Response:
    """Render a complete report to a file.

    Refuses with 409 when a required figure could not be pinned — §11.2 forbids
    emitting a report with a blank where a number should be, and the refusal
    names the fields so it is actionable rather than merely negative.

    Narrative sections are written here, at render time, from the figures the
    manifest already pinned — never at generation time. Two reasons: the figures
    must exist before prose can be checked against them, and a report whose
    model was unavailable at generation should not be permanently prose-less
    when it is rendered later.
    """
    manifest = _load(store, report_id, scope)
    template = _template_for(manifest)
    narration = narrate(template, manifest, client_from_settings(store.settings))

    try:
        body = _render(fmt, template, manifest, narration.sections)
    except ReportIncompleteError as incomplete:
        raise HTTPException(status_code=409, detail=str(incomplete)) from incomplete

    store.audit.record(
        "report.downloaded",
        actor_username=None,
        subject_type="report",
        subject_id=report_id,
        detail={
            "format": fmt,
            "state": manifest.state.value,
            "template_id": manifest.template_id,
            "narrative_sections": sorted(narration.sections),
            # Which sections had a sentence dropped for carrying an unsupported
            # numeral. Recorded rather than merely returned: a reviewer reading
            # the trail later needs to know the model was edited, and where.
            "narrative_flagged": sorted(narration.flagged),
        },
        request_id=getattr(request.state, "request_id", None),
        source_ip=caller_ip,
    )
    stem = manifest.report_id
    return Response(
        content=body,
        media_type=CONTENT_TYPES[fmt],
        headers={"content-disposition": f'attachment; filename="{stem}.{fmt}"'},
    )


def _render(
    fmt: str,
    template: ReportTemplate,
    manifest: ReportManifest,
    narratives: dict[str, str],
) -> bytes:
    """Dispatch to the writer for one format.

    Markdown is encoded here rather than in :func:`render_markdown`, which
    returns text because that is what it is — the HTTP layer is where a string
    becomes bytes.
    """
    if fmt == "md":
        return render_markdown(template, manifest, narratives=narratives).encode("utf-8")
    if fmt == "docx":
        return render_docx(template, manifest, narratives=narratives)
    if fmt == "xlsx":
        return render_xlsx(template, manifest, narratives=narratives)
    return render_pptx(template, manifest, narratives=narratives)


def _template_for(manifest: ReportManifest) -> ReportTemplate:
    """The template a stored manifest was generated from.

    A manifest records its ``template_id`` *and* ``template_version``, so a
    report re-renders under the definition that produced it. If that template is
    no longer installed the render refuses rather than silently substituting
    another one — the layout is part of what was approved.
    """
    try:
        return builtin_template(manifest.template_id)
    except KeyError as missing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Template {manifest.template_id!r} is no longer installed, so "
                "this report cannot be re-rendered under the definition that "
                "produced it."
            ),
        ) from missing


def _load(store: StoreDep, report_id: str, scope: ScopeDep) -> ReportManifest:
    manifest = store.reports.get(report_id, scope)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"No report {report_id!r}")
    return manifest
