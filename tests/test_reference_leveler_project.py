"""Project manifest contract and persistence regression tests."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from reference_leveler_project import (
    ProjectManifestError,
    create_manifest,
    load_manifest,
    save_manifest,
    validate_manifest,
)


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()

    snapshot = tmp_path / "inventory.json"
    snapshot.write_text("{}\n", encoding="utf-8")

    return create_manifest(
        name="Halloween Playlist",
        source_root=source,
        output_root=output,
        reference_path=tmp_path / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=123456,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="balanced",
        inventory_snapshot=snapshot,
        inventory_count=452,
        inventory_scanned_at="2026-10-12T16:25:36-07:00",
    )


def test_manifest_identity_and_defaults(project):
    assert project.schema == "reference_leveler.project"
    assert project.schema_version == 1
    assert project.revision == 1
    assert project.inventory_count == 452
    assert project.target_lufs == -11.0
    assert project.ceiling_dbtp == -1.0
    assert project.profile == "balanced"
    assert len(project.policy_fingerprint) == 64
    assert len(project.project_id) == 36


def test_manifest_round_trip(tmp_path, project):
    destination = tmp_path / "project.json"

    save_manifest(project, destination)

    loaded = load_manifest(destination)

    assert loaded == project
    assert loaded.as_dict() == project.as_dict()


def test_manifest_is_json_serializable(project):
    encoded = json.dumps(
        project.as_dict(),
        allow_nan=False,
    )

    decoded = json.loads(encoded)

    assert validate_manifest(decoded) == project


def test_manifest_cannot_overwrite_existing_file(tmp_path, project):
    destination = tmp_path / "project.json"
    destination.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        save_manifest(project, destination)

    assert destination.read_text() == "existing"


def test_manifest_cannot_overwrite_symlink(tmp_path, project):
    original = tmp_path / "original.json"
    original.write_text("protected", encoding="utf-8")

    destination = tmp_path / "project.json"
    destination.symlink_to(original)

    with pytest.raises(FileExistsError):
        save_manifest(project, destination)

    assert original.read_text() == "protected"


def test_missing_parent_is_rejected(tmp_path, project):
    destination = tmp_path / "missing" / "project.json"

    with pytest.raises(FileNotFoundError):
        save_manifest(project, destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "reference_leveler.plan"),
        ("schema_version", 2),
        ("schema_version", True),
        ("revision", 0),
        ("revision", True),
        ("name", ""),
        ("reference_sha256", "invalid"),
        ("reference_size", -1),
        ("reference_size", True),
        ("target_lufs", math.nan),
        ("target_lufs", math.inf),
        ("ceiling_dbtp", -math.inf),
        ("profile", "aggressive"),
        ("inventory_count", -1),
        ("inventory_count", True),
        ("created_utc", "not-a-date"),
        ("inventory_scanned_at", "2026-10-12"),
    ],
)
def test_invalid_manifest_fields_rejected(project, field, value):
    data = project.as_dict()
    data[field] = value

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_unknown_fields_rejected(project):
    data = project.as_dict()
    data["surprise"] = "unapproved"

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_missing_fields_rejected(project):
    data = project.as_dict()
    del data["project_id"]

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_non_object_rejected():
    with pytest.raises(ProjectManifestError):
        validate_manifest([])


@pytest.mark.parametrize(
    "field,value",
    [
        ("preferred_loudness_error", -0.1),
        ("acceptable_loudness_error", math.nan),
        ("exception_loudness_error", math.inf),
        ("maximum_normal_lra_change", -1),
    ],
)
def test_invalid_budget_values_rejected(project, field, value):
    data = project.as_dict()
    data["processing_budget"][field] = value

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_inconsistent_loudness_budget_rejected(project):
    data = project.as_dict()
    data["processing_budget"]["preferred_loudness_error"] = 1.5

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_inconsistent_lra_budget_rejected(project):
    data = project.as_dict()
    data["processing_budget"]["maximum_normal_lra_change"] = 6.0

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_source_output_same_directory_rejected(project):
    data = project.as_dict()
    data["output_root"] = data["source_root"]

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_output_inside_source_rejected(project):
    data = project.as_dict()
    data["output_root"] = str(
        Path(data["source_root"]) / "nested"
    )

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_source_inside_output_rejected(project):
    data = project.as_dict()
    data["source_root"] = str(
        Path(data["output_root"]) / "nested"
    )

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_failed_validation_does_not_create_file(
    tmp_path, project
):
    destination = tmp_path / "project.json"

    invalid = replace(project, target_lufs=math.nan)

    with pytest.raises(ProjectManifestError):
        save_manifest(invalid, destination)

    assert not destination.exists()


def test_publication_leaves_no_temporary_files(
    tmp_path, project
):
    destination = tmp_path / "project.json"

    save_manifest(project, destination)

    assert sorted(path.name for path in tmp_path.iterdir()) == sorted([
        "project.json",
        "source",
        "output",
        "inventory.json",
    ])

    assert not list(tmp_path.glob(".project.json.*.tmp"))


def test_adversarial_symlink_alias_rejected(tmp_path, project):
    """A symlink must not disguise overlapping source/output roots."""

    alias = tmp_path / "source-alias"
    alias.symlink_to(Path(project.source_root), target_is_directory=True)

    data = project.as_dict()
    data["output_root"] = str(alias)

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_adversarial_nested_symlink_alias_rejected(tmp_path, project):
    """An output beneath a source alias is still inside the source."""

    alias = tmp_path / "source-alias"
    alias.symlink_to(Path(project.source_root), target_is_directory=True)

    data = project.as_dict()
    data["output_root"] = str(alias / "nested")

    with pytest.raises(ProjectManifestError):
        validate_manifest(data)


def test_adversarial_concurrent_publication(tmp_path, project):
    """Two writers cannot both publish the same destination."""

    from concurrent.futures import ThreadPoolExecutor

    destination = tmp_path / "project.json"

    def attempt():
        try:
            save_manifest(project, destination)
            return "created"
        except FileExistsError:
            return "exists"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))

    assert sorted(results) == ["created", "exists"]
    assert load_manifest(destination) == project
    assert not list(tmp_path.glob(".project.json.*.tmp"))


def test_adversarial_file_sync_failure(tmp_path, project, monkeypatch):
    """A failed file fsync must prevent publication."""

    import reference_leveler_project as module

    destination = tmp_path / "project.json"

    def fail_sync(fd):
        raise OSError("simulated file sync failure")

    monkeypatch.setattr(module.os, "fsync", fail_sync)

    with pytest.raises(OSError, match="simulated"):
        save_manifest(project, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".project.json.*.tmp"))


def test_adversarial_directory_sync_failure(
    tmp_path, project, monkeypatch
):
    """A post-publication sync failure must be distinguishable."""

    import reference_leveler_project as module

    destination = tmp_path / "project.json"
    real_fsync = module.os.fsync
    calls = 0

    def fail_second_sync(fd):
        nonlocal calls
        calls += 1

        if calls == 2:
            raise OSError("simulated directory sync failure")

        return real_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", fail_second_sync)

    from reference_leveler_project import (
        ProjectManifestDurabilityError,
    )

    with pytest.raises(ProjectManifestDurabilityError) as captured:
        save_manifest(project, destination)

    error = captured.value

    assert error.destination == destination
    assert isinstance(error.cause, OSError)
    assert str(error.cause) == "simulated directory sync failure"
    assert error.__cause__ is error.cause

    # Publication happened, but durable persistence is unconfirmed.
    assert destination.exists()
    assert load_manifest(destination) == project
    assert not list(tmp_path.glob(".project.json.*.tmp"))
