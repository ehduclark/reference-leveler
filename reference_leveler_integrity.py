"""Source identity and content-integrity safeguards for Reference Leveler."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path


class SourceIntegrityError(ValueError):
    """Source identity or content integrity could not be established."""


@dataclass(frozen=True)
class SourceFingerprint:
    path: str
    device: int
    inode: int
    size: int
    sha256: str


def fingerprint_file(path: Path) -> SourceFingerprint:
    """Fingerprint a regular file without modifying it."""

    path = Path(path).resolve(strict=True)

    before = path.stat()

    if not stat.S_ISREG(before.st_mode):
        raise SourceIntegrityError(
            f"Source is not a regular file: {path}"
        )

    digest = hashlib.sha256()

    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())

        if not stat.S_ISREG(opened.st_mode):
            raise SourceIntegrityError(
                f"Source is not a regular file: {path}"
            )

        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise SourceIntegrityError(
                f"Source changed during opening: {path}"
            )

        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

        after = os.fstat(handle.fileno())

    current = path.stat()

    identity_before = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )

    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )

    identity_current = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )

    if not (
        identity_before
        == identity_after
        == identity_current
    ):
        raise SourceIntegrityError(
            f"Source changed during fingerprinting: {path}"
        )

    return SourceFingerprint(
        path=str(path),
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        sha256=digest.hexdigest(),
    )


def verify_fingerprint(
    path: Path,
    expected: SourceFingerprint,
) -> None:
    """Reject a source whose identity or bytes have changed."""

    actual = fingerprint_file(path)

    if actual != expected:
        raise SourceIntegrityError(
            f"Source fingerprint mismatch: {path}"
        )


def validate_distinct_paths(
    source: Path,
    destination: Path,
) -> tuple[Path, Path]:
    """Reject destination aliases of an existing source."""

    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()

    if not source.is_file():
        raise SourceIntegrityError(
            f"Source is not a regular file: {source}"
        )

    # Resolves symlinks in existing destination path components.
    resolved_destination = destination.resolve(strict=False)

    if source == resolved_destination:
        raise SourceIntegrityError(
            "Source and destination identify the same path"
        )

    # Reject every existing destination, including dangling symlinks.
    # Distinguish aliases of the source from unrelated existing files.
    if destination.exists() or destination.is_symlink():
        if destination.exists() and os.path.samefile(
            source, destination
        ):
            raise SourceIntegrityError(
                "Source and destination identify the same file"
            )

        raise FileExistsError(
            f"Destination already exists: {destination}"
        )

    return source, destination
