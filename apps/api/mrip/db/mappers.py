"""Row ↔ domain-model conversion.

Kept in one place so the flattening is defined once. The wire and API shapes are
nested (a :class:`Fact` owns an :class:`EvidenceRef` which owns a :class:`BBox`)
while the table is flat, and every repository needs the same translation.

Note what is *not* here: no mapper invents a value. A null bounding box stays
null rather than becoming a zero-area box at the origin, and a missing confidence
stage stays ``None`` rather than becoming ``0.0`` — a stage that did not run is
not a stage that scored zero.
"""

from __future__ import annotations

from typing import Any

from mrip.schemas import BBox, Confidence, Document, EvidenceRef, Fact

__all__ = ["document_to_row", "fact_to_row", "row_to_document", "row_to_fact"]


def _bbox(row: dict[str, Any]) -> BBox | None:
    x0, y0, x1, y1 = (
        row.get("bbox_x0"),
        row.get("bbox_y0"),
        row.get("bbox_x1"),
        row.get("bbox_y1"),
    )
    if None in (x0, y0, x1, y1):
        return None
    return BBox(x0=x0, y0=y0, x1=x1, y1=y1)


# ------------------------------------------------------------------- documents


def row_to_document(row: dict[str, Any]) -> Document:
    return Document(
        document_id=row["document_id"],
        content_hash=row["content_hash"],
        filename=row["filename"],
        doc_class=row["doc_class"],
        version=row["version"],
        supersedes=row["supersedes"],
        page_count=row["page_count"],
        size_bytes=row["size_bytes"],
        title=row["title"],
        publisher_entity_id=row["publisher_entity_id"],
        fiscal_year=row["fiscal_year"],
        ingested_at=row["ingested_at"],
        is_synthetic=row["is_synthetic"],
        notes=row["notes"],
        source_url=row.get("source_url"),
        state=row["state"],
        sensitivity=row["sensitivity"],
        blob_key=row["blob_key"],
        uploaded_by=row["uploaded_by"],
        failed_stage=row["failed_stage"],
        failed_reason=row["failed_reason"],
        stage_progress=row["stage_progress"] or {},
    )


def document_to_row(document: Document) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "content_hash": document.content_hash,
        "filename": document.filename,
        "doc_class": document.doc_class.value,
        "version": document.version,
        "supersedes": document.supersedes,
        "page_count": document.page_count,
        "size_bytes": document.size_bytes,
        "title": document.title,
        "publisher_entity_id": document.publisher_entity_id,
        "fiscal_year": document.fiscal_year,
        "ingested_at": document.ingested_at,
        "is_synthetic": document.is_synthetic,
        "notes": document.notes,
        "source_url": document.source_url,
        "state": document.state.value,
        "sensitivity": document.sensitivity.value,
        "blob_key": document.blob_key,
        "uploaded_by": document.uploaded_by,
        # The publisher is the natural access owner until ingestion attributes the
        # document more precisely.
        "owner_entity_id": document.publisher_entity_id,
    }


# ----------------------------------------------------------------------- facts


def row_to_fact(row: dict[str, Any]) -> Fact:
    return Fact(
        fact_id=row["fact_id"],
        entity_id=row["entity_id"],
        mine_or_block=row["mine_or_block"],
        metric=row["metric"],
        value=row["value"],
        unit=row["unit"],
        dimension=row["dimension"],
        raw_value=row["raw_value"],
        raw_unit=row["raw_unit"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        period_label=row["period_label"],
        fiscal_year=row["fiscal_year"],
        evidence=EvidenceRef(
            document_id=row["document_id"],
            document_version=row["document_version"],
            page=row["page"],
            table_id=row["table_id"],
            cell_ref=row["cell_ref"],
            bbox=_bbox(row),
            snippet=row["snippet"],
        ),
        extraction_method=row["extraction_method"],
        confidence=Confidence(
            ocr=row["conf_ocr"],
            parse=row["conf_parse"],
            answer=row["conf_answer"],
        ),
        status=row["status"],
        review_state=row["review_state"],
        unit_ambiguous=row["unit_ambiguous"],
        notes=row["notes"],
        extracted_at=row["extracted_at"],
    )


def fact_to_row(fact: Fact) -> dict[str, Any]:
    evidence = fact.evidence
    bbox = evidence.bbox
    return {
        "fact_id": fact.fact_id,
        "entity_id": fact.entity_id,
        "mine_or_block": fact.mine_or_block,
        "metric": fact.metric,
        "value": fact.value,
        "unit": fact.unit,
        "dimension": fact.dimension,
        "raw_value": fact.raw_value,
        "raw_unit": fact.raw_unit,
        "period_start": fact.period_start,
        "period_end": fact.period_end,
        "period_label": fact.period_label,
        "fiscal_year": fact.fiscal_year,
        "document_id": evidence.document_id,
        "document_version": evidence.document_version,
        "page": evidence.page,
        "table_id": evidence.table_id,
        "cell_ref": evidence.cell_ref,
        "bbox_x0": bbox.x0 if bbox else None,
        "bbox_y0": bbox.y0 if bbox else None,
        "bbox_x1": bbox.x1 if bbox else None,
        "bbox_y1": bbox.y1 if bbox else None,
        "snippet": evidence.snippet,
        "extraction_method": fact.extraction_method.value,
        "conf_ocr": fact.confidence.ocr,
        "conf_parse": fact.confidence.parse,
        "conf_answer": fact.confidence.answer,
        "status": fact.status.value,
        "review_state": fact.review_state.value,
        "unit_ambiguous": fact.unit_ambiguous,
        "notes": fact.notes,
        "extracted_at": fact.extracted_at,
    }
