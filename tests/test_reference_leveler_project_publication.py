"""Integration tests for initial project revision publication."""

from dataclasses import replace

import pytest

import reference_leveler_project_publication as coordinator

from reference_leveler_history import (
    inventory_fingerprint,
    manifest_fingerprint,
)
from reference_leveler_inventory import validate_snapshot
from reference_leveler_project import create_manifest


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"

    for name in ("inventories", "manifests", "history"):
        (root / name).mkdir(parents=True)

    source_root = tmp_path / "source"

    inventory = validate_snapshot({
        "schema_version": 1,
        "source_root": str(source_root),
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
        name="Coordinator Integration Test",
        source_root=source_root,
        output_root=tmp_path / "output",
        reference_path=source_root / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=100,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=(
            root / "inventories" / "revision-000001.json"
        ),
        inventory_count=1,
        inventory_scanned_at=inventory.scanned_at,
    )

    return root, manifest, inventory


def artifact_paths(root):
    return tuple(
        root / directory / "revision-000001.json"
        for directory in ("inventories", "manifests", "history")
    )


def test_initial_publication_creates_three_artifacts(project):
    root, manifest, inventory = project

    result = coordinator.publish_initial_project(
        root, manifest, inventory
    )

    assert all(path.is_file() for path in artifact_paths(root))
    assert result.manifest == manifest
    assert result.inventory == inventory
    assert result.record.revision == 1
    assert result.record.previous_record_sha256 is None


def test_committed_revision_verifies_all_artifacts(project):
    root, manifest, inventory = project

    coordinator.publish_initial_project(root, manifest, inventory)

    restored = coordinator.verify_committed_project(root)

    assert restored.record.manifest_sha256 == (
        manifest_fingerprint(manifest)
    )
    assert restored.record.inventory_sha256 == (
        inventory_fingerprint(inventory)
    )
    assert restored.manifest == manifest
    assert restored.inventory == inventory


def test_duplicate_publication_preserves_existing_artifacts(project):
    root, manifest, inventory = project

    coordinator.publish_initial_project(root, manifest, inventory)

    before = {
        path: path.read_bytes()
        for path in artifact_paths(root)
    }

    with pytest.raises(
        coordinator.ProjectPublicationError,
        match="already contains",
    ):
        coordinator.publish_initial_project(
            root, manifest, inventory
        )

    assert {
        path: path.read_bytes()
        for path in artifact_paths(root)
    } == before


def test_wrong_inventory_destination_rejected_before_writing(project):
    root, manifest, inventory = project

    altered = replace(
        manifest,
        inventory_snapshot=str(root / "wrong.json"),
    )

    with pytest.raises(
        coordinator.ProjectPublicationError,
        match="inventory path",
    ):
        coordinator.publish_initial_project(
            root, altered, inventory
        )

    assert not any(path.exists() for path in artifact_paths(root))


def test_missing_project_directory_rejected_before_writing(project):
    root, manifest, inventory = project

    (root / "history").rmdir()

    with pytest.raises(
        coordinator.ProjectPublicationError,
        match="existing real project directory",
    ):
        coordinator.publish_initial_project(
            root, manifest, inventory
        )

    assert not any(path.exists() for path in artifact_paths(root))


def test_manifest_failure_leaves_revision_uncommitted(
    project, monkeypatch
):
    root, manifest, inventory = project

    def fail_manifest(*args, **kwargs):
        raise OSError("simulated manifest publication failure")

    monkeypatch.setattr(
        coordinator,
        "publish_manifest",
        fail_manifest,
    )

    with pytest.raises(
        OSError,
        match="simulated manifest publication failure",
    ):
        coordinator.publish_initial_project(
            root, manifest, inventory
        )

    inventory_path, manifest_path, history_path = (
        artifact_paths(root)
    )

    assert inventory_path.is_file()
    assert not manifest_path.exists()
    assert not history_path.exists()

    with pytest.raises(Exception):
        coordinator.verify_committed_project(root)



def test_tampered_inventory_rejected(project):
    root, manifest, inventory = project
    coordinator.publish_initial_project(root, manifest, inventory)

    inventory_path, _, _ = artifact_paths(root)
    import json

    data = json.loads(inventory_path.read_text(encoding="utf-8"))
    data["files"][0]["size_bytes"] += 1

    inventory_path.write_text(
        json.dumps(data, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(Exception):
        coordinator.verify_committed_project(root)


def test_tampered_manifest_rejected(project):
    root, manifest, inventory = project
    coordinator.publish_initial_project(root, manifest, inventory)

    _, manifest_path, _ = artifact_paths(root)
    import json

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["name"] = "Tampered Project Name"

    manifest_path.write_text(
        json.dumps(data, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(Exception):
        coordinator.verify_committed_project(root)


def test_missing_inventory_rejected(project):
    root, manifest, inventory = project
    coordinator.publish_initial_project(root, manifest, inventory)

    inventory_path, _, _ = artifact_paths(root)
    inventory_path.unlink()

    with pytest.raises(Exception):
        coordinator.verify_committed_project(root)


def test_missing_manifest_rejected(project):
    root, manifest, inventory = project
    coordinator.publish_initial_project(root, manifest, inventory)

    _, manifest_path, _ = artifact_paths(root)
    manifest_path.unlink()

    with pytest.raises(Exception):
        coordinator.verify_committed_project(root)


def test_history_failure_preserves_uncommitted_artifacts(
    project, monkeypatch
):
    root, manifest, inventory = project

    def fail_history(*args, **kwargs):
        raise OSError("simulated history publication failure")

    monkeypatch.setattr(
        coordinator,
        "publish_revision",
        fail_history,
    )

    with pytest.raises(
        OSError,
        match="simulated history publication failure",
    ):
        coordinator.publish_initial_project(
            root, manifest, inventory
        )

    inventory_path, manifest_path, history_path = (
        artifact_paths(root)
    )

    assert inventory_path.is_file()
    assert manifest_path.is_file()
    assert not history_path.exists()

    with pytest.raises(Exception):
        coordinator.verify_committed_project(root)


def test_uncertain_history_durability_is_not_reported_as_success(
    project, monkeypatch
):
    from reference_leveler_history_store import (
        RevisionPublicationDurabilityError,
    )

    root, manifest, inventory = project
    original = coordinator.publish_revision

    def publish_then_fail(directory, record):
        destination = original(directory, record)

        raise RevisionPublicationDurabilityError(
            destination,
            OSError("simulated directory synchronization failure"),
        )

    monkeypatch.setattr(
        coordinator,
        "publish_revision",
        publish_then_fail,
    )

    with pytest.raises(RevisionPublicationDurabilityError):
        coordinator.publish_initial_project(
            root, manifest, inventory
        )

    assert all(path.is_file() for path in artifact_paths(root))


def test_inventory_source_root_mismatch_rejected_before_writing(
    project
):
    root, manifest, inventory = project

    altered = replace(
        manifest,
        source_root=str(root / "different-source"),
    )

    with pytest.raises(ValueError, match="source root mismatch"):
        coordinator.publish_initial_project(
            root, altered, inventory
        )

    assert not any(path.exists() for path in artifact_paths(root))


def test_inventory_count_mismatch_rejected_before_writing(project):
    root, manifest, inventory = project

    altered = replace(
        manifest,
        inventory_count=2,
    )

    with pytest.raises(ValueError, match="count mismatch"):
        coordinator.publish_initial_project(
            root, altered, inventory
        )

    assert not any(path.exists() for path in artifact_paths(root))


def test_inventory_timestamp_mismatch_rejected_before_writing(
    project
):
    root, manifest, inventory = project

    altered = replace(
        manifest,
        inventory_scanned_at="2026-10-14T12:00:00-07:00",
    )

    with pytest.raises(ValueError, match="timestamp mismatch"):
        coordinator.publish_initial_project(
            root, altered, inventory
        )

    assert not any(path.exists() for path in artifact_paths(root))



def test_correct_trusted_project_identity(project):
    root, manifest, inventory = project

    committed = coordinator.publish_initial_project(
        root, manifest, inventory
    )

    verified = coordinator.verify_committed_project(
        root,
        expected_project_id=committed.record.project_id,
    )

    assert verified == committed


def test_incorrect_trusted_project_identity(project):
    root, manifest, inventory = project

    coordinator.publish_initial_project(root, manifest, inventory)

    with pytest.raises(
        coordinator.ProjectPublicationError,
        match="identity mismatch",
    ):
        coordinator.verify_committed_project(
            root,
            expected_project_id="wrong-project-id",
        )


def test_correct_trusted_history_fingerprint(project):
    from reference_leveler_history import (
        revision_record_fingerprint,
    )

    root, manifest, inventory = project

    committed = coordinator.publish_initial_project(
        root, manifest, inventory
    )

    verified = coordinator.verify_committed_project(
        root,
        expected_head_sha256=revision_record_fingerprint(
            committed.record
        ),
    )

    assert verified == committed


def test_incorrect_trusted_history_fingerprint(project):
    root, manifest, inventory = project

    coordinator.publish_initial_project(root, manifest, inventory)

    with pytest.raises(ValueError):
        coordinator.verify_committed_project(
            root,
            expected_head_sha256="0" * 64,
        )


def test_directory_replacement_before_manifest_publication(
    project, monkeypatch
):
    root, manifest, inventory = project

    original = coordinator.publish_inventory

    def publish_then_replace(*args, **kwargs):
        result = original(*args, **kwargs)

        directory = root / "manifests"
        displaced = root / "manifests-displaced"

        directory.rename(displaced)
        directory.mkdir()

        return result

    monkeypatch.setattr(
        coordinator,
        "publish_inventory",
        publish_then_replace,
    )

    with pytest.raises(
        coordinator.ProjectPublicationError,
        match="directory changed",
    ):
        coordinator.publish_initial_project(
            root, manifest, inventory
        )

    inventory_path, manifest_path, history_path = (
        artifact_paths(root)
    )

    assert inventory_path.is_file()
    assert not manifest_path.exists()
    assert not history_path.exists()


def test_directory_replacement_before_history_publication(
    project, monkeypatch
):
    root, manifest, inventory = project

    original = coordinator.publish_manifest

    def publish_then_replace(*args, **kwargs):
        result = original(*args, **kwargs)

        directory = root / "history"
        displaced = root / "history-displaced"

        directory.rename(displaced)
        directory.mkdir()

        return result

    monkeypatch.setattr(
        coordinator,
        "publish_manifest",
        publish_then_replace,
    )

    with pytest.raises(
        coordinator.ProjectPublicationError,
        match="directory changed",
    ):
        coordinator.publish_initial_project(
            root, manifest, inventory
        )

    inventory_path, manifest_path, history_path = (
        artifact_paths(root)
    )

    assert inventory_path.is_file()
    assert manifest_path.is_file()
    assert not history_path.exists()
