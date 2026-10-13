"""Regression coverage for metadata fingerprints and revision chains."""

from dataclasses import replace

import pytest

from reference_leveler_history import (
    RevisionHistoryError,
    create_revision_record,
    inventory_fingerprint,
    manifest_fingerprint,
    revision_record_fingerprint,
    validate_revision_record,
    verify_revision_record,
)
from reference_leveler_inventory import InventorySnapshot, validate_snapshot
from reference_leveler_project import create_manifest


def inventory(root, timestamp, entries):
    files = [
        {"relative_path": name, "size_bytes": size}
        for name, size in entries
    ]

    from collections import Counter
    from pathlib import PurePosixPath

    counts = dict(Counter(
        PurePosixPath(name).suffix.lower()
        for name, _ in entries
    ))

    return validate_snapshot({
        "schema_version": 1,
        "source_root": str(root),
        "scanned_at": timestamp,
        "file_count": len(files),
        "format_counts": counts,
        "files": files,
    })


@pytest.fixture
def context(tmp_path):
    root = tmp_path / "source"
    timestamp = "2026-10-13T12:18:05-07:00"

    snapshot = inventory(
        root,
        timestamp,
        [("b.flac", 200), ("a.mp3", 100)],
    )

    manifest = create_manifest(
        name="History Test",
        source_root=root,
        output_root=tmp_path / "output",
        reference_path=root / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=100,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=tmp_path / "snapshot.json",
        inventory_count=2,
        inventory_scanned_at=timestamp,
    )

    return manifest, snapshot


def test_inventory_fingerprint_is_deterministic(context):
    manifest, snapshot = context

    reordered = inventory(
        manifest.source_root,
        snapshot.scanned_at,
        [("a.mp3", 100), ("b.flac", 200)],
    )

    assert inventory_fingerprint(snapshot) == inventory_fingerprint(reordered)


@pytest.mark.parametrize(
    "change",
    [
        "path",
        "size",
        "timestamp",
        "root",
    ],
)
def test_inventory_changes_alter_fingerprint(context, change, tmp_path):
    manifest, snapshot = context

    root = manifest.source_root
    timestamp = snapshot.scanned_at
    entries = [("a.mp3", 100), ("b.flac", 200)]

    if change == "path":
        entries[0] = ("renamed.mp3", 100)
    elif change == "size":
        entries[0] = ("a.mp3", 101)
    elif change == "timestamp":
        timestamp = "2026-10-14T12:18:05-07:00"
    else:
        root = str(tmp_path / "another-source")

    altered = inventory(root, timestamp, entries)

    assert inventory_fingerprint(snapshot) != inventory_fingerprint(altered)


def test_forged_inventory_cannot_be_fingerprinted(context):
    manifest, snapshot = context

    forged = InventorySnapshot(
        source_root=manifest.source_root,
        scanned_at=snapshot.scanned_at,
        files={"../escape.flac": 100},
    )

    with pytest.raises(ValueError):
        inventory_fingerprint(forged)


def test_manifest_fingerprint_changes_with_revision(context):
    manifest, snapshot = context

    changed = replace(manifest, revision=2)

    assert manifest_fingerprint(manifest) != manifest_fingerprint(changed)


def test_initial_revision_record_round_trip(context):
    manifest, snapshot = context

    record = create_revision_record(manifest, snapshot)

    assert record.revision == 1
    assert record.previous_record_sha256 is None
    assert validate_revision_record(record.as_dict()) == record
    assert verify_revision_record(record, manifest, snapshot)


def test_revision_record_digest_is_deterministic(context):
    manifest, snapshot = context

    record = create_revision_record(manifest, snapshot)

    assert revision_record_fingerprint(record) == revision_record_fingerprint(
        validate_revision_record(record.as_dict())
    )


def test_revision_chain(context):
    manifest, snapshot = context

    first = create_revision_record(manifest, snapshot)

    next_snapshot = inventory(
        manifest.source_root,
        "2026-10-14T12:18:05-07:00",
        [("a.mp3", 100), ("b.flac", 201)],
    )

    second_manifest = replace(
        manifest,
        revision=2,
        inventory_scanned_at=next_snapshot.scanned_at,
        inventory_snapshot=str(
            __import__("pathlib").Path(manifest.inventory_snapshot).parent
            / "next.json"
        ),
    )

    second = create_revision_record(
        second_manifest,
        next_snapshot,
        first,
    )

    assert second.previous_record_sha256 == revision_record_fingerprint(first)
    assert verify_revision_record(
        second,
        second_manifest,
        next_snapshot,
        first,
    )


def test_modified_inventory_breaks_verification(context):
    manifest, snapshot = context

    record = create_revision_record(manifest, snapshot)

    changed = inventory(
        manifest.source_root,
        snapshot.scanned_at,
        [("a.mp3", 101), ("b.flac", 200)],
    )

    assert not verify_revision_record(record, manifest, changed)


def test_modified_manifest_breaks_verification(context):
    manifest, snapshot = context

    record = create_revision_record(manifest, snapshot)

    assert not verify_revision_record(
        record,
        replace(manifest, name="Altered"),
        snapshot,
    )


def test_wrong_predecessor_rejected(context):
    manifest, snapshot = context

    first = create_revision_record(manifest, snapshot)

    second_manifest = replace(manifest, revision=3)

    with pytest.raises(RevisionHistoryError, match="Predecessor revision"):
        create_revision_record(second_manifest, snapshot, first)


def test_missing_predecessor_rejected(context):
    manifest, snapshot = context

    with pytest.raises(RevisionHistoryError, match="Previous revision"):
        create_revision_record(
            replace(manifest, revision=2),
            snapshot,
        )


def test_inventory_count_mismatch_rejected(context):
    manifest, snapshot = context

    with pytest.raises(RevisionHistoryError, match="Inventory count"):
        create_revision_record(
            replace(manifest, inventory_count=1),
            snapshot,
        )


def test_inventory_timestamp_mismatch_rejected(context):
    manifest, snapshot = context

    with pytest.raises(RevisionHistoryError, match="Inventory timestamp"):
        create_revision_record(
            replace(
                manifest,
                inventory_scanned_at="2026-10-14T12:18:05-07:00",
            ),
            snapshot,
        )


def test_invalid_record_digest_rejected(context):
    manifest, snapshot = context

    record = create_revision_record(manifest, snapshot)
    data = record.as_dict()
    data["inventory_sha256"] = "INVALID"

    with pytest.raises(RevisionHistoryError, match="inventory_sha256"):
        validate_revision_record(data)


def test_boolean_revision_rejected(context):
    manifest, snapshot = context

    record = create_revision_record(manifest, snapshot)
    data = record.as_dict()
    data["revision"] = True

    with pytest.raises(RevisionHistoryError, match="revision"):
        validate_revision_record(data)


def test_initial_revision_cannot_have_predecessor(context):
    manifest, snapshot = context

    record = create_revision_record(manifest, snapshot)
    data = record.as_dict()
    data["previous_record_sha256"] = "a" * 64

    with pytest.raises(RevisionHistoryError, match="Initial revision"):
        validate_revision_record(data)


def test_pure_history_operations_do_not_create_files(context, tmp_path):
    manifest, snapshot = context

    before = set(tmp_path.rglob("*"))

    record = create_revision_record(manifest, snapshot)
    inventory_fingerprint(snapshot)
    manifest_fingerprint(manifest)
    revision_record_fingerprint(record)
    verify_revision_record(record, manifest, snapshot)

    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "incorrect.schema"),
        ("schema_version", True),
        ("schema_version", 2),
        ("project_id", "not-a-uuid"),
        ("revision", 0),
        ("revision", -1),
        ("manifest_sha256", "A" * 64),
        ("inventory_sha256", "0" * 63),
    ],
)
def test_malformed_revision_record_fields_rejected(
    context, field, value
):
    manifest, snapshot = context
    record = create_revision_record(manifest, snapshot)

    data = record.as_dict()
    data[field] = value

    with pytest.raises(RevisionHistoryError):
        validate_revision_record(data)


def test_unexpected_revision_record_field_rejected(context):
    manifest, snapshot = context
    record = create_revision_record(manifest, snapshot)

    data = record.as_dict()
    data["unexpected"] = "surprise"

    with pytest.raises(RevisionHistoryError):
        validate_revision_record(data)


def test_predecessor_from_another_project_rejected(context):
    manifest, snapshot = context
    first = create_revision_record(manifest, snapshot)

    from dataclasses import replace
    from uuid import uuid4

    unrelated = replace(
        first,
        project_id=str(uuid4()),
    )

    with pytest.raises(RevisionHistoryError, match="project mismatch"):
        create_revision_record(
            replace(manifest, revision=2),
            snapshot,
            unrelated,
        )


def test_revision_record_fingerprint_changes_with_content(context):
    manifest, snapshot = context
    record = create_revision_record(manifest, snapshot)

    altered = replace(
        record,
        inventory_sha256="b" * 64,
    )

    assert (
        revision_record_fingerprint(record)
        != revision_record_fingerprint(altered)
    )
