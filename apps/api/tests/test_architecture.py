"""Architecture tests.

The rules in `docs/ARCHITECTURE.md` that can be checked mechanically are checked
here, because an architectural rule nobody can violate is worth more than one
everybody agrees with.

Two are asserted:

**The figure path has no model client** (§7). Not "we are careful not to call
it" — the modules that produce figures must not be able to reach a generative
client at all, and this test fails the build if one ever imports it.

**Every document class the upload boundary accepts has a digitizer** (§10). This
one exists because the two drifted apart once: images and Word files were
admitted at the boundary, told the uploader "accepted", and then died two stages
later with "no digitizer". A boundary that accepts what the pipeline cannot
process is worse than one that refuses.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mrip.ingest.intake import _CLASS_BY_TYPE
from mrip.schemas import DocumentClass

API_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = API_ROOT / "mrip"

#: Modules that produce figures. Nothing here may reach a generative model — the
#: exact-figure and comparison paths are SQL over the fact store, and a
#: contributor who adds a model call to one of them fails this test rather than
#: shipping a hallucinated production number.
FIGURE_PATH = (
    "mrip/facts",
    "mrip/db",
    "mrip/normalize",
    "mrip/topics",
    "mrip/validate",
    "mrip/query/exact.py",
    "mrip/query/compare.py",
    "mrip/reports/render.py",
    "mrip/reports/writers.py",
    "mrip/reports/generate.py",
)

#: Import names that mean "a generative model is reachable from here".
MODEL_IMPORTS = (
    "mrip.llm",
    "ollama",
    "openai",
    "anthropic",
    "google.generativeai",
    "transformers",
    "llama_cpp",
    "litellm",
)


def _imports_of(path: Path) -> set[str]:
    """Every module name imported by a file, including `from x import y`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _figure_path_files() -> list[Path]:
    files: list[Path] = []
    for entry in FIGURE_PATH:
        target = API_ROOT / entry
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
        elif target.is_file():
            files.append(target)
    return files


def test_the_figure_path_cannot_reach_a_model():
    """ARCHITECTURE §7, enforced.

    Checked by reading the import graph rather than by trusting a convention,
    and written so that it keeps holding as `mrip/query/` and `mrip/reports/`
    are built — the paths are listed whether or not they exist yet.
    """
    offenders: list[str] = []
    for file in _figure_path_files():
        for imported in _imports_of(file):
            if any(
                imported == banned or imported.startswith(f"{banned}.")
                for banned in MODEL_IMPORTS
            ):
                offenders.append(f"{file.relative_to(API_ROOT)} imports {imported}")

    assert offenders == [], (
        "The figure path must not be able to reach a generative model:\n  "
        + "\n  ".join(offenders)
        + "\nFigures come from the fact store. If prose is needed, build it in a "
        "module outside the figure path and pass it the facts."
    )


def test_the_figure_path_list_still_points_at_real_code():
    """A rule that guards nothing passes silently. At least the built parts of
    the figure path must exist, or this test is decoration."""
    existing = [entry for entry in FIGURE_PATH if (API_ROOT / entry).exists()]
    assert len(existing) >= 4, (
        f"Only {existing} of the figure path exists; the rest of the list is "
        "either stale or the modules were renamed."
    )


def test_only_one_report_module_can_reach_a_model():
    """§11.2 — reports generate prose in exactly one place.

    The generator, the renderer and the four writers lay out figures that were
    already pinned; `narrate.py` is the only module that produces prose, and so
    the only one allowed a model client. Keeping that boundary at one file is
    what makes "a report never asks a model for a figure" checkable rather than
    a habit — and it is why the verifier lives on the query side and is imported
    here rather than copied.
    """
    reports = sorted((PACKAGE / "reports").glob("*.py"))
    assert reports, "mrip/reports/ has no modules; this test guards nothing."

    reaching = {
        file.name
        for file in reports
        for imported in _imports_of(file)
        if any(
            imported == banned or imported.startswith(f"{banned}.")
            for banned in MODEL_IMPORTS
        )
    }

    assert reaching == {"narrate.py"}, (
        f"Model access in mrip/reports/ is {sorted(reaching) or 'nowhere'}, "
        "expected exactly ['narrate.py']. Prose belongs in narrate.py, which "
        "receives already-pinned figures; every other report module is on the "
        "figure path and must stay unable to reach a model."
    )


def test_every_accepted_document_class_has_a_digitizer():
    """ARCHITECTURE §10, enforced.

    The boundary tells an uploader "accepted". That is a promise the pipeline has
    to be able to keep, and it did not for images and Word files until this test
    existed.
    """
    from mrip.ingest import digitize as digitizer

    handled = {
        DocumentClass.TEXT_PDF: digitizer.digitize_pdf_text,
        DocumentClass.SCANNED_PDF: digitizer.digitize_scanned_pages,
        DocumentClass.MIXED_PDF: digitizer.digitize_pdf_text,
        DocumentClass.SPREADSHEET: digitizer.digitize_spreadsheet,
        DocumentClass.DOCX: digitizer.digitize_docx,
        DocumentClass.IMAGE: digitizer.digitize_image,
    }

    accepted = {
        doc_class
        for doc_class in _CLASS_BY_TYPE.values()
        if doc_class is not DocumentClass.UNKNOWN
    }
    unhandled = sorted(item.value for item in accepted - set(handled))

    assert unhandled == [], (
        f"The upload boundary accepts {unhandled} but no digitizer handles them. "
        "Either add the digitizer, or refuse the type at the boundary with a "
        "reason — accepting a file the pipeline cannot read is the worse option."
    )


@pytest.mark.parametrize(
    "module",
    ["mrip.ingest.pipeline", "mrip.facts.extract", "mrip.validate.rules"],
)
def test_pipeline_modules_import_cleanly_without_the_optional_extras(module):
    """The OCR and ML extras are optional installs.

    Importing the pipeline must not require them: a deployment that handles only
    born-digital filings should not have to carry a few hundred megabytes of
    ONNX runtime to start a worker.
    """
    __import__(module)


#: Domain exceptions that no HTTP route needs to catch, each with the reason it
#: cannot reach one. Everything else must be caught somewhere under ``mrip/api/``,
#: because a domain exception that escapes becomes a 500 — and "Internal Server
#: Error" tells an officer nothing about whether to retry, fix their input, or
#: call someone.
#:
#: The list is short on purpose. Adding to it is a claim that a failure is
#: unreachable from a request, and that claim goes stale: ``BlobNotFoundError`` is
#: here only because nothing serves document bytes yet, and roadmap 8.2 will
#: change that.
UNREACHABLE_FROM_HTTP: dict[str, str] = {
    "BlobNotFoundError": (
        "raised on a blob read, and no route reads blobs — evidence shown to a "
        "reader comes from the database. When 8.2 adds signed document URLs, that "
        "route must catch this"
    ),
    "ContentHashMismatchError": (
        "raised by the blob store only when the caller supplies an expected hash, "
        "which the upload path does not"
    ),
    "TokenError": "base class; both subclasses are caught in deps.py",
    "PermanentJobError": "worker-side dead-lettering, never raised in a request",
    "LeaseLostError": "worker-side lease expiry, never raised in a request",
    "ChallengedError": "corpus harvester, reachable only from mrip-admin",
    "_RestartWithoutRangeError": "corpus harvester internal control flow",
}


def _exception_classes() -> dict[str, str]:
    """Every ``*Error`` / ``*Exception`` class under ``mrip/``, and where it lives."""
    found: dict[str, str] = {}
    for file in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith(
                ("Error", "Exception")
            ):
                found[node.name] = str(file.relative_to(API_ROOT))
    return found


def test_every_domain_exception_a_request_can_raise_is_mapped_to_a_status():
    """A domain exception that escapes a route becomes a 500.

    That is a product defect, not a tidiness one: this platform's refusals are
    the part an officer has to act on. "This period is not one I recognise" and
    "that report was already approved" are answers. "Internal Server Error" is
    the same response the system gives when it is genuinely broken, so it teaches
    people either to retry blindly or to stop reading the error text.

    Derived from the source rather than listed, so a *new* exception fails this
    test until somebody decides whether a route has to handle it. The matching is
    deliberately crude — the class name appearing in an ``except`` clause anywhere
    under ``mrip/api/`` — because the alternative is modelling which route can
    raise what, and a test that models the call graph is a test that drifts from
    it.
    """
    import re

    defined = _exception_classes()
    routes = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((PACKAGE / "api").glob("*.py"))
    )
    unhandled = {
        name: where
        for name, where in defined.items()
        if name not in UNREACHABLE_FROM_HTTP
        and not re.search(rf"except[^\n]*\b{name}\b", routes)
    }

    assert defined, "no exception classes found, so this test is looking at nothing"
    assert unhandled == {}, (
        "These domain exceptions are neither caught under mrip/api/ nor declared "
        "unreachable from a request:\n  "
        + "\n  ".join(f"{name}  ({where})" for name, where in sorted(unhandled.items()))
        + "\nCatch it in the route and map it to a status an officer can act on, or "
        "add it to UNREACHABLE_FROM_HTTP with the reason it cannot reach one."
    )


def test_the_unreachable_list_does_not_outlive_the_exceptions_it_excuses():
    """An excuse for a class that no longer exists is an excuse nobody re-read."""
    stale = sorted(set(UNREACHABLE_FROM_HTTP) - set(_exception_classes()))

    assert stale == [], f"UNREACHABLE_FROM_HTTP names exceptions that are gone: {stale}"


def test_no_module_locates_itself_by_counting_parent_directories():
    """A path that is only correct in a source checkout is a deployment bug.

    ``Path(__file__).resolve().parents[3]`` is a statement about where this file
    sits in a tree, and it stops being true the moment the package is installed
    somewhere else. Two modules did it and both broke the container image, in
    ways that looked nothing like the cause:

    * ``config.py`` counted four levels to the repository root. In the image the
      package is at ``/app/mrip``, which has no fourth parent, so *every*
      process raised ``IndexError`` while importing settings — including the
      migration job, so the whole stack never started.
    * ``schema_version.py`` counted three levels to find ``migrations/``. The API
      is launched as ``uvicorn mrip.main:app``, which puts the working directory
      on ``sys.path``; the worker and scheduler are console scripts, which do
      not. They therefore imported a different copy of the same code, found no
      migration history above it, and crash-looped — against an API reporting
      healthy. A deployment that answers every read and ingests nothing.

    Neither was visible to a suite that imports the package from the checkout it
    lives in, which is why this is a source check rather than a behavioural one.
    Locate things by looking for a marker file instead; the two functions those
    modules now use are the pattern.

    Depth 0 and 1 are allowed. ``parents[0]`` is the module's own directory and
    ``parents[1]`` its package's parent — the import system guarantees both,
    whatever tree the package was installed into. Depth 2 and beyond is where a
    claim about *this repository's* shape begins, and where both bugs lived.
    """
    import ast

    #: The first depth that reaches past the module's own package.
    tree_assumption_depth = 2

    offenders: list[str] = []
    for file in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            # Match `<anything>.parents[<int>]`, which is the subscript that
            # makes the assumption. `.parent` and iterating `.parents` are both
            # fine and common, so neither is matched here.
            if not isinstance(node, ast.Subscript):
                continue
            target = node.value
            if not (isinstance(target, ast.Attribute) and target.attr == "parents"):
                continue
            index = node.slice
            if not (isinstance(index, ast.Constant) and isinstance(index.value, int)):
                continue
            if index.value < tree_assumption_depth:
                continue
            offenders.append(f"{file.relative_to(API_ROOT)}:{node.lineno}")

    assert offenders == [], (
        "These modules locate a path by counting parent directories:\n  "
        + "\n  ".join(offenders)
        + "\nThat is true in a checkout and false once the package is installed, "
        "so it breaks the container image and nothing else. Search upward for a "
        "marker file — see mrip/config.py::_repo_root and "
        "mrip/db/schema_version.py::_alembic_root."
    )


#: Route handlers that do not call ``audit.record`` themselves because a service
#: function does it inside the same transaction — which is the better place for
#: it: the audit row and the state change commit or roll back together, and the
#: code that knows *what* happened writes the row. Maps handler name to the
#: function responsible, and the test checks that function really does audit, so
#: this cannot become a way to wave a route through.
AUDITS_IN_SERVICE = {
    "login": "authenticate",
    "set_own_password": "change_password",
}

#: State-changing handlers that legitimately leave no trail. Empty, and worth
#: keeping that way — every entry is a question an auditor cannot get an answer
#: to. Add one only with a reason that survives being read aloud.
NO_AUDIT_NEEDED: dict[str, str] = {}


def _audits_directly(node: ast.AST) -> bool:
    """Whether this function's own body calls ``<something>.audit.record(...)``."""
    for sub in ast.walk(node):
        if not (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)):
            continue
        if sub.func.attr != "record":
            continue
        owner = sub.func.value
        if isinstance(owner, ast.Attribute) and owner.attr == "audit":
            return True
    return False


def test_every_state_changing_route_leaves_an_audit_trail():
    """ARCHITECTURE §9 — "who changed this figure?" must always have an answer.

    The audit log is what makes this platform defensible to the people who audit
    Coal India rather than merely useful to the people who run it. A route that
    mutates state without writing a row converts "the trail is complete" into
    "the trail is complete except where someone forgot".

    Derived from the routes rather than written out — the opposite choice from
    tests/test_authorization_matrix.py, and deliberately so. There, a second
    independent statement of the rules is the point. Here the point is that a
    *new* route fails this test until somebody decides whether it audits, so the
    list has to be discovered rather than maintained.

    It found one: ``POST /conflicts/detect`` audited nothing, while deleting
    every unresolved conflict in the caller's scope that no longer met the
    materiality threshold — a threshold the caller supplied. See
    tests/test_conflict_radar.py.
    """
    mutating = {"post", "put", "patch", "delete"}
    offenders: list[str] = []
    checked = 0

    for file in sorted((PACKAGE / "api").glob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            methods = [
                decorator.func.attr
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in mutating
            ]
            if not methods:
                continue
            checked += 1
            if _audits_directly(node):
                continue
            if node.name in NO_AUDIT_NEEDED or node.name in AUDITS_IN_SERVICE:
                continue
            offenders.append(f"{file.name}:{node.name} ({methods[0].upper()})")

    assert checked >= 15, (
        f"only found {checked} state-changing routes, so this test has stopped "
        "looking at the real thing"
    )
    assert offenders == [], (
        "These routes change state and write no audit row:\n  "
        + "\n  ".join(offenders)
        + "\nEither call store.audit.record(...), or declare the route in "
        "AUDITS_IN_SERVICE if a service function audits inside the same "
        "transaction, or in NO_AUDIT_NEEDED with a reason."
    )


def test_the_routes_that_delegate_auditing_really_do_audit_somewhere():
    """``AUDITS_IN_SERVICE`` is an exemption, so it has to be checked.

    Otherwise the entry outlives the function it points at: someone refactors
    the service, the audit call goes with it, and the route is still waved
    through by a dictionary nobody re-read.
    """
    auditing: set[str] = set()
    for file in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef
            ) and _audits_directly(node):
                auditing.add(node.name)

    missing = {
        handler: service
        for handler, service in AUDITS_IN_SERVICE.items()
        if service not in auditing
    }
    assert missing == {}, (
        f"These routes are exempted because a service audits for them, but that "
        f"function no longer contains an audit.record call: {missing}"
    )


def test_no_application_code_can_disable_the_audit_trigger():
    """ARCHITECTURE §9 — the audit log is append-only, with no exceptions.

    The guarantee is a database trigger that binds even the table owner, so the
    only way to defeat it is to turn it off. This test asserts that no module
    under ``mrip/`` does, which is what turns §9 from "append-only, except where
    a repository method decides otherwise" into "append-only".

    It exists because there *was* such a method: ``Store.reset()`` truncated every
    table and disabled this trigger to do it. It had no callers — tests roll their
    transaction back instead — and its only guard was ``MRIP_PROFILE``, a
    client-side setting that defaults to ``dev``. A process pointed at a real
    database with the profile unset would have wiped the one record the
    architecture promises is immutable.

    Tests may still make the exception locally and visibly (one does, to clean up
    rows it deliberately committed outside a transaction). Shipping code may not.
    """
    offenders: list[str] = []
    for file in sorted(PACKAGE.rglob("*.py")):
        source = file.read_text(encoding="utf-8")
        lowered = source.lower()
        if "disable trigger" in lowered or "alter table audit_log" in lowered:
            offenders.append(str(file.relative_to(API_ROOT)))

    assert offenders == [], (
        "These modules can disable the audit log's append-only trigger:\n  "
        + "\n  ".join(offenders)
        + "\nThe trigger is the whole of the §9 guarantee. Correct an audit record "
        "with a new compensating entry, never by editing or removing one."
    )


def test_the_audit_log_really_refuses_to_be_rewritten(store):
    """The trigger itself, not just the absence of code that disables it.

    Asserted against a live database because the guarantee is the database's, and
    a migration that dropped the trigger would otherwise pass every other test in
    the suite.
    """
    import sqlalchemy as sa
    from sqlalchemy.exc import DatabaseError

    from mrip.db.tables import audit_log

    store.audit.record("architecture.probe", actor_username="probe")
    recorded = store.audit.recent(limit=1, action="architecture.probe")
    assert recorded, "the probe row was not written, so this test proves nothing"

    with pytest.raises(DatabaseError, match="append-only"):
        store.connection.execute(
            sa.update(audit_log)
            .where(audit_log.c.action == "architecture.probe")
            .values(action="architecture.tampered")
        )
