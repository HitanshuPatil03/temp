"""What a prose question answers when there is no prose to be had.

A narrative or drafting question degrades in four steps: generated prose, then
cited passages, then the figures alone, then a refusal. The third step did not
exist. With the model off — which is how CI runs half the suite, and how a
reviewer without a GPU runs the whole product — a question like "explain why
SECL production changed" fell through to a lexical passage search, found no
matching *text*, and answered:

    out_of_corpus — "Nothing in your corpus matches this question."  facts=[]

while the corpus held validated SECL production facts for both fiscal years.
The officer was told their data was missing. It was not; the model was off.

README and ARCHITECTURE §13.1 both promise the generative paths "say so rather
than degrading silently", so this was a documented guarantee the code did not
keep — and the one person most likely to meet it is an evaluator on a laptop.
"""

from __future__ import annotations

import pytest

from mrip.auth.scope import Scope
from mrip.db import Store
from mrip.llm import LLMClient
from mrip.query.service import answer
from mrip.schemas import FactStatus, RefusalReason

SCOPE = Scope.unrestricted("degradation test")

#: Matches the NARRATIVE pattern in query/router.py, and names an entity and a
#: metric the slot parser can resolve — without both, no facts are looked up and
#: the question really is unanswerable.
WHY = "Explain why secl coal production changed in FY2024-25"


def _client(*, enabled: bool) -> LLMClient:
    """A model client that cannot produce prose.

    Two real conditions, not a stubbed method: ``enabled=False`` is
    ``MRIP_LLM_ENABLED=false``, and ``enabled=True`` against a closed port is a
    runtime that is off, unreachable, or slower than the generation ceiling —
    all of which surface as ``LLMUnavailableError`` from the same call.
    """
    return LLMClient(
        base_url="http://127.0.0.1:1",
        model="test",
        timeout=0.05,
        thinking=False,
        enabled=enabled,
    )


@pytest.fixture
def secl_production(store: Store, make_fact):
    """Validated SECL production figures — the data the old refusal denied."""
    store.insert_facts(
        [
            make_fact(value=167_000_000.0, status=FactStatus.VALIDATED),
            make_fact(value=193_000_000.0, status=FactStatus.VALIDATED),
        ]
    )


@pytest.mark.parametrize("enabled", [False, True], ids=["disabled", "unreachable"])
def test_a_model_outage_is_not_reported_as_an_empty_corpus(
    store: Store, secl_production, enabled: bool
) -> None:
    """The defect, from both routes that cause it."""
    response = answer(store, SCOPE, WHY, llm=_client(enabled=enabled))

    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.MODEL_UNAVAILABLE, (
        "a model outage was reported as the corpus having nothing, which sends an "
        "officer looking for a document they already uploaded"
    )
    assert response.model_used is False


def test_the_figures_travel_with_the_refusal(store: Store, secl_production) -> None:
    """A refusal that withholds the figures is barely better than the wrong one.

    The numbers come from the deterministic path and never needed the model, so
    there is no reason to hide them behind its outage.
    """
    response = answer(store, SCOPE, WHY, llm=_client(enabled=False))

    assert response.refusal is not None
    facts = response.refusal.facts
    assert facts, "the figures were found and then dropped"
    assert {fact.value for fact in facts} == {167_000_000.0, 193_000_000.0}
    # Still citable: the point of showing them is that they can be checked.
    assert all(fact.evidence.document_id for fact in facts)


def test_the_message_says_the_model_is_the_problem(store: Store, secl_production) -> None:
    """Whoever reads this has to know whether to upload something or wait."""
    response = answer(store, SCOPE, WHY, llm=_client(enabled=False))

    assert response.refusal is not None
    message = response.refusal.message.lower()
    assert "model" in message
    assert "corpus" not in message or "your corpus" not in message


def test_an_empty_corpus_is_still_reported_as_an_empty_corpus(store: Store) -> None:
    """The fix must not turn every refusal into a model complaint.

    With no facts and no passages, "nothing matches" is the true answer and has
    to survive — otherwise this trades one misleading message for another.
    """
    response = answer(store, SCOPE, WHY, llm=_client(enabled=False))

    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.OUT_OF_CORPUS


def test_cited_passages_still_outrank_bare_figures(
    store: Store, make_document, make_fact
) -> None:
    """Degradation order: passages before figures.

    A passage quotes the document in its own words, which is closer to the prose
    that was asked for than a bare number is. This asserts the rung above is
    tried first rather than skipped now that a lower one exists.

    The question is narrower than :data:`WHY` on purpose. ``websearch_to_tsquery``
    ands every term, so a passage only surfaces when it contains all of them —
    which is the search behaving correctly, and means a test about *ordering*
    has to use a question its passage actually answers rather than contorting
    the passage to echo the question.
    """
    question = "Explain why SECL coal production rose"
    document = make_document("why-production-rose.pdf")
    store.register_document(document)
    store.insert_facts([make_fact(status=FactStatus.VALIDATED)])
    store.insert_evidence(
        [
            {
                "document_id": document.document_id,
                "page": 1,
                "kind": "paragraph",
                "text": (
                    "Explain: SECL coal production rose because two new mines "
                    "reached rated capacity during the year."
                ),
                "extraction_method": "pdf_text_layer",
            }
        ]
    )

    response = answer(store, SCOPE, question, llm=_client(enabled=False))

    assert response.refusal is None, "a cited passage was available and was skipped"
    assert response.discovery is not None
    assert response.discovery.passages


def test_the_streaming_endpoint_degrades_the_same_way(
    store: Store, secl_production, make_user, bearer, monkeypatch
) -> None:
    """``/query/stream`` is a second entry point into the same question.

    It delegates to ``answer`` when the model is unavailable, which is why it
    inherited this fix for free — and exactly why the delegation is worth
    pinning. A change that gave the streaming path its own fallback could
    reintroduce "nothing in your corpus matches" on one endpoint while the other
    stayed honest, and nothing else here would notice.

    The setting is patched rather than a client injected, because this route
    builds its own client from settings; that is the thing under test.
    """
    from fastapi.testclient import TestClient

    from mrip.api.deps import provide_read_only_store, provide_store
    from mrip.auth.scope import SCOPE_ALL
    from mrip.config import get_settings
    from mrip.main import create_app
    from mrip.schemas import Role

    monkeypatch.setenv("MRIP_LLM_ENABLED", "false")
    get_settings.cache_clear()
    # The route reads `reader.settings`, and the store captured its settings when
    # the fixture built it — before this patch. Swapping them is what actually
    # puts the route on the disabled path, which is the whole point of the test.
    monkeypatch.setattr(store, "_settings", get_settings())
    try:
        app = create_app()
        app.dependency_overrides[provide_store] = lambda: store
        app.dependency_overrides[provide_read_only_store] = lambda: store
        officer = make_user("stream.officer", role=Role.OFFICER, entities=(SCOPE_ALL,))
        with TestClient(app, headers=bearer(officer)) as client:
            body = client.post("/api/query/stream", json={"question": WHY}).json()
    finally:
        get_settings.cache_clear()

    assert body["refusal"]["reason"] == RefusalReason.MODEL_UNAVAILABLE.value
    assert body["refusal"]["facts"], "the streaming path dropped the figures"
