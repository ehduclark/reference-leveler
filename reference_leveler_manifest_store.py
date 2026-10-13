"""Immutable project manifest publication.

Metadata only. No audio processing or source-file reads.
"""

from __future__ import annotations

import json
import os
import secrets
import stat

from pathlib import Path

from reference_leveler_history import manifest_fingerprint
from reference_leveler_project import (
    ProjectManifest,
    validate_manifest,
)


class ManifestPublicationError(ValueError):
    """Invalid manifest publication relationship."""


class ManifestPublicationDurabilityError(OSError):
    """Manifest is visible, but durable persistence is uncertain."""

    def __init__(self, destination: Path, cause: OSError):
        self.destination = destination
        self.cause = cause
        super().__init__(
            f"Manifest published at {destination}, "
            f"but directory synchronization failed: {cause}"
        )


class ManifestPublicationCleanupError(OSError):
    """Temporary cleanup failed after a known publication outcome."""

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
            f"Manifest {state} at {destination}, "
            f"but temporary cleanup failed: {cause}"
        )


def manifest_path(directory: Path, revision: int) -> Path:
    """Return the deterministic immutable manifest filename."""

    if type(revision) is not int or revision < 1:
        raise ManifestPublicationError("Invalid manifest revision")

    return Path(directory) / f"revision-{revision:06d}.json"


def _open_directory(directory: Path) -> int:
    return os.open(
        directory,
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_CLOEXEC,
    )


def _check_directory_identity(
    directory: Path,
    directory_fd: int,
) -> None:
    original = os.fstat(directory_fd)
    current = os.stat(directory, follow_symlinks=False)

    if (
        original.st_dev != current.st_dev
        or original.st_ino != current.st_ino
    ):
        raise ManifestPublicationError(
            "Manifest directory changed during publication"
        )


def _read_manifest_fd(
    directory_fd: int,
    revision: int,
) -> ProjectManifest:
    name = manifest_path(Path("."), revision).name

    fd = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory_fd,
    )

    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ManifestPublicationError(
                "Published manifest is not a regular file"
            )

        try:
            data = json.load(handle)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ManifestPublicationError(
                "Published manifest contains invalid JSON"
            ) from exc

    manifest = validate_manifest(data)

    if manifest.revision != revision:
        raise ManifestPublicationError(
            "Published manifest revision mismatch"
        )

    return manifest


def verify_published_manifest(
    directory: Path,
    revision: int,
    expected_sha256: str,
) -> ProjectManifest:
    """Securely load and verify a published manifest."""

    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_sha256)
    ):
        raise ManifestPublicationError(
            "Invalid expected manifest fingerprint"
        )

    directory = Path(directory).absolute()
    directory_fd = _open_directory(directory)

    try:
        manifest = _read_manifest_fd(directory_fd, revision)

        if manifest_fingerprint(manifest) != expected_sha256:
            raise ManifestPublicationError(
                "Published manifest fingerprint mismatch"
            )

        return manifest
    finally:
        os.close(directory_fd)


def publish_manifest(
    directory: Path,
    revision: int,
    manifest: ProjectManifest,
) -> Path:
    """Publish one immutable manifest without overwriting."""

    manifest = validate_manifest(manifest.as_dict())
    destination = manifest_path(
        Path(directory).absolute(),
        revision,
    )

    if manifest.revision != revision:
        raise ManifestPublicationError(
            "Manifest revision does not match destination"
        )

    directory = destination.parent
    name = destination.name

    directory_fd = _open_directory(directory)
    temporary_name = None
    published = False
    primary_error = None

    try:
        _check_directory_identity(directory, directory_fd)

        payload = (
            json.dumps(
                manifest.as_dict(),
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
            raise ManifestPublicationError(
                "Unable to allocate temporary manifest"
            )

        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        _check_directory_identity(directory, directory_fd)

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
            raise ManifestPublicationDurabilityError(
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
            raise ManifestPublicationCleanupError(
                destination,
                published,
                cleanup_error,
            ) from cleanup_error
