"""Tests for automated report generation (ARCHITECTURE §11).

The thesis under test: every figure in a report is pinned to a validated fact and
its source ``document@version``; a required figure with no validated fact fails
loudly rather than blanking; and a published manifest reproduces its figures on
its own and diffs against the corpus as it later becomes.
"""

from __future__ import annotations

import pytest

from mrip.auth.scope import Scope
from mrip.db import Store
from mrip.reports.generate import diff, generate, reproduce
from mrip.reports.render import ReportIncompleteError, render_markdown
from mrip.reports.template import default_template
from mrip.schemas import FactStatus, RefusalReason

SCOPE = Scope.unrestricted("test suite")


def test_generate_pins_figure_to_fact_and_document_version(
    store: Store, make_fact
) -> None:
    fact = make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)
    store.insert_facts([fact])

    manifest = generate(
        store, SCOPE, default_template(), entity="secl", period="FY2024-25"
    )

    assert manifest.complete
    pinned = next(f for f in manifest.figures if f.metric == "coal_production")
    assert pinned.fact_id == fact.fact_id
    assert pinned.value == 193.0e6
    assert pinned.document_id == fact.evidence.document_id
    assert pinned.document_version == fact.evidence.document_version


def test_generate_fails_loudly_on_missing_required_figure(store: Store) -> None:
    manifest = generate(
        store, SCOPE, default_template(), entity="secl", period="FY2024-25"
    )
    assert not manifest.complete
    missing = manifest.missing_required[0]
    assert missing.metric == "coal_production"
    assert missing.reason is RefusalReason.OUT_OF_CORPUS


def test_generate_omits_optional_figures_without_failing(store: Store, make_fact) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    manifest = generate(
        store, SCOPE, default_template(), entity="secl", period="FY2024-25"
    )
    # coal_production (required) is pinned; offtake/overburden (optional) are absent
    # but the report is still complete.
    assert manifest.complete
    assert manifest.missing_optional
    assert {m.metric for m in manifest.missing_optional} <= {
        "coal_offtake",
        "overburden_removal",
    }


def test_unknown_entity_or_period_is_a_value_error(store: Store) -> None:
    with pytest.raises(ValueError, match="entity"):
        generate(store, SCOPE, default_template(), entity="atlantis", period="FY2024-25")


def test_render_markdown_shows_figures_sources_and_disabled_note(
    store: Store, make_fact
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    template = default_template()
    manifest = generate(store, SCOPE, template, entity="secl", period="FY2024-25")

    markdown = render_markdown(template, manifest)

    assert (
        "# Production Summary — South Eastern Coalfields Limited, FY2024-25" in markdown
    )
    assert "193,000,000 t" in markdown  # the pinned value, formatted
    assert "doc:" in markdown  # a locator is cited
    # No narratives passed and the model is not called here: the section says so.
    assert "the local model is disabled" in markdown


def test_render_refuses_an_incomplete_manifest(store: Store) -> None:
    template = default_template()
    manifest = generate(store, SCOPE, template, entity="secl", period="FY2024-25")
    assert not manifest.complete
    with pytest.raises(ReportIncompleteError, match="Coal production"):
        render_markdown(template, manifest)


def test_manifest_reproduces_its_figures_without_the_store(
    store: Store, make_fact
) -> None:
    store.insert_facts([make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)])
    manifest = generate(
        store, SCOPE, default_template(), entity="secl", period="FY2024-25"
    )
    # Reproduction is a pure function of the manifest — the figures as approved.
    assert reproduce(manifest) == manifest.figures


def test_diff_reports_a_moved_figure(store: Store, make_fact) -> None:
    original = make_fact(status=FactStatus.VALIDATED, unit_ambiguous=False)
    store.insert_facts([original])
    manifest = generate(
        store, SCOPE, default_template(), entity="secl", period="FY2024-25"
    )

    # The corpus moves on: the pinned fact is superseded by a revised figure.
    store.facts.set_status([original.fact_id], FactStatus.SUPERSEDED)
    store.insert_facts(
        [
            make_fact(
                status=FactStatus.VALIDATED,
                unit_ambiguous=False,
                value=200.0e6,
                raw_value=200.0,
            )
        ]
    )

    deltas = diff(store, SCOPE, manifest)
    production = next(d for d in deltas if d.metric == "coal_production")
    assert production.changed
    assert production.approved_value == 193.0e6
    assert production.current_value == 200.0e6
