"""Access-scope enforcement.

This file is the gate for roadmap 0.8, and it is written to fail in the ways that
matter rather than to confirm the happy path. The question is never "does a scoped
read return the right rows" — it is **"can a read ever return rows it shouldn't"**,
and a system that leaks does so through the paths nobody wrote a test for.

So there is an introspection test at the bottom that asserts every repository read
*requires* a scope argument. A new query method added without one fails this file
even if its author never opens it, which is the only way a rule like this survives
contact with a growing codebase.
"""

from __future__ import annotations

import inspect

import pytest

from mrip.auth.scope import SCOPE_ALL, Scope
from mrip.db.repositories.conflicts import ConflictRepository
from mrip.db.repositories.documents import DocumentRepository, EvidenceRepository
from mrip.db.repositories.facts import FactRepository
from mrip.db.repositories.users import AuditRepository, UserRepository
from mrip.schemas import FactStatus

SECL = Scope.of("secl")
MCL = Scope.of("mcl")
HQ = Scope.unrestricted("test: headquarters view")


# ------------------------------------------------------------------ the value object


def test_an_empty_scope_matches_nothing_rather_than_everything():
    """The degenerate case must fail closed.

    If an ungranted account's scope compiled to "no predicate", it would read the
    whole corpus. That is the single most dangerous bug this module can have, so
    it is the first thing asserted.
    """
    assert Scope.nothing().is_empty is True
    assert Scope.nothing().allows("secl") is False
    assert Scope.nothing().allows(None) is False


def test_scope_of_nothing_is_refused_not_silently_permissive():
    """``Scope.of()`` with no arguments is a programming error, not 'allow all'."""
    with pytest.raises(ValueError, match="match nothing"):
        Scope.of()


def test_unrestricted_scope_must_state_a_reason():
    """An unscoped read is always attributable to a decision someone wrote down."""
    with pytest.raises(ValueError, match="state why"):
        Scope.unrestricted("   ")
    assert Scope.unrestricted("nightly report job").is_unrestricted is True


def test_unattributed_rows_are_not_public():
    """A document with no owning entity is visible only to an unrestricted scope.

    Treating "not yet attributed" as "everyone may read" is the wrong default for
    a need-to-know corpus — and unattributed is exactly the state a document is in
    between upload and entity resolution.
    """
    assert SECL.allows(None) is False
    assert HQ.allows(None) is True


def test_a_scope_does_not_leak_across_entities():
    assert SECL.allows("secl") is True
    assert SECL.allows("mcl") is False
    assert Scope.of("secl", "mcl").allows("mcl") is True
    # The sentinel is a grant, not a magic string an entity id could collide with.
    assert Scope.of(SCOPE_ALL).is_unrestricted is True


def test_sensitivity_is_recorded_but_does_not_restrict_reading(store, make_document):
    """Pin the gap, so it cannot be forgotten *or* quietly closed.

    ``Sensitivity`` is displayed to an officer and settable on upload, and its
    docstring used to claim it "gates access, and which processing is permitted".
    Nothing reads it — this asserts that, so the next person to assume otherwise
    gets a failing test instead of a false assurance.

    Written as an equality rather than "restricted is readable" on purpose: when
    enforcement lands, this test *should* fail, and whoever implements it has to
    come here and describe the rule. A test that merely tolerated both behaviours
    would let the gap persist indefinitely.
    """
    from mrip.schemas import Sensitivity

    # Published *by* SECL, so it is squarely inside SECL's own scope. That is
    # the strongest form of the claim: not merely that some other entity can see
    # it, but that the label changes nothing for a reader who is entitled to the
    # document anyway.
    store.register_document(
        make_document(
            "restricted-draft.pdf",
            sensitivity=Sensitivity.RESTRICTED,
            publisher_entity_id="secl",
        )
    )

    visible = store.list_documents(SECL)

    assert [document.filename for document in visible] == ["restricted-draft.pdf"], (
        "If this now fails because sensitivity *does* gate access, that is the "
        "intended trigger: update this test to state the rule and remove the "
        "caveat from Sensitivity's docstring."
    )


# --------------------------------------------------------------------- enforcement


def test_facts_are_invisible_outside_their_entity_scope(store, make_fact):
    store.insert_facts(
        [
            make_fact(entity_id="secl", value=193.0e6),
            make_fact(entity_id="mcl", value=91.0e6),
        ]
    )

    assert {f.entity_id for f in store.query_facts(SECL)} == {"secl"}
    assert {f.entity_id for f in store.query_facts(MCL)} == {"mcl"}
    assert {f.entity_id for f in store.query_facts(HQ)} == {"secl", "mcl"}
    assert store.query_facts(Scope.nothing()) == []


def test_a_single_fact_lookup_is_scoped_too(store, make_fact):
    """The list endpoint being scoped is worthless if the by-id one is not."""
    mcl_fact = make_fact(entity_id="mcl")
    store.insert_facts([mcl_fact])

    assert store.get_fact(mcl_fact.fact_id, HQ) is not None
    assert store.get_fact(mcl_fact.fact_id, SECL) is None


def test_documents_are_scoped_by_their_owning_entity(store, make_document):
    store.register_document(make_document("secl.pdf", publisher_entity_id="secl"))
    store.register_document(make_document("mcl.pdf", publisher_entity_id="mcl"))

    assert [d.filename for d in store.list_documents(SECL)] == ["secl.pdf"]
    assert len(store.list_documents(HQ)) == 2


def test_evidence_is_scoped_through_its_document(store, make_document):
    """A page's text is exactly as sensitive as the document it came from."""
    document = store.register_document(
        make_document("mcl-annual.pdf", document_id="doc_mcl", publisher_entity_id="mcl")
    )
    store.insert_evidence(
        [
            {
                "document_id": document.document_id,
                "page": 12,
                "kind": "text_span",
                "text": "Unpublished provisional production figure",
                "extraction_method": "pdf_text_layer",
            }
        ]
    )

    assert len(store.page_evidence(document.document_id, 12, HQ)) == 1
    assert store.page_evidence(document.document_id, 12, SECL) == []


def test_summary_counts_do_not_leak_corpus_size(store, make_document, make_fact):
    """A total that includes unreadable rows discloses another subsidiary's volume."""
    secl_doc = store.register_document(
        make_document("secl.pdf", publisher_entity_id="secl")
    )
    mcl_doc = store.register_document(make_document("mcl.pdf", publisher_entity_id="mcl"))
    store.insert_facts(
        [
            make_fact(document_id=secl_doc.document_id, entity_id="secl"),
            make_fact(document_id=mcl_doc.document_id, entity_id="mcl"),
            make_fact(document_id=mcl_doc.document_id, entity_id="mcl", value=5.0e6),
        ]
    )

    secl_view = store.summary(SECL)
    assert secl_view["documents"] == 1
    assert secl_view["facts"] == 1
    assert secl_view["entities"] == 1

    assert store.summary(HQ)["facts"] == 3
    assert store.summary(Scope.nothing())["facts"] == 0


def test_series_is_scoped(store, make_fact):
    store.insert_facts(
        [
            make_fact(entity_id="secl", value=193.0e6, status=FactStatus.VALIDATED),
            make_fact(entity_id="mcl", value=91.0e6, status=FactStatus.VALIDATED),
        ]
    )
    series = store.entity_metric_series("coal_production", SECL, unit="t")
    assert [row["entity_id"] for row in series] == ["secl"]


def test_conflict_detection_does_not_group_across_a_scope_boundary(store, make_fact):
    """Detection run under one subsidiary's scope must not read another's facts."""
    store.insert_facts(
        [
            make_fact(entity_id="mcl", value=91.0e6),
            make_fact(entity_id="mcl", value=88.0e6),
        ]
    )
    assert store.detect_conflicts(SECL) == []
    assert len(store.detect_conflicts(HQ)) == 1


def test_a_conflict_cannot_be_resolved_from_outside_its_scope(store, make_fact):
    """Adjudication is a write. Scope has to hold on the write path too."""
    winner = make_fact(entity_id="mcl", value=91.0e6)
    store.insert_facts([winner, make_fact(entity_id="mcl", value=88.0e6)])
    conflict = store.detect_conflicts(HQ)[0]

    assert store.resolve_conflict(conflict.conflict_id, winner.fact_id, SECL) is False
    assert store.get_fact(winner.fact_id, HQ).status is not FactStatus.VALIDATED

    assert store.resolve_conflict(conflict.conflict_id, winner.fact_id, HQ) is True


# ------------------------------------------------------- the rule, enforced by test

#: Writes. A write takes the ids it operates on from the caller's already-scoped
#: read, so it is not itself a disclosure path. The exception is
#: ``ConflictRepository.resolve``, which *is* scoped, because adjudication is a
#: reviewer-facing action and the conflict id arrives straight from a request.
WRITE_METHODS: dict[str, str] = {
    "DocumentRepository.register": "write",
    "DocumentRepository.mark_superseded": "write",
    "DocumentRepository.set_state": "write, pipeline-internal",
    "DocumentRepository.set_progress": "write, pipeline-internal progress counter",
    "DocumentRepository.set_class": "write, what the classifier decided",
    "DocumentRepository.set_fiscal_year": "write, inferred from the document's own facts",
    "DocumentRepository.set_publisher": "write, attributes a document to its entity",
    "FactRepository.delete_for_document": "write, a stage replacing its own output",
    "FactRepository.flag_for_review": "write, validation routing",
    "FactRepository.flag_low_confidence_for_document": "write, per-document review sweep",
    "FactRepository.promote_high_confidence_for_document": (
        "write, per-document acceptance sweep — the other half of the review sweep"
    ),
    "EvidenceRepository.insert": "write",
    "EvidenceRepository.replace_stage": "write, idempotent stage rewrite",
    "FactRepository.insert": "write",
    "FactRepository.set_status": "write",
    "FactRepository.flag_low_confidence": "write, corpus-wide confidence sweep",
    "FactRepository.promote_high_confidence": (
        "write, corpus-wide acceptance sweep — the other half of the confidence sweep"
    ),
}

#: Reads that are deliberately unscoped, each with the reason it has to be.
UNSCOPED_READS: dict[str, str] = {
    # Dedup on the write path: it must not be possible to create a duplicate of a
    # document merely because the uploader cannot see the original.
    "DocumentRepository.find_by_hash": "content-hash dedup on the write path",
    # Operational counters for the pipeline health probe. Returns lifecycle state
    # names and tallies, never document content or entity attribution.
    "DocumentRepository.count_by_state": "operational metric, no row content",
    # ------------------------------------------------------------- pipeline
    # The ingestion stages read their *own* document. A worker has no user
    # behind it, and the document is being processed because it was accepted —
    # not because someone can see it. Both methods are keyed on a single
    # document id that arrived in the job payload, never on a corpus query, and
    # the unrestricted scope the pipeline uses states that reason in its own
    # constructor (mrip.ingest.pipeline.PIPELINE_SCOPE).
    "EvidenceRepository.for_document": "pipeline-internal, one document by id",
    "EvidenceRepository.page_text": "pipeline-internal, one document by id",
    "FactRepository.document_facets": "pipeline-internal, facets of one document",
    # ---------------------------------------------------------------- identity
    # `users` and `user_scopes` are the tables that *define* scope, so scoping
    # their reads would be circular: resolving a caller's scope requires reading
    # their own grants. Access is gated by role at the route instead — only an
    # admin may list or modify accounts — and by the fact that every lookup here
    # is keyed on a single id or username rather than returning a corpus.
    "UserRepository.get": "identity lookup by id; scope is derived from it",
    "UserRepository.by_username": "login path, before a principal exists",
    "UserRepository.list_accounts": "account administration, admin role required",
    "UserRepository.count_active_admins": "counter that guards the last-admin rule",
    "UserRepository.grants_for": "reads the grants that constitute a scope",
    "UserRepository.scope_for": "builds a Scope; cannot require one",
    "UserRepository.users_with_entity": "access review, admin role required",
    # Writes on the identity tables. Each takes a user id the caller already
    # resolved through an admin-gated route or the operator CLI.
    "UserRepository.create": "write, admin or CLI",
    "UserRepository.set_password": "write, own account or admin reset",
    "UserRepository.refresh_password_hash": "write, re-hash at current cost",
    "UserRepository.set_role": "write, admin",
    "UserRepository.set_display_name": "write, admin",
    "UserRepository.set_active": "write, admin",
    "UserRepository.record_login_success": "write, login bookkeeping",
    "UserRepository.record_login_failure": "write, lockout counter",
    "UserRepository.unlock": "write, admin",
    "UserRepository.grant_scope": "write, admin — grants scope, cannot take one",
    "UserRepository.revoke_scope": "write, admin",
    "UserRepository.replace_scopes": "write, admin",
    # ------------------------------------------------------------------ audit
    # An audit trail filtered by the reader's own entity scope is not an audit
    # trail. The admin role is the access control here, asserted in test_auth.py.
    "AuditRepository.record": "append-only write",
    "AuditRepository.recent": "audit trail is deliberately cross-entity, admin only",
    "AuditRepository.count": "operational metric",
}


def _public_methods() -> list[tuple[str, inspect.Signature]]:
    """Every public repository method, regardless of what it looks like it does.

    Deliberately *not* filtered by name prefix. A prefix filter is a guess about
    which methods read rows, and this project has already shipped one hole that
    way: ``EvidenceRepository.distinct_pages`` read evidence, took no scope, and
    matched none of the prefixes, so it was never checked. Enumerating everything
    and requiring an explicit registration is the only version of this test that
    cannot be bypassed by naming.
    """
    found = []
    for repository in (
        DocumentRepository,
        EvidenceRepository,
        FactRepository,
        ConflictRepository,
        UserRepository,
        AuditRepository,
    ):
        for name, member in inspect.getmembers(repository, inspect.isfunction):
            if name.startswith("_"):
                continue
            found.append((f"{repository.__name__}.{name}", inspect.signature(member)))
    return found


def test_every_repository_method_is_scoped_or_explicitly_registered():
    """The structural gate.

    Authorisation you can forget to write at a call site is authorisation you
    will forget. A method added without a ``scope`` parameter fails here, and the
    only way to pass is to add one or to register it above with a written reason.
    """
    assert _public_methods(), "introspection found nothing — the filter is wrong"

    registered = WRITE_METHODS | UNSCOPED_READS
    unaccounted = [
        name
        for name, signature in _public_methods()
        if "scope" not in signature.parameters and name not in registered
    ]
    assert unaccounted == [], (
        "These repository methods take no scope and are not registered: "
        f"{', '.join(unaccounted)}. Add a `scope: Scope` parameter, or register "
        "the method in WRITE_METHODS / UNSCOPED_READS with the reason it is safe."
    )


def test_registrations_still_refer_to_real_methods():
    """A registration for a method that was renamed or deleted is dead weight that
    would silently cover a *new* method that happens to reuse the name."""
    known = {name for name, _ in _public_methods()}
    stale = (set(WRITE_METHODS) | set(UNSCOPED_READS)) - known
    assert stale == set(), (
        f"Registrations reference methods that no longer exist: {stale}"
    )


def test_a_scoped_method_is_not_also_registered_as_exempt():
    """If a method gained a scope, its exemption should have been removed — an
    exemption that outlives its reason is how the next hole gets covered up."""
    scoped = {
        name for name, signature in _public_methods() if "scope" in signature.parameters
    }
    contradictory = scoped & (set(WRITE_METHODS) | set(UNSCOPED_READS))
    assert contradictory == set(), (
        f"These methods take a scope but are still registered as exempt: "
        f"{contradictory}. Remove the registration."
    )
