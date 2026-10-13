"""Immutable inventory snapshot persistence.

Inventory metadata only. No audio processing or source-file reads.
"""

from __future__ import annotations

import json
import os
import secrets
import stat

from collections import Counter
from pathlib import Path, PurePosixPath

from reference_leveler_history import inventory_fingerprint
from reference_leveler_inventory import (
    InventorySnapshot,
    validate_snapshot,
)
from reference_leveler_revisions import _validated_inventory


def serialize_inventory(snapshot: InventorySnapshot) -> dict:
    """Return a validated, deterministic inventory JSON structure."""

    snapshot = _validated_inventory(snapshot)

    entries = [
        {
            "relative_path": name,
            "size_bytes": snapshot.files[name],
        }
        for name in sorted(snapshot.files)
    ]

    formats = dict(sorted(Counter(
        PurePosixPath(name).suffix.lower()
        for name in snapshot.files
    ).items()))

    data = {
        "schema_version": 1,
        "source_root": snapshot.source_root,
        "scanned_at": snapshot.scanned_at,
        "file_count": len(entries),
        "format_counts": formats,
        "files": entries,
    }

    validate_snapshot(data)

    return data


class InventoryPublicationError(ValueError):
    """Invalid published inventory or publication relationship."""


class InventoryPublicationDurabilityError(OSError):
    """Inventory is visible, but durable persistence is uncertain."""

    def __init__(self, destination: Path, cause: OSError):
        self.destination = destination
        self.cause = cause
        super().__init__(
            f"Inventory published at {destination}, "
            f"but directory synchronization failed: {cause}"
        )


class InventoryPublicationCleanupError(OSError):
    """Publication outcome is known, but temporary cleanup failed."""

    def __init__(
        self,
        destination: Path,
        published: bool,
        cause: OSError,
    ):
        self.destination = destination
        self.published = published
        self.cause = cause

        state = "published" if published else "not published"

        super().__init__(
            f"Inventory {state} at {destination}, "
            f"but temporary cleanup failed: {cause}"
        )


def inventory_path(directory: Path, revision: int) -> Path:
    """Return the deterministic inventory revision path."""

    if type(revision) is not int or revision < 1:
        raise InventoryPublicationError("Invalid inventory revision")

    return Path(directory) / f"revision-{revision:06d}.json"


def _open_inventory_directory(directory: Path) -> int:
    return os.open(
        directory,
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_CLOEXEC,
    )


def _read_inventory_fd(directory_fd: int, revision: int):
    name = inventory_path(Path("."), revision).name

    fd = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory_fd,
    )

    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise InventoryPublicationError(
                "Published inventory is not a regular file"
            )

        try:
            data = json.load(handle)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise InventoryPublicationError(
                "Published inventory contains invalid JSON"
            ) from exc

    return validate_snapshot(data)


def verify_published_inventory(
    directory: Path,
    revision: int,
    expected_sha256: str,
) -> InventorySnapshot:
    """Load and verify an immutable published inventory."""

    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_sha256)
    ):
        raise InventoryPublicationError(
            "Invalid expected inventory fingerprint"
        )

    directory = Path(directory).absolute()
    directory_fd = _open_inventory_directory(directory)

    try:
        snapshot = _read_inventory_fd(directory_fd, revision)

        if inventory_fingerprint(snapshot) != expected_sha256:
            raise InventoryPublicationError(
                "Published inventory fingerprint mismatch"
            )

        return snapshot
    finally:
        os.close(directory_fd)


def publish_inventory(
    directory: Path,
    revision: int,
    snapshot: InventorySnapshot,
) -> Path:
    """Atomically publish immutable inventory metadata."""

    data = serialize_inventory(snapshot)

    directory = Path(directory).absolute()
    destination = inventory_path(directory, revision)
    name = destination.name

    directory_fd = _open_inventory_directory(directory)
    temporary_name = None
    published = False
    primary_error = None

    try:
        identity = os.fstat(directory_fd)

        def check_directory_identity():
            current = os.stat(
                directory,
                follow_symlinks=False,
            )

            if (
                current.st_dev != identity.st_dev
                or current.st_ino != identity.st_ino
            ):
                raise InventoryPublicationError(
                    "Inventory directory changed during publication"
                )

        check_directory_identity()

        payload = (
            json.dumps(
                data,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

        for _ in range(32):
            candidate = (
                f".{name}.{secrets.token_hex(16)}.tmp"
            )

            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue

            temporary_name = candidate
            break
        else:
            raise InventoryPublicationError(
                "Unable to allocate temporary inventory"
            )

        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        check_directory_identity()

        os.link(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        published = True

        try:
            os.fsync(directory_fd)
        except OSError as exc:
            raise InventoryPublicationDurabilityError(
                destination,
                exc,
            ) from exc

        return destination

    except BaseException as exc:
        primary_error = exc
        raise

    finally:
        cleanup_error = None

        try:
            if temporary_name is not None:
                os.unlink(
                    temporary_name,
                    dir_fd=directory_fd,
                )
        except OSError as exc:
            cleanup_error = exc
        finally:
            os.close(directory_fd)

        if cleanup_error is not None and primary_error is None:
            raise InventoryPublicationCleanupError(
                destination,
                published,
                cleanup_error,
            ) from cleanup_error
