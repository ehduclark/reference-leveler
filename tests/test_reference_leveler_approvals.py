"""Tests for durable, context-bound approval records."""

from dataclasses import replace

import pytest

from reference_leveler_approvals import (
    ApprovalError,
    ApprovalDurabilityError,
    approval_matches,
    create_approval,
    load_approval,
    save_approval,
    validate_approval,
)


@pytest.fixture
def context(tmp_path):
    from reference_leveler_project import create_manifest

    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()

    project = create_manifest(
        name="Approval Test",
        source_root=source,
        output_root=output,
        reference_path=source / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=100,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="balanced",
        inventory_snapshot="snapshot.json",
        inventory_count=1,
        inventory_scanned_at="2026-10-12T12:00:00+00:00",
    )

    source_context = {
        "source_path": "Album/Track.flac",
        "source_sha256": "b" * 64,
        "source_size": 200,
        "treatment": {
            "name": "gain_only",
            "gain_db": -2.5,
        },
    }

    approval = create_approval(
        project=project,
        approved_by="Operator",
        reason="Approved quieter-than-target exception",
        **source_context,
    )

    return project, source_context, approval


def test_approval_round_trip(context, tmp_path):
    _, _, approval = context
    destination = tmp_path / "approval.json"

    save_approval(approval, destination)

    assert load_approval(destination) == approval


def test_matching_context_is_authorized(context):
    project, source, approval = context

    assert approval_matches(
        approval,
        project=project,
        **source,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"source_path": "Album/Other.flac"},
        {"source_sha256": "c" * 64},
        {"source_size": 201},
        {"treatment": {"name": "gain_only", "gain_db": -2.4}},
    ],
)
def test_source_or_treatment_change_invalidates(context, change):
    project, source, approval = context

    changed = {**source, **change}

    assert not approval_matches(
        approval,
        project=project,
        **changed,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 2),
        ("target_lufs", -12.0),
        ("ceiling_dbtp", -2.0),
        ("policy_fingerprint", "c" * 64),
        ("project_id", "00000000-0000-4000-8000-000000000000"),
    ],
)
def test_project_change_invalidates(context, field, value):
    project, source, approval = context

    changed = replace(project, **{field: value})

    assert not approval_matches(
        approval,
        project=changed,
        **source,
    )


def test_budget_change_invalidates(context):
    project, source, approval = context

    budget = dict(project.processing_budget)
    budget["exception_loudness_error"] += 0.1

    changed = replace(project, processing_budget=budget)

    assert not approval_matches(
        approval,
        project=changed,
        **source,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved_by", ""),
        ("reason", " "),
        ("source_path", "../escape.flac"),
        ("source_path", "/absolute.flac"),
        ("source_sha256", "NOT-A-HASH"),
        ("source_size", True),
        ("target_lufs", float("nan")),
        ("project_revision", 0),
        ("scope", "unlimited"),
    ],
)
def test_malformed_approval_rejected(context, field, value):
    _, _, approval = context

    data = approval.as_dict()
    data[field] = value

    with pytest.raises(ApprovalError):
        validate_approval(data)


def test_treatment_tampering_rejected(context):
    _, _, approval = context

    data = approval.as_dict()
    data["treatment"]["gain_db"] = 100.0

    with pytest.raises(ApprovalError, match="fingerprint"):
        validate_approval(data)


def test_existing_approval_cannot_be_overwritten(context, tmp_path):
    _, _, approval = context
    destination = tmp_path / "approval.json"

    save_approval(approval, destination)
    original = destination.read_bytes()

    with pytest.raises(FileExistsError):
        save_approval(approval, destination)

    assert destination.read_bytes() == original


def test_file_sync_failure_never_publishes(
    context, tmp_path, monkeypatch
):
    import reference_leveler_approvals as module

    _, _, approval = context
    destination = tmp_path / "approval.json"

    def fail_sync(_fd):
        raise OSError("simulated file sync failure")

    monkeypatch.setattr(module.os, "fsync", fail_sync)

    with pytest.raises(OSError, match="simulated file sync"):
        save_approval(approval, destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".approval.json.*.tmp"))


def test_directory_sync_failure_reports_uncertainty(
    context, tmp_path, monkeypatch
):
    import reference_leveler_approvals as module

    _, _, approval = context
    destination = tmp_path / "approval.json"

    original_sync = module.os.fsync
    calls = 0

    def fail_directory_sync(fd):
        nonlocal calls
        calls += 1

        if calls == 2:
            raise OSError("simulated directory sync failure")

        return original_sync(fd)

    monkeypatch.setattr(
        module.os,
        "fsync",
        fail_directory_sync,
    )

    with pytest.raises(ApprovalDurabilityError) as captured:
        save_approval(approval, destination)

    assert captured.value.destination == destination
    assert str(captured.value.cause) == (
        "simulated directory sync failure"
    )
    assert captured.value.__cause__ is captured.value.cause

    assert destination.exists()
    assert load_approval(destination) == approval
    assert not list(tmp_path.glob(".approval.json.*.tmp"))


def test_no_temporary_files_after_success(context, tmp_path):
    _, _, approval = context
    destination = tmp_path / "approval.json"

    save_approval(approval, destination)

    assert not list(tmp_path.glob(".approval.json.*.tmp"))
