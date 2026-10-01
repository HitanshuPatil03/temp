"""Tests for report narrative sections (ARCHITECTURE §11.2, §13.4).

The question under test is the one that matters most about this feature: can a
model put a number into a Ministry report that the deterministic layer did not
supply? The answer has to be no, and it has to be no for a reason stronger than
the prompt asking nicely.

The model is faked here rather than run. That is deliberate: these assertions are
about the *verifier*, and a real model would make them flaky without making them
stronger. Whether Qwen3 writes good prose is a separate question from whether
unsupported prose can reach a published report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mrip.auth.scope import Scope
from mrip.db import Store
from mrip.llm import LLMClient, LLMUnavailableError
from mrip.reports.generate import generate
from mrip.reports.narrate import narrate
from mrip.reports.template import (
    BUILTIN_TEMPLATES,
    builtin_template,
    default_template,
    parliamentary_response_template,
)
from mrip.reports.writers import MODEL_DISABLED
from mrip.schemas import FactStatus

SCOPE = Scope.unrestricted("test suite")


@dataclass(frozen=True, slots=True)
class FakeLLM(LLMClient):
    """An LLM client that returns a scripted answer, or refuses.

    Subclasses the real client so the narrator cannot tell the difference and the
    test exercises the real ``available`` / :class:`LLMUnavailableError` contract
    rather than a duck-typed stand-in that happens to have the same methods.

    It has to repeat ``frozen=True, slots=True`` because the parent carries them.
    A plain subclass with an ``__init__`` looks like it works on Python 3.13 and
    raises ``TypeError: super(type, obj)`` on 3.12, because a slotted frozen
    dataclass builds a replacement class and its generated ``__setattr__``
    closes over the original. ``prompts`` is appended to rather than reassigned,
    which frozen permits.
    """

    reply: str = ""
    prompts: list[str] = field(default_factory=list)

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        if not self.enabled:
            raise LLMUnavailableError("disabled in this test")
        self.prompts.append(prompt)
        return self.reply


def fake_llm(reply: str = "", *, enabled: bool = True) -> FakeLLM:
    """A :class:`FakeLLM` whose transport values would fail loudly if used.

    Port 0 and a 1 ms timeout: if a change ever lets this reach the network, the
    test fails rather than quietly talking to whatever is listening.
    """
    return FakeLLM(
        base_url="http://127.0.0.1:0",
        model="fake",
        timeout=0.001,
        thinking=False,
        enabled=enabled,
        reply=reply,
    )


def _manifest(store: Store, make_fact):
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    return generate(store, SCOPE, default_template(), entity="secl", period="FY2024-25")


# ---------------------------------------------------------------- verification


def test_prose_grounded_in_the_pinned_figures_survives(store: Store, make_fact) -> None:
    manifest = _manifest(store, make_fact)
    llm = fake_llm("SECL produced 193 Million Tonnes in the period.")

    result = narrate(default_template(), manifest, llm)

    assert result.sections["Summary"] == "SECL produced 193 Million Tonnes in the period."
    assert not result.any_flagged


def test_an_invented_figure_is_stripped_and_the_section_flagged(
    store: Store, make_fact
) -> None:
    """The failure mode this whole path exists to prevent."""
    manifest = _manifest(store, make_fact)
    llm = fake_llm(
        "SECL produced 193 Million Tonnes in the period. "
        "Offtake reached 187 Million Tonnes."
    )

    result = narrate(default_template(), manifest, llm)

    assert "193" in result.sections["Summary"]
    assert "187" not in result.sections["Summary"]
    assert result.flagged == {"Summary"}
    assert result.any_flagged


def test_a_figure_the_model_computed_is_not_supported(store: Store, make_fact) -> None:
    """Arithmetic is the deterministic layer's job.

    A percentage derived from the pinned figures is not itself a pinned figure,
    and the verifier does not make an exception for one that happens to be
    correct — a reviewer cannot tell a correct derivation from an invented one by
    reading the prose.
    """
    manifest = _manifest(store, make_fact)
    llm = fake_llm("Production rose 4.7 percent against the previous year.")

    result = narrate(default_template(), manifest, llm)

    assert result.sections.get("Summary", "") == ""
    assert result.flagged == {"Summary"}


def test_a_year_in_prose_is_not_treated_as_an_invented_figure(
    store: Store, make_fact
) -> None:
    """Four-digit years are allowed through, or every sentence naming the period
    would be dropped and the feature would be useless."""
    manifest = _manifest(store, make_fact)
    llm = fake_llm("In 2024 SECL produced 193 Million Tonnes.")

    result = narrate(default_template(), manifest, llm)

    assert "2024" in result.sections["Summary"]
    assert not result.any_flagged


def test_the_model_is_handed_figures_and_not_the_fact_store(
    store: Store, make_fact
) -> None:
    """§13.4 — it receives pinned figures and nothing else."""
    manifest = _manifest(store, make_fact)
    llm = fake_llm("SECL produced 193 Million Tonnes.")

    narrate(default_template(), manifest, llm)

    prompt = llm.prompts[0]
    assert "FIGURES:" in prompt
    assert "193" in prompt
    assert manifest.figures[0].locator in prompt
    # Nothing resembling a query interface, a table name or a connection.
    for leak in ("SELECT", "facts", "evidence", "postgresql"):
        assert leak not in prompt


# -------------------------------------------------------------- model disabled


def test_a_disabled_model_yields_no_sections(store: Store, make_fact) -> None:
    """§7 — prose is what is lost, and nothing else."""
    manifest = _manifest(store, make_fact)

    result = narrate(default_template(), manifest, fake_llm(enabled=False))

    assert result.sections == {}
    assert not result.any_flagged


def test_the_writers_state_the_model_was_disabled(store: Store, make_fact) -> None:
    """A missing narrative is announced, not silently omitted."""
    import io

    from docx import Document as DocxDocument

    from mrip.reports.writers import render_docx

    manifest = _manifest(store, make_fact)
    document = DocxDocument(io.BytesIO(render_docx(default_template(), manifest)))
    body = "\n".join(paragraph.text for paragraph in document.paragraphs)

    assert MODEL_DISABLED in body


# ------------------------------------------------------- parliamentary template


def test_the_parliamentary_template_requires_offtake() -> None:
    """A reply about production is almost always also about despatch, so this
    template fails rather than answering half the question."""
    template = parliamentary_response_template()

    required = {field.metric for field in template.required}
    assert required == {"coal_production", "coal_offtake"}


def test_the_parliamentary_template_leads_with_the_reply() -> None:
    """The section order is the document's argument: a reply opens with the
    position, a management report opens with the table."""
    kinds = [section.kind for section in parliamentary_response_template().sections]

    assert kinds[0] == "narrative"
    assert kinds[-1] == "evidence_appendix"
    assert next(s.kind for s in default_template().sections) == "figures"


def test_the_parliamentary_template_refuses_without_offtake(
    store: Store, make_fact
) -> None:
    """Only production is in the corpus, so the required offtake figure is
    reported as missing and the report will not render."""
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])

    manifest = generate(
        store,
        SCOPE,
        parliamentary_response_template(),
        entity="secl",
        period="FY2024-25",
    )

    assert not manifest.complete
    assert {item.metric for item in manifest.missing_required} == {"coal_offtake"}


def test_every_builtin_template_is_loadable_by_id() -> None:
    """The registry is the only way a template is selected, so an entry that
    does not build is a 500 waiting for a caller."""
    for template_id in BUILTIN_TEMPLATES:
        template = builtin_template(template_id)
        assert template.id == template_id
        assert template.version >= 1
        assert template.sections


def test_an_unknown_template_id_names_what_exists() -> None:
    import pytest

    with pytest.raises(KeyError, match="production-summary"):
        builtin_template("no-such-template")
