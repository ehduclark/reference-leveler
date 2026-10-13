"""Pure, inventory-bound project revision planning.

No manifests, inventories, approvals, or audio are written.
Inventory metadata is not proof of source-content identity.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Mapping

from reference_leveler_inventory import (
    InventoryReconciliation,
    InventorySnapshot,
    InventoryError,
    reconcile,
    validate_snapshot,
)
from reference_leveler_project import (
    ProjectManifest,
    ProjectManifestError,
    validate_manifest,
)


class ProjectRevisionError(ValueError):
    """An inventory cannot safely advance this project."""


@dataclass(frozen=True)
class ProjectRevisionPlan:
    previous: ProjectManifest
    proposed: ProjectManifest
    reconciliation: InventoryReconciliation


def _validated_inventory(snapshot: InventorySnapshot) -> InventorySnapshot:
    """Revalidate an inventory, including directly constructed instances."""

    if not isinstance(snapshot, InventorySnapshot):
        raise ProjectRevisionError("Invalid inventory snapshot")

    if not isinstance(snapshot.files, Mapping):
        raise ProjectRevisionError("Invalid inventory file mapping")

    try:
        entries = [
            {
                "relative_path": name,
                "size_bytes": size,
            }
            for name, size in snapshot.files.items()
        ]

        formats = dict(Counter(
            PurePosixPath(name).suffix.lower()
            for name in snapshot.files
        ))

        return validate_snapshot({
            "schema_version": 1,
            "source_root": snapshot.source_root,
            "scanned_at": snapshot.scanned_at,
            "file_count": len(entries),
            "format_counts": formats,
            "files": entries,
        })

    except (InventoryError, TypeError, AttributeError, ValueError) as exc:
        raise ProjectRevisionError(
            "Invalid inventory snapshot"
        ) from exc


def _snapshot_path(value) -> Path:
    """Require a canonical absolute snapshot path without filesystem access."""

    if not isinstance(value, (str, Path)):
        raise ProjectRevisionError("Invalid snapshot path")

    raw = str(value)

    if (
        not raw.startswith("/")
        or "\\\\" in raw
        or "\\x00" in raw
    ):
        raise ProjectRevisionError(
            "Snapshot path must be absolute and canonical"
        )

    if any(part in ("", ".", "..") for part in raw.split("/")[1:]):
        raise ProjectRevisionError(
            "Snapshot path must be canonical"
        )

    return Path(raw)


def revise_project_inventory(
    project: ProjectManifest,
    previous_snapshot: InventorySnapshot,
    current_snapshot: InventorySnapshot,
    current_snapshot_path: Path,
) -> ProjectRevisionPlan:
    """Prepare, but do not persist, the next project revision."""

    project = validate_manifest(project.as_dict())

    previous_snapshot = _validated_inventory(previous_snapshot)
    current_snapshot = _validated_inventory(current_snapshot)

    previous_path = _snapshot_path(project.inventory_snapshot)
    current_path = _snapshot_path(current_snapshot_path)

    if current_path == previous_path:
        raise ProjectRevisionError(
            "New inventory must use a different snapshot path"
        )

    if previous_snapshot.source_root != project.source_root:
        raise ProjectRevisionError(
            "Previous inventory source root differs from project"
        )

    if current_snapshot.source_root != project.source_root:
        raise ProjectRevisionError(
            "Current inventory source root differs from project"
        )

    if len(previous_snapshot.files) != project.inventory_count:
        raise ProjectRevisionError(
            "Previous inventory count differs from project"
        )

    if previous_snapshot.scanned_at != project.inventory_scanned_at:
        raise ProjectRevisionError(
            "Previous inventory timestamp differs from project"
        )

    previous_time = datetime.fromisoformat(
        previous_snapshot.scanned_at
    )
    current_time = datetime.fromisoformat(
        current_snapshot.scanned_at
    )

    if current_time <= previous_time:
        raise ProjectRevisionError(
            "New inventory must have a later scan timestamp"
        )

    reconciliation = reconcile(
        previous_snapshot,
        current_snapshot,
    )

    data = project.as_dict()

    data.update({
        "revision": project.revision + 1,
        "inventory_snapshot": str(current_path),
        "inventory_count": len(current_snapshot.files),
        "inventory_scanned_at": current_snapshot.scanned_at,
    })

    try:
        proposed = validate_manifest(data)
    except ProjectManifestError as exc:
        raise ProjectRevisionError(
            "Proposed project revision is invalid"
        ) from exc

    return ProjectRevisionPlan(
        previous=project,
        proposed=proposed,
        reconciliation=reconciliation,
    )
