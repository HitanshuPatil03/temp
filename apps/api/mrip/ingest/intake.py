"""The upload boundary.

Everything in this module runs **before** a file is accepted, and it is the only
place in the platform that looks at bytes it has not already vouched for. The
rule it enforces is narrow and absolute: *a file is what its bytes say it is, and
it is accepted only if we can state what will happen to it.*

Four refusals, each for a failure this system would otherwise have:

**The declared type is ignored.** Both the filename and the ``Content-Type``
header are chosen by the uploader. A ``.pdf`` that begins ``PK\\x03\\x04`` is a
zip, and handing it to a PDF parser is how a parser bug becomes an exploit. The
class is decided by the magic bytes, and a mismatch is *recorded* — a report
named ``.xlsx`` that is really a PDF is usually a mistake worth telling someone
about, not an attack.

**An encrypted PDF is refused, not attempted.** It cannot be read, so every later
stage would produce an empty document that looks successfully ingested. A
reporting system whose corpus silently contains empty documents is worse than one
that refused the file.

**Active content is stripped.** ``/OpenAction``, ``/AA``, ``/JavaScript`` and
embedded files do nothing for extraction and exist only to run when a document is
opened — including in a reviewer's browser, where the evidence viewer will show
this page. The sanitized bytes are what get stored.

**Archives are measured before they are opened.** A 42 kB zip that expands to
4 GB is a denial of service against the worker pool, and the ratio is knowable
from the central directory without decompressing a byte.

The output is an :class:`IntakeReport`: what this file is, how many pages, what
was stripped, and either an accepted class or a refusal with a reason a person
can act on. Nothing here writes to the database — the caller decides what to do
with the verdict.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from mrip import log
from mrip.schemas import DocumentClass

__all__ = [
    "MAX_ARCHIVE_ENTRIES",
    "MAX_EXPANSION_RATIO",
    "IntakeError",
    "IntakeReport",
    "detect_class",
    "inspect_upload",
    "sniff",
]

logger = log.get_logger("mrip.intake")

#: Refuse an archive that expands to more than this multiple of its own size.
#: A legitimate zip of PDFs compresses to roughly 1:1 (they are already
#: compressed); a spreadsheet-heavy archive might reach 20:1. A hundredfold is
#: well past anything a coal-ministry filing produces and well short of what a
#: bomb needs.
MAX_EXPANSION_RATIO: Final = 100

#: And refuse one with implausibly many members, since a bomb can also be wide
#: rather than deep.
MAX_ARCHIVE_ENTRIES: Final = 2_000

#: Magic-number table. Ordered longest-first so a prefix cannot shadow a longer
#: signature.
_SIGNATURES: Final[tuple[tuple[bytes, str], ...]] = (
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),  # xlsx/docx/pptx are zips; refined below
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/vnd.ms-excel"),  # legacy OLE
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# Spelled once: these media types are long enough that repeating them makes the
# tables below unreadable.
_XLSX: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DOCX: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX: Final = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

#: Inside an OOXML zip, the part that says which application owns it.
_OOXML_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    ("xl/workbook.xml", _XLSX),
    ("word/document.xml", _DOCX),
    ("ppt/presentation.xml", _PPTX),
)

_CLASS_BY_TYPE: Final[dict[str, DocumentClass]] = {
    "application/pdf": DocumentClass.TEXT_PDF,  # refined by the page scan
    _XLSX: DocumentClass.SPREADSHEET,
    "application/vnd.ms-excel": DocumentClass.SPREADSHEET,
    _DOCX: DocumentClass.DOCX,
    "image/png": DocumentClass.IMAGE,
    "image/jpeg": DocumentClass.IMAGE,
    "image/tiff": DocumentClass.IMAGE,
    "image/gif": DocumentClass.IMAGE,
}

#: PDF keys that exist to *do* something when a document is opened. None of them
#: carry information this platform extracts.
_ACTIVE_PDF_KEYS: Final = (
    "OpenAction",
    "AA",
    "JavaScript",
    "JS",
    "Launch",
    "EmbeddedFile",
)


class IntakeError(Exception):
    """A file that will not be accepted, with a reason meant for a person.

    Carries a machine-readable ``code`` as well, because the upload endpoint
    turns these into HTTP responses and the frontend renders different help for
    "encrypted" than for "too large".
    """

    def __init__(
        self, code: str, message: str, *, detail: dict[str, object] | None = None
    ):
        self.code = code
        self.detail = detail or {}
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class IntakeReport:
    """What the boundary concluded about a file."""

    media_type: str
    doc_class: DocumentClass
    size_bytes: int
    page_count: int | None = None
    #: True when the extension disagrees with the bytes. Not fatal — usually a
    #: rename — but recorded on the document and shown to the reviewer.
    extension_mismatch: bool = False
    #: Names of the active-content keys removed, if any.
    stripped: tuple[str, ...] = ()
    #: Set when the file was rewritten (active content removed); the caller must
    #: store *these* bytes, not the ones it received.
    sanitized_path: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def was_sanitized(self) -> bool:
        return bool(self.stripped)


def sniff(head: bytes) -> str:
    """Media type from the leading bytes. ``application/octet-stream`` if unknown."""
    for signature, media_type in _SIGNATURES:
        if head.startswith(signature):
            return media_type
    return "application/octet-stream"


def _refine_zip(path: Path) -> str:
    """Tell an OOXML document from a plain archive by reading its manifest."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as bad:
        raise IntakeError(
            "corrupt_archive",
            "This file starts like a zip archive but its directory cannot be read. "
            "It is likely truncated — try uploading it again.",
        ) from bad

    for marker, media_type in _OOXML_MARKERS:
        if marker in names:
            return media_type
    return "application/zip"


def detect_class(media_type: str) -> DocumentClass:
    return _CLASS_BY_TYPE.get(media_type, DocumentClass.UNKNOWN)


def _check_archive_safety(path: Path) -> None:
    """Measure an archive without decompressing it.

    The central directory records both sizes, so a bomb is detectable for the
    cost of reading a few kilobytes. Also refuses absolute and traversing member
    paths — "zip slip" — even though nothing here extracts to disk yet, because
    the check belongs with the other boundary rules rather than with whoever adds
    extraction later.
    """
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise IntakeError(
                "archive_too_many_entries",
                f"This archive holds {len(entries):,} files; the limit is "
                f"{MAX_ARCHIVE_ENTRIES:,}. Split it, or upload the documents "
                "individually.",
                detail={"entries": len(entries)},
            )

        declared = sum(entry.file_size for entry in entries)
        compressed = max(1, sum(entry.compress_size for entry in entries))
        ratio = declared / compressed
        if ratio > MAX_EXPANSION_RATIO:
            raise IntakeError(
                "archive_expansion_ratio",
                f"This archive expands {ratio:,.0f}× ({declared / 1e6:,.0f} MB from "
                f"{compressed / 1e6:,.1f} MB), past the {MAX_EXPANSION_RATIO}× limit. "
                "That pattern is a decompression bomb rather than a filing.",
                detail={"ratio": round(ratio, 1), "expanded_bytes": declared},
            )

        for entry in entries:
            name = entry.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise IntakeError(
                    "archive_unsafe_path",
                    f"This archive contains a member with an unsafe path "
                    f"({name!r}). Archives that escape their own directory are "
                    "refused.",
                    detail={"member": name},
                )


def _inspect_pdf(path: Path, *, max_pages: int) -> tuple[int, tuple[str, ...], bool]:
    """Page count, active content found, and whether the file was rewritten.

    Opening a PDF is itself parsing untrusted input, which is why it happens here
    — once, at the boundary, on a file that has already passed the size cap —
    rather than in a worker holding a database transaction.
    """
    import pymupdf

    try:
        document = pymupdf.open(path)
    except Exception as broken:  # pymupdf raises several unrelated types
        raise IntakeError(
            "unreadable_pdf",
            "This PDF cannot be opened. It is likely truncated or corrupt; "
            f"the parser said: {broken}",
        ) from broken

    with document:
        if document.needs_pass:
            raise IntakeError(
                "encrypted_pdf",
                "This PDF is password-protected, so no text or table can be read "
                "from it. Upload an unprotected copy — an encrypted file would be "
                "ingested as an empty document, which is worse than refusing it.",
            )

        page_count = document.page_count
        if page_count == 0:
            # PyMuPDF repairs damaged files, which is what we want for a
            # slightly corrupt annual report. Zero pages after repair means
            # there was nothing to recover, and an empty document in the corpus
            # is indistinguishable from one that was ingested successfully.
            raise IntakeError(
                "empty_pdf",
                "This PDF has no readable pages. It is either corrupt beyond "
                "repair or not really a PDF.",
            )
        if page_count > max_pages:
            raise IntakeError(
                "too_many_pages",
                f"This PDF has {page_count:,} pages; the limit is {max_pages:,}. "
                "Split it, or raise MRIP_MAX_UPLOAD_PAGES if the deployment can "
                "afford the processing time.",
                detail={"pages": page_count, "limit": max_pages},
            )

        found = _find_active_content(document)
        if not found:
            return page_count, (), False

        # Rewrite with the active content removed. Two steps, because neither is
        # sufficient alone:
        #
        # `scrub` is PyMuPDF's own sanitizer and handles the name trees and
        # embedded-file streams — but it leaves `/OpenAction` sitting in the
        # catalogue, which a test here caught. So the catalogue keys are then
        # nulled explicitly, and the result is re-read to confirm: a sanitizer
        # that merely *reports* having cleaned a file is worse than none, because
        # the reviewer opens the page believing it is safe.
        document.scrub(attached_files=True, embedded_files=True, javascript=True)
        catalogue_xref = document.pdf_catalog()
        for key in document.xref_get_keys(catalogue_xref):
            if key in _ACTIVE_PDF_KEYS:
                document.xref_set_key(catalogue_xref, key, "null")

        remaining = _find_active_content(document)
        if remaining:
            raise IntakeError(
                "active_content_not_removable",
                "This PDF carries active content ("
                + ", ".join(f"/{key}" for key in remaining)
                + ") that could not be removed, so it is refused rather than "
                "stored. Print it to a new PDF and upload that.",
                detail={"remaining": list(remaining)},
            )

        cleaned = path.with_suffix(path.suffix + ".clean")
        document.save(cleaned, garbage=3, deflate=True)

    # Outside the `with`: the rename has to happen after the handle is closed,
    # or Windows refuses to replace a file that is still open.
    cleaned.replace(path)
    return page_count, found, True


def _find_active_content(document: object) -> tuple[str, ...]:
    """Active-content constructs actually present in a PDF's catalogue.

    Read key by key rather than by scanning the catalogue's source text. The
    difference matters after sanitizing: removing a key sets it to ``null``, and
    the string ``/OpenAction`` is still *in* the source at that point — a
    substring check would report a file as still dangerous forever, and a
    re-check built on one would either fail every sanitized file or be quietly
    dropped. The typed read distinguishes "present" from "present and null".
    """
    import pymupdf

    assert isinstance(document, pymupdf.Document)
    catalogue = document.pdf_catalog()
    if not catalogue:  # not a PDF catalogue we can read
        return ()

    present: list[str] = []
    for key in document.xref_get_keys(catalogue):
        if key not in _ACTIVE_PDF_KEYS:
            continue
        kind, _value = document.xref_get_key(catalogue, key)
        if kind != "null":
            present.append(key)

    # The document-level JavaScript name tree hangs off /Names rather than being
    # a catalogue key of its own.
    kind, value = document.xref_get_key(catalogue, "Names")
    if kind != "null" and "/JavaScript" in str(value):
        present.append("Names/JavaScript")

    if document.embfile_count():
        present.append("EmbeddedFile")

    return tuple(present)


def _inspect_spreadsheet(path: Path) -> int | None:
    """Sheet count, and a refusal for a workbook openpyxl cannot parse."""
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as broken:
        raise IntakeError(
            "unreadable_spreadsheet",
            f"This workbook cannot be opened: {broken}. If it was exported from "
            "an old system, re-save it as .xlsx and try again.",
        ) from broken
    try:
        return len(workbook.sheetnames)
    finally:
        workbook.close()


def inspect_upload(
    path: Path,
    *,
    filename: str,
    max_bytes: int,
    max_pages: int,
) -> IntakeReport:
    """Judge an uploaded file. Raises :class:`IntakeError` to refuse it.

    ``path`` is a staged file on disk, not a stream: the size cap has to be
    enforced while receiving, and every check here needs random access. The file
    may be **rewritten in place** when active content is stripped, so the caller
    must hash *after* this returns — which is also what makes the content hash
    the hash of what was stored rather than of what was sent.
    """
    size = path.stat().st_size
    if size == 0:
        raise IntakeError("empty_file", "This file is empty.")
    if size > max_bytes:
        raise IntakeError(
            "too_large",
            f"This file is {size / 1e6:,.1f} MB; the limit is {max_bytes / 1e6:,.0f} MB.",
            detail={"size_bytes": size, "limit_bytes": max_bytes},
        )

    with path.open("rb") as handle:
        head = handle.read(4096)

    media_type = sniff(head)
    if media_type == "application/zip":
        media_type = _refine_zip(path)
        if media_type == "application/zip":
            _check_archive_safety(path)

    notes: list[str] = []
    suffix = Path(filename).suffix.lower()
    expected = _EXPECTED_SUFFIXES.get(media_type, ())
    mismatch = bool(suffix) and bool(expected) and suffix not in expected
    if mismatch:
        notes.append(
            f"Named {suffix!r} but the contents are {media_type}. The contents "
            "decide how it is processed."
        )

    page_count: int | None = None
    stripped: tuple[str, ...] = ()
    sanitized: Path | None = None

    if media_type == "application/pdf":
        page_count, stripped, rewritten = _inspect_pdf(path, max_pages=max_pages)
        if rewritten:
            sanitized = path
            notes.append(
                "Active content was removed before storage: "
                + ", ".join(f"/{key}" for key in stripped)
            )
    elif (
        media_type.endswith("spreadsheetml.sheet")
        or media_type == "application/vnd.ms-excel"
    ):
        page_count = _inspect_spreadsheet(path)

    doc_class = detect_class(media_type)
    if doc_class is DocumentClass.UNKNOWN:
        raise IntakeError(
            "unsupported_type",
            f"This file is {media_type}, which MRIP does not extract figures from. "
            "Supported: PDF, Excel, Word and images.",
            detail={"media_type": media_type},
        )

    logger.info(
        "upload inspected",
        media_type=media_type,
        doc_class=doc_class.value,
        size_bytes=size,
        pages=page_count,
        stripped=list(stripped),
        extension_mismatch=mismatch,
    )
    return IntakeReport(
        media_type=media_type,
        doc_class=doc_class,
        size_bytes=path.stat().st_size,  # re-read: sanitizing changes it
        page_count=page_count,
        extension_mismatch=mismatch,
        stripped=stripped,
        sanitized_path=sanitized,
        notes=notes,
    )


_EXPECTED_SUFFIXES: Final[dict[str, tuple[str, ...]]] = {
    "application/pdf": (".pdf",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        ".xlsx",
        ".xlsm",
    ),
    "application/vnd.ms-excel": (".xls",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx",),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/tiff": (".tif", ".tiff"),
    "image/gif": (".gif",),
}
