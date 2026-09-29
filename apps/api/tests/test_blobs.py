"""Tests for the blob store.

The store's docstring makes four claims — content-addressed, atomic, immutable,
streaming. Each test below is written so that the plausible wrong implementation
fails it: a store that trusted the caller's hash, or overwrote on collision, or
left a partial file at the real name, or read the whole upload into memory.
"""

from __future__ import annotations

import hashlib
import io

import pytest

from mrip.blobs import (
    BlobNotFoundError,
    ContentHashMismatchError,
    FilesystemBlobStore,
    copy_stream,
    hash_bytes,
    hash_stream,
)

PDF = b"%PDF-1.7\nCoal India Limited Annual Report 2023-24\n%%EOF\n"
PDF_HASH = hashlib.sha256(PDF).hexdigest()


@pytest.fixture
def store(tmp_path) -> FilesystemBlobStore:
    return FilesystemBlobStore(tmp_path / "blobs")


# ------------------------------------------------------------ content addressing


def test_the_key_is_the_hash_of_the_content(store):
    ref = store.put(PDF)

    assert ref.content_hash == PDF_HASH
    assert ref.size_bytes == len(PDF)
    assert ref.created is True
    assert store.read(PDF_HASH) == PDF


def test_blobs_fan_out_two_levels(store):
    """A flat directory of a million files is a restore that takes a weekend."""
    store.put(PDF)
    path = store.path_for(PDF_HASH)

    assert path.parent.name == PDF_HASH[2:4]
    assert path.parent.parent.name == PDF_HASH[:2]
    assert path.name == PDF_HASH


def test_a_key_that_is_not_a_digest_is_refused(store):
    """This value reaches the filesystem, so its shape is not a formality."""
    for bad in ("../../etc/passwd", "", "ZZ" * 32, PDF_HASH[:-1]):
        with pytest.raises(ValueError, match="SHA-256"):
            store.path_for(bad)


def test_hash_helpers_agree(store):
    digest, size = hash_stream(io.BytesIO(PDF))
    assert digest == hash_bytes(PDF) == PDF_HASH
    assert size == len(PDF)


# -------------------------------------------------------------------- integrity


def test_a_promised_hash_is_verified_not_trusted(store):
    """An upload truncated in flight must not be admitted as evidence."""
    wrong = hash_bytes(b"a different document entirely")

    with pytest.raises(ContentHashMismatchError, match="truncated or altered"):
        store.put(PDF, expected_hash=wrong)

    assert not store.exists(wrong), "nothing may be stored under a hash that failed"
    assert not store.exists(PDF_HASH), "and not under the real hash either"


def test_a_matching_promised_hash_is_accepted(store):
    assert store.put(PDF, expected_hash=PDF_HASH.upper()).created is True


def test_verify_re_hashes_the_stored_bytes(store):
    """Bit rot and a bad restore are both detectable without a second copy."""
    store.put(PDF)
    assert store.verify(PDF_HASH) is True

    # Corrupt it in place, as a failing disk would.
    path = store.path_for(PDF_HASH)
    path.chmod(0o640)
    path.write_bytes(PDF.replace(b"2023-24", b"2024-25"))

    assert store.verify(PDF_HASH) is False


def test_a_failed_put_leaves_no_partial_file(store):
    """The temporary file is cleaned up; the blob tree stays clean."""
    with pytest.raises(ContentHashMismatchError):
        store.put(PDF, expected_hash=hash_bytes(b"nope"))

    leftovers = list((store.root / "_incoming").glob("*.part"))
    assert leftovers == []
    assert store.usage_bytes() == 0


# ------------------------------------------------------------------ immutability


def test_storing_identical_content_twice_is_one_blob(store):
    """What makes upload idempotent: a retried upload is not a second document."""
    first = store.put(PDF)
    second = store.put(io.BytesIO(PDF))

    assert first.content_hash == second.content_hash
    assert first.created is True
    assert second.created is False, "the second call stored nothing new"
    assert store.usage_bytes() == len(PDF), "and did not double the bytes on disk"


def test_a_stored_blob_is_not_writable(store):
    """Nothing in this platform edits a source document."""
    store.put(PDF)
    with pytest.raises(PermissionError):
        store.path_for(PDF_HASH).open("ab")


# ---------------------------------------------------------------------- reading


def test_reading_a_missing_blob_raises(store):
    missing = hash_bytes(b"never stored")

    assert store.exists(missing) is False
    with pytest.raises(BlobNotFoundError):
        store.open(missing)
    with pytest.raises(BlobNotFoundError):
        store.size(missing)


def test_size_is_reported_without_reading(store):
    store.put(PDF)
    assert store.size(PDF_HASH) == len(PDF)


# -------------------------------------------------------------------- streaming


def test_a_large_upload_is_streamed_in_chunks(store):
    """The read pattern is the test: a source that refuses a single big read
    still stores, which is what proves nothing slurps the whole file."""
    payload = b"x" * (3 * 1024 * 1024 + 17)

    class ChunkedOnly(io.RawIOBase):
        """Rejects reads larger than one chunk, as a socket would."""

        def __init__(self) -> None:
            self._buffer = io.BytesIO(payload)

        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            if size < 0 or size > 1024 * 1024:
                raise AssertionError("the store must read in bounded chunks")
            return self._buffer.read(size)

    ref = store.put(ChunkedOnly())  # type: ignore[arg-type]

    assert ref.size_bytes == len(payload)
    assert ref.content_hash == hash_bytes(payload)


def test_copy_stream_hashes_on_the_way_past(tmp_path):
    """Upload staging: written once, hashed once, never read twice."""
    staged = tmp_path / "staging" / "upload.bin"

    digest, size = copy_stream(io.BytesIO(PDF), staged)

    assert digest == PDF_HASH
    assert size == len(PDF)
    assert staged.read_bytes() == PDF


def test_put_file_accepts_a_staged_upload(store, tmp_path):
    staged = tmp_path / "staged.pdf"
    staged.write_bytes(PDF)

    ref = store.put_file(staged, expected_hash=PDF_HASH)

    assert ref.content_hash == PDF_HASH
    assert store.exists(PDF_HASH)


# ------------------------------------------------------------------ maintenance


def test_discard_removes_a_blob_and_reports_whether_it_existed(store):
    """The only deletion path: quarantine, not garbage collection."""
    store.put(PDF)

    assert store.discard(PDF_HASH, reason="malware quarantine") is True
    assert store.exists(PDF_HASH) is False
    assert store.discard(PDF_HASH, reason="again") is False


def test_sweeping_incoming_removes_only_old_partials(store):
    import os
    import time

    staging = store.root / "_incoming"
    staging.mkdir(parents=True, exist_ok=True)
    fresh = staging / "put_fresh.part"
    stale = staging / "put_stale.part"
    fresh.write_bytes(b"in progress")
    stale.write_bytes(b"abandoned by a killed worker")
    old = time.time() - 200_000
    os.utime(stale, (old, old))

    assert store.sweep_incoming(older_than_seconds=86_400) == 1
    assert fresh.exists()
    assert not stale.exists()


def test_usage_excludes_partial_writes(store):
    store.put(PDF)
    (store.root / "_incoming").mkdir(parents=True, exist_ok=True)
    (store.root / "_incoming" / "put_x.part").write_bytes(b"y" * 999)

    assert store.usage_bytes() == len(PDF)
