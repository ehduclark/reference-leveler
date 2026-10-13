"""Pure cryptographic identities and revision-chain validation.

Fingerprints identify validated metadata, not source audio contents.
No files are read, written, published, or rendered.
"""

from __future__ import annotations

import hashlib
import json
import re

from dataclasses import asdict, dataclass
from typing import Any

from reference_leveler_inventory import InventorySnapshot
from reference_leveler_project import ProjectManifest, validate_manifest
from reference_leveler_revisions import _validated_inventory


SCHEMA = "reference_leveler.revision_record"
SCHEMA_VERSION = 1
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class RevisionHistoryError(ValueError):
    """Invalid revision record or broken revision relationship."""


def _canonical_digest(data: dict) -> str:
    payload = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    return hashlib.sha256(payload).hexdigest()


def inventory_fingerprint(snapshot: InventorySnapshot) -> str:
    """Fingerprint validated inventory metadata deterministically."""

    snapshot = _validated_inventory(snapshot)

    return _canonical_digest({
        "schema": "reference_leveler.inventory_identity",
        "schema_version": 1,
        "source_root": snapshot.source_root,
        "scanned_at": snapshot.scanned_at,
        "files": [
            {
                "relative_path": name,
                "size_bytes": snapshot.files[name],
            }
            for name in sorted(snapshot.files)
        ],
    })


def manifest_fingerprint(manifest: ProjectManifest) -> str:
    """Fingerprint the complete validated project manifest."""

    manifest = validate_manifest(manifest.as_dict())

    return _canonical_digest({
        "schema": "reference_leveler.manifest_identity",
        "schema_version": 1,
        "manifest": manifest.as_dict(),
    })


@dataclass(frozen=True)
class RevisionRecord:
    schema: str
    schema_version: int
    project_id: str
    revision: int
    manifest_sha256: str
    inventory_sha256: str
    previous_record_sha256: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_revision_record(data: Any) -> RevisionRecord:
    """Strictly validate a revision record's structure."""

    if type(data) is not dict:
        raise RevisionHistoryError("Revision record must be an object")

    expected = set(RevisionRecord.__dataclass_fields__)

    if set(data) != expected:
        raise RevisionHistoryError("Revision record fields mismatch")

    if data["schema"] != SCHEMA:
        raise RevisionHistoryError("Unsupported revision schema")

    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != SCHEMA_VERSION
    ):
        raise RevisionHistoryError("Unsupported revision schema version")

    if type(data["revision"]) is not int or data["revision"] < 1:
        raise RevisionHistoryError("Invalid revision number")

    import uuid

    try:
        project_id = str(uuid.UUID(data["project_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RevisionHistoryError("Invalid project UUID") from exc

    if project_id != data["project_id"]:
        raise RevisionHistoryError("Noncanonical project UUID")

    for field in ("manifest_sha256", "inventory_sha256"):
        value = data[field]

        if not isinstance(value, str) or not DIGEST_PATTERN.fullmatch(value):
            raise RevisionHistoryError(f"Invalid {field}")

    predecessor = data["previous_record_sha256"]

    if data["revision"] == 1:
        if predecessor is not None:
            raise RevisionHistoryError(
                "Initial revision cannot have a predecessor"
            )
    elif (
        not isinstance(predecessor, str)
        or not DIGEST_PATTERN.fullmatch(predecessor)
    ):
        raise RevisionHistoryError(
            "Later revisions require a predecessor digest"
        )

    return RevisionRecord(**data)


def revision_record_fingerprint(record: RevisionRecord) -> str:
    """Fingerprint a strictly validated revision record."""

    record = validate_revision_record(record.as_dict())

    return _canonical_digest(record.as_dict())


def create_revision_record(
    manifest: ProjectManifest,
    inventory: InventorySnapshot,
    previous: RevisionRecord | None = None,
) -> RevisionRecord:
    """Construct a record bound to a manifest and inventory."""

    manifest = validate_manifest(manifest.as_dict())
    inventory = _validated_inventory(inventory)

    if inventory.source_root != manifest.source_root:
        raise RevisionHistoryError("Inventory source root mismatch")

    if len(inventory.files) != manifest.inventory_count:
        raise RevisionHistoryError("Inventory count mismatch")

    if inventory.scanned_at != manifest.inventory_scanned_at:
        raise RevisionHistoryError("Inventory timestamp mismatch")

    if manifest.revision == 1:
        if previous is not None:
            raise RevisionHistoryError(
                "Initial revision cannot have a predecessor"
            )
        predecessor_digest = None
    else:
        if previous is None:
            raise RevisionHistoryError(
                "Previous revision record required"
            )

        previous = validate_revision_record(previous.as_dict())

        if previous.project_id != manifest.project_id:
            raise RevisionHistoryError("Predecessor project mismatch")

        if previous.revision != manifest.revision - 1:
            raise RevisionHistoryError("Predecessor revision mismatch")

        predecessor_digest = revision_record_fingerprint(previous)

    return validate_revision_record({
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "project_id": manifest.project_id,
        "revision": manifest.revision,
        "manifest_sha256": manifest_fingerprint(manifest),
        "inventory_sha256": inventory_fingerprint(inventory),
        "previous_record_sha256": predecessor_digest,
    })


def verify_revision_record(
    record: RevisionRecord,
    manifest: ProjectManifest,
    inventory: InventorySnapshot,
    previous: RevisionRecord | None = None,
) -> bool:
    """Verify the complete supplied revision relationship."""

    try:
        record = validate_revision_record(record.as_dict())
        expected = create_revision_record(
            manifest,
            inventory,
            previous,
        )
    except (ValueError, TypeError, AttributeError):
        return False

    return record == expected
