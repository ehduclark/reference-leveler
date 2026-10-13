"""Adversarial tests for approval immutability and publication."""

import copy
import json
import os
import threading
from dataclasses import FrozenInstanceError

import pytest

import reference_leveler_approvals as approvals


@pytest.fixture
def approval():
    from reference_leveler_approvals import validate_approval

    treatment = {
        "name": "gain_only",
        "gain_db": -2.5,
    }

    from reference_leveler_treatments import treatment_fingerprint
    from reference_leveler_policy import ProcessingBudget
    from dataclasses import asdict
    import uuid

    budget = asdict(ProcessingBudget())

    data = {
        "schema": approvals.SCHEMA,
        "schema_version": approvals.SCHEMA_VERSION,
        "approval_id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()),
        "project_revision": 1,
        "source_path": "Album/Track.flac",
        "source_sha256": "a" * 64,
        "source_size": 100,
        "treatment": treatment,
        "treatment_fingerprint": treatment_fingerprint(
            treatment,
            target_lufs=-11.0,
            ceiling_dbtp=-1.0,
        ),
        "target_lufs": -11.0,
        "ceiling_dbtp": -1.0,
        "processing_budget": budget,
        "policy_fingerprint": "b" * 64,
        "approved_by": "Operator",
        "approved_utc": "2026-10-12T12:00:00+00:00",
        "reason": "Reviewed quieter exception",
        "scope": approvals.SCOPE,
    }

    return validate_approval(data)


def test_nested_approval_fields_are_immutable(approval):
    with pytest.raises(TypeError):
        approval.treatment["gain_db"] = 99.0

    with pytest.raises(TypeError):
        approval.processing_budget[
            "exception_loudness_error"
        ] = 99.0


def test_approval_as_dict_is_detached(approval):
    exported = approval.as_dict()

    exported["treatment"]["gain_db"] = 99.0
    exported["processing_budget"][
        "exception_loudness_error"
    ] = 99.0

    assert approval.treatment["gain_db"] == -2.5
    assert (
        approval.processing_budget["exception_loudness_error"]
        != 99.0
    )


def test_record_fields_cannot_be_reassigned(approval):
    with pytest.raises(FrozenInstanceError):
        approval.approved_by = "Someone Else"


def test_symlink_directory_rejected(approval, tmp_path):
    real = tmp_path / "real"
    real.mkdir()

    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(OSError):
        approvals.save_approval(
            approval,
            alias / "approval.json",
        )

    assert not (real / "approval.json").exists()


def test_existing_symlink_destination_not_overwritten(
    approval, tmp_path
):
    target = tmp_path / "target.json"
    target.write_text("untouched")

    destination = tmp_path / "approval.json"
    destination.symlink_to(target)

    with pytest.raises(FileExistsError):
        approvals.save_approval(approval, destination)

    assert target.read_text() == "untouched"
    assert destination.is_symlink()


def test_concurrent_writers_publish_exactly_once(
    approval, tmp_path
):
    destination = tmp_path / "approval.json"
    outcomes = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        try:
            barrier.wait(timeout=10)
            approvals.save_approval(approval, destination)
            outcome = "published"
        except FileExistsError:
            outcome = "exists"
        except Exception as exc:
            outcome = repr(exc)

        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=worker)
        for _ in range(8)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    assert outcomes.count("published") == 1
    assert outcomes.count("exists") == 7
    assert approvals.load_approval(destination) == approval


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update({"scope": "unlimited"}),
        lambda d: d.update({"source_sha256": "wrong"}),
        lambda d: d["treatment"].update({"gain_db": 100}),
        lambda d: d.update({"approved_utc": "not-a-date"}),
        lambda d: d.update({"unexpected": True}),
    ],
)
def test_malformed_persisted_approval_rejected(
    approval, tmp_path, mutation
):
    destination = tmp_path / "approval.json"

    data = approval.as_dict()
    mutation(data)

    destination.write_text(json.dumps(data))

    with pytest.raises(approvals.ApprovalError):
        approvals.load_approval(destination)


def test_directory_substitution_before_publication_rejected(
    approval, tmp_path, monkeypatch
):
    original_link = approvals.os.link

    parent = tmp_path / "approvals"
    parent.mkdir()

    replacement = tmp_path / "replacement"
    replacement.mkdir()

    destination = parent / "approval.json"

    original_stat = approvals.os.stat
    calls = 0

    def substitute_on_second_stat(path, *args, **kwargs):
        nonlocal calls

        if os.fspath(path) == os.fspath(parent):
            calls += 1

            if calls == 2:
                parent.rename(tmp_path / "moved")
                replacement.rename(parent)

        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        approvals.os,
        "stat",
        substitute_on_second_stat,
    )

    with pytest.raises(approvals.ApprovalError):
        approvals.save_approval(approval, destination)

    assert not destination.exists()
    assert not (tmp_path / "moved" / "approval.json").exists()
    assert not list((tmp_path / "moved").glob("*.tmp"))
