"""Read-only published revision-chain regression coverage."""

import json

from dataclasses import replace

import pytest

from reference_leveler_history import (
    create_revision_record,
    revision_record_fingerprint,
)
from reference_leveler_history_store import (
    RevisionChainError,
    load_published_revision,
    revision_path,
    verify_published_chain,
)
from reference_leveler_inventory import validate_snapshot
from reference_leveler_project import create_manifest


@pytest.fixture
def chain(tmp_path):
    source = tmp_path / "source"
    directory = tmp_path / "history"
    directory.mkdir()

    records = []
    previous = None

    for revision in range(1, 4):
        timestamp = f"2026-10-{revision + 10:02d}T12:00:00-07:00"

        snapshot = validate_snapshot({
            "schema_version": 1,
            "source_root": str(source),
            "scanned_at": timestamp,
            "file_count": 1,
            "format_counts": {".flac": 1},
            "files": [
                {
                    "relative_path": "song.flac",
                    "size_bytes": 100 + revision,
                }
            ],
        })

        if revision == 1:
            manifest = create_manifest(
                name="Chain Test",
                source_root=source,
                output_root=tmp_path / "output",
                reference_path=source / "reference.flac",
                reference_sha256="a" * 64,
                reference_size=100,
                target_lufs=-11.0,
                ceiling_dbtp=-1.0,
                profile="consistent",
                inventory_snapshot=tmp_path / "snapshot-1.json",
                inventory_count=1,
                inventory_scanned_at=timestamp,
            )
        else:
            manifest = replace(
                manifest,
                revision=revision,
                inventory_snapshot=str(
                    tmp_path / f"snapshot-{revision}.json"
                ),
                inventory_scanned_at=timestamp,
            )

        record = create_revision_record(
            manifest,
            snapshot,
            previous,
        )

        revision_path(directory, revision).write_text(
            json.dumps(record.as_dict()),
            encoding="utf-8",
        )

        records.append(record)
        previous = record

    return directory, tuple(records)


def test_revision_paths_are_deterministic(tmp_path):
    assert revision_path(tmp_path, 1).name == "revision-000001.json"
    assert revision_path(tmp_path, 1000000).name == (
        "revision-1000000.json"
    )


@pytest.mark.parametrize("revision", [0, -1, True, 1.5])
def test_invalid_revision_path_rejected(tmp_path, revision):
    with pytest.raises(RevisionChainError):
        revision_path(tmp_path, revision)


def test_load_published_revision(chain):
    directory, records = chain

    assert load_published_revision(directory, 2) == records[1]


def test_complete_chain_verifies(chain):
    directory, records = chain

    assert verify_published_chain(directory, 3) == records


def test_trusted_head_verifies(chain):
    directory, records = chain

    assert verify_published_chain(
        directory,
        3,
        revision_record_fingerprint(records[-1]),
    ) == records


def test_wrong_trusted_head_rejected(chain):
    directory, records = chain

    with pytest.raises(RevisionChainError, match="Trusted head"):
        verify_published_chain(directory, 3, "0" * 64)


def test_missing_middle_revision_rejected(chain):
    directory, records = chain

    revision_path(directory, 2).unlink()

    with pytest.raises(RevisionChainError, match="Missing"):
        verify_published_chain(directory, 3)


def test_corrupted_middle_revision_rejected(chain):
    directory, records = chain

    revision_path(directory, 2).write_text(
        "{invalid-json",
        encoding="utf-8",
    )

    with pytest.raises(RevisionChainError):
        verify_published_chain(directory, 3)


def test_tampered_middle_revision_breaks_link(chain):
    directory, records = chain

    data = records[1].as_dict()
    data["inventory_sha256"] = "b" * 64

    revision_path(directory, 2).write_text(
        json.dumps(data),
        encoding="utf-8",
    )

    with pytest.raises(RevisionChainError, match="Broken predecessor"):
        verify_published_chain(directory, 3)


def test_project_identity_mismatch_rejected(chain):
    directory, records = chain

    data = records[1].as_dict()
    data["project_id"] = (
        "12345678-1234-4234-8234-123456789abc"
    )

    revision_path(directory, 2).write_text(
        json.dumps(data),
        encoding="utf-8",
    )

    with pytest.raises(RevisionChainError, match="Project identity"):
        verify_published_chain(directory, 3)


def test_revision_number_mismatch_rejected(chain):
    directory, records = chain

    data = records[1].as_dict()
    data["revision"] = 7

    revision_path(directory, 2).write_text(
        json.dumps(data),
        encoding="utf-8",
    )

    with pytest.raises(RevisionChainError, match="Revision number"):
        verify_published_chain(directory, 3)


def test_symlink_revision_rejected(chain, tmp_path):
    directory, records = chain

    target = tmp_path / "outside.json"
    target.write_text(
        json.dumps(records[1].as_dict()),
        encoding="utf-8",
    )

    revision_path(directory, 2).unlink()
    revision_path(directory, 2).symlink_to(target)

    with pytest.raises(OSError):
        load_published_revision(directory, 2)


def test_verification_does_not_modify_files(chain):
    directory, records = chain

    before = {
        path.name: path.read_bytes()
        for path in directory.iterdir()
    }

    verify_published_chain(directory, 3)

    after = {
        path.name: path.read_bytes()
        for path in directory.iterdir()
    }

    assert before == after


def test_publication_creates_initial_record(tmp_path):
    from reference_leveler_history_store import publish_revision

    source = tmp_path / "source"
    directory = tmp_path / "history"
    directory.mkdir()

    snapshot = validate_snapshot({
        "schema_version": 1,
        "source_root": str(source),
        "scanned_at": "2026-10-13T12:00:00-07:00",
        "file_count": 1,
        "format_counts": {".flac": 1},
        "files": [
            {"relative_path": "song.flac", "size_bytes": 100}
        ],
    })

    manifest = create_manifest(
        name="Publication Test",
        source_root=source,
        output_root=tmp_path / "output",
        reference_path=source / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=100,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=tmp_path / "snapshot.json",
        inventory_count=1,
        inventory_scanned_at=snapshot.scanned_at,
    )

    record = create_revision_record(manifest, snapshot)

    destination = publish_revision(directory, record)

    assert destination == revision_path(directory, 1)
    assert load_published_revision(directory, 1) == record
    assert verify_published_chain(directory, 1) == (record,)
    assert not list(directory.glob(".*.tmp"))


def test_duplicate_publication_does_not_replace(chain):
    from reference_leveler_history_store import publish_revision

    directory, records = chain
    destination = revision_path(directory, 1)
    before = destination.read_bytes()

    with pytest.raises(FileExistsError):
        publish_revision(directory, records[0])

    assert destination.read_bytes() == before


def test_missing_predecessor_blocks_publication(chain):
    from reference_leveler_history_store import publish_revision

    directory, records = chain
    revision_path(directory, 2).unlink()
    revision_path(directory, 3).unlink()

    with pytest.raises(RevisionChainError, match="Missing"):
        publish_revision(directory, records[2])

    assert not revision_path(directory, 3).exists()


def test_broken_earlier_chain_blocks_publication(chain):
    from reference_leveler_history_store import publish_revision

    directory, records = chain
    revision_path(directory, 3).unlink()

    data = records[0].as_dict()
    data["inventory_sha256"] = "b" * 64

    revision_path(directory, 1).write_text(
        json.dumps(data),
        encoding="utf-8",
    )

    with pytest.raises(RevisionChainError, match="Broken predecessor"):
        publish_revision(directory, records[2])

    assert not revision_path(directory, 3).exists()


def test_concurrent_publication_has_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from reference_leveler_history_store import publish_revision

    source = tmp_path / "source"
    directory = tmp_path / "history"
    directory.mkdir()

    snapshot = validate_snapshot({
        "schema_version": 1,
        "source_root": str(source),
        "scanned_at": "2026-10-13T12:00:00-07:00",
        "file_count": 0,
        "format_counts": {},
        "files": [],
    })

    manifest = create_manifest(
        name="Concurrent Test",
        source_root=source,
        output_root=tmp_path / "output",
        reference_path=source / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=100,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=tmp_path / "snapshot.json",
        inventory_count=0,
        inventory_scanned_at=snapshot.scanned_at,
    )

    record = create_revision_record(manifest, snapshot)

    def attempt(_):
        try:
            publish_revision(directory, record)
            return "created"
        except FileExistsError:
            return "exists"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, range(2)))

    assert sorted(outcomes) == ["created", "exists"]
    assert load_published_revision(directory, 1) == record
    assert not list(directory.glob(".*.tmp"))


def test_file_sync_failure_prevents_publication(
    tmp_path, monkeypatch
):
    import reference_leveler_history_store as module
    from reference_leveler_history_store import publish_revision

    source = tmp_path / "source"
    directory = tmp_path / "history"
    directory.mkdir()

    snapshot = validate_snapshot({
        "schema_version": 1,
        "source_root": str(source),
        "scanned_at": "2026-10-13T12:00:00-07:00",
        "file_count": 0,
        "format_counts": {},
        "files": [],
    })

    manifest = create_manifest(
        name="Sync Test",
        source_root=source,
        output_root=tmp_path / "output",
        reference_path=source / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=100,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=tmp_path / "snapshot.json",
        inventory_count=0,
        inventory_scanned_at=snapshot.scanned_at,
    )

    record = create_revision_record(manifest, snapshot)

    def fail_sync(_fd):
        raise OSError("simulated file sync failure")

    monkeypatch.setattr(module.os, "fsync", fail_sync)

    with pytest.raises(OSError, match="simulated file sync"):
        publish_revision(directory, record)

    assert not revision_path(directory, 1).exists()
    assert not list(directory.glob(".*.tmp"))


def test_directory_sync_failure_reports_uncertainty(
    tmp_path, monkeypatch
):
    import reference_leveler_history_store as module
    from reference_leveler_history_store import (
        RevisionPublicationDurabilityError,
        publish_revision,
    )

    source = tmp_path / "source"
    directory = tmp_path / "history"
    directory.mkdir()

    snapshot = validate_snapshot({
        "schema_version": 1,
        "source_root": str(source),
        "scanned_at": "2026-10-13T12:00:00-07:00",
        "file_count": 0,
        "format_counts": {},
        "files": [],
    })

    manifest = create_manifest(
        name="Durability Test",
        source_root=source,
        output_root=tmp_path / "output",
        reference_path=source / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=100,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=tmp_path / "snapshot.json",
        inventory_count=0,
        inventory_scanned_at=snapshot.scanned_at,
    )

    record = create_revision_record(manifest, snapshot)

    original_sync = module.os.fsync
    calls = 0

    def fail_second_sync(fd):
        nonlocal calls
        calls += 1

        if calls == 2:
            raise OSError("simulated directory sync failure")

        return original_sync(fd)

    monkeypatch.setattr(module.os, "fsync", fail_second_sync)

    with pytest.raises(
        RevisionPublicationDurabilityError
    ) as captured:
        publish_revision(directory, record)

    assert captured.value.destination == revision_path(directory, 1)
    assert str(captured.value.cause) == (
        "simulated directory sync failure"
    )
    assert captured.value.__cause__ is captured.value.cause
    assert load_published_revision(directory, 1) == record
    assert not list(directory.glob(".*.tmp"))


# ------------------------------------------------------------
# v0.2.13b2b: adversarial publication tests
# ------------------------------------------------------------

def _publication_fixture(tmp_path):
    """Construct an unpublished initial revision without disk writes."""

    source = tmp_path / "source"
    directory = tmp_path / "history"
    directory.mkdir()

    snapshot = validate_snapshot({
        "schema_version": 1,
        "source_root": str(source),
        "scanned_at": "2026-10-13T12:00:00-07:00",
        "file_count": 1,
        "format_counts": {".flac": 1},
        "files": [
            {
                "relative_path": "song.flac",
                "size_bytes": 100,
            }
        ],
    })

    manifest = create_manifest(
        name="Adversarial Publication",
        source_root=source,
        output_root=tmp_path / "output",
        reference_path=source / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=100,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=tmp_path / "snapshot.json",
        inventory_count=1,
        inventory_scanned_at=snapshot.scanned_at,
    )

    return directory, create_revision_record(manifest, snapshot)


def test_existing_destination_symlink_is_never_replaced(tmp_path):
    from reference_leveler_history_store import publish_revision

    directory, record = _publication_fixture(tmp_path)

    outside = tmp_path / "outside.json"
    outside.write_text("untouched", encoding="utf-8")

    destination = revision_path(directory, 1)
    destination.symlink_to(outside)

    with pytest.raises(FileExistsError):
        publish_revision(directory, record)

    assert destination.is_symlink()
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert not list(directory.glob(".*.tmp"))


def test_temporary_creation_failure_does_not_publish(
    tmp_path, monkeypatch
):
    import reference_leveler_history_store as module

    directory, record = _publication_fixture(tmp_path)

    original_open = module.os.open

    def fail_temporary_open(path, flags, *args, **kwargs):
        if isinstance(path, str) and path.endswith(".tmp"):
            raise PermissionError("simulated temporary creation failure")

        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", fail_temporary_open)

    with pytest.raises(
        PermissionError,
        match="simulated temporary creation failure",
    ):
        module.publish_revision(directory, record)

    assert not revision_path(directory, 1).exists()
    assert not list(directory.glob(".*.tmp"))


def test_temporary_write_failure_does_not_publish(
    tmp_path, monkeypatch
):
    import reference_leveler_history_store as module

    directory, record = _publication_fixture(tmp_path)

    original_fdopen = module.os.fdopen

    class FailingWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def write(self, data):
            raise OSError("simulated temporary write failure")

        def flush(self):
            return self.handle.flush()

        def fileno(self):
            return self.handle.fileno()

    def fail_fdopen(fd, mode="r", *args, **kwargs):
        handle = original_fdopen(fd, mode, *args, **kwargs)

        if mode == "wb":
            return FailingWriter(handle)

        return handle

    monkeypatch.setattr(module.os, "fdopen", fail_fdopen)

    with pytest.raises(
        OSError,
        match="simulated temporary write failure",
    ):
        module.publish_revision(directory, record)

    assert not revision_path(directory, 1).exists()
    assert not list(directory.glob(".*.tmp"))


def test_directory_substitution_before_link_is_rejected(
    tmp_path, monkeypatch
):
    import reference_leveler_history_store as module

    directory, record = _publication_fixture(tmp_path)

    original_fdopen = module.os.fdopen
    displaced = tmp_path / "displaced-history"

    def substitute_directory(fd, mode="r", *args, **kwargs):
        handle = original_fdopen(fd, mode, *args, **kwargs)

        if mode == "wb":
            directory.rename(displaced)
            directory.mkdir()

        return handle

    monkeypatch.setattr(
        module.os,
        "fdopen",
        substitute_directory,
    )

    with pytest.raises(
        RevisionChainError,
        match="directory changed",
    ):
        module.publish_revision(directory, record)

    assert not revision_path(directory, 1).exists()
    assert not revision_path(displaced, 1).exists()
    assert not list(displaced.glob(".*.tmp"))


def test_predecessor_change_before_link_is_rejected(
    chain, monkeypatch
):
    import reference_leveler_history_store as module

    directory, records = chain

    revision_path(directory, 3).unlink()

    original_fdopen = module.os.fdopen

    def corrupt_predecessor(fd, mode="r", *args, **kwargs):
        handle = original_fdopen(fd, mode, *args, **kwargs)

        if mode == "wb":
            data = records[1].as_dict()
            data["inventory_sha256"] = "b" * 64

            revision_path(directory, 2).write_text(
                json.dumps(data),
                encoding="utf-8",
            )

        return handle

    monkeypatch.setattr(
        module.os,
        "fdopen",
        corrupt_predecessor,
    )

    with pytest.raises(
        RevisionChainError,
        match="Predecessor changed",
    ):
        module.publish_revision(directory, records[2])

    assert not revision_path(directory, 3).exists()
    assert not list(directory.glob(".*.tmp"))


def test_uncertain_durability_recovery_preserves_identity(
    tmp_path, monkeypatch
):
    import reference_leveler_history_store as module

    directory, record = _publication_fixture(tmp_path)

    original_sync = module.os.fsync
    calls = 0

    def fail_directory_sync(fd):
        nonlocal calls
        calls += 1

        if calls == 2:
            raise OSError("simulated uncertain durability")

        return original_sync(fd)

    monkeypatch.setattr(module.os, "fsync", fail_directory_sync)

    with pytest.raises(
        module.RevisionPublicationDurabilityError
    ):
        module.publish_revision(directory, record)

    destination = revision_path(directory, 1)

    assert destination.exists()
    assert module.load_published_revision(directory, 1) == record

    trusted_digest = revision_record_fingerprint(record)

    assert module.verify_published_chain(
        directory,
        1,
        expected_head_sha256=trusted_digest,
    ) == (record,)

    before = destination.read_bytes()

    with pytest.raises(FileExistsError):
        module.publish_revision(directory, record)

    assert destination.read_bytes() == before
    assert not list(directory.glob(".*.tmp"))


# ------------------------------------------------------------
# Cleanup failure and publication outcome regressions
# ------------------------------------------------------------

def test_cleanup_failure_before_publication_reports_not_published(
    tmp_path, monkeypatch
):
    import reference_leveler_history_store as module

    directory, record = _publication_fixture(tmp_path)

    original_sync = module.os.fsync
    original_unlink = module.os.unlink

    def fail_file_sync(fd):
        raise OSError("simulated file synchronization failure")

    def fail_cleanup(path, *args, **kwargs):
        if isinstance(path, str) and path.endswith(".tmp"):
            raise PermissionError("simulated cleanup failure")

        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "fsync", fail_file_sync)
    monkeypatch.setattr(module.os, "unlink", fail_cleanup)

    with pytest.raises(
        OSError,
        match="simulated file synchronization failure",
    ):
        module.publish_revision(directory, record)

    assert not revision_path(directory, 1).exists()


def test_cleanup_failure_after_publication_reports_published(
    tmp_path, monkeypatch
):
    import reference_leveler_history_store as module

    directory, record = _publication_fixture(tmp_path)

    original_unlink = module.os.unlink

    def fail_cleanup(path, *args, **kwargs):
        if isinstance(path, str) and path.endswith(".tmp"):
            raise PermissionError("simulated cleanup failure")

        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "unlink", fail_cleanup)

    with pytest.raises(
        module.RevisionPublicationCleanupError
    ) as captured:
        module.publish_revision(directory, record)

    assert captured.value.published is True
    assert captured.value.destination == revision_path(directory, 1)
    assert isinstance(captured.value.cause, PermissionError)
    assert module.load_published_revision(directory, 1) == record


def test_cleanup_failure_does_not_hide_durability_error(
    tmp_path, monkeypatch
):
    import reference_leveler_history_store as module

    directory, record = _publication_fixture(tmp_path)

    original_sync = module.os.fsync
    original_unlink = module.os.unlink
    calls = 0

    def fail_directory_sync(fd):
        nonlocal calls
        calls += 1

        if calls == 2:
            raise OSError("simulated directory synchronization failure")

        return original_sync(fd)

    def fail_cleanup(path, *args, **kwargs):
        if isinstance(path, str) and path.endswith(".tmp"):
            raise PermissionError("simulated cleanup failure")

        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "fsync", fail_directory_sync)
    monkeypatch.setattr(module.os, "unlink", fail_cleanup)

    with pytest.raises(
        module.RevisionPublicationDurabilityError
    ) as captured:
        module.publish_revision(directory, record)

    assert captured.value.destination == revision_path(directory, 1)
    assert module.load_published_revision(directory, 1) == record
