"""Tests for the AI query and response system (ARCHITECTURE §13).

Two things are asserted here above all: a numeric answer is a stored, validated
fact carrying its own evidence — never a value the router computed — and every
way of not answering is a *structured refusal* with a reason the client can act
on, not a bare error. The figure paths run with ``model_used=False``, which is
§7's "no model on the figure path" made checkable at the response boundary.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mrip.api.deps import provide_read_only_store, provide_store
from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.db import Store
from mrip.llm import LLMClient, LLMUnavailableError
from mrip.main import create_app
from mrip.query.router import classify
from mrip.query.service import answer
from mrip.schemas import FactStatus, QueryIntent, RefusalReason, Role

SCOPE = Scope.unrestricted("test suite")


class _FakeLLM:
    """A stand-in for the model runtime, so the prose path is testable offline."""

    def __init__(self, text: str, *, available: bool = True) -> None:
        self._text = text
        self._available = available
        self.prompts: list[tuple[str, str | None]] = []

    @property
    def available(self) -> bool:
        return self._available

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append((prompt, system))
        return self._text


# --------------------------------------------------------------------- routing


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("SECL coal production FY2024-25", QueryIntent.EXACT_FIGURE),
        ("compare coal production across subsidiaries", QueryIntent.COMPARISON),
        ("coal production", QueryIntent.COMPARISON),
        ("why did SECL offtake fall in Q2", QueryIntent.NARRATIVE),
        ("draft a reply to PQ 1247", QueryIntent.DRAFT),
        ("documents mentioning Gevra", QueryIntent.DISCOVERY),
    ],
)
def test_router_classifies_on_shape(question: str, intent: QueryIntent) -> None:
    assert classify(question).intent is intent


def test_router_extracts_entity_metric_and_period() -> None:
    slots = classify("SECL coal production FY2024-25").slots
    assert slots.entity_id == "secl"
    assert slots.metric_key == "coal_production"
    assert slots.period is not None
    assert slots.period.label == "FY2024-25"


# ---------------------------------------------------------------- exact figure


def test_exact_figure_returns_the_validated_fact(store: Store, make_fact) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    response = answer(store, SCOPE, "SECL coal production FY2024-25")
    assert response.model_used is False
    assert response.figure is not None
    assert response.figure.fact.value == 193.0e6
    assert response.figure.fact.evidence.document_id  # carries its own evidence


def test_exact_figure_out_of_corpus_when_no_such_fact(store: Store) -> None:
    response = answer(store, SCOPE, "MCL coal production FY2024-25")
    assert response.figure is None
    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.OUT_OF_CORPUS


def test_exact_figure_refuses_unvalidated(store: Store, make_fact) -> None:
    store.insert_facts([make_fact(status=FactStatus.EXTRACTED, unit_ambiguous=False)])
    response = answer(store, SCOPE, "SECL coal production FY2024-25")
    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.NO_VALIDATED_FACT


def test_exact_figure_refuses_ambiguous_unit(store: Store, make_fact) -> None:
    # The default fixture fact is flagged unit_ambiguous.
    store.insert_facts([make_fact(status=FactStatus.VALIDATED)])
    response = answer(store, SCOPE, "SECL coal production FY2024-25")
    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.AMBIGUOUS_UNIT
    assert response.refusal.facts  # the offending facts travel with the refusal


# ------------------------------------------------------------------ comparison


def test_comparison_returns_a_series_across_entities(store: Store, make_fact) -> None:
    store.insert_facts(
        [
            make_fact(
                entity_id="secl",
                value=193.0e6,
                unit_ambiguous=False,
                status=FactStatus.VALIDATED,
            ),
            make_fact(
                entity_id="mcl",
                value=201.0e6,
                unit_ambiguous=False,
                status=FactStatus.VALIDATED,
            ),
        ]
    )
    response = answer(store, SCOPE, "compare coal production across subsidiaries")
    assert response.model_used is False
    assert response.comparison is not None
    entities = {point.entity_id for point in response.comparison.points}
    assert {"secl", "mcl"} <= entities


def test_comparison_out_of_corpus_when_metric_absent(store: Store) -> None:
    response = answer(store, SCOPE, "compare coal production across subsidiaries")
    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.OUT_OF_CORPUS


# ------------------------------------------------------------------- discovery


def test_discovery_refuses_when_nothing_matches(store: Store) -> None:
    response = answer(store, SCOPE, "documents mentioning Gevra")
    assert response.intent is QueryIntent.DISCOVERY
    assert response.refusal is not None
    assert response.refusal.reason is RefusalReason.OUT_OF_CORPUS


# ----------------------------------------------------------------------- route


@pytest.fixture
def client(store: Store, make_user, bearer) -> TestClient:
    """A client whose read-write *and* read-only stores are the test store.

    Both are overridden onto the one transactional fixture: the figure path opens
    a separate read-only connection in production, but a test's uncommitted rows
    live in a single transaction, so the query path has to share it to see them.
    """
    app = create_app()
    app.dependency_overrides[provide_store] = lambda: store
    app.dependency_overrides[provide_read_only_store] = lambda: store
    operator = make_user("query.tests", role=Role.ADMIN, entities=(SCOPE_ALL,))
    with TestClient(app, headers=bearer(operator)) as test_client:
        yield test_client


def test_query_route_answers_and_audits(
    client: TestClient, store: Store, make_fact
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    reply = client.post("/api/query", json={"question": "SECL coal production FY2024-25"})
    assert reply.status_code == 200
    body = reply.json()
    assert body["intent"] == "exact_figure"
    assert body["model_used"] is False
    assert body["figure"]["fact"]["value"] == 193.0e6

    # The question is on the audit trail (§9).
    assert any(entry.action == "query" for entry in store.audit.recent(action="query"))


# ------------------------------------------------------------------- narrative


def test_narrative_grounds_prose_and_drops_invented_numbers(
    store: Store, make_fact
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED)])
    fake = _FakeLLM(
        "SECL produced 193 MT in FY2024-25. Output then leapt to 999 crore tonnes."
    )
    response = answer(store, SCOPE, "why did SECL coal production rise", llm=fake)

    assert response.model_used is True
    assert response.narrative is not None
    # The supported figure survives; the invented one takes its sentence with it.
    assert "193" in response.narrative.prose
    assert "999" not in response.narrative.prose
    assert response.narrative.flagged is True
    assert response.narrative.facts  # the prose is grounded on pinned facts
    assert fake.prompts  # the model was actually consulted


def test_narrative_falls_back_when_model_disabled(store: Store, make_fact) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED)])
    fake = _FakeLLM("ignored", available=False)
    response = answer(store, SCOPE, "why did SECL coal production rise", llm=fake)

    assert response.model_used is False
    assert response.narrative is None  # no ungrounded prose
    assert not fake.prompts  # the model was never consulted


def test_narrative_falls_back_when_model_unreachable(store: Store, make_fact) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED)])

    class _Boom:
        available = True

        def generate(self, prompt: str, *, system: str | None = None) -> str:
            raise LLMUnavailableError("runtime down")

    response = answer(store, SCOPE, "why did SECL coal production rise", llm=_Boom())
    assert response.model_used is False


def test_disabled_client_raises_rather_than_returning_empty() -> None:
    client = LLMClient(
        base_url="http://127.0.0.1:11434",
        model="qwen3:8b",
        timeout=1.0,
        thinking=False,
        enabled=False,
    )
    assert client.available is False
    with pytest.raises(LLMUnavailableError):
        client.generate("hello")


# ------------------------------------------------------- streaming audit trail


class _FakeStreamLLM:
    """A model that streams tokens, for the SSE path."""

    def __init__(self, tokens: list[str], *, fail_after: int | None = None) -> None:
        self._tokens = tokens
        self._fail_after = fail_after
        self.available = True

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        return "".join(self._tokens)

    def generate_stream(self, prompt: str, *, system: str | None = None):
        for index, token in enumerate(self._tokens):
            if self._fail_after is not None and index >= self._fail_after:
                raise LLMUnavailableError("runtime died mid-stream")
            yield token


def _stream(client: TestClient, question: str) -> str:
    with client.stream("POST", "/api/query/stream", json={"question": question}) as reply:
        return "".join(reply.iter_text())


def _query_audits(store: Store) -> list[dict]:
    return [
        entry.detail
        for entry in store.audit.recent(action="query")
        if entry.detail.get("stream")
    ]


def test_a_stream_audits_what_the_caller_was_actually_shown(
    client: TestClient, store: Store, make_fact, monkeypatch
) -> None:
    """The audit row must be written after the stream, not before it.

    Recorded up front, the trail claimed a narrative answer with
    ``model_used=true`` no matter what happened next — including a stream that
    died. This is the record someone reads months later to establish what a
    figure in a submitted answer rested on, so it has to say what was shown.
    """
    store.insert_facts([make_fact(status=FactStatus.VALIDATED)])
    fake = _FakeStreamLLM(["SECL produced ", "193 MT", " in FY2024-25."])
    monkeypatch.setattr("mrip.api.query.client_from_settings", lambda _settings: fake)

    body = _stream(client, "why did SECL coal production rise")

    assert "event: done" in body
    audited = _query_audits(store)
    assert audited, "a streamed query must leave an audit row"
    assert audited[0]["answer_kind"] == "narrative"
    assert audited[0]["model_used"] is True
    # The numeral check's verdict is now on the trail; it was not before.
    assert "narrative_flagged" in audited[0]
    assert audited[0]["stream_error"] is None


def test_a_stream_that_dies_is_audited_as_a_failure_not_an_answer(
    client: TestClient, store: Store, make_fact, monkeypatch
) -> None:
    """A runtime that dies mid-stream must not leave 'narrative' on the trail."""
    store.insert_facts([make_fact(status=FactStatus.VALIDATED)])
    fake = _FakeStreamLLM(["SECL produced ", "193 MT"], fail_after=1)
    monkeypatch.setattr("mrip.api.query.client_from_settings", lambda _settings: fake)

    body = _stream(client, "why did SECL coal production rise")

    assert "event: error" in body
    audited = _query_audits(store)
    assert audited, "a failed stream must still leave an audit row"
    assert audited[0]["answer_kind"] == "stream_failed"
    assert "runtime died mid-stream" in audited[0]["stream_error"]


def test_a_stream_records_that_the_numeral_check_removed_something(
    client: TestClient, store: Store, make_fact, monkeypatch
) -> None:
    """Whether prose was edited is part of what a reviewer needs to know."""
    store.insert_facts([make_fact(status=FactStatus.VALIDATED)])
    fake = _FakeStreamLLM(["Output leapt to ", "999 crore tonnes."])
    monkeypatch.setattr("mrip.api.query.client_from_settings", lambda _settings: fake)

    _stream(client, "why did SECL coal production rise")

    audited = _query_audits(store)
    assert audited[0]["narrative_flagged"] is True
