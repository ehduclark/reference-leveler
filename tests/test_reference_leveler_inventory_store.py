"""Inventory serialization contract tests."""

import json

import pytest

from reference_leveler_inventory import (
    InventorySnapshot,
    validate_snapshot,
)
from reference_leveler_inventory_store import serialize_inventory


def inventory():
    return validate_snapshot({
        "schema_version": 1,
        "source_root": "/tmp/reference-leveler-source",
        "scanned_at": "2026-10-13T12:00:00-07:00",
        "file_count": 3,
        "format_counts": {
            ".flac": 2,
            ".mp3": 1,
        },
        "files": [
            {"relative_path": "z.flac", "size_bytes": 300},
            {"relative_path": "a.mp3", "size_bytes": 100},
            {"relative_path": "b.flac", "size_bytes": 200},
        ],
    })


def test_serialization_round_trip():
    original = inventory()

    serialized = serialize_inventory(original)
    restored = validate_snapshot(
        json.loads(json.dumps(serialized))
    )

    assert restored == original


def test_serialization_sorts_files():
    serialized = serialize_inventory(inventory())

    assert [
        item["relative_path"]
        for item in serialized["files"]
    ] == [
        "a.mp3",
        "b.flac",
        "z.flac",
    ]


def test_serialization_is_deterministic():
    original = inventory()

    first = json.dumps(
        serialize_inventory(original),
        sort_keys=True,
    )

    second = json.dumps(
        serialize_inventory(original),
        sort_keys=True,
    )

    assert first == second


def test_forged_inventory_rejected():
    forged = InventorySnapshot(
        source_root="/tmp/reference-leveler-source",
        scanned_at="2026-10-13T12:00:00-07:00",
        files={"../escape.flac": 100},
    )

    with pytest.raises(ValueError):
        serialize_inventory(forged)


from reference_leveler_history import inventory_fingerprint
from reference_leveler_inventory_store import (
    InventoryPublicationError,
    inventory_path,
    publish_inventory,
    verify_published_inventory,
)


def test_inventory_path_is_deterministic(tmp_path):
    assert inventory_path(tmp_path, 1).name == (
        "revision-000001.json"
    )


@pytest.mark.parametrize("revision", [0, -1, True, 1.5])
def test_invalid_inventory_revision_rejected(tmp_path, revision):
    with pytest.raises(InventoryPublicationError):
        inventory_path(tmp_path, revision)


def test_publish_and_verify_inventory(tmp_path):
    original = inventory()
    expected = inventory_fingerprint(original)

    destination = publish_inventory(tmp_path, 1, original)

    assert destination.is_file()

    restored = verify_published_inventory(
        tmp_path,
        1,
        expected,
    )

    assert restored == original


def test_duplicate_publication_cannot_overwrite(tmp_path):
    original = inventory()

    destination = publish_inventory(tmp_path, 1, original)
    before = destination.read_bytes()

    with pytest.raises(FileExistsError):
        publish_inventory(tmp_path, 1, original)

    assert destination.read_bytes() == before


def test_wrong_fingerprint_rejected(tmp_path):
    publish_inventory(tmp_path, 1, inventory())

    with pytest.raises(
        InventoryPublicationError,
        match="fingerprint mismatch",
    ):
        verify_published_inventory(
            tmp_path,
            1,
            "0" * 64,
        )


def test_published_inventory_symlink_rejected(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("protected", encoding="utf-8")

    destination = inventory_path(tmp_path, 1)
    destination.symlink_to(outside)

    with pytest.raises(OSError):
        publish_inventory(tmp_path, 1, inventory())

    with pytest.raises(OSError):
        verify_published_inventory(
            tmp_path,
            1,
            inventory_fingerprint(inventory()),
        )

    assert outside.read_text(encoding="utf-8") == "protected"


def test_corrupt_published_inventory_rejected(tmp_path):
    destination = inventory_path(tmp_path, 1)
    destination.write_text("{invalid", encoding="utf-8")

    with pytest.raises(InventoryPublicationError):
        verify_published_inventory(
            tmp_path,
            1,
            inventory_fingerprint(inventory()),
        )


def test_missing_directory_does_not_get_created(tmp_path):
    missing = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
        publish_inventory(missing, 1, inventory())

    assert not missing.exists()


# ------------------------------------------------------------
# v0.2.13b3a3: adversarial inventory publication
# ------------------------------------------------------------

import concurrent.futures

import reference_leveler_inventory_store as store


def test_inventory_file_sync_failure(tmp_path, monkeypatch):
    def fail_sync(_fd):
        raise OSError("simulated file sync failure")

    monkeypatch.setattr(store.os, "fsync", fail_sync)

    with pytest.raises(OSError, match="simulated file sync"):
        publish_inventory(tmp_path, 1, inventory())

    assert not inventory_path(tmp_path, 1).exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_inventory_directory_sync_failure(tmp_path, monkeypatch):
    original_sync = store.os.fsync
    calls = 0

    def fail_second_sync(fd):
        nonlocal calls
        calls += 1

        if calls == 2:
            raise OSError("simulated directory sync failure")

        return original_sync(fd)

    monkeypatch.setattr(store.os, "fsync", fail_second_sync)

    with pytest.raises(
        store.InventoryPublicationDurabilityError
    ) as captured:
        publish_inventory(tmp_path, 1, inventory())

    assert captured.value.destination == inventory_path(tmp_path, 1)
    assert isinstance(captured.value.cause, OSError)
    assert captured.value.__cause__ is captured.value.cause

    assert verify_published_inventory(
        tmp_path,
        1,
        inventory_fingerprint(inventory()),
    ) == inventory()

    assert not list(tmp_path.glob(".*.tmp"))


def test_inventory_directory_substitution(tmp_path, monkeypatch):
    directory = tmp_path / "inventories"
    directory.mkdir()

    displaced = tmp_path / "displaced-inventories"
    original_fdopen = store.os.fdopen

    def substitute(fd, mode="r", *args, **kwargs):
        handle = original_fdopen(fd, mode, *args, **kwargs)

        if mode == "wb":
            directory.rename(displaced)
            directory.mkdir()

        return handle

    monkeypatch.setattr(store.os, "fdopen", substitute)

    with pytest.raises(
        store.InventoryPublicationError,
        match="directory changed",
    ):
        publish_inventory(directory, 1, inventory())

    assert not inventory_path(directory, 1).exists()
    assert not inventory_path(displaced, 1).exists()
    assert not list(displaced.glob(".*.tmp"))


def test_inventory_cleanup_failure_before_publication(
    tmp_path, monkeypatch
):
    original_unlink = store.os.unlink

    def fail_sync(_fd):
        raise OSError("simulated file synchronization failure")

    def fail_cleanup(name, *args, **kwargs):
        if isinstance(name, str) and name.endswith(".tmp"):
            raise PermissionError("simulated cleanup failure")

        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(store.os, "fsync", fail_sync)
    monkeypatch.setattr(store.os, "unlink", fail_cleanup)

    with pytest.raises(
        OSError,
        match="simulated file synchronization failure",
    ):
        publish_inventory(tmp_path, 1, inventory())

    assert not inventory_path(tmp_path, 1).exists()


def test_inventory_cleanup_failure_after_publication(
    tmp_path, monkeypatch
):
    original_unlink = store.os.unlink

    def fail_cleanup(name, *args, **kwargs):
        if isinstance(name, str) and name.endswith(".tmp"):
            raise PermissionError("simulated cleanup failure")

        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(store.os, "unlink", fail_cleanup)

    with pytest.raises(
        store.InventoryPublicationCleanupError
    ) as captured:
        publish_inventory(tmp_path, 1, inventory())

    assert captured.value.published is True
    assert captured.value.destination == inventory_path(tmp_path, 1)
    assert isinstance(captured.value.cause, PermissionError)

    assert verify_published_inventory(
        tmp_path,
        1,
        inventory_fingerprint(inventory()),
    ) == inventory()


def test_inventory_cleanup_does_not_hide_durability_error(
    tmp_path, monkeypatch
):
    original_sync = store.os.fsync
    original_unlink = store.os.unlink
    calls = 0

    def fail_second_sync(fd):
        nonlocal calls
        calls += 1

        if calls == 2:
            raise OSError("simulated directory sync failure")

        return original_sync(fd)

    def fail_cleanup(name, *args, **kwargs):
        if isinstance(name, str) and name.endswith(".tmp"):
            raise PermissionError("simulated cleanup failure")

        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(store.os, "fsync", fail_second_sync)
    monkeypatch.setattr(store.os, "unlink", fail_cleanup)

    with pytest.raises(
        store.InventoryPublicationDurabilityError
    ) as captured:
        publish_inventory(tmp_path, 1, inventory())

    assert captured.value.destination == inventory_path(tmp_path, 1)

    assert verify_published_inventory(
        tmp_path,
        1,
        inventory_fingerprint(inventory()),
    ) == inventory()


def test_concurrent_inventory_publication_has_one_winner(tmp_path):
    snapshot = inventory()

    def attempt():
        try:
            return publish_inventory(tmp_path, 1, snapshot)
        except FileExistsError:
            return None

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=8
    ) as executor:
        outcomes = list(executor.map(lambda _: attempt(), range(8)))

    assert sum(result is not None for result in outcomes) == 1

    assert verify_published_inventory(
        tmp_path,
        1,
        inventory_fingerprint(snapshot),
    ) == snapshot

    assert not list(tmp_path.glob(".*.tmp"))


# ------------------------------------------------------------
# v0.2.13b3a4: final publication failure coverage
# ------------------------------------------------------------

def test_inventory_temp_creation_failure(tmp_path, monkeypatch):
    original_open = store.os.open

    def fail_temp_creation(path, flags, *args, **kwargs):
        if (
            isinstance(path, str)
            and path.endswith(".tmp")
            and flags & store.os.O_CREAT
        ):
            raise PermissionError(
                "simulated temporary creation failure"
            )

        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", fail_temp_creation)

    with pytest.raises(
        PermissionError,
        match="simulated temporary creation failure",
    ):
        publish_inventory(tmp_path, 1, inventory())

    assert not inventory_path(tmp_path, 1).exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_inventory_temp_write_failure(tmp_path, monkeypatch):
    original_fdopen = store.os.fdopen

    class FailingWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def write(self, _data):
            raise OSError("simulated temporary write failure")

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def fail_temp_write(fd, mode="r", *args, **kwargs):
        handle = original_fdopen(fd, mode, *args, **kwargs)

        if mode == "wb":
            return FailingWriter(handle)

        return handle

    monkeypatch.setattr(store.os, "fdopen", fail_temp_write)

    with pytest.raises(
        OSError,
        match="simulated temporary write failure",
    ):
        publish_inventory(tmp_path, 1, inventory())

    assert not inventory_path(tmp_path, 1).exists()
    assert not list(tmp_path.glob(".*.tmp"))
