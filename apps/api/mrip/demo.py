"""The demonstration corpus.

`mrip-admin seed-demo` builds a small, deliberately realistic corpus and runs it
through **the real pipeline** — the same intake boundary, the same six stages,
the same extractor — so an evaluator sees the system working rather than a
fixture dump. Nothing here writes a fact directly.

Every document it creates is marked ``is_synthetic=True``, which the UI renders
as a badge on every row. That flag is not decoration: the one thing this platform
must never do is let a made-up figure be mistaken for a government source, and a
seeded corpus is exactly where that could happen.

The documents are shaped like the real thing:

- a **production table** by subsidiary and fiscal year, with the unit in a
  caption and a total row that must not be extracted;
- a **revised version** of the same report whose SECL figure disagrees, so the
  conflict radar has a genuine disagreement to adjudicate rather than a
  manufactured one;
- an **offtake spreadsheet**, so the sheet-cell path is exercised;
- a **Word note**, which has no pages and therefore no page number to cite.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mrip import log
from mrip.auth import passwords
from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.blobs import get_blob_store
from mrip.db import Store, store_session
from mrip.db.repositories.documents import new_id
from mrip.ingest.intake import inspect_upload

# Importing the handler module is what registers the six ingestion stages — the
# same side-effecting import the worker and the API make. Without it, seeding
# fails with "no handler registered", which is the registry telling the truth.
from mrip.jobs import handlers  # noqa: F401
from mrip.jobs.queue import JobQueue
from mrip.jobs.registry import JobContext, get_handler
from mrip.schemas import Document, DocumentState, Role, Sensitivity

__all__ = ["DEMO_ACCOUNTS", "DEMO_PASSWORD", "seed_demo"]

logger = log.get_logger("mrip.demo")

#: One password for every demonstration account, printed by the command and
#: documented in docs/EVALUATION.md. It satisfies the real policy (12+
#: characters, five distinct, not the username) because the policy is not
#: relaxed for the demo — an evaluator seeing a weak password accepted would be
#: right to wonder what else is relaxed.
DEMO_PASSWORD = "sih-demo-2026-mrip"  # noqa: S105 — published on purpose

#: The four roles, so the access model can be inspected by signing in rather
#: than by reading about it. Scope differs on purpose: `officer.secl` sees one
#: subsidiary's corpus, and that is visible immediately as a shorter document
#: list.
DEMO_ACCOUNTS: tuple[tuple[str, Role, tuple[str, ...], str], ...] = (
    ("admin", Role.ADMIN, (SCOPE_ALL,), "Deployment administrator"),
    ("hq.officer", Role.OFFICER, (SCOPE_ALL,), "CIL headquarters reporting officer"),
    ("secl.officer", Role.OFFICER, ("secl",), "SECL reporting officer — one subsidiary"),
    (
        "cmpdi.reviewer",
        Role.REVIEWER,
        (SCOPE_ALL,),
        "CMPDI reviewer — adjudicates conflicts",
    ),
    ("ministry.viewer", Role.VIEWER, (SCOPE_ALL,), "Ministry of Coal — read only"),
)

_PIPELINE = (
    "document.classify",
    "document.digitize",
    "document.extract",
    "document.normalize",
    "document.validate",
    "document.index",
)


@dataclass(frozen=True, slots=True)
class DemoDocument:
    filename: str
    title: str
    build: str
    publisher: str | None = None
    fiscal_year: str | None = None
    notes: str | None = None


# --------------------------------------------------------------- the documents

#: Production as first reported, then as revised. The SECL figure moves, which
#: is the disagreement the conflict radar exists for.
_PRODUCTION_ORIGINAL = [
    ["Subsidiary", "FY2023-24", "FY2024-25"],
    ["SECL", "167.00", "193.00"],
    ["MCL", "193.00", "210.50"],
    ["WCL", "62.00", "68.50"],
    ["NCL", "131.00", "139.00"],
    ["Total", "553.00", "611.00"],
]

_PRODUCTION_REVISED = [
    ["Subsidiary", "FY2023-24", "FY2024-25"],
    ["SECL", "167.00", "191.50"],  # ← revised down; the conflict
    ["MCL", "193.00", "210.50"],
    ["WCL", "62.00", "68.50"],
    ["NCL", "131.00", "139.00"],
    ["Total", "553.00", "609.50"],
]


def _write_table_pdf(
    path: Path, caption: str, unit_note: str, rows: list[list[str]], footer: str
) -> None:
    """A one-page PDF with a *ruled* table.

    The ruling lines matter: with them pdfplumber reads the table's structure
    from the document instead of inferring it from whitespace, which is the
    difference between 0.95 and 0.78 parse confidence on every figure inside.
    """
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()

    page.insert_text((72, 70), "COAL INDIA LIMITED", fontsize=9)
    page.insert_text((72, 96), caption, fontsize=13)
    page.insert_text((72, 114), unit_note, fontsize=9)

    left, top, row_height, col_width = 72, 134, 22, 110
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            page.insert_text(
                (left + col_index * col_width + 6, top + row_index * row_height + 15),
                value,
                fontsize=10,
            )

    for row_index in range(len(rows) + 1):
        y = top + row_index * row_height
        page.draw_line((left, y), (left + len(rows[0]) * col_width, y))
    for col_index in range(len(rows[0]) + 1):
        x = left + col_index * col_width
        page.draw_line((x, top), (x, top + len(rows) * row_height))

    page.insert_text((72, top + len(rows) * row_height + 30), footer, fontsize=8)

    # Fixed metadata and no fresh document id, so re-running this command
    # produces byte-identical files. Without it every run generates a new
    # timestamp, the content hash changes, and the dedup that makes uploads
    # idempotent cannot recognise a document it has already ingested — the
    # corpus grows a duplicate on each run.
    document.set_metadata(
        {
            "title": caption,
            "author": "Coal India Limited (synthetic demonstration document)",
            "creator": "MRIP demonstration corpus",
            "producer": "MRIP demonstration corpus",
            "creationDate": "D:20250415000000Z",
            "modDate": "D:20250415000000Z",
        }
    )
    document.save(path, no_new_id=True)
    document.close()


def _write_offtake_xlsx(path: Path) -> None:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Offtake"
    sheet["A1"] = "Coal offtake by subsidiary"
    sheet["A2"] = "Figures in Million Tonnes"
    offtake: list[list[str | float]] = [
        ["Subsidiary", "FY2023-24", "FY2024-25"],
        ["SECL", 162.40, 188.20],
        ["MCL", 190.10, 205.70],
        ["WCL", 60.80, 66.90],
    ]
    for index, row in enumerate(offtake, start=4):
        for column, cell_value in enumerate(row, start=1):
            sheet.cell(row=index, column=column, value=cell_value)
    workbook.save(path)


def _write_capacity_docx(path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_paragraph("Rated capacity of operating mines")
    document.add_paragraph("(Figures in Million Tonnes)")
    table = document.add_table(rows=4, cols=2)
    for row_index, row in enumerate(
        [
            ["Subsidiary", "FY2024-25"],
            ["SECL", "185.00"],
            ["MCL", "205.00"],
            ["WCL", "70.00"],
        ]
    ):
        for col_index, value in enumerate(row):
            table.cell(row_index, col_index).text = value
    document.add_paragraph(
        "Capacity as sanctioned. Production above capacity is flagged for review "
        "by the validation rules rather than rejected."
    )
    document.save(str(path))


def _build_all(workspace: Path) -> list[tuple[Path, DemoDocument]]:
    """Write the demonstration files to disk and describe each one."""
    built: list[tuple[Path, DemoDocument]] = []

    original = workspace / "cil-production-fy2024-25.pdf"
    _write_table_pdf(
        original,
        "Coal production by subsidiary",
        "(Figures in Million Tonnes)",
        _PRODUCTION_ORIGINAL,
        "Provisional figures as reported on 15 April 2025.",
    )
    built.append(
        (
            original,
            DemoDocument(
                filename=original.name,
                title="CIL production by subsidiary, FY2024-25 (provisional)",
                build="production",
                publisher="cil",
            ),
        )
    )

    revised = workspace / "cil-production-fy2024-25-revised.pdf"
    _write_table_pdf(
        revised,
        "Coal production by subsidiary",
        "(Figures in Million Tonnes)",
        _PRODUCTION_REVISED,
        "Revised figures issued 30 June 2025. Supersedes the provisional statement.",
    )
    built.append(
        (
            revised,
            DemoDocument(
                filename=revised.name,
                title="CIL production by subsidiary, FY2024-25 (revised)",
                build="production_revised",
                publisher="cil",
                notes="Revises the provisional statement; SECL FY2024-25 moves "
                "from 193.00 to 191.50 Mt.",
            ),
        )
    )

    offtake = workspace / "cil-offtake-fy2024-25.xlsx"
    _write_offtake_xlsx(offtake)
    built.append(
        (
            offtake,
            DemoDocument(
                filename=offtake.name,
                title="CIL offtake by subsidiary, FY2024-25",
                build="offtake",
                publisher="cil",
            ),
        )
    )

    capacity = workspace / "operating-mine-capacity.docx"
    _write_capacity_docx(capacity)
    built.append(
        (
            capacity,
            DemoDocument(
                filename=capacity.name,
                title="Rated capacity of operating mines",
                build="capacity",
                publisher="cil",
            ),
        )
    )

    return built


# ----------------------------------------------------------------- the seeding


def _ensure_accounts(store: Store) -> list[str]:
    """Create the demonstration accounts, skipping any that already exist."""
    created: list[str] = []
    password_hash = passwords.hash_password(DEMO_PASSWORD)

    for username, role, entities, display_name in DEMO_ACCOUNTS:
        if store.users.by_username(username) is not None:
            continue
        record = store.users.create(
            username,
            role=role,
            password_hash=password_hash,
            display_name=display_name,
            # A demonstration account that demanded a password change on first
            # sign-in would waste an evaluator's first minute.
            must_change_password=False,
        )
        store.users.replace_scopes(record.user_id, list(entities))
        store.audit.record(
            "user.created",
            actor_username="mrip-admin (seed-demo)",
            subject_type="user",
            subject_id=record.user_id,
            detail={"username": username, "role": role.value, "via": "seed-demo"},
        )
        created.append(username)
    return created


def _register(
    store: Store,
    path: Path,
    described: DemoDocument,
    uploader: str | None,
    existing_by_filename: dict[str, Document],
) -> str | None:
    """Put one file through the real intake boundary and register it.

    Returns the document id to run the pipeline for, or ``None`` when there is
    nothing to do. A document that is already ``ready`` is left alone; one left
    part-way by an interrupted run is resumed, which is safe because every stage
    replaces its own output.

    Identity is checked **by filename as well as by content hash**. The hash is
    the real rule for uploads and still applies below; the filename check exists
    because this corpus is regenerated on every run, and a generator that ever
    produced a byte of difference would otherwise duplicate the whole corpus
    quietly.
    """
    previous = existing_by_filename.get(described.filename)
    if previous is not None:
        return None if previous.state is DocumentState.READY else previous.document_id

    settings = store.settings
    report = inspect_upload(
        path,
        filename=described.filename,
        max_bytes=settings.max_upload_bytes,
        max_pages=settings.max_upload_pages,
    )
    blob = get_blob_store().put_file(path)

    existing = store.find_document_by_hash(blob.content_hash)
    if existing is not None:
        # Already registered. If a previous run stopped part-way — the pipeline
        # failed, the process was killed — the document is sitting in an
        # unfinished state, and re-running this command should carry it forward
        # rather than leave it stranded. Re-running the stages is safe: each one
        # replaces its own output.
        return None if existing.state is DocumentState.READY else existing.document_id

    document = Document(
        document_id=new_id("doc"),
        content_hash=blob.content_hash,
        filename=described.filename,
        doc_class=report.doc_class,
        page_count=report.page_count,
        size_bytes=report.size_bytes,
        title=described.title,
        publisher_entity_id=described.publisher,
        fiscal_year=described.fiscal_year,
        ingested_at=datetime.now(UTC),
        notes=described.notes,
        state=DocumentState.RECEIVED,
        sensitivity=Sensitivity.INTERNAL,
        blob_key=blob.content_hash,
        uploaded_by=uploader,
        # The badge that keeps a demonstration figure from ever being mistaken
        # for a government source.
        is_synthetic=True,
    )
    store.register_document(document)
    return document.document_id


def _run_pipeline_inline(document_id: str) -> None:
    """Run the six stages here rather than leaving them for a worker.

    A seeding command that required a second terminal to finish would be a worse
    first experience, and the stages are the same code either way — each runs in
    its own transaction, exactly as the worker runs them.
    """
    for kind in _PIPELINE:
        with store_session() as store:
            job = (
                JobQueue(store.connection)
                .enqueue(
                    kind,
                    {"document_id": document_id},
                    idempotency_key=f"seed:{document_id}:{kind}",
                )
                .job
            )
            get_handler(kind)(
                JobContext(
                    job=job,
                    store=store,
                    worker_id="seed-demo",
                    lease_seconds=300,
                )
            )


def seed_demo(*, with_documents: bool = True) -> dict[str, Any]:
    """Create the demonstration accounts and corpus. Idempotent.

    Returns a summary for the CLI to print. Safe to re-run: accounts that exist
    are left alone, and a document whose bytes are already stored is skipped by
    the same content-hash rule that makes a retried upload a no-op.
    """
    with store_session() as store:
        accounts = _ensure_accounts(store)
        uploader = store.users.by_username("hq.officer")
        uploader_id = uploader.user_id if uploader else None

    if not with_documents:
        return {"accounts": accounts, "documents": [], "facts": 0}

    with store_session() as store:
        existing_by_filename = {
            document.filename: document
            for document in store.list_documents(
                Scope.unrestricted("seed-demo: finds its own previous run"),
                limit=200,
            )
            if document.is_synthetic
        }

    registered: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mrip-demo-") as workspace:
        for path, described in _build_all(Path(workspace)):
            with store_session() as store:
                document_id = _register(
                    store, path, described, uploader_id, existing_by_filename
                )
            if document_id is None:
                continue
            registered.append(document_id)
            _run_pipeline_inline(document_id)

    with store_session() as store:
        scope = Scope.unrestricted("seed-demo: reports what it just created")
        summary = store.summary(scope)
        conflicts = len(store.open_conflicts(scope))

    logger.info(
        "demo corpus seeded",
        accounts=len(accounts),
        documents=len(registered),
        facts=summary["facts"],
        open_conflicts=conflicts,
    )
    return {
        "accounts": accounts,
        "documents": registered,
        "facts": summary["facts"],
        "open_conflicts": conflicts,
        "counts": summary,
    }
