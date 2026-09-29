"""The blob store: where source documents live.

The documents are the evidence. Every figure this platform reports resolves to a
page in one of these files, so the store has one job beyond "keep bytes": make it
impossible to serve bytes that are not the bytes that were ingested.

Four properties, each tested in ``tests/test_blobs.py``:

**Content-addressed.** The key *is* the SHA-256 of the contents. Two uploads of
one file collapse to one blob, which is what makes the upload path idempotent
without a lock, and a blob can be re-verified at any time against its own name.

**Atomic.** Bytes are written to a temporary file on the same filesystem and
``os.replace``d into place. A power cut mid-write leaves the temporary file
behind, never a half-written blob at a name that promises a full one — a
truncated PDF would parse, extract fewer facts, and look like a smaller report.

**Immutable.** A ``put`` of content that already exists is a no-op returning
``created=False``. There is no code path that overwrites a blob, because
"identical name, different bytes" is a contradiction this store cannot represent.

**Streaming.** Hashing and writing happen in one pass over 1 MiB chunks. A
400-page scanned annual report is a few hundred megabytes and must never be held
in memory twice, once by the request parser and once by us.

The port is deliberately narrow — five methods, no partial reads, no metadata —
so an S3/MinIO implementation is a small class rather than a refactor. The
filesystem implementation is the one CIL will run: an on-prem deployment puts
this directory on the same backed-up volume as the database, and a blob store
that needed an object gateway would be one more thing to install offline.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

from mrip import log

__all__ = [
    "CHUNK_BYTES",
    "BlobNotFoundError",
    "BlobRef",
    "BlobStore",
    "ContentHashMismatchError",
    "FilesystemBlobStore",
    "hash_bytes",
    "hash_stream",
]

#: 1 MiB. Large enough that syscall overhead is irrelevant, small enough that a
#: dozen concurrent uploads do not add up to a memory problem.
CHUNK_BYTES = 1024 * 1024

logger = log.get_logger("mrip.blobs")


class BlobNotFoundError(KeyError):
    """No blob is stored under this content hash."""


class ContentHashMismatchError(ValueError):
    """The bytes received did not hash to the hash that was promised.

    Raised rather than stored. An upload that was truncated or altered in flight
    must not be admitted as evidence, and the caller's expectation is the only
    signal that it happened.
    """


@dataclass(frozen=True, slots=True)
class BlobRef:
    """Where bytes ended up, and whether this call is what put them there."""

    content_hash: str
    size_bytes: int
    #: False when identical content was already stored. Not an error: it is the
    #: mechanism behind idempotent upload.
    created: bool


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_stream(stream: BinaryIO) -> tuple[str, int]:
    """Hash a stream without loading it, returning ``(hex digest, size)``."""
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


@runtime_checkable
class BlobStore(Protocol):
    """The port. Content-addressed, immutable, no partial writes."""

    def put(
        self, source: bytes | BinaryIO, *, expected_hash: str | None = None
    ) -> BlobRef:
        """Store bytes and return their reference.

        ``expected_hash`` is verified, not trusted: a mismatch raises
        :class:`ContentHashMismatchError` and stores nothing.
        """
        ...

    def open(self, content_hash: str) -> BinaryIO:
        """Open a blob for reading. Raises :class:`BlobNotFoundError`."""
        ...

    def read(self, content_hash: str) -> bytes:
        """Read a whole blob. For small artefacts only."""
        ...

    def exists(self, content_hash: str) -> bool: ...

    def size(self, content_hash: str) -> int:
        """Size in bytes. Raises :class:`BlobNotFoundError`."""
        ...


class FilesystemBlobStore:
    """Blobs on a local or mounted filesystem, fanned out two levels.

    ``ab/cd/abcd…`` rather than one flat directory: a CIL-scale corpus is
    hundreds of thousands of files, and both ext4 and NFS degrade badly listing a
    directory that large. Two levels of 256 gives ~4k files per directory at a
    million blobs.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ layout

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, content_hash: str) -> Path:
        """The path a blob occupies. Also validates the hash's shape.

        Validation is not pedantry: this value reaches the filesystem, and
        ``../../etc/passwd`` is a perfectly good string. A content hash is 64 hex
        characters or it is not a content hash.
        """
        digest = content_hash.lower()
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise ValueError(
                f"Not a SHA-256 hex digest: {content_hash!r}. "
                "Blob keys are the 64-character hash of the content."
            )
        return self._root / digest[:2] / digest[2:4] / digest

    # ------------------------------------------------------------------ writes

    def put(
        self, source: bytes | BinaryIO, *, expected_hash: str | None = None
    ) -> BlobRef:
        # Written to a temp file in the destination's own directory, so the
        # final rename is on one filesystem and therefore atomic. A temp file in
        # /tmp would make this a copy across devices, which is not.
        staging = self._root / "_incoming"
        staging.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256()
        size = 0
        handle, temp_name = tempfile.mkstemp(dir=staging, prefix="put_", suffix=".part")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(handle, "wb") as out:
                if isinstance(source, bytes):
                    digest.update(source)
                    size = len(source)
                    out.write(source)
                else:
                    while chunk := source.read(CHUNK_BYTES):
                        digest.update(chunk)
                        size += len(chunk)
                        out.write(chunk)
                out.flush()
                # The rename is atomic, but only durable if the data reached the
                # disk first. Without this, a crash can leave a correctly-named
                # blob whose contents are zeroes.
                os.fsync(out.fileno())

            content_hash = digest.hexdigest()
            if expected_hash is not None and content_hash != expected_hash.lower():
                raise ContentHashMismatchError(
                    f"Content hashed to {content_hash} but {expected_hash.lower()} was "
                    "expected. The upload was truncated or altered in transit; "
                    "nothing was stored."
                )

            destination = self.path_for(content_hash)
            if destination.exists():
                # Identical content is already stored. Immutability means there
                # is nothing to do and nothing to decide.
                return BlobRef(content_hash, destination.stat().st_size, created=False)

            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_path.replace(destination)
            # Read-only: nothing in this platform edits a source document, and a
            # mode that says so turns a bug into an error instead of a silent
            # change to the evidence.
            destination.chmod(0o440)
            logger.info(
                "blob stored",
                content_hash=content_hash,
                size_bytes=size,
            )
            return BlobRef(content_hash, size, created=True)
        finally:
            temp_path.unlink(missing_ok=True)

    def put_file(self, path: Path, *, expected_hash: str | None = None) -> BlobRef:
        """Store a file already on disk (an upload that was streamed to staging)."""
        with path.open("rb") as stream:
            return self.put(stream, expected_hash=expected_hash)

    # ------------------------------------------------------------------- reads

    def open(self, content_hash: str) -> BinaryIO:
        path = self.path_for(content_hash)
        try:
            return path.open("rb")
        except FileNotFoundError:
            raise BlobNotFoundError(content_hash) from None

    def read(self, content_hash: str) -> bytes:
        with self.open(content_hash) as stream:
            return stream.read()

    def exists(self, content_hash: str) -> bool:
        return self.path_for(content_hash).is_file()

    def size(self, content_hash: str) -> int:
        try:
            return self.path_for(content_hash).stat().st_size
        except FileNotFoundError:
            raise BlobNotFoundError(content_hash) from None

    # -------------------------------------------------------------- integrity

    def verify(self, content_hash: str) -> bool:
        """Re-hash a stored blob and compare it to its own name.

        The point of content addressing: bit rot, a bad restore or a tampered
        volume are all detectable without a second copy of anything. Backs the
        Phase 8 integrity sweep.
        """
        with self.open(content_hash) as stream:
            actual, _ = hash_stream(stream)
        if actual != content_hash.lower():
            logger.error(
                "blob integrity failure",
                content_hash=content_hash,
                actual_hash=actual,
            )
            return False
        return True

    # ------------------------------------------------------------- quarantine

    def discard(self, content_hash: str, *, reason: str) -> bool:
        """Remove a blob. The only deletion path, and it is loud.

        Exists for one case: an upload that must not be retained (malware, or a
        document withdrawn under legal hold). It is not garbage collection —
        a blob whose document row is gone stays, because the document row can be
        restored from a backup and the bytes cannot be re-derived.
        """
        path = self.path_for(content_hash)
        if not path.is_file():
            return False
        path.chmod(0o640)
        path.unlink()
        logger.warning("blob discarded", content_hash=content_hash, reason=reason)
        return True

    # ------------------------------------------------------------ maintenance

    def sweep_incoming(self, *, older_than_seconds: float = 86_400) -> int:
        """Delete abandoned partial writes left by killed processes."""
        staging = self._root / "_incoming"
        if not staging.is_dir():
            return 0

        cutoff = time.time() - older_than_seconds
        removed = 0
        for leftover in staging.glob("put_*.part"):
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink(missing_ok=True)
                removed += 1
        return removed

    def usage_bytes(self) -> int:
        """Total bytes stored. Reported on the ops dashboard."""
        return sum(
            path.stat().st_size
            for path in self._root.rglob("*")
            if path.is_file() and path.parent.name != "_incoming"
        )

    def __repr__(self) -> str:
        return f"FilesystemBlobStore({str(self._root)!r})"


def get_blob_store() -> FilesystemBlobStore:
    """The process-wide blob store, rooted at ``MRIP_BLOB_DIR``."""
    from mrip.config import get_settings

    settings = get_settings()
    assert settings.blob_dir is not None  # derived in Settings
    return FilesystemBlobStore(settings.blob_dir)


def copy_stream(source: BinaryIO, destination: Path) -> tuple[str, int]:
    """Stream an upload to a staging path, returning ``(hash, size)``.

    Used by the upload route: the request body is written to disk once, hashed on
    the way past, and only then handed to the store — so a rejected upload never
    became a blob, and an accepted one is never read twice.
    """
    digest = hashlib.sha256()
    size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as out:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    return digest.hexdigest(), size
