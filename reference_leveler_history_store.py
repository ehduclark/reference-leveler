"""Immutable revision publication and chain verification."""

from __future__ import annotations

import json
import os
import stat

from pathlib import Path

from reference_leveler_history import (
    RevisionHistoryError,
    RevisionRecord,
    revision_record_fingerprint,
    validate_revision_record,
)


class RevisionChainError(RevisionHistoryError):
    """Published revision history is missing, malformed, or inconsistent."""


def revision_path(directory: Path, revision: int) -> Path:
    """Return the deterministic destination for a revision."""

    if type(revision) is not int or revision < 1:
        raise RevisionChainError("Invalid revision number")

    return Path(directory) / f"revision-{revision:06d}.json"


def _open_directory(directory: Path) -> int:
    """Open an existing real directory without following its final symlink."""

    return os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )


def _read_revision(directory_fd: int, revision: int) -> RevisionRecord:
    name = revision_path(Path("."), revision).name

    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
    except FileNotFoundError as exc:
        raise RevisionChainError(
            f"Missing published revision {revision}"
        ) from exc

    try:
        identity = os.fstat(fd)

        if not stat.S_ISREG(identity.st_mode):
            raise RevisionChainError(
                f"Revision {revision} is not a regular file"
            )

        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = json.load(handle)

    except (OSError, ValueError, UnicodeError, TypeError) as exc:
        raise RevisionChainError(
            f"Cannot read revision {revision}"
        ) from exc

    finally:
        if fd >= 0:
            os.close(fd)

    try:
        record = validate_revision_record(data)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RevisionChainError(
            f"Invalid published revision {revision}"
        ) from exc

    if record.revision != revision:
        raise RevisionChainError(
            f"Revision number mismatch at {revision}"
        )

    return record


def load_published_revision(
    directory: Path,
    revision: int,
) -> RevisionRecord:
    """Load one strictly validated published revision."""

    revision_path(directory, revision)

    directory_fd = _open_directory(directory)

    try:
        return _read_revision(directory_fd, revision)
    finally:
        os.close(directory_fd)


def verify_published_chain(
    directory: Path,
    revision: int,
    expected_head_sha256: str | None = None,
) -> tuple[RevisionRecord, ...]:
    """Verify every predecessor link back to the initial revision.

    A trusted expected head digest can anchor the verification.
    Without one, this checks internal consistency, not authenticity.
    """

    revision_path(directory, revision)

    directory_fd = _open_directory(directory)

    try:
        return _verify_chain_fd(
            directory_fd,
            revision,
            expected_head_sha256,
        )
    finally:
        os.close(directory_fd)


def _verify_chain_fd(
    directory_fd: int,
    revision: int,
    expected_head_sha256: str | None = None,
) -> tuple[RevisionRecord, ...]:
    """Verify a chain through an already-open history directory."""

    records = []

    for number in range(1, revision + 1):
        record = _read_revision(directory_fd, number)

        if records:
            previous = records[-1]

            if record.project_id != previous.project_id:
                raise RevisionChainError(
                    f"Project identity mismatch at revision {number}"
                )

            expected = revision_record_fingerprint(previous)

            if record.previous_record_sha256 != expected:
                raise RevisionChainError(
                    f"Broken predecessor link at revision {number}"
                )

        records.append(record)

    head = records[-1]

    if expected_head_sha256 is not None:
        if (
            type(expected_head_sha256) is not str
            or len(expected_head_sha256) != 64
            or any(
                c not in "0123456789abcdef"
                for c in expected_head_sha256
            )
        ):
            raise RevisionChainError("Invalid trusted head digest")

        if revision_record_fingerprint(head) != expected_head_sha256:
            raise RevisionChainError("Trusted head digest mismatch")

    return tuple(records)


class RevisionPublicationDurabilityError(OSError):
    """Record is visible, but durable persistence is uncertain."""

    def __init__(self, destination: Path, cause: OSError):
        self.destination = destination
        self.cause = cause
        super().__init__(
            f"Revision published at {destination}, "
            f"but directory synchronization failed: {cause}"
        )



class RevisionPublicationCleanupError(OSError):
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
            f"Revision {state} at {destination}, "
            f"but temporary cleanup failed: {cause}"
        )


def publish_revision(
    directory: Path,
    record: RevisionRecord,
) -> Path:
    """Publish one immutable revision record atomically.

    A directory-sync failure raises RevisionPublicationDurabilityError:
    the destination may already be visible and must not be overwritten.
    """

    import secrets

    record = validate_revision_record(record.as_dict())

    directory = Path(directory).absolute()
    destination = revision_path(directory, record.revision)
    name = destination.name

    directory_fd = _open_directory(directory)
    temporary_name = None
    published = False
    primary_error = None

    try:
        directory_identity = os.fstat(directory_fd)

        if not stat.S_ISDIR(directory_identity.st_mode):
            raise NotADirectoryError(directory)

        def check_directory_identity():
            current = os.stat(directory, follow_symlinks=False)

            if (
                current.st_dev != directory_identity.st_dev
                or current.st_ino != directory_identity.st_ino
            ):
                raise RevisionChainError(
                    "History directory changed during publication"
                )

        check_directory_identity()

        if record.revision > 1:
            chain = _verify_chain_fd(
                directory_fd,
                record.revision - 1,
            )

            predecessor = chain[-1]

            if predecessor.project_id != record.project_id:
                raise RevisionChainError(
                    "Published predecessor project mismatch"
                )

            if (
                revision_record_fingerprint(predecessor)
                != record.previous_record_sha256
            ):
                raise RevisionChainError(
                    "Published predecessor digest mismatch"
                )

        check_directory_identity()

        payload = (
            json.dumps(
                record.as_dict(),
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
                temporary_fd = os.open(
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
            raise RevisionChainError(
                "Unable to allocate exclusive temporary revision"
            )

        with os.fdopen(temporary_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        check_directory_identity()

        if record.revision > 1:
            current_predecessor = _read_revision(
                directory_fd,
                record.revision - 1,
            )

            if (
                revision_record_fingerprint(current_predecessor)
                != record.previous_record_sha256
            ):
                raise RevisionChainError(
                    "Predecessor changed before publication"
                )

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
            raise RevisionPublicationDurabilityError(
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
            raise RevisionPublicationCleanupError(
                destination,
                published,
                cleanup_error,
            ) from cleanup_error
