"""Validated, metadata-only inventory reconciliation.

This module never reads or modifies source audio.
Matching path and size do not establish content identity.
"""

import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping


class InventoryError(ValueError):
    """Invalid or incompatible inventory snapshot."""


@dataclass(frozen=True)
class InventorySnapshot:
    source_root: str
    scanned_at: str
    files: Mapping[str, int]


@dataclass(frozen=True)
class InventoryReconciliation:
    previous_count: int
    current_count: int
    added: tuple[str, ...]
    removed: tuple[str, ...]
    size_changed: tuple[str, ...]
    metadata_unchanged: tuple[str, ...]

    def summary(self):
        return {
            "previous_count": self.previous_count,
            "current_count": self.current_count,
            "added_count": len(self.added),
            "removed_count": len(self.removed),
            "size_changed_count": len(self.size_changed),
            "metadata_unchanged_count": len(self.metadata_unchanged),
            "identity_verified": False,
        }


def _relative_path(value):
    if not isinstance(value, str) or not value:
        raise InventoryError("Invalid relative path")

    if (
        "\\" in value
        or "\x00" in value
        or any(
            part in ("", ".", "..")
            for part in value.split("/")
        )
    ):
        raise InventoryError(f"Unsafe relative path: {value!r}")

    path = PurePosixPath(value)

    if path.is_absolute():
        raise InventoryError(f"Absolute inventory path: {value!r}")

    return value


def _canonical_root(value):
    if not isinstance(value, str) or not value:
        raise InventoryError("Invalid source root")

    if "\\x00" in value or "\\\\" in value:
        raise InventoryError("Invalid source root")

    if not value.startswith("/"):
        raise InventoryError("Source root must be absolute")

    if os.path.normpath(value) != value:
        raise InventoryError("Source root must be normalized")

    return value


def _scan_timestamp(value):
    if not isinstance(value, str) or not value:
        raise InventoryError("Invalid scan timestamp")

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InventoryError("Invalid scan timestamp") from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InventoryError("Scan timestamp requires timezone")

    if parsed.isoformat() != value:
        raise InventoryError("Scan timestamp must be canonical ISO 8601")

    return value


def validate_snapshot(data):
    if type(data) is not dict:
        raise InventoryError("Snapshot must be an object")

    if type(data.get("schema_version")) is not int:
        raise InventoryError("Invalid schema version")

    if data["schema_version"] != 1:
        raise InventoryError("Unsupported schema version")

    root = _canonical_root(data.get("source_root"))
    scanned_at = _scan_timestamp(data.get("scanned_at"))

    entries = data.get("files")

    if not isinstance(entries, list):
        raise InventoryError("Inventory files must be a list")

    declared_count = data.get("file_count")

    if (
        type(declared_count) is not int
        or declared_count != len(entries)
    ):
        raise InventoryError("Inventory file count mismatch")

    files = {}

    for entry in entries:
        if type(entry) is not dict:
            raise InventoryError("Inventory entry must be an object")

        if set(entry) != {"relative_path", "size_bytes"}:
            raise InventoryError("Unexpected inventory entry fields")

        name = _relative_path(entry["relative_path"])
        size = entry["size_bytes"]

        if type(size) is not int or size < 0:
            raise InventoryError(
                f"Invalid size for {name!r}"
            )

        if name in files:
            raise InventoryError(
                f"Duplicate inventory path: {name}"
            )

        files[name] = size

    expected_formats = dict(
        Counter(
            PurePosixPath(name).suffix.lower()
            for name in files
        )
    )

    if data.get("format_counts") != expected_formats:
        raise InventoryError("Inventory format counts mismatch")

    return InventorySnapshot(
        source_root=root,
        scanned_at=scanned_at,
        files=MappingProxyType(files),
    )


def load_snapshot(path):
    path = Path(path)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InventoryError(
            f"Cannot load inventory snapshot: {path}"
        ) from exc

    return validate_snapshot(data)


def reconcile(previous, current):
    if previous.source_root != current.source_root:
        raise InventoryError(
            "Cannot reconcile different source roots"
        )

    old_names = set(previous.files)
    new_names = set(current.files)

    shared = old_names & new_names

    added = tuple(sorted(new_names - old_names))
    removed = tuple(sorted(old_names - new_names))

    changed = tuple(
        sorted(
            name
            for name in shared
            if previous.files[name] != current.files[name]
        )
    )

    unchanged = tuple(
        sorted(
            name
            for name in shared
            if previous.files[name] == current.files[name]
        )
    )

    return InventoryReconciliation(
        previous_count=len(previous.files),
        current_count=len(current.files),
        added=added,
        removed=removed,
        size_changed=changed,
        metadata_unchanged=unchanged,
    )
