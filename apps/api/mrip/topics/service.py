"""Topic extraction over the evidence store (ARCHITECTURE §12).

This module is the seam between :mod:`mrip.topics.extract`, which is pure and
knows nothing about storage, and the database. It reads a document version's
evidence text, ranks its terms, and writes the result to
``document_keyphrases`` so the cloud is a table read rather than a corpus scan
on every page load (§12.1).

Corpus statistics are read from the keyphrase table itself rather than
recomputed from evidence. That is a deliberate approximation: a term's document
frequency reflects the documents extracted *so far*, so the very first documents
in a fresh corpus get weaker IDF than they eventually deserve. The alternative —
re-tokenizing the whole corpus on every ingest — turns a per-document job into an
O(corpus) one and makes ingestion quadratic. :func:`recompute_corpus` exists for
when the drift matters and a reviewer wants the cloud rebuilt exactly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from mrip.db.tables import document_keyphrases, documents, evidence
from mrip.topics.extract import extract_keyphrases, tokenize

if TYPE_CHECKING:
    from mrip.db.store import Store

__all__ = ["extract_for_document", "recompute_corpus"]

#: How much text to rank per document version. A 400-page annual report has more
#: than enough signal in its first few thousand spans, and reading all of them
#: into memory to produce 60 terms is work nobody asked for.
_MAX_SPANS = 4_000


def _corpus_statistics(store: Store) -> tuple[dict[str, int], int]:
    """Document frequency per term, and how many documents are extracted.

    Unscoped on purpose. IDF is a property of the corpus, not of the reader: if
    two officers with different scopes saw terms weighted differently, the same
    document would produce two different clouds and neither would be the
    corpus's. Scope is applied when the cloud is *read*, which is where it
    belongs — and no term, count or document ever crosses a scope boundary here,
    because this function returns only aggregate weights.
    """
    frequencies = {
        str(term): int(count)
        for term, count in store.connection.execute(
            sa.select(
                document_keyphrases.c.term,
                sa.func.count(sa.distinct(document_keyphrases.c.document_id)),
            ).group_by(document_keyphrases.c.term)
        ).all()
    }
    corpus_size = (
        store.connection.execute(
            sa.select(sa.func.count(sa.distinct(document_keyphrases.c.document_id)))
        ).scalar()
        or 0
    )
    return frequencies, corpus_size


def extract_for_document(
    store: Store, document_id: str, document_version: int = 1, *, limit: int = 60
) -> int:
    """Rank and store one document version's keyphrases. Returns the row count.

    Idempotent: re-running replaces this version's rows rather than adding a
    second set, the same contract every ingestion stage keeps.
    """
    texts = (
        store.connection.execute(
            sa.select(evidence.c.text)
            .where(
                evidence.c.document_id == document_id,
                evidence.c.document_version == document_version,
                evidence.c.text.is_not(None),
            )
            .limit(_MAX_SPANS)
        )
        .scalars()
        .all()
    )
    if not texts:
        # No text layer, or a scanned document whose OCR produced nothing. Clear
        # any stale rows and say so with a zero rather than leaving the previous
        # extraction behind to be read as current.
        store.keyphrases.replace_for_document(document_id, document_version, [])
        return 0

    frequencies, corpus_size = _corpus_statistics(store)
    phrases = extract_keyphrases(
        [text for text in texts if text],
        document_frequencies=frequencies,
        # +1 so this document counts itself: without it the first document in a
        # corpus divides by zero documents and every term looks maximally rare.
        corpus_size=corpus_size + 1,
        limit=limit,
    )
    rows = [phrase.as_row(document_id, document_version) for phrase in phrases]
    return store.keyphrases.replace_for_document(document_id, document_version, rows)


def recompute_corpus(store: Store, *, limit: int = 60) -> dict[str, int]:
    """Re-extract every ready document, with IDF over the true corpus.

    Two passes, because correct IDF needs to know the whole corpus before it can
    weight any part of it: tokenize everything, count document frequencies, then
    rank each document against those counts. The per-document job approximates
    this; running it here produces the cloud the corpus actually implies.

    Returns ``{document_id: rows_written}``.
    """
    versions = store.connection.execute(
        sa.select(documents.c.document_id, documents.c.version).order_by(
            documents.c.document_id
        )
    ).all()

    tokenized: dict[tuple[str, int], list[str]] = {}
    for document_id, version in versions:
        texts = (
            store.connection.execute(
                sa.select(evidence.c.text)
                .where(
                    evidence.c.document_id == document_id,
                    evidence.c.document_version == version,
                    evidence.c.text.is_not(None),
                )
                .limit(_MAX_SPANS)
            )
            .scalars()
            .all()
        )
        tokens: list[str] = []
        for text in texts:
            if text:
                tokens.extend(tokenize(text))
        tokenized[(str(document_id), int(version))] = tokens

    frequencies: dict[str, int] = {}
    for tokens in tokenized.values():
        for term in set(tokens):
            frequencies[term] = frequencies.get(term, 0) + 1
    corpus_size = len(tokenized)

    written: dict[str, int] = {}
    for (document_id, version), tokens in tokenized.items():
        if not tokens:
            store.keyphrases.replace_for_document(document_id, version, [])
            written[document_id] = 0
            continue
        # The tokens are already stoplisted, so they are passed as one blob of
        # space-joined text: `extract_keyphrases` re-tokenizes, which is cheap
        # and keeps a single definition of what a term is.
        phrases = extract_keyphrases(
            [" ".join(tokens)],
            document_frequencies=frequencies,
            corpus_size=corpus_size,
            limit=limit,
        )
        rows = [phrase.as_row(document_id, version) for phrase in phrases]
        written[document_id] = store.keyphrases.replace_for_document(
            document_id, version, rows
        )
    return written
