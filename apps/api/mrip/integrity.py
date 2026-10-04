"""Does the evidence still exist?

Everything this platform claims rests on one property: a figure in a report names
a document, a version, a page and a cell, and someone can open that document and
see the number. The database enforces most of what keeps that true — foreign keys
from facts to documents, the append-only audit trigger, the lifecycle tables.

One link has no such protection. **The blob store is outside the database.** The
bytes of every source document live on a filesystem (or, later, an object store),
addressed by their own SHA-256, and nothing in PostgreSQL knows whether they are
still there. A restore that loads yesterday's database beside today's blob
directory, a volume mounted read-only during a migration, an operator reclaiming
disk — each leaves a corpus that answers every query correctly right up until
somebody clicks through to the source, during a Ministry deadline.

So this module exists to ask the question out loud, cheaply enough to run from
cron and thoroughly enough to trust a restore:

**Presence** — every registered document's blob is where the registry says.
A stat per document; seconds for the whole corpus.

**Integrity** — every blob still hashes to its own name. Reads every byte, so it
is opt-in. This is what catches bit rot and a tampered volume, and it is only
possible because the store is content-addressed: the name *is* the checksum, so
no second copy of anything is needed to verify the first.

**Reproducibility** — every figure a stored report pinned still resolves. §11.3
promises that re-opening a two-year-old report reproduces the figures as approved;
a manifest pointing at a fact that no longer exists breaks that promise silently,
because the manifest renders from its own stored copy and would not notice.

**Orphans** — blobs no document references. Harmless to a reader, but they are the
fingerprint of a partial rollback, and an operator deciding whether a restore
completed wants to know.

The result is a report, not an exception. An operator running this after a restore
needs to see everything that is wrong at once, not the first thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa

from mrip import log
from mrip.blobs import BlobNotFoundError, get_blob_store
from mrip.db.schema_version import check_schema_version
from mrip.db.tables import documents, facts, reports

if TYPE_CHECKING:
    from mrip.db.store import Store

__all__ = ["Finding", "IntegrityReport", "verify_corpus"]

logger = log.get_logger("mrip.integrity")


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing that is wrong, and what it means.

    ``remedy`` is not decoration. Whoever runs this is usually mid-incident, and a
    check that reports "17 missing blobs" without saying what to do with that has
    made their evening worse rather than better.
    """

    check: str
    detail: str
    remedy: str
    #: ``error`` means an answer this platform gives is now wrong or
    #: unverifiable. ``warning`` means something is untidy but every answer still
    #: holds.
    #:
    #: The distinction decides the exit code, and it exists because of how this
    #: command gets used: in cron. A sweep that exits non-zero for harmless
    #: residue — and a blob directory accumulates residue, from every rolled-back
    #: ingest — trains an operator to ignore it, and then it is not a check any
    #: more. So warnings are printed and do not fail.
    severity: Literal["error", "warning"] = "error"
    #: Identifiers to act on — document ids, content hashes, report ids. Capped by
    #: the caller, because a wholly empty blob directory would otherwise print
    #: three thousand lines before the summary.
    examples: list[str] = field(default_factory=list)
    #: Total affected, which may exceed ``len(examples)``.
    count: int = 0


@dataclass
class IntegrityReport:
    """Everything the sweep looked at, and everything it found."""

    documents_checked: int = 0
    blobs_hashed: int = 0
    reports_checked: int = 0
    figures_checked: int = 0
    orphan_blobs: int = 0
    findings: list[Finding] = field(default_factory=list)
    #: Set when the database is not at the revision the code expects. Reported
    #: first and loudly: every other count below it is being read through a schema
    #: the code does not agree with, so none of them mean what they appear to.
    schema_mismatch: str | None = None

    @property
    def errors(self) -> list[Finding]:
        """Findings that mean an answer is wrong or cannot be verified."""
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        """Findings worth seeing that do not make any answer wrong."""
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        """Whether every answer this platform would give is still sound.

        Warnings do not clear this flag, and that is the point — see
        :attr:`Finding.severity`. A schema mismatch does, because every count in
        the report was read through a schema the code disagrees with.
        """
        return not self.errors and self.schema_mismatch is None

    def describe(self) -> str:
        """The report as an operator should read it."""
        lines: list[str] = []
        if self.schema_mismatch:
            lines.append(f"SCHEMA   {self.schema_mismatch}")
            lines.append(
                "         Every count below was read through a schema the code "
                "does not agree with. Run `alembic upgrade head` and verify again."
            )
            lines.append("")

        lines.append(f"documents    {self.documents_checked:,} checked for presence")
        if self.blobs_hashed:
            lines.append(f"blobs        {self.blobs_hashed:,} re-hashed")
        lines.append(
            f"reports      {self.reports_checked:,} checked "
            f"({self.figures_checked:,} pinned figures)"
        )
        if self.orphan_blobs:
            lines.append(f"orphans      {self.orphan_blobs:,} unreferenced blobs")

        if not self.findings:
            lines.append("")
            lines.append("No problems found.")
            return "\n".join(lines)

        lines.append("")
        # Errors first. An operator skimming this under time pressure should meet
        # the thing that makes an answer wrong before the thing that is untidy.
        for finding in self.errors + self.warnings:
            marker = "" if finding.severity == "error" else "  (warning)"
            lines.append(f"{finding.check}{marker}")
            lines.append(f"  {finding.detail}")
            if finding.examples:
                shown = ", ".join(finding.examples)
                more = finding.count - len(finding.examples)
                tail = f" (+{more:,} more)" if more > 0 else ""
                lines.append(f"  e.g. {shown}{tail}")
            lines.append(f"  -> {finding.remedy}")
            lines.append("")
        return "\n".join(lines).rstrip()


def verify_corpus(
    store: Store, *, deep: bool = False, examples: int = 5
) -> IntegrityReport:
    """Check that the corpus's evidence is actually there.

    Unscoped by necessity: this is an operator's question about the whole
    deployment, and a scoped answer ("your subsidiary's evidence is fine") is
    exactly the wrong shape — the blob directory is shared, so a gap in it belongs
    to everybody. Reachable only from ``mrip-admin``, which requires shell access
    to the host.

    ``deep`` re-hashes every blob. Linear in corpus bytes, so it is the weekly or
    post-restore run rather than the hourly one.
    """
    report = IntegrityReport()

    version = check_schema_version()
    if not version.is_current:
        report.schema_mismatch = version.describe()

    _check_blob_presence(store, report, deep=deep, examples=examples)
    _check_pinned_figures(store, report, examples=examples)
    _check_orphan_blobs(store, report, examples=examples)

    logger.info(
        "integrity sweep complete",
        ok=report.ok,
        documents=report.documents_checked,
        findings=len(report.findings),
        deep=deep,
    )
    return report


def _check_blob_presence(
    store: Store, report: IntegrityReport, *, deep: bool, examples: int
) -> None:
    """Every registered document's bytes are where the registry says they are."""
    blobs = get_blob_store()
    rows = (
        store.connection.execute(
            sa.select(documents.c.document_id, documents.c.blob_key).where(
                documents.c.blob_key.is_not(None)
            )
        )
        .mappings()
        .all()
    )

    missing: list[str] = []
    corrupt: list[str] = []
    for row in rows:
        report.documents_checked += 1
        key = str(row["blob_key"])
        if not blobs.exists(key):
            missing.append(str(row["document_id"]))
            continue
        if deep:
            try:
                intact = blobs.verify(key)
            except BlobNotFoundError:
                # Vanished between the exists() check and the read: a volume
                # being unmounted underneath us is exactly the condition this
                # sweep is for, so it counts as missing rather than crashing.
                missing.append(str(row["document_id"]))
                continue
            report.blobs_hashed += 1
            if not intact:
                corrupt.append(str(row["document_id"]))

    if missing:
        report.findings.append(
            Finding(
                check="MISSING EVIDENCE",
                detail=(
                    f"{len(missing):,} of {report.documents_checked:,} documents are "
                    "registered but their bytes are not in the blob store. Every "
                    "figure extracted from them still answers queries, and every "
                    "citation on those figures is now a dead end."
                ),
                remedy=(
                    "Restore the blob directory from the backup taken with this "
                    "database — they are one unit. If the blobs are genuinely gone, "
                    "re-upload the sources; the content hash will match and the "
                    "existing documents will be reused rather than duplicated."
                ),
                examples=missing[:examples],
                count=len(missing),
            )
        )

    if corrupt:
        report.findings.append(
            Finding(
                check="ALTERED EVIDENCE",
                detail=(
                    f"{len(corrupt):,} blobs no longer hash to their own name. The "
                    "bytes on disk are not the bytes that were ingested, so the "
                    "document a citation opens is not the document the figure came "
                    "from. This is bit rot, a bad restore, or tampering."
                ),
                remedy=(
                    "Do not serve these. Restore the blob directory from backup and "
                    "verify again; if the backup is also altered, treat every figure "
                    "extracted from these documents as unverified until re-ingested."
                ),
                examples=corrupt[:examples],
                count=len(corrupt),
            )
        )


def _check_pinned_figures(
    store: Store, report: IntegrityReport, *, examples: int
) -> None:
    """Every figure a stored report pinned still resolves to a fact.

    §11.3's promise is that re-opening an approved report reproduces the figures as
    approved. The manifest renders from its own stored copy, so a vanished fact
    would not stop it rendering — it would just mean the report can no longer be
    reconciled against the fact store it claims to come from. Silence is the whole
    problem, so this says it.
    """
    rows = (
        store.connection.execute(
            sa.select(reports.c.report_id, reports.c.state, reports.c.manifest)
        )
        .mappings()
        .all()
    )
    if not rows:
        return

    dangling: list[str] = []
    for row in rows:
        report.reports_checked += 1
        manifest = row["manifest"] or {}
        pinned = [
            str(figure["fact_id"])
            for figure in manifest.get("figures", [])
            if isinstance(figure, dict) and figure.get("fact_id")
        ]
        if not pinned:
            continue
        report.figures_checked += len(pinned)

        present = set(
            store.connection.execute(
                sa.select(facts.c.fact_id).where(facts.c.fact_id.in_(pinned))
            )
            .scalars()
            .all()
        )
        if gone := [fact_id for fact_id in pinned if fact_id not in present]:
            dangling.append(f"{row['report_id']} ({len(gone)} of {len(pinned)})")

    if dangling:
        report.findings.append(
            Finding(
                check="UNRECONCILABLE REPORT",
                detail=(
                    f"{len(dangling):,} stored reports pin figures that are no longer "
                    "in the fact store. They still render, from the manifest's own "
                    "copy, but they can no longer be checked against the corpus they "
                    "claim to come from — which is the guarantee that makes a "
                    "published report evidence rather than a document."
                ),
                remedy=(
                    "Facts are never deleted by this application, so this means the "
                    "database was restored from a point before the reports were "
                    "generated, or rows were removed by hand. Restore to a "
                    "consistent point; a published report must not outlive its "
                    "evidence."
                ),
                examples=dangling[:examples],
                count=len(dangling),
            )
        )


def _check_orphan_blobs(store: Store, report: IntegrityReport, *, examples: int) -> None:
    """Blobs no document references.

    Harmless to a reader — nothing points at them — but they are the fingerprint
    of a partial rollback, and an operator deciding whether a restore finished
    wants to know. Reported, never deleted: the store is append-only, and a sweep
    that removed evidence on its own authority would be the most dangerous code in
    this repository.
    """
    blobs = get_blob_store()
    root = getattr(blobs, "root", None)
    if root is None:  # an object-store implementation; skip rather than guess
        return

    referenced = set(
        store.connection.execute(
            sa.select(documents.c.blob_key).where(documents.c.blob_key.is_not(None))
        )
        .scalars()
        .all()
    )

    orphans: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.parent.name == "incoming":
            continue
        # The filename is the content hash; that is the whole point of the layout.
        if path.name not in referenced:
            orphans.append(path.name)

    report.orphan_blobs = len(orphans)
    if orphans:
        report.findings.append(
            Finding(
                check="UNREFERENCED BLOBS",
                # A warning, not an error: nothing points at these, so no
                # answer this platform gives is affected. Failing a cron check
                # on residue is how a check stops being read.
                severity="warning",
                detail=(
                    f"{len(orphans):,} blobs are in the store but no document "
                    "references them. Nothing reads them, so no answer is wrong — "
                    "but this is what a rolled-back ingest or a database restored "
                    "to an earlier point than the blob directory looks like."
                ),
                remedy=(
                    "If the database was restored, restore the blob directory to the "
                    "same point and verify again. If this is residue from a failed "
                    "upload it is safe to leave: the store is append-only and these "
                    "cost only disk."
                ),
                examples=orphans[:examples],
                count=len(orphans),
            )
        )
