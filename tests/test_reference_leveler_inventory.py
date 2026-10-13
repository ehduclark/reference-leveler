import copy

import pytest

from reference_leveler_inventory import (
    InventoryError,
    load_snapshot,
    reconcile,
    validate_snapshot,
)


def snapshot(entries, root="/music"):
    formats = {}

    for name, _ in entries:
        suffix = "." + name.rsplit(".", 1)[-1].lower()
        formats[suffix] = formats.get(suffix, 0) + 1

    return {
        "schema_version": 1,
        "source_root": root,
        "scanned_at": "2026-10-13T12:00:00-07:00",
        "file_count": len(entries),
        "format_counts": formats,
        "files": [
            {
                "relative_path": name,
                "size_bytes": size,
            }
            for name, size in entries
        ],
    }


def test_valid_snapshot():
    result = validate_snapshot(
        snapshot([("A.flac", 100)])
    )

    assert result.files == {"A.flac": 100}


def test_reconciliation_classifies_changes():
    previous = validate_snapshot(
        snapshot([
            ("same.flac", 100),
            ("changed.flac", 200),
            ("removed.mp3", 300),
        ])
    )

    current = validate_snapshot(
        snapshot([
            ("same.flac", 100),
            ("changed.flac", 201),
            ("added.m4a", 400),
        ])
    )

    result = reconcile(previous, current)

    assert result.added == ("added.m4a",)
    assert result.removed == ("removed.mp3",)
    assert result.size_changed == ("changed.flac",)
    assert result.metadata_unchanged == ("same.flac",)

    assert result.summary()["identity_verified"] is False


@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "/absolute.flac",
        "../escape.flac",
        "music/../escape.flac",
        "./song.flac",
        "music//song.flac",
        "music\\song.flac",
        "song\x00.flac",
    ],
)
def test_unsafe_paths_rejected(bad_path):
    with pytest.raises(InventoryError):
        validate_snapshot(snapshot([(bad_path, 100)]))


def test_duplicate_path_rejected():
    with pytest.raises(InventoryError, match="Duplicate"):
        validate_snapshot(
            snapshot([
                ("song.flac", 100),
                ("song.flac", 100),
            ])
        )


def test_boolean_size_rejected():
    data = snapshot([("song.flac", 100)])
    data["files"][0]["size_bytes"] = True

    with pytest.raises(InventoryError):
        validate_snapshot(data)


def test_negative_size_rejected():
    with pytest.raises(InventoryError):
        validate_snapshot(
            snapshot([("song.flac", -1)])
        )


def test_declared_count_mismatch_rejected():
    data = snapshot([("song.flac", 100)])
    data["file_count"] = 2

    with pytest.raises(InventoryError):
        validate_snapshot(data)


def test_format_count_mismatch_rejected():
    data = snapshot([("song.flac", 100)])
    data["format_counts"] = {".mp3": 1}

    with pytest.raises(InventoryError):
        validate_snapshot(data)


def test_different_roots_rejected():
    previous = validate_snapshot(
        snapshot([("song.flac", 100)], "/music/a")
    )

    current = validate_snapshot(
        snapshot([("song.flac", 100)], "/music/b")
    )

    with pytest.raises(InventoryError, match="source roots"):
        reconcile(previous, current)


def test_unsupported_schema_rejected():
    data = snapshot([("song.flac", 100)])
    data["schema_version"] = 99

    with pytest.raises(InventoryError):
        validate_snapshot(data)


def test_input_is_not_modified():
    data = snapshot([("song.flac", 100)])
    original = copy.deepcopy(data)

    validate_snapshot(data)

    assert data == original


def test_large_inventory_reconciliation():
    """Reconcile a large synthetic inventory without private data."""

    previous_files = [
        (f"track-{number:04d}.flac", 1000 + number)
        for number in range(452)
    ]

    current_files = previous_files[2:] + [
        (f"track-{number:04d}.flac", 1000 + number)
        for number in range(452, 464)
    ]

    old = validate_snapshot(snapshot(previous_files))
    new = validate_snapshot(snapshot(current_files))

    result = reconcile(old, new)

    assert result.previous_count == 452
    assert result.current_count == 462
    assert len(result.added) == 12
    assert len(result.removed) == 2
    assert len(result.size_changed) == 0
    assert len(result.metadata_unchanged) == 450



def test_snapshot_files_are_immutable():
    result = validate_snapshot(
        snapshot([("song.flac", 100)])
    )

    with pytest.raises(TypeError):
        result.files["song.flac"] = 200


def test_snapshot_does_not_share_input_dictionary():
    data = snapshot([("song.flac", 100)])
    result = validate_snapshot(data)

    data["files"][0]["size_bytes"] = 999

    assert result.files["song.flac"] == 100


@pytest.mark.parametrize(
    "timestamp",
    [
        "",
        "not-a-date",
        "2026-10-13T12:00:00",
        "2026-10-13",
        "2026-10-13T12:00:00Z",
        "2026-10-13 12:00:00-07:00",
    ],
)
def test_invalid_or_noncanonical_timestamp_rejected(timestamp):
    data = snapshot([("song.flac", 100)])
    data["scanned_at"] = timestamp

    with pytest.raises(InventoryError):
        validate_snapshot(data)


@pytest.mark.parametrize(
    "root",
    [
        "",
        "relative/music",
        "/music/../other",
        "/music/./songs",
        "/music//songs",
        "/music/",
        "/music\\\\songs",
    ],
)
def test_noncanonical_source_root_rejected(root):
    data = snapshot([("song.flac", 100)], root=root)

    with pytest.raises(InventoryError):
        validate_snapshot(data)


def test_canonical_timestamp_with_offset_accepted():
    data = snapshot([("song.flac", 100)])
    data["scanned_at"] = "2026-10-13T12:00:00-07:00"

    result = validate_snapshot(data)

    assert result.scanned_at == data["scanned_at"]
