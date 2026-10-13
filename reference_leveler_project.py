"""Versioned Reference Leveler project manifests.

Project manifests describe processing configuration and inventory identity.
They do not authorize deviations or initiate rendering.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import uuid

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reference_leveler_decisions import policy_fingerprint
from reference_leveler_policy import DEFAULT_BUDGET, ProcessingBudget


SCHEMA = "reference_leveler.project"
SCHEMA_VERSION = 1

PROFILES = frozenset({
    "transparent",
    "balanced",
    "consistent",
})


class ProjectManifestError(ValueError):
    """Invalid or incompatible project manifest."""


class ProjectManifestDurabilityError(OSError):
    """Manifest was published, but durability is not confirmed."""

    def __init__(self, destination: Path, cause: OSError):
        self.destination = Path(destination)
        self.cause = cause
        super().__init__(
            f"Manifest published but durability is unconfirmed: "
            f"{destination}: {cause}"
        )


@dataclass(frozen=True)
class ProjectManifest:
    schema: str
    schema_version: int
    project_id: str
    name: str
    created_utc: str
    revision: int

    source_root: str
    output_root: str

    reference_path: str
    reference_sha256: str
    reference_size: int

    target_lufs: float
    ceiling_dbtp: float

    profile: str
    processing_budget: dict[str, float]
    policy_fingerprint: str

    inventory_snapshot: str
    inventory_count: int
    inventory_scanned_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ProjectManifestError(
            f"{label} must be a finite number"
        )
    return float(value)


def _absolute_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectManifestError(f"{label} must be a path")

    path = Path(value)

    if not path.is_absolute():
        raise ProjectManifestError(
            f"{label} must be absolute"
        )

    if str(path) != value:
        raise ProjectManifestError(
            f"{label} must be normalized"
        )

    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ProjectManifestError(
            f"{label} must be an ISO timestamp"
        )

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProjectManifestError(
            f"{label} must be an ISO timestamp"
        ) from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProjectManifestError(
            f"{label} must include a timezone"
        )

    return value


def validate_manifest(data: Any) -> ProjectManifest:
    """Validate a complete manifest without filesystem side effects."""

    if not isinstance(data, dict):
        raise ProjectManifestError(
            "Manifest must be a JSON object"
        )

    expected = {field.name for field in fields(ProjectManifest)}

    if set(data) != expected:
        missing = sorted(expected - set(data))
        extra = sorted(set(data) - expected)

        raise ProjectManifestError(
            f"Manifest fields mismatch: missing={missing}, extra={extra}"
        )

    if data["schema"] != SCHEMA:
        raise ProjectManifestError("Unsupported project schema")

    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != SCHEMA_VERSION
    ):
        raise ProjectManifestError(
            "Unsupported project schema version"
        )

    try:
        uuid.UUID(data["project_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProjectManifestError(
            "Invalid project UUID"
        ) from exc

    if (
        not isinstance(data["name"], str)
        or not data["name"].strip()
    ):
        raise ProjectManifestError("Project name is required")

    _timestamp(data["created_utc"], "created_utc")
    _timestamp(
        data["inventory_scanned_at"],
        "inventory_scanned_at",
    )

    if type(data["revision"]) is not int or data["revision"] < 1:
        raise ProjectManifestError(
            "Revision must be a positive integer"
        )

    for key in (
        "source_root",
        "output_root",
        "reference_path",
        "inventory_snapshot",
    ):
        _absolute_path(data[key], key)

    # Resolve existing components, including symlink aliases.
    # Missing final components are permitted during project planning.
    source = Path(data["source_root"]).resolve(strict=False)
    output = Path(data["output_root"]).resolve(strict=False)

    if (
        source == output
        or source in output.parents
        or output in source.parents
    ):
        raise ProjectManifestError(
            "Source and output roots must not overlap"
        )

    digest = data["reference_sha256"]

    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ProjectManifestError(
            "Invalid reference SHA-256"
        )

    if (
        type(data["reference_size"]) is not int
        or data["reference_size"] < 0
    ):
        raise ProjectManifestError(
            "Invalid reference size"
        )

    _finite(data["target_lufs"], "target_lufs")
    _finite(data["ceiling_dbtp"], "ceiling_dbtp")

    if data["profile"] not in PROFILES:
        raise ProjectManifestError(
            "Unsupported processing profile"
        )

    budget = data["processing_budget"]

    if not isinstance(budget, dict):
        raise ProjectManifestError(
            "Processing budget must be an object"
        )

    budget_fields = {
        field.name for field in fields(ProcessingBudget)
    }

    if set(budget) != budget_fields:
        raise ProjectManifestError(
            "Processing budget fields mismatch"
        )

    for key, value in budget.items():
        if _finite(value, key) < 0:
            raise ProjectManifestError(
                f"{key} must be nonnegative"
            )

    if (
        budget["preferred_loudness_error"]
        > budget["acceptable_loudness_error"]
        or budget["acceptable_loudness_error"]
        > budget["exception_loudness_error"]
        or budget["maximum_normal_lra_change"]
        > budget["maximum_fallback_lra_change"]
    ):
        raise ProjectManifestError(
            "Processing budget thresholds are inconsistent"
        )

    policy = data["policy_fingerprint"]

    if (
        not isinstance(policy, str)
        or len(policy) != 64
        or any(c not in "0123456789abcdef" for c in policy)
    ):
        raise ProjectManifestError(
            "Invalid policy fingerprint"
        )

    if (
        type(data["inventory_count"]) is not int
        or data["inventory_count"] < 0
    ):
        raise ProjectManifestError(
            "Invalid inventory count"
        )

    return ProjectManifest(**data)


def create_manifest(
    *,
    name: str,
    source_root: Path,
    output_root: Path,
    reference_path: Path,
    reference_sha256: str,
    reference_size: int,
    target_lufs: float,
    ceiling_dbtp: float,
    profile: str,
    inventory_snapshot: Path,
    inventory_count: int,
    inventory_scanned_at: str,
    budget: ProcessingBudget = DEFAULT_BUDGET,
) -> ProjectManifest:
    """Create a validated project description without writing files."""

    data = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "project_id": str(uuid.uuid4()),
        "name": name,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "revision": 1,
        "source_root": str(Path(source_root).absolute()),
        "output_root": str(Path(output_root).absolute()),
        "reference_path": str(Path(reference_path).absolute()),
        "reference_sha256": reference_sha256,
        "reference_size": reference_size,
        "target_lufs": target_lufs,
        "ceiling_dbtp": ceiling_dbtp,
        "profile": profile,
        "processing_budget": asdict(budget),
        "policy_fingerprint": policy_fingerprint(),
        "inventory_snapshot": str(
            Path(inventory_snapshot).absolute()
        ),
        "inventory_count": inventory_count,
        "inventory_scanned_at": inventory_scanned_at,
    }

    return validate_manifest(data)


def load_manifest(path: Path) -> ProjectManifest:
    """Load and validate a manifest."""

    data = json.loads(
        Path(path).read_text(encoding="utf-8")
    )

    return validate_manifest(data)


def save_manifest(
    manifest: ProjectManifest,
    destination: Path,
) -> None:
    """Atomically create a manifest without replacing existing files."""

    validated = validate_manifest(manifest.as_dict())

    destination = Path(destination).absolute()
    parent = destination.parent

    if not parent.is_dir():
        raise FileNotFoundError(
            f"Manifest directory does not exist: {parent}"
        )

    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)

    payload = (
        json.dumps(
            validated.as_dict(),
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")

    temporary = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        os.link(temporary, destination)

        try:
            directory_fd = os.open(parent, os.O_RDONLY)

            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

        except OSError as exc:
            raise ProjectManifestDurabilityError(
                destination, exc
            ) from exc

    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
