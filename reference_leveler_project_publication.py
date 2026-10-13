"""Publication and verification of committed project revisions.

Metadata only. Never reads or processes source audio.

A history record is published last as the logical commit marker.
Partial publication is preserved for explicit recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import stat
from pathlib import Path

from reference_leveler_history import (
    RevisionRecord,
    create_revision_record,
    inventory_fingerprint,
    manifest_fingerprint,
    verify_revision_record,
)
from reference_leveler_history_store import (
    publish_revision,
    verify_published_chain,
)
from reference_leveler_inventory import (
    InventorySnapshot,
)
from reference_leveler_inventory_store import (
    inventory_path,
    publish_inventory,
    verify_published_inventory,
)
from reference_leveler_manifest_store import (
    manifest_path,
    publish_manifest,
    verify_published_manifest,
)
from reference_leveler_project import (
    ProjectManifest,
    validate_manifest,
)
from reference_leveler_revisions import _validated_inventory


class ProjectPublicationError(ValueError):
    """Invalid project publication or committed revision."""


@dataclass(frozen=True)
class CommittedProjectRevision:
    manifest: ProjectManifest
    inventory: InventorySnapshot
    record: RevisionRecord


def _directories(project_directory: Path):
    root = Path(project_directory).absolute()

    return (
        root / "inventories",
        root / "manifests",
        root / "history",
    )


def _validate_initial_relationship(
    project_directory: Path,
    manifest: ProjectManifest,
    inventory: InventorySnapshot,
) -> RevisionRecord:
    if manifest.revision != 1:
        raise ProjectPublicationError(
            "Initial project publication requires revision 1"
        )

    inventories, _, _ = _directories(project_directory)

    expected_inventory = inventory_path(
        inventories,
        manifest.revision,
    )

    if manifest.inventory_snapshot != str(expected_inventory):
        raise ProjectPublicationError(
            "Manifest inventory path does not match "
            "the project revision destination"
        )

    return create_revision_record(
        manifest,
        inventory,
        previous=None,
    )


def verify_committed_project(
    project_directory: Path,
    revision: int = 1,
    expected_project_id: str | None = None,
    expected_head_sha256: str | None = None,
) -> CommittedProjectRevision:
    """Verify history and both referenced immutable artifacts."""

    inventories, manifests, history = _directories(
        project_directory
    )

    chain = verify_published_chain(
        history,
        revision,
        expected_head_sha256=expected_head_sha256,
    )
    record = chain[-1]

    if (
        expected_project_id is not None
        and record.project_id != expected_project_id
    ):
        raise ProjectPublicationError(
            "Committed project identity mismatch"
        )

    inventory = verify_published_inventory(
        inventories,
        revision,
        record.inventory_sha256,
    )

    manifest = verify_published_manifest(
        manifests,
        revision,
        record.manifest_sha256,
    )

    if manifest.project_id != record.project_id:
        raise ProjectPublicationError(
            "Manifest and history project identities differ"
        )

    expected_inventory = inventory_path(
        inventories,
        revision,
    )

    if manifest.inventory_snapshot != str(expected_inventory):
        raise ProjectPublicationError(
            "Committed manifest references wrong inventory path"
        )

    if not verify_revision_record(
        record,
        manifest,
        inventory,
        previous=chain[-2] if revision > 1 else None,
    ):
        raise ProjectPublicationError(
            "Committed revision relationship is invalid"
        )

    return CommittedProjectRevision(
        manifest=manifest,
        inventory=inventory,
        record=record,
    )


def _capture_directory_identities(project_directory: Path):
    """Capture project and metadata directory identities."""
    root = Path(project_directory).absolute()
    directories = (root, *_directories(root))
    identities = []

    for directory in directories:
        try:
            info = os.stat(directory, follow_symlinks=False)
        except OSError as exc:
            raise ProjectPublicationError(
                f"Expected existing real project directory: {directory}"
            ) from exc

        if not stat.S_ISDIR(info.st_mode):
            raise ProjectPublicationError(
                f"Expected existing real project directory: {directory}"
            )

        identities.append((directory, info.st_dev, info.st_ino))

    return tuple(identities)


def _check_directory_identities(identities):
    """Reject directories replaced since initial preflight."""
    for directory, device, inode in identities:
        try:
            current = os.stat(
                directory,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ProjectPublicationError(
                f"Project directory disappeared: {directory}"
            ) from exc

        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_dev != device
            or current.st_ino != inode
        ):
            raise ProjectPublicationError(
                f"Project directory changed: {directory}"
            )


def publish_initial_project(
    project_directory: Path,
    manifest: ProjectManifest,
    inventory: InventorySnapshot,
) -> CommittedProjectRevision:
    """Publish an initial revision with history committed last.

    The caller must supply an existing project directory containing
    inventories/, manifests/, and history/.

    Any interrupted publication remains on disk. This function
    never retries, replaces, or deletes published artifacts.
    """

    manifest = validate_manifest(manifest.as_dict())
    inventory = _validated_inventory(inventory)

    record = _validate_initial_relationship(
        project_directory,
        manifest,
        inventory,
    )

    inventories, manifests, history = _directories(
        project_directory
    )

    # Capture the project root and metadata directory identities.
    identities = _capture_directory_identities(project_directory)

    destinations = (
        inventory_path(inventories, 1),
        manifest_path(manifests, 1),
        history / "revision-000001.json",
    )

    if any(path.exists() or path.is_symlink() for path in destinations):
        raise ProjectPublicationError(
            "Initial revision already contains published artifacts"
        )

    _check_directory_identities(identities)

    publish_inventory(
        directory=inventories,
        revision=1,
        snapshot=inventory,
    )

    verify_published_inventory(
        inventories,
        1,
        inventory_fingerprint(inventory),
    )

    _check_directory_identities(identities)

    publish_manifest(manifests, 1, manifest)

    verify_published_manifest(
        manifests,
        1,
        manifest_fingerprint(manifest),
    )

    # Commit marker. Never publish before both artifacts verify.
    _check_directory_identities(identities)

    publish_revision(history, record)

    _check_directory_identities(identities)

    result = verify_committed_project(
        project_directory,
        revision=1,
    )

    _check_directory_identities(identities)

    if result.record != record:
        raise ProjectPublicationError(
            "Committed revision differs from intended publication"
        )

    return result
