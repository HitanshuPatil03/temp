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
    "mrip/validate",
    "mrip/query/exact.py",
    "mrip/query/compare.py",
    "mrip/reports/render.py",
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
