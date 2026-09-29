"""Push the real corpus through the pipeline and report what actually happened.

``seed-demo`` proves the pipeline runs on tables we wrote. This proves — or
disproves — that it works on documents Coal India published, which is a different
claim and the one that matters.

The honesty rule here is the same one the whole project runs on: **the report is
the deliverable**. It is easy to make a corpus ingest by loosening the boundary
until nothing is refused. It is useful to know that 4% of the Ministry's
statistics are scanned annexures needing OCR, that a third of the CIL monthly
statements carry an OCR layer that must not be trusted, and that 1,400 of 2,900
documents yield no figure at all because they are AGM notices rather than data.

So this module counts. Per document it records the class, whether OCR was needed,
how many evidence rows the digitizer produced, how many facts the extractor
produced from them, and — when it produced none — *why*. The reasons are the
interesting part: "no table found" and "table found but no metric row" are two
different problems, one solved by a better table detector and one by a better
metric lexicon, and a single "failed" count would hide which we have.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mrip import log
from mrip.auth.scope import Scope
from mrip.schemas import DocumentState

__all__ = ["CorpusReport", "IngestOutcome", "ingest_corpus"]

logger = log.get_logger("mrip.corpus.ingest")

_PIPELINE = (
    "document.classify",
    "document.digitize",
    "document.extract",
    "document.normalize",
    "document.validate",
    "document.index",
)

#: Sensible ceiling on one run. The corpus is ~3,000 documents; ingesting all of
#: them takes hours, and an operator trying this out wants a number they can pick.
DEFAULT_LIMIT = 250


@dataclass
class IngestOutcome:
    """One document's trip through the pipeline."""

    filename: str
    company: str
    document_id: str
    doc_class: str
    pages: int
    state: str
    facts: int = 0
    error: str | None = None
    #: Facts the extractor refused, by reason. A document that yields none is
    #: either empty of figures or hitting a limit in the extractor, and this says
    #: which.
    skips: dict[str, int] = field(default_factory=dict)


@dataclass
class CorpusReport:
    """What a run found, in the shape the command prints and tests assert on."""

    outcomes: list[IngestOutcome] = field(default_factory=list)
    registered: int = 0
    already_ready: int = 0
    failed: int = 0

    @property
    def with_facts(self) -> list[IngestOutcome]:
        return [o for o in self.outcomes if o.facts > 0]

    def summary(self) -> dict[str, Any]:
        by_class = Counter(o.doc_class for o in self.outcomes)
        by_state = Counter(o.state for o in self.outcomes)
        skips: Counter[str] = Counter()
        for outcome in self.outcomes:
            skips.update(outcome.skips)
        facts = sum(o.facts for o in self.outcomes)
        pages = sum(o.pages for o in self.outcomes)
        return {
            "documents": len(self.outcomes),
            "registered": self.registered,
            "facts": facts,
            "pages": pages,
            "with_facts": len(self.with_facts),
            "by_class": dict(by_class),
            "by_state": dict(by_state),
            "skips": dict(skips.most_common(12)),
            "errors": [o for o in self.outcomes if o.error],
            "best": sorted(self.outcomes, key=lambda o: -o.facts)[:10],
        }


def ingest_corpus(
    root: Path,
    *,
    limit: int | None = DEFAULT_LIMIT,
    companies: tuple[str, ...] | None = None,
    uploader: str = "hq.officer",
    only: list[str] | None = None,
    skip_ready: bool = True,
    manifest: Path | None = None,
) -> CorpusReport:
    """Register the real documents and run every stage over them.

    Ordering is by size, smallest first, because the small documents are the
    monthly production statements — the actual target — and a run that is stopped
    after ten minutes should have done the interesting ones rather than three
    annual reports.

    ``manifest`` is read for the source URL of each file. Provenance is the point
    of this platform, and a fetched document's provenance is a URL, so it is
    carried into the database rather than left behind in a JSON file on disk.
    """
    from mrip.auth.scope import Scope as _Scope
    from mrip.corpus import load_manifest
    from mrip.db import store_session
    from mrip.demo import _run_pipeline_inline

    report = CorpusReport()
    unrestricted = _Scope.unrestricted("ingest-corpus: reads its own prior runs")

    manifest = manifest or (root / "manifest.json")
    origins = {
        f"{entry.company}/{entry.filename}": entry.url
        for entry in load_manifest(manifest)
    }
    logger.info("manifest loaded", entries=len(origins), path=str(manifest))

    with store_session() as store:
        account = store.users.by_username(uploader)
        uploader_id = account.user_id if account else None
        known = {
            document.filename: document
            for document in store.list_documents(unrestricted, limit=20000)
            if not document.is_synthetic
        }

    paths = _candidates(root, companies=companies, only=only, limit=limit)
    logger.info("ingesting corpus", documents=len(paths), root=str(root))

    for path in paths:
        company = path.parent.name
        filename = path.name
        source_url = origins.get(f"{company}/{filename}")

        previous = known.get(filename)
        document_id: str | None
        if previous is not None:
            if skip_ready and previous.state is DocumentState.READY:
                report.already_ready += 1
                continue
            document_id = previous.document_id
        else:
            with store_session() as store:
                document_id = _register_real(
                    store, path, company, filename, uploader_id, source_url
                )
            if document_id is None:
                # Rejected at the boundary, or a duplicate of a document already
                # stored under a different name. Counted, not hidden: a corpus
                # entry the product refuses is a finding.
                report.failed += 1
                report.outcomes.append(
                    IngestOutcome(
                        filename=filename,
                        company=company,
                        document_id="",
                        doc_class="rejected",
                        pages=0,
                        state="refused_at_boundary",
                    )
                )
                continue
            report.registered += 1

        try:
            _run_pipeline_inline(document_id)
        except Exception as failure:
            report.failed += 1
            report.outcomes.append(
                IngestOutcome(
                    filename=filename,
                    company=company,
                    document_id=document_id,
                    doc_class="?",
                    pages=0,
                    state="failed",
                    error=f"{type(failure).__name__}: {failure}"[:300],
                )
            )
            logger.warning("pipeline failed", filename=filename, error=str(failure)[:200])
            continue

        report.outcomes.append(_inspect(document_id, filename, company))

    return report


def _candidates(
    root: Path,
    *,
    companies: tuple[str, ...] | None,
    only: list[str] | None,
    limit: int | None,
) -> list[Path]:
    wanted = {name.lower() for name in (only or [])}
    paths: list[Path] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if companies and directory.name not in companies:
            continue
        for path in sorted(directory.glob("*.pdf")) + sorted(directory.glob("*.xlsx")):
            if wanted and path.name.lower() not in wanted:
                continue
            paths.append(path)
    # Smallest first: the monthly statements are small and are the target.
    paths.sort(key=lambda p: p.stat().st_size)
    return paths[:limit] if limit is not None else paths


def _register_real(
    store: Any,
    path: Path,
    company: str,
    filename: str,
    uploader_id: str | None,
    source_url: str | None,
) -> str | None:
    """One real document through the real intake boundary.

    Deliberately *not* flagged synthetic — that badge is for documents this
    project generated, and putting it on a document fetched from coalindia.in
    would be the same category error in the other direction. What marks it as
    externally sourced is ``source_url``, read from the manifest.
    """
    from datetime import UTC, datetime

    from mrip.blobs import get_blob_store
    from mrip.db.repositories.documents import new_id
    from mrip.ingest.intake import IntakeError, inspect_upload
    from mrip.schemas import Document, Sensitivity

    settings = store.settings
    try:
        inspection = inspect_upload(
            path,
            filename=filename,
            max_bytes=settings.max_upload_bytes,
            max_pages=settings.max_upload_pages,
        )
    except IntakeError as refusal:
        logger.warning("refused at the boundary", filename=filename, reason=str(refusal))
        return None

    blob = get_blob_store().put_file(path)
    existing = store.find_document_by_hash(blob.content_hash)
    if existing is not None:
        return None if existing.state is DocumentState.READY else existing.document_id

    document = Document(
        document_id=new_id("doc"),
        content_hash=blob.content_hash,
        filename=filename,
        doc_class=inspection.doc_class,
        page_count=inspection.page_count,
        size_bytes=inspection.size_bytes,
        title=Path(filename).stem.replace("_", " ")[:200],
        publisher_entity_id=_publisher_for(company),
        fiscal_year=None,
        ingested_at=datetime.now(UTC),
        notes=f"Public document fetched from {source_url}" if source_url else None,
        state=DocumentState.RECEIVED,
        sensitivity=Sensitivity.PUBLIC,
        blob_key=blob.content_hash,
        uploaded_by=uploader_id,
        is_synthetic=False,
        source_url=source_url,
    )
    store.register_document(document)
    return document.document_id


def _publisher_for(company: str) -> str | None:
    from mrip.normalize.entities import ENTITIES

    for entity in ENTITIES:
        if entity.entity_id == company:
            return entity.entity_id
    return None


def _inspect(document_id: str, filename: str, company: str) -> IngestOutcome:
    """Read back what the pipeline produced for one document."""
    from mrip.db import store_session

    outcome = IngestOutcome(
        filename=filename,
        company=company,
        document_id=document_id,
        doc_class="?",
        pages=0,
        state="?",
    )
    unrestricted = Scope.unrestricted("ingest-corpus: reporting on its own run")

    with store_session() as store:
        document = store.get_document(document_id, unrestricted)
        if document is not None:
            outcome.doc_class = document.doc_class.value
            outcome.pages = document.page_count or 0
            outcome.state = document.state.value
        facts = store.query_facts(unrestricted, document_id=document_id, limit=10000)
        outcome.facts = len(facts)
        jobs = store.jobs.for_document(document_id) if hasattr(store, "jobs") else []
        for job in jobs:
            for skip in _skips_from(job):
                reason = getattr(skip, "reason", None) or str(skip)
                outcome.skips[str(reason)] = outcome.skips.get(str(reason), 0) + 1
    return outcome


def _skips_from(job: Any) -> list[Any]:
    """The extractor's refusal list off a finished job, if it recorded one."""
    output = getattr(job, "output", None) or {}
    if not isinstance(output, dict):
        return []
    report = output.get("extraction_report") or {}
    skipped = report.get("skipped") if isinstance(report, dict) else None
    return list(skipped or ())


def format_summary(summary: dict[str, Any]) -> str:
    """The report the command prints. Counts, not adjectives."""
    lines: list[str] = []
    lines.append(f"documents ingested      {summary['documents']}")
    lines.append(f"newly registered        {summary['registered']}")
    lines.append(f"pages digitized         {summary['pages']:,}")
    lines.append(f"facts extracted         {summary['facts']:,}")
    produced = summary["with_facts"]
    documents = summary["documents"] or 1
    lines.append(
        f"documents with facts    {produced} "
        f"({100 * produced / documents:.0f}% of those attempted)"
    )
    if summary["by_class"]:
        rendered = ", ".join(
            f"{name} {count}" for name, count in sorted(summary["by_class"].items())
        )
        lines.append(f"by class                {rendered}")
    if summary["by_state"]:
        rendered = ", ".join(
            f"{name} {count}" for name, count in sorted(summary["by_state"].items())
        )
        lines.append(f"by state                {rendered}")
    if summary["skips"]:
        lines.append("")
        lines.append("why documents produced no figure:")
        for reason, count in summary["skips"].items():
            lines.append(f"  {count:6d}  {reason}")
    if summary["errors"]:
        lines.append("")
        lines.append(f"pipeline errors ({len(summary['errors'])}), first few:")
        for outcome in summary["errors"][:8]:
            lines.append(f"  {outcome.filename[:44]:44s} {outcome.error}")
    if summary["best"]:
        lines.append("")
        lines.append("most productive documents:")
        for outcome in summary["best"]:
            if outcome.facts:
                lines.append(
                    f"  {outcome.facts:4d} facts  {outcome.company:6s} "
                    f"{outcome.filename[:52]}"
                )
    return "\n".join(lines)
