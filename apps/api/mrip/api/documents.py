"""Document and evidence endpoints.

The evidence route is what makes the click-through work: the frontend renders a
page image and overlays the boxes returned here, so a reviewer sees the figure
highlighted in its original table.

Every read is scoped. Note that an out-of-scope document returns **404, not 403**:
telling an SECL account that a specific MCL document exists but is forbidden is
itself a disclosure, and for a need-to-know corpus the right answer is that the
document is not there.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from mrip.api.deps import ScopeDep, StoreDep
from mrip.schemas import Document, DocumentState

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("", response_model=list[Document])
def list_documents(
    store: StoreDep,
    scope: ScopeDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    state: DocumentState | None = Query(
        default=None, description="Filter by ingestion lifecycle state."
    ),
) -> list[Document]:
    """Registered source documents, newest first."""
    return store.list_documents(scope, limit=limit, offset=offset, state=state)


@router.get("/{document_id}", response_model=Document)
def get_document(document_id: str, store: StoreDep, scope: ScopeDep) -> Document:
    """One document's registration record."""
    document = store.get_document(document_id, scope)
    if document is None:
        raise HTTPException(status_code=404, detail=f"No document {document_id!r}")
    return document


@router.get("/{document_id}/pages/{page}/evidence")
def page_evidence(
    document_id: str, page: int, store: StoreDep, scope: ScopeDep
) -> list[dict[str, Any]]:
    """Every evidence span on one page, ordered top-to-bottom, left-to-right."""
    if store.get_document(document_id, scope) is None:
        raise HTTPException(status_code=404, detail=f"No document {document_id!r}")
    return store.page_evidence(document_id, page, scope)
