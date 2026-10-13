"""Immutable manifest publication contract tests."""

import json

import pytest

from reference_leveler_history import manifest_fingerprint
from reference_leveler_manifest_store import (
    ManifestPublicationError,
    manifest_path,
    publish_manifest,
    verify_published_manifest,
)
from reference_leveler_project import (
    create_manifest,
    validate_manifest,
)


def manifest(tmp_path):
    source = tmp_path / "source"

    return create_manifest(
        name="Manifest Publication Test",
        source_root=source,
        output_root=tmp_path / "output",
        reference_path=source / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=100,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=tmp_path / "inventory.json",
        inventory_count=0,
        inventory_scanned_at="2026-10-13T12:00:00-07:00",
    )


def test_manifest_path_is_deterministic(tmp_path):
    assert manifest_path(tmp_path, 1).name == (
        "revision-000001.json"
    )


@pytest.mark.parametrize("revision", [0, -1, True, 1.5])
def test_invalid_manifest_revision_rejected(tmp_path, revision):
    with pytest.raises(ManifestPublicationError):
        manifest_path(tmp_path, revision)


def test_publish_and_verify_manifest(tmp_path):
    directory = tmp_path / "manifests"
    directory.mkdir()

    original = manifest(tmp_path)
    expected = manifest_fingerprint(original)

    destination = publish_manifest(directory, 1, original)

    assert destination.is_file()

    restored = verify_published_manifest(
        directory,
        1,
        expected,
    )

    assert restored == original


def test_duplicate_publication_cannot_overwrite(tmp_path):
    directory = tmp_path / "manifests"
    directory.mkdir()

    original = manifest(tmp_path)

    destination = publish_manifest(directory, 1, original)
    before = destination.read_bytes()

    with pytest.raises(FileExistsError):
        publish_manifest(directory, 1, original)

    assert destination.read_bytes() == before


def test_manifest_revision_mismatch_rejected(tmp_path):
    directory = tmp_path / "manifests"
    directory.mkdir()

    with pytest.raises(
        ManifestPublicationError,
        match="revision does not match",
    ):
        publish_manifest(directory, 2, manifest(tmp_path))

    assert not manifest_path(directory, 2).exists()


def test_wrong_fingerprint_rejected(tmp_path):
    directory = tmp_path / "manifests"
    directory.mkdir()

    publish_manifest(directory, 1, manifest(tmp_path))

    with pytest.raises(
        ManifestPublicationError,
        match="fingerprint mismatch",
    ):
        verify_published_manifest(
            directory,
            1,
            "0" * 64,
        )


def test_published_manifest_symlink_rejected(tmp_path):
    directory = tmp_path / "manifests"
    directory.mkdir()

    outside = tmp_path / "outside.json"
    outside.write_text("protected", encoding="utf-8")

    destination = manifest_path(directory, 1)
    destination.symlink_to(outside)

    with pytest.raises(OSError):
        publish_manifest(directory, 1, manifest(tmp_path))

    with pytest.raises(OSError):
        verify_published_manifest(
            directory,
            1,
            manifest_fingerprint(manifest(tmp_path)),
        )

    assert outside.read_text(encoding="utf-8") == "protected"


def test_corrupt_published_manifest_rejected(tmp_path):
    directory = tmp_path / "manifests"
    directory.mkdir()

    destination = manifest_path(directory, 1)
    destination.write_text("{invalid", encoding="utf-8")

    with pytest.raises(ManifestPublicationError):
        verify_published_manifest(
            directory,
            1,
            manifest_fingerprint(manifest(tmp_path)),
        )


def test_published_revision_mismatch_rejected(tmp_path):
    directory = tmp_path / "manifests"
    directory.mkdir()

    original = manifest(tmp_path)
    data = original.as_dict()
    data["revision"] = 2

    altered = validate_manifest(data)

    destination = manifest_path(directory, 1)
    destination.write_text(
        json.dumps(altered.as_dict()),
        encoding="utf-8",
    )

    with pytest.raises(
        ManifestPublicationError,
        match="revision mismatch",
    ):
        verify_published_manifest(
            directory,
            1,
            manifest_fingerprint(altered),
        )


def test_missing_directory_does_not_get_created(tmp_path):
    directory = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
        publish_manifest(directory, 1, manifest(tmp_path))

    assert not directory.exists()


# ------------------------------------------------------------
# v0.2.13b3b1: adversarial publication
# ------------------------------------------------------------

import concurrent.futures

import reference_leveler_manifest_store as store


def test_manifest_file_sync_failure(tmp_path, monkeypatch):
    directory = tmp_path / "manifests"
    directory.mkdir()

    def fail_sync(_fd):
        raise OSError("simulated file sync failure")

    monkeypatch.setattr(store.os, "fsync", fail_sync)

    with pytest.raises(OSError, match="simulated file sync"):
        publish_manifest(directory, 1, manifest(tmp_path))

    assert not manifest_path(directory, 1).exists()
    assert not list(directory.glob(".*.tmp"))


def test_manifest_directory_sync_failure(tmp_path, monkeypatch):
    directory = tmp_path / "manifests"
    directory.mkdir()

    original_sync = store.os.fsync
    calls = 0

    def fail_second_sync(fd):
        nonlocal calls
        calls += 1

        if calls == 2:
            raise OSError("simulated directory sync failure")

        return original_sync(fd)

    monkeypatch.setattr(store.os, "fsync", fail_second_sync)

    original = manifest(tmp_path)

    with pytest.raises(
        store.ManifestPublicationDurabilityError
    ) as captured:
        publish_manifest(directory, 1, original)

    assert captured.value.destination == manifest_path(directory, 1)
    assert captured.value.__cause__ is captured.value.cause

    assert verify_published_manifest(
        directory,
        1,
        manifest_fingerprint(original),
    ) == original

    assert not list(directory.glob(".*.tmp"))


def test_manifest_directory_substitution(tmp_path, monkeypatch):
    directory = tmp_path / "manifests"
    directory.mkdir()

    displaced = tmp_path / "displaced-manifests"
    original_fdopen = store.os.fdopen

    def substitute(fd, mode="r", *args, **kwargs):
        handle = original_fdopen(fd, mode, *args, **kwargs)

        if mode == "wb":
            directory.rename(displaced)
            directory.mkdir()

        return handle

    monkeypatch.setattr(store.os, "fdopen", substitute)

    with pytest.raises(
        store.ManifestPublicationError,
        match="directory changed",
    ):
        publish_manifest(directory, 1, manifest(tmp_path))

    assert not manifest_path(directory, 1).exists()
    assert not manifest_path(displaced, 1).exists()
    assert not list(displaced.glob(".*.tmp"))


def test_manifest_temp_creation_failure(tmp_path, monkeypatch):
    directory = tmp_path / "manifests"
    directory.mkdir()

    original_open = store.os.open

    def fail_creation(path, flags, *args, **kwargs):
        if (
            isinstance(path, str)
            and path.endswith(".tmp")
            and flags & store.os.O_CREAT
        ):
            raise PermissionError("simulated creation failure")

        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(store.os, "open", fail_creation)

    with pytest.raises(
        PermissionError,
        match="simulated creation failure",
    ):
        publish_manifest(directory, 1, manifest(tmp_path))

    assert not manifest_path(directory, 1).exists()
    assert not list(directory.glob(".*.tmp"))


def test_manifest_temp_write_failure(tmp_path, monkeypatch):
    directory = tmp_path / "manifests"
    directory.mkdir()

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
            raise OSError("simulated write failure")

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def fail_write(fd, mode="r", *args, **kwargs):
        handle = original_fdopen(fd, mode, *args, **kwargs)

        if mode == "wb":
            return FailingWriter(handle)

        return handle

    monkeypatch.setattr(store.os, "fdopen", fail_write)

    with pytest.raises(
        OSError,
        match="simulated write failure",
    ):
        publish_manifest(directory, 1, manifest(tmp_path))

    assert not manifest_path(directory, 1).exists()
    assert not list(directory.glob(".*.tmp"))


def test_manifest_cleanup_failure_before_publication(
    tmp_path, monkeypatch
):
    directory = tmp_path / "manifests"
    directory.mkdir()

    original_unlink = store.os.unlink

    def fail_sync(_fd):
        raise OSError("simulated file sync failure")

    def fail_cleanup(name, *args, **kwargs):
        if isinstance(name, str) and name.endswith(".tmp"):
            raise PermissionError("simulated cleanup failure")

        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(store.os, "fsync", fail_sync)
    monkeypatch.setattr(store.os, "unlink", fail_cleanup)

    with pytest.raises(OSError, match="simulated file sync"):
        publish_manifest(directory, 1, manifest(tmp_path))

    assert not manifest_path(directory, 1).exists()


def test_manifest_cleanup_failure_after_publication(
    tmp_path, monkeypatch
):
    directory = tmp_path / "manifests"
    directory.mkdir()

    original_unlink = store.os.unlink

    def fail_cleanup(name, *args, **kwargs):
        if isinstance(name, str) and name.endswith(".tmp"):
            raise PermissionError("simulated cleanup failure")

        return original_unlink(name, *args, **kwargs)

    monkeypatch.setattr(store.os, "unlink", fail_cleanup)

    original = manifest(tmp_path)

    with pytest.raises(
        store.ManifestPublicationCleanupError
    ) as captured:
        publish_manifest(directory, 1, original)

    assert captured.value.published is True
    assert captured.value.destination == manifest_path(directory, 1)

    assert verify_published_manifest(
        directory,
        1,
        manifest_fingerprint(original),
    ) == original


def test_manifest_cleanup_does_not_hide_durability_error(
    tmp_path, monkeypatch
):
    directory = tmp_path / "manifests"
    directory.mkdir()

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
        store.ManifestPublicationDurabilityError
    ):
        publish_manifest(directory, 1, manifest(tmp_path))

    assert manifest_path(directory, 1).exists()


def test_concurrent_manifest_publication_has_one_winner(tmp_path):
    directory = tmp_path / "manifests"
    directory.mkdir()

    original = manifest(tmp_path)

    def attempt():
        try:
            return publish_manifest(directory, 1, original)
        except FileExistsError:
            return None

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=8
    ) as executor:
        outcomes = list(executor.map(lambda _: attempt(), range(8)))

    assert sum(result is not None for result in outcomes) == 1

    assert verify_published_manifest(
        directory,
        1,
        manifest_fingerprint(original),
    ) == original

    assert not list(directory.glob(".*.tmp"))
