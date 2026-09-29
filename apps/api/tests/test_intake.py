"""Tests for the upload boundary.

Every test here builds its own hostile file rather than reading a fixture, so
what is being refused is visible in the test itself: an encrypted PDF, a PDF
carrying an `/OpenAction`, a zip whose central directory claims a gigabyte, a
spreadsheet renamed to `.pdf`.

The assertions are about the *reason* as much as the refusal. A boundary that
rejects everything with "invalid file" is a boundary that generates support
tickets; these check that the message names what was wrong and what to do.
"""

from __future__ import annotations

import zipfile

import pymupdf
import pytest

from mrip.ingest.intake import (
    MAX_EXPANSION_RATIO,
    IntakeError,
    inspect_upload,
    sniff,
)
from mrip.schemas import DocumentClass

LIMITS = {"max_bytes": 50_000_000, "max_pages": 5_000}


def make_pdf(path, *, pages: int = 2, text: str = "Coal production 193.00 MT") -> None:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page()
        page.insert_text((72, 100), f"{text} — page {index + 1}")
    document.save(path)
    document.close()


def inspect(path, filename: str | None = None):
    return inspect_upload(path, filename=filename or path.name, **LIMITS)


# ----------------------------------------------------------------- sniffing


def test_the_class_comes_from_the_bytes_not_the_name(tmp_path):
    """The filename is chosen by the uploader; the magic number is not."""
    disguised = tmp_path / "annual-report.xlsx"
    make_pdf(disguised)

    report = inspect(disguised)

    assert report.media_type == "application/pdf"
    assert report.doc_class is DocumentClass.TEXT_PDF
    assert report.extension_mismatch is True
    assert "contents decide" in " ".join(report.notes)


def test_a_correctly_named_file_is_not_flagged(tmp_path):
    path = tmp_path / "report.pdf"
    make_pdf(path)
    assert inspect(path).extension_mismatch is False


def test_sniff_knows_the_formats_this_platform_accepts():
    assert sniff(b"%PDF-1.7\n") == "application/pdf"
    assert sniff(b"PK\x03\x04rest") == "application/zip"
    assert sniff(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert sniff(b"nothing recognisable") == "application/octet-stream"


def test_an_xlsx_is_recognised_through_its_manifest(tmp_path):
    """OOXML files are zips; the class comes from the part they contain."""
    import openpyxl

    path = tmp_path / "production.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(["entity", "production_mt"])
    workbook.active.append(["SECL", 193.0])
    workbook.save(path)

    report = inspect(path)
    assert report.doc_class is DocumentClass.SPREADSHEET
    assert report.page_count == 1, "sheet count stands in for pages"


# ------------------------------------------------------------------- limits


def test_an_empty_file_is_refused(tmp_path):
    path = tmp_path / "nothing.pdf"
    path.write_bytes(b"")
    with pytest.raises(IntakeError, match="empty"):
        inspect(path)


def test_a_file_over_the_size_cap_is_refused_before_parsing(tmp_path):
    path = tmp_path / "big.pdf"
    make_pdf(path)

    with pytest.raises(IntakeError) as raised:
        inspect_upload(path, filename="big.pdf", max_bytes=10, max_pages=5_000)

    assert raised.value.code == "too_large"
    assert "limit" in str(raised.value)


def test_a_document_over_the_page_cap_is_refused(tmp_path):
    path = tmp_path / "long.pdf"
    make_pdf(path, pages=12)

    with pytest.raises(IntakeError) as raised:
        inspect_upload(path, filename="long.pdf", max_bytes=50_000_000, max_pages=10)

    assert raised.value.code == "too_many_pages"
    assert raised.value.detail["pages"] == 12


# --------------------------------------------------------------------- PDFs


def test_an_encrypted_pdf_is_refused_rather_than_ingested_empty(tmp_path):
    """The failure this prevents: a corpus that silently contains empty
    documents, each of which looks successfully ingested."""
    path = tmp_path / "locked.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 100), "secret")
    document.save(
        path,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    document.close()

    with pytest.raises(IntakeError) as raised:
        inspect(path)

    assert raised.value.code == "encrypted_pdf"
    assert "unprotected copy" in str(raised.value)


def test_active_content_is_stripped_and_reported(tmp_path):
    """`/OpenAction` runs when the document is opened — including in the
    reviewer's browser, where the evidence viewer renders this page."""
    path = tmp_path / "active.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 100), "Production 193.00 MT")
    # Written straight into the catalogue, which is how a real one arrives:
    # open this file in any viewer and the script runs.
    document.xref_set_key(
        document.pdf_catalog(),
        "OpenAction",
        r"<</S/JavaScript/JS(app.alert\(1\))>>",
    )
    document.save(path)
    document.close()

    report = inspect(path)

    assert "OpenAction" in report.stripped
    assert report.was_sanitized is True
    assert report.sanitized_path == path
    assert "Active content was removed" in " ".join(report.notes)

    # And it is genuinely gone from the stored bytes, not merely noted. Checked
    # by reading the key's *type*: sanitizing nulls the entry, so the literal
    # string "/OpenAction" can still appear in the catalogue's source while the
    # action itself is gone.
    with pymupdf.open(path) as cleaned:
        catalogue = cleaned.pdf_catalog()
        kind, _ = cleaned.xref_get_key(catalogue, "OpenAction")
        assert kind == "null"
        assert "JavaScript" not in cleaned.xref_object(catalogue, compressed=True)


def test_a_clean_pdf_is_not_rewritten(tmp_path):
    """Sanitizing changes the bytes, and therefore the content hash. A file with
    nothing to strip must be stored exactly as it arrived."""
    path = tmp_path / "clean.pdf"
    make_pdf(path)
    before = path.read_bytes()

    report = inspect(path)

    assert report.stripped == ()
    assert report.sanitized_path is None
    assert path.read_bytes() == before


def test_a_pdf_with_nothing_readable_in_it_is_refused(tmp_path):
    """PyMuPDF repairs damaged PDFs, which is what we want for a slightly
    corrupt annual report — a recoverable file should still be ingested. The
    line is drawn where recovery yields *no pages*: there is nothing to extract,
    so accepting it would add an empty document to the corpus."""
    path = tmp_path / "rubble.pdf"
    path.write_bytes(b"%PDF-1.7\n" + b"not actually a pdf body " * 20)

    with pytest.raises(IntakeError) as raised:
        inspect(path)

    assert raised.value.code in {"unreadable_pdf", "empty_pdf"}


def test_the_page_count_is_read_at_the_boundary(tmp_path):
    path = tmp_path / "three.pdf"
    make_pdf(path, pages=3)
    assert inspect(path).page_count == 3


# ----------------------------------------------------------------- archives


def test_a_decompression_bomb_is_refused_without_being_expanded(tmp_path):
    """Measured from the central directory: the ratio is knowable without
    decompressing a byte, which is the only safe way to ask."""
    path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # Highly compressible: 40 MB of zeros lands in a few dozen kilobytes.
        archive.writestr("payload.bin", b"\x00" * (40 * 1024 * 1024))

    with pytest.raises(IntakeError) as raised:
        inspect(path)

    assert raised.value.code == "archive_expansion_ratio"
    assert raised.value.detail["ratio"] > MAX_EXPANSION_RATIO


def test_an_archive_member_that_escapes_its_directory_is_refused(tmp_path):
    path = tmp_path / "slip.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../../etc/cron.d/backdoor", "* * * * * root sh")

    with pytest.raises(IntakeError) as raised:
        inspect(path)

    assert raised.value.code == "archive_unsafe_path"


def test_a_corrupt_zip_is_refused_with_a_readable_reason(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"PK\x03\x04" + b"garbage" * 40)

    with pytest.raises(IntakeError) as raised:
        inspect(path)

    assert raised.value.code == "corrupt_archive"
    assert "truncated" in str(raised.value)


def test_a_plain_archive_is_not_an_extractable_document(tmp_path):
    """A safe zip still is not something figures can be read from, so it is
    refused with that reason rather than accepted into a pipeline that would
    produce nothing."""
    path = tmp_path / "filings.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "two reports inside")

    with pytest.raises(IntakeError) as raised:
        inspect(path)

    assert raised.value.code == "unsupported_type"


# --------------------------------------------------------------- other types


def test_an_unsupported_type_is_named_in_the_refusal(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("SECL produced 193 MT", encoding="utf-8")

    with pytest.raises(IntakeError) as raised:
        inspect(path)

    assert raised.value.code == "unsupported_type"
    assert "Supported" in str(raised.value)


def test_an_image_is_accepted_for_the_ocr_path(tmp_path):
    """A photographed table is a real CIL input; it is accepted here and routed
    to OCR rather than refused."""
    path = tmp_path / "scan.png"
    path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        )
    )

    assert inspect(path).doc_class is DocumentClass.IMAGE
