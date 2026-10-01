"""Tests for word cloud and topic identification (ARCHITECTURE §12).

The thesis under test: extraction is deterministic and reproducible, the cloud
is sized by *document* frequency so one repetitive annexure cannot dominate it,
every term reaches a document, and the whole thing is built from the caller's
scope rather than filtered after the fact.
"""

from __future__ import annotations

from mrip.auth.scope import Scope
from mrip.db import Store
from mrip.schemas import ExtractionMethod
from mrip.topics.extract import (
    DOMAIN_STOPWORDS,
    extract_keyphrases,
    tokenize,
)
from mrip.topics.service import extract_for_document, recompute_corpus

SCOPE = Scope.unrestricted("test suite")


def _add_evidence(store: Store, document_id: str, version: int, *texts: str) -> None:
    store.insert_evidence(
        [
            {
                "evidence_id": f"ev_{document_id}_{index}",
                "document_id": document_id,
                "document_version": version,
                "page": 1,
                "kind": "line",
                "text": text,
                "extraction_method": ExtractionMethod.PDF_TEXT_LAYER.value,
            }
            for index, text in enumerate(texts)
        ]
    )


# --------------------------------------------------------------- extraction


def test_tokenize_drops_stopwords_and_short_tokens() -> None:
    tokens = tokenize("The overburden removal at Gevra was 12 Mm3 and the mine ran")

    assert "overburden" in tokens
    assert "gevra" in tokens
    # English function words, domain words, and anything under four letters.
    assert "the" not in tokens
    assert "was" not in tokens
    assert "mine" not in tokens


def test_the_domain_stoplist_removes_what_the_corpus_is_about() -> None:
    """§12.1 — in a corpus entirely about coal production, 'coal' and
    'production' carry no information about any single document in it."""
    for word in ("coal", "production", "tonnes", "limited"):
        assert word in DOMAIN_STOPWORDS
        assert tokenize(f"the {word} figure") == []


def test_extraction_is_deterministic() -> None:
    """The reproducibility property: same corpus, same cloud, every time —
    otherwise a reviewer cannot tell a data change from a code change."""
    text = "Gevra expansion gevra expansion dragline dragline dragline Korba"

    first = extract_keyphrases([text])
    second = extract_keyphrases([text])

    assert [(p.term, p.occurrences) for p in first] == [
        (p.term, p.occurrences) for p in second
    ]
    assert [p.score for p in first] == [p.score for p in second]


def test_a_term_seen_once_is_not_a_keyphrase() -> None:
    """One mangled glyph sequence from an OCR page must not reach the cloud."""
    phrases = extract_keyphrases(["dragline dragline dragline rnisread"])
    assert "rnisread" not in {phrase.term for phrase in phrases}


def test_a_rare_term_outranks_a_ubiquitous_one() -> None:
    """IDF doing its job: a term in every document is worth less than a term in
    one, at equal frequency within the document."""
    phrases = extract_keyphrases(
        ["dragline dragline gevra gevra"],
        document_frequencies={"dragline": 90, "gevra": 2},
        corpus_size=100,
    )
    ranked = [phrase.term for phrase in phrases]
    assert ranked.index("gevra") < ranked.index("dragline")


# ------------------------------------------------------------------ storage


def test_extraction_is_idempotent(store: Store, make_document) -> None:
    """Re-running replaces this version's rows rather than adding a second set —
    the same contract every ingestion stage keeps."""
    document = make_document("gevra.pdf", publisher_entity_id="secl")
    store.register_document(document)
    _add_evidence(
        store, document.document_id, 1, "dragline dragline Gevra expansion Gevra"
    )

    first = extract_for_document(store, document.document_id, 1)
    second = extract_for_document(store, document.document_id, 1)

    assert first == second
    assert first > 0
    terms = store.keyphrases.cloud(SCOPE)
    assert len(terms) == len({term.term for term in terms})


def test_a_document_with_no_text_clears_its_keyphrases(
    store: Store, make_document
) -> None:
    """A scanned document whose OCR produced nothing must not leave a previous
    extraction behind to be read as current."""
    document = make_document("scan.pdf", publisher_entity_id="secl")
    store.register_document(document)
    _add_evidence(store, document.document_id, 1, "dragline dragline Gevra Gevra")
    assert extract_for_document(store, document.document_id, 1) > 0

    store.connection.exec_driver_sql(
        "UPDATE evidence SET text = NULL WHERE document_id = %s", (document.document_id,)
    )
    assert extract_for_document(store, document.document_id, 1) == 0
    assert store.keyphrases.cloud(SCOPE) == []


# -------------------------------------------------------------------- cloud


def test_the_cloud_is_sized_by_document_frequency(store: Store, make_document) -> None:
    """§12.2 — one repetitive annexure must not dominate the corpus with its own
    boilerplate. 'dragline' is hammered in one document; 'korba' is mentioned
    twice in each of three. Korba is the bigger word."""
    for index in range(3):
        document = make_document(f"statement-{index}.pdf", publisher_entity_id="secl")
        store.register_document(document)
        _add_evidence(store, document.document_id, 1, "Korba Korba Raigarh Raigarh")
        extract_for_document(store, document.document_id, 1)

    annexure = make_document("annexure.pdf", publisher_entity_id="secl")
    store.register_document(annexure)
    _add_evidence(store, annexure.document_id, 1, " ".join(["dragline"] * 50))
    extract_for_document(store, annexure.document_id, 1)

    cloud = {term.term: term for term in store.keyphrases.cloud(SCOPE)}

    assert cloud["korba"].document_count == 3
    assert cloud["dragline"].document_count == 1
    assert cloud["korba"].document_count > cloud["dragline"].document_count
    # The raw count still travels, because the two numbers answer different
    # questions and collapsing them loses one.
    assert cloud["dragline"].occurrences == 50


def test_the_cloud_is_built_from_the_callers_scope(store: Store, make_document) -> None:
    """§12.3 — a term appearing only in MCL's filings must not tell an SECL
    officer that those filings exist."""
    secl = make_document("secl.pdf", publisher_entity_id="secl")
    mcl = make_document("mcl.pdf", publisher_entity_id="mcl")
    store.register_document(secl)
    store.register_document(mcl)
    _add_evidence(store, secl.document_id, 1, "Korba Korba Gevra Gevra")
    _add_evidence(store, mcl.document_id, 1, "Talcher Talcher Jagannath Jagannath")
    extract_for_document(store, secl.document_id, 1)
    extract_for_document(store, mcl.document_id, 1)

    secl_terms = {term.term for term in store.keyphrases.cloud(Scope.of("secl"))}

    assert "korba" in secl_terms
    assert "talcher" not in secl_terms
    assert "talcher" in {term.term for term in store.keyphrases.cloud(SCOPE)}


def test_every_term_reaches_a_document(store: Store, make_document) -> None:
    """The §12 gate: no term is a dead end. A cloud you cannot interrogate is
    decoration; this one is an index."""
    document = make_document("gevra.pdf", publisher_entity_id="secl")
    store.register_document(document)
    _add_evidence(store, document.document_id, 1, "Gevra Gevra dragline dragline")
    extract_for_document(store, document.document_id, 1)

    for term in store.keyphrases.cloud(SCOPE):
        reached = store.keyphrases.documents_for_term(term.term, SCOPE)
        assert reached, f"{term.term!r} is a dead end"
        assert reached[0].document_id == document.document_id


def test_drill_through_respects_scope(store: Store, make_document) -> None:
    """Knowing a term exists must not be a way to read who used it."""
    mcl = make_document("mcl.pdf", publisher_entity_id="mcl")
    store.register_document(mcl)
    _add_evidence(store, mcl.document_id, 1, "Talcher Talcher Jagannath Jagannath")
    extract_for_document(store, mcl.document_id, 1)

    assert store.keyphrases.documents_for_term("talcher", Scope.of("secl")) == []
    assert store.keyphrases.documents_for_term("talcher", Scope.of("mcl")) != []


def test_filters_re_weight_the_cloud_rather_than_hiding_rows(
    store: Store, make_document
) -> None:
    """§12.3 — filters are part of the query. Narrowing to a fiscal year changes
    document frequency, which a post-filter on a rendered image could not do."""
    old = make_document("fy23.pdf", publisher_entity_id="secl", fiscal_year="FY2023-24")
    new = make_document("fy24.pdf", publisher_entity_id="secl", fiscal_year="FY2024-25")
    store.register_document(old)
    store.register_document(new)
    _add_evidence(store, old.document_id, 1, "Korba Korba dragline dragline")
    _add_evidence(store, new.document_id, 1, "Korba Korba Raigarh Raigarh")
    extract_for_document(store, old.document_id, 1)
    extract_for_document(store, new.document_id, 1)

    overall = {t.term: t.document_count for t in store.keyphrases.cloud(SCOPE)}
    scoped = {
        t.term: t.document_count
        for t in store.keyphrases.cloud(SCOPE, fiscal_year="FY2024-25")
    }

    assert overall["korba"] == 2
    assert scoped["korba"] == 1
    assert "dragline" not in scoped


def test_prevalence_tracks_a_term_across_fiscal_years(
    store: Store, make_document
) -> None:
    """§12.7 — the shape that answers whether a subject is getting more
    attention or less, which a single cloud cannot show."""
    for year in ("FY2023-24", "FY2024-25"):
        document = make_document(
            f"{year}.pdf", publisher_entity_id="secl", fiscal_year=year
        )
        store.register_document(document)
        _add_evidence(store, document.document_id, 1, "Korba Korba Raigarh Raigarh")
        extract_for_document(store, document.document_id, 1)

    series = store.keyphrases.prevalence("korba", SCOPE)

    assert [point["fiscal_year"] for point in series] == ["FY2023-24", "FY2024-25"]
    assert all(point["document_count"] == 1 for point in series)


def test_recompute_corpus_rebuilds_every_document(store: Store, make_document) -> None:
    """The two-pass rebuild: correct IDF needs the whole corpus before it can
    weight any part of it."""
    for index in range(2):
        document = make_document(f"doc-{index}.pdf", publisher_entity_id="secl")
        store.register_document(document)
        _add_evidence(store, document.document_id, 1, "Korba Korba Gevra Gevra")

    written = recompute_corpus(store)

    assert len(written) == 2
    assert all(count > 0 for count in written.values())
    assert {term.term for term in store.keyphrases.cloud(SCOPE)} >= {"korba", "gevra"}
