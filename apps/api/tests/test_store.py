"""Tests for the PostgreSQL evidence and fact store.

Two behaviours matter more than the rest and are tested hardest:

* a fact must survive a round-trip with its **evidence and decomposed confidence
  intact** — a fact that loses its citation in storage is worse than no fact;
* :meth:`Store.detect_conflicts` must **surface** disagreements and never
  resolve them. Every test that could pass by quietly picking a winner is
  written so that picking a winner fails it.
"""

from __future__ import annotations

from datetime import date

from mrip.auth.scope import Scope
from mrip.schemas import DocumentClass, ExtractionMethod, FactStatus, ReviewState

#: These suites exercise storage and routing, not access control; the
#: scope-enforcement assertions live in tests/test_scope.py.
SCOPE = Scope.unrestricted("test suite")
# ----------------------------------------------------------------------- documents


def test_document_round_trips(store, make_document):
    document = make_document()
    store.register_document(document)

    loaded = store.get_document(document.document_id, SCOPE)
    assert loaded is not None
    assert loaded.filename == document.filename
    assert loaded.doc_class is DocumentClass.TEXT_PDF
    assert loaded.content_hash == document.content_hash
    assert loaded.fiscal_year == "FY2024-25"


def test_reuploading_identical_bytes_is_a_no_op(store, make_document):
    """Double-registering the same file would double-count every fact in it."""
    first = store.register_document(make_document())
    second = store.register_document(make_document(document_id="doc_different"))

    assert second.document_id == first.document_id
    assert len(store.list_documents(SCOPE)) == 1


def test_lookup_by_hash_is_case_insensitive(store, make_document):
    document = store.register_document(make_document())
    assert store.find_document_by_hash(document.content_hash.upper()) is not None


def test_synthetic_documents_are_flagged_through_storage(store, make_document):
    """Demo data must stay distinguishable from authentic government sources."""
    document = store.register_document(make_document(is_synthetic=True))
    assert store.get_document(document.document_id, SCOPE).is_synthetic is True


def test_superseding_retires_old_facts_without_deleting_them(
    store, make_document, make_fact
):
    old = store.register_document(make_document("report-v1.pdf"))
    new = store.register_document(make_document("report-v2.pdf"))
    store.insert_facts([make_fact(document_id=old.document_id)])

    retired = store.mark_superseded(old.document_id, new.document_id)

    assert retired == 1
    assert store.query_facts(SCOPE, document_id=old.document_id) == []
    still_there = store.query_facts(
        SCOPE, document_id=old.document_id, include_inactive=True
    )
    assert len(still_there) == 1
    assert still_there[0].status is FactStatus.SUPERSEDED
    assert store.get_document(new.document_id, SCOPE).supersedes == old.document_id


def test_one_stage_report_does_not_erase_another(store, make_document):
    """``stage_progress`` accumulates; it is not the last stage's scratch space.

    The extraction stage records what it refused — "37 cells had no unit, here are
    three of them" — and that is the work item for whoever owns the filing. Before
    this, the next stage's own count replaced the whole object seconds later and
    the report was destroyed before anyone could read it.
    """
    from mrip.schemas import DocumentState

    document = store.register_document(make_document())
    store.documents.set_state(
        document.document_id,
        DocumentState.CLASSIFIED,
        progress={"classify": {"done": 1, "total": 1}},
    )
    store.documents.set_state(
        document.document_id,
        DocumentState.DIGITIZED,
        progress={"digitize": {"done": 400, "total": 400}},
    )
    store.documents.set_state(
        document.document_id,
        DocumentState.EXTRACTED,
        progress={"extract": {"done": 112, "total": 149, "skipped": {"no_unit": 37}}},
    )
    # Two more stages run after extraction. Neither may take its report with them.
    store.documents.set_state(
        document.document_id,
        DocumentState.NORMALIZED,
        progress={"normalize": {"done": 4, "total": 4}},
    )
    store.documents.set_state(
        document.document_id,
        DocumentState.VALIDATED,
        progress={"validate": {"done": 149, "total": 149}},
    )

    progress = store.get_document(document.document_id, SCOPE).stage_progress

    assert set(progress) == {
        "classify",
        "digitize",
        "extract",
        "normalize",
        "validate",
    }
    assert progress["extract"]["skipped"] == {"no_unit": 37}
    assert progress["digitize"]["total"] == 400


def test_a_stage_rerun_replaces_its_own_count_and_only_its_own(store, make_document):
    """Merging must not mean a stale count sticks around.

    A re-run of digitize after a fixed OCR path has to overwrite its own earlier
    figure — otherwise the officer reads a number from the run that failed — while
    still leaving every other stage's alone.
    """
    from mrip.schemas import DocumentState

    document = store.register_document(make_document())
    store.documents.set_state(
        document.document_id,
        DocumentState.DIGITIZED,
        progress={
            "digitize": {"done": 142, "total": 400},
            "classify": {"done": 1, "total": 1},
        },
    )
    store.documents.set_state(
        document.document_id,
        DocumentState.DIGITIZED,
        progress={"digitize": {"done": 400, "total": 400}},
    )

    progress = store.get_document(document.document_id, SCOPE).stage_progress

    assert progress["digitize"] == {"done": 400, "total": 400}
    assert progress["classify"] == {"done": 1, "total": 1}


def test_live_progress_and_stage_completion_share_one_record(store, make_document):
    """``set_progress`` (outside the transaction) and ``set_state`` (inside it)
    write to the same object, so the live OCR counter and the stage's final count
    cannot end up in two places that disagree."""
    from mrip.schemas import DocumentState

    document = store.register_document(make_document())
    store.documents.set_state(
        document.document_id,
        DocumentState.CLASSIFIED,
        progress={"classify": {"done": 1, "total": 1}},
    )
    # What the worker publishes mid-OCR, so a reviewer sees 142/400 while it runs.
    store.documents.set_progress(document.document_id, "digitize", 142, 400)

    progress = store.get_document(document.document_id, SCOPE).stage_progress
    assert progress["digitize"]["done"] == 142
    assert progress["digitize"]["total"] == 400
    assert "at" in progress["digitize"], "the live counter stamps when it reported"
    assert progress["classify"] == {"done": 1, "total": 1}


# --------------------------------------------------------------------------- facts


def test_fact_round_trips_with_evidence_intact(store, make_fact):
    """The citation is the product. It must survive storage byte for byte."""
    fact = make_fact()
    store.insert_facts([fact])

    loaded = store.get_fact(fact.fact_id, SCOPE)
    assert loaded is not None
    assert loaded.evidence.document_id == fact.evidence.document_id
    assert loaded.evidence.page == 47
    assert loaded.evidence.table_id == "t1"
    assert loaded.evidence.cell_ref == "r3c2"
    assert loaded.evidence.bbox == fact.evidence.bbox
    assert loaded.evidence.snippet == "SECL 193.00"
    assert loaded.evidence.locator == "doc:doc_fixture p.47 t1 r3c2"


def test_raw_and_canonical_values_are_both_persisted(store, make_fact):
    """Losing the raw value would make the receipt quote our maths, not the source."""
    store.insert_facts([make_fact(raw_value=193.0, raw_unit="MT", value=193.0e6)])
    loaded = store.query_facts(SCOPE)[0]
    assert (loaded.raw_value, loaded.raw_unit) == (193.0, "MT")
    assert (loaded.value, loaded.unit) == (193.0e6, "t")
    assert loaded.unit_ambiguous is True


def test_confidence_stays_decomposed_through_storage(store, make_fact):
    """Three stages in, three stages out — never one collapsed number."""
    store.insert_facts(
        [make_fact(confidence={"ocr": 0.72, "parse": 0.95, "answer": None})]
    )
    confidence = store.query_facts(SCOPE)[0].confidence
    assert (confidence.ocr, confidence.parse, confidence.answer) == (0.72, 0.95, None)
    assert confidence.limiting == 0.72
    assert confidence.limiting_stage == "ocr"


def test_fact_without_bbox_round_trips_as_none(store, make_fact):
    """A spreadsheet cell has a cell ref but no geometry."""
    evidence = make_fact().evidence.model_copy(
        update={"bbox": None, "page": None, "cell_ref": "C14"}
    )
    store.insert_facts(
        [
            make_fact(
                evidence=evidence, extraction_method=ExtractionMethod.SPREADSHEET_CELL
            )
        ]
    )
    loaded = store.query_facts(SCOPE)[0]
    assert loaded.evidence.bbox is None
    assert loaded.evidence.cell_ref == "C14"
    assert loaded.extraction_method is ExtractionMethod.SPREADSHEET_CELL


def test_query_filters_compose(store, make_fact):
    store.insert_facts(
        [
            make_fact(entity_id="secl", metric="coal_production"),
            make_fact(entity_id="mcl", metric="coal_production"),
            make_fact(entity_id="secl", metric="overburden_removal", unit="m3"),
        ]
    )
    assert len(store.query_facts(SCOPE, entity_id="secl")) == 2
    assert len(store.query_facts(SCOPE, metric="coal_production")) == 2
    assert len(store.query_facts(SCOPE, entity_id="secl", metric="coal_production")) == 1


def test_rejected_facts_are_excluded_by_default(store, make_fact):
    fact = make_fact()
    store.insert_facts([fact])
    store.set_fact_status([fact.fact_id], FactStatus.REJECTED)

    assert store.query_facts(SCOPE) == []
    assert len(store.query_facts(SCOPE, include_inactive=True)) == 1


def test_low_confidence_facts_are_routed_to_review(store, make_fact):
    store.insert_facts(
        [
            make_fact(confidence={"parse": 0.99}),
            make_fact(confidence={"ocr": 0.61, "parse": 0.99}),
        ]
    )
    flagged = store.flag_low_confidence(threshold=0.80)

    assert flagged == 1
    review = store.query_facts(SCOPE)
    statuses = {f.status for f in review}
    assert statuses == {FactStatus.EXTRACTED, FactStatus.NEEDS_REVIEW}


def test_review_threshold_uses_the_weakest_stage_not_an_average(store, make_fact):
    """OCR 0.55 with parse 0.99 averages to 0.77 but is not 77% trustworthy."""
    store.insert_facts([make_fact(confidence={"ocr": 0.55, "parse": 0.99})])
    assert store.flag_low_confidence(threshold=0.60) == 1


# ------------------------------------------------------------------ conflict radar


def test_disagreeing_facts_are_surfaced_not_merged(store, make_fact):
    """The behaviour the whole project is built to guarantee."""
    annual_report = make_fact(value=193.0e6, raw_value=193.0)
    provisional = make_fact(value=191.5e6, raw_value=191.5)
    store.insert_facts([annual_report, provisional])

    conflicts = store.detect_conflicts(SCOPE)

    assert len(conflicts) == 1
    group = conflicts[0]
    assert {f.fact_id for f in group.facts} == {
        annual_report.fact_id,
        provisional.fact_id,
    }
    assert group.spread == 1.5e6
    assert group.is_resolved is False
    # Both values remain readable. Neither has been overwritten or averaged.
    assert sorted(f.value for f in group.facts) == [191.5e6, 193.0e6]


def test_rounding_differences_are_not_escalated(store, make_fact):
    """193.00 vs 193.02 MT is a rounding artefact, not a reporting conflict."""
    store.insert_facts([make_fact(value=193.00e6), make_fact(value=193.02e6)])
    assert store.detect_conflicts(SCOPE, material_spread=0.005) == []


def test_conflicting_facts_are_marked_conflicted(store, make_fact):
    store.insert_facts([make_fact(value=193.0e6), make_fact(value=150.0e6)])
    store.detect_conflicts(SCOPE)

    assert all(f.status is FactStatus.CONFLICTED for f in store.query_facts(SCOPE))
    assert store.summary(SCOPE)["open_conflicts"] == 1


def test_agreement_across_sources_is_not_a_conflict(store, make_fact):
    """Two sources stating the same figure is corroboration, not disagreement."""
    store.insert_facts([make_fact(value=193.0e6), make_fact(value=193.0e6)])
    assert store.detect_conflicts(SCOPE) == []


def test_different_periods_are_never_conflated(store, make_fact):
    """FY2023-24 and FY2024-25 production differ because the years differ."""
    store.insert_facts(
        [
            make_fact(
                value=167.0e6,
                period_label="FY2023-24",
                fiscal_year="FY2023-24",
                period_start=date(2023, 4, 1),
                period_end=date(2024, 3, 31),
            ),
            make_fact(value=193.0e6),
        ]
    )
    assert store.detect_conflicts(SCOPE) == []


def test_different_units_are_never_conflated(store, make_fact):
    """Tonnes and cubic metres are not two claims about one number."""
    store.insert_facts(
        [
            make_fact(value=193.0e6, unit="t", dimension="mass"),
            make_fact(
                value=850.0e6, unit="m3", dimension="volume", metric="coal_production"
            ),
        ]
    )
    assert store.detect_conflicts(SCOPE) == []


def test_resolution_records_a_winner_and_rejects_the_rest(store, make_fact):
    audited = make_fact(value=193.0e6)
    provisional = make_fact(value=191.5e6)
    store.insert_facts([audited, provisional])
    conflict = store.detect_conflicts(SCOPE)[0]

    resolved = store.resolve_conflict(
        conflict.conflict_id,
        audited.fact_id,
        SCOPE,
        note="Annual report supersedes provisional",
    )

    assert resolved is True
    winner = store.get_fact(audited.fact_id, SCOPE)
    loser = store.get_fact(provisional.fact_id, SCOPE)
    assert winner.status is FactStatus.VALIDATED
    assert winner.review_state is ReviewState.APPROVED
    assert loser.status is FactStatus.REJECTED
    assert store.open_conflicts(SCOPE) == []


def test_resolution_refuses_a_fact_outside_the_conflict(store, make_fact):
    """A reviewer cannot resolve a conflict in favour of an unrelated figure."""
    store.insert_facts([make_fact(value=193.0e6), make_fact(value=150.0e6)])
    conflict = store.detect_conflicts(SCOPE)[0]
    assert (
        store.resolve_conflict(conflict.conflict_id, "fact_not_in_group", SCOPE) is False
    )


def test_open_conflicts_rehydrate_their_facts(store, make_fact):
    store.insert_facts([make_fact(value=193.0e6), make_fact(value=150.0e6)])
    store.detect_conflicts(SCOPE)

    reopened = store.open_conflicts(SCOPE)
    assert len(reopened) == 1
    assert len(reopened[0].facts) == 2
    assert reopened[0].relative_spread > 0.2
    assert all(f.evidence.snippet == "SECL 193.00" for f in reopened[0].facts)


# ------------------------------------------------------------------------- summary


def test_summary_counts_what_the_dashboard_shows(store, make_document, make_fact):
    document = store.register_document(make_document())
    store.insert_facts(
        [
            make_fact(
                document_id=document.document_id,
                entity_id="secl",
                metric="coal_production",
            ),
            make_fact(
                document_id=document.document_id,
                entity_id="mcl",
                metric="coal_production",
            ),
            make_fact(
                document_id=document.document_id,
                entity_id="mcl",
                metric="overburden_removal",
            ),
        ]
    )
    summary = store.summary(SCOPE)

    assert summary["documents"] == 1
    assert summary["pages"] == 180
    assert summary["facts"] == 3
    assert summary["entities"] == 2
    assert summary["metrics"] == 2
    assert summary["open_conflicts"] == 0


def test_series_excludes_calendar_year_facts(store, make_fact):
    """A calendar-year figure must not land on a fiscal-year axis."""
    store.insert_facts(
        [
            make_fact(entity_id="secl", value=193.0e6, status=FactStatus.VALIDATED),
            make_fact(
                entity_id="secl",
                value=180.0e6,
                status=FactStatus.VALIDATED,
                fiscal_year=None,
                period_label="2024",
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
            ),
        ]
    )
    series = store.entity_metric_series("coal_production", SCOPE, unit="t")

    assert len(series) == 1
    assert series[0]["fiscal_year"] == "FY2024-25"
    assert series[0]["value"] == 193.0e6


def test_evidence_spans_are_queryable_by_page(store, make_document):
    store.register_document(make_document(document_id="doc_1"))
    store.insert_evidence(
        [
            {
                "document_id": "doc_1",
                "page": 47,
                "kind": "table_cell",
                "table_id": "t1",
                "cell_ref": "r3c2",
                "text": "193.00",
                "bbox_y0": 310.5,
                "bbox_x0": 72.0,
                "extraction_method": "table_lattice",
            },
            {
                "document_id": "doc_1",
                "page": 47,
                "kind": "text_span",
                "text": "Production (in MT)",
                "bbox_y0": 280.0,
                "bbox_x0": 72.0,
                "extraction_method": "pdf_text_layer",
            },
            {
                "document_id": "doc_1",
                "page": 48,
                "kind": "text_span",
                "text": "Overburden removal",
                "extraction_method": "pdf_text_layer",
            },
        ]
    )
    page = store.page_evidence("doc_1", 47, SCOPE)

    assert len(page) == 2
    assert page[0]["text"] == "Production (in MT)"  # ordered by vertical position
    assert all(row["evidence_id"].startswith("ev_") for row in page)
