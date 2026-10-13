"""Verify output acceptance controls atomic publication."""

import math
from pathlib import Path

import pytest

import reference_leveler_renderer as renderer
from reference_leveler_integrity import fingerprint_file
from reference_leveler_project import create_manifest
from reference_leveler_approvals import create_approval, save_approval


@pytest.fixture
def source_file(tmp_path):
    source = tmp_path / "source.flac"
    source.write_bytes(b"original source")
    return source


@pytest.fixture
def mocked_pipeline(monkeypatch):
    """Mock audio processing while retaining real filesystem publication."""

    def fake_ffmpeg(arguments):
        Path(arguments[-1]).write_bytes(b"rendered audio")
        return ""

    monkeypatch.setattr(renderer, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(renderer, "transfer_metadata", lambda *args: None)
    monkeypatch.setattr(renderer, "verify_audio", lambda *args: None)
    monkeypatch.setattr(renderer, "probe_audio", lambda *args: object())
    monkeypatch.setattr(
        renderer,
        "encoding_arguments",
        lambda *args: ["-c:a", "flac"],
    )

    return monkeypatch


def render_with_measurement(
    tmp_path,
    source_file,
    mocked_pipeline,
    *,
    lufs=-11.0,
    peak=-1.2,
    lra=4.0,
    target=-11.0,
    approved=False,
):
    destination = tmp_path / "output.flac"

    mocked_pipeline.setattr(
        renderer,
        "measure",
        lambda path: {
            "lufs": lufs,
            "true_peak": peak,
            "lra": lra,
        },
    )

    treatment = {"name": "gain_only", "gain_db": -3.0}

    project = None
    approval_path = None

    if approved:
        source_identity = fingerprint_file(source_file)

        reference = tmp_path / "reference.flac"
        reference.write_bytes(b"reference fixture")

        reference_identity = fingerprint_file(reference)

        inventory = tmp_path / "inventory.json"
        inventory.write_text("{}")

        project = create_manifest(
            name="Publication acceptance fixture",
            source_root=tmp_path,
            output_root=tmp_path.parent / (tmp_path.name + "-leveled"),
            reference_path=reference,
            reference_sha256=reference_identity.sha256,
            reference_size=reference_identity.size,
            target_lufs=target,
            ceiling_dbtp=-1.0,
            profile="consistent",
            inventory_snapshot=inventory,
            inventory_count=1,
            inventory_scanned_at="2026-10-12T00:00:00Z",
        )

        approval = create_approval(
            project=project,
            source_path=source_file.name,
            source_sha256=source_identity.sha256,
            source_size=source_identity.size,
            treatment=treatment,
            approved_by="regression-test",
            reason="Explicit approval for acceptance regression.",
        )

        approval_path = tmp_path / "approval.json"
        save_approval(approval, approval_path)

    result = renderer.render(
        source_file,
        destination,
        treatment,
        ceiling=-1.0,
        target_lufs=target,
        project=project,
        approval_path=approval_path,
    )

    return result, destination


def assert_no_publication(tmp_path):
    destination = tmp_path / "output.flac"

    assert not destination.exists()
    assert not list(tmp_path.glob(".output.*.flac"))


def test_exact_target_publishes(
    tmp_path, source_file, mocked_pipeline
):
    result, destination = render_with_measurement(
        tmp_path, source_file, mocked_pipeline
    )

    assert destination.read_bytes() == b"rendered audio"
    assert result["acceptance"]["outcome"] == "accepted"
    assert result["acceptance"]["publishable"] is True
    assert result["target_lufs"] == -11.0


def test_three_lu_below_target_never_publishes(
    tmp_path, source_file, mocked_pipeline
):
    with pytest.raises(ValueError, match="failed acceptance"):
        render_with_measurement(
            tmp_path,
            source_file,
            mocked_pipeline,
            lufs=-14.0,
        )

    assert_no_publication(tmp_path)


def test_three_lu_above_target_never_publishes(
    tmp_path, source_file, mocked_pipeline
):
    with pytest.raises(ValueError, match="failed acceptance"):
        render_with_measurement(
            tmp_path,
            source_file,
            mocked_pipeline,
            lufs=-8.0,
        )

    assert_no_publication(tmp_path)


def test_unapproved_exception_never_publishes(
    tmp_path, source_file, mocked_pipeline
):
    with pytest.raises(ValueError, match="review_required"):
        render_with_measurement(
            tmp_path,
            source_file,
            mocked_pipeline,
            lufs=-12.5,
        )

    assert_no_publication(tmp_path)


def test_explicitly_approved_exception_publishes(
    tmp_path, source_file, mocked_pipeline
):
    result, destination = render_with_measurement(
        tmp_path,
        source_file,
        mocked_pipeline,
        lufs=-12.5,
        approved=True,
    )

    assert destination.exists()
    assert result["acceptance"]["outcome"] == (
        "accepted_with_deviation"
    )


def test_approval_never_overrides_true_peak(
    tmp_path, source_file, mocked_pipeline
):
    with pytest.raises(ValueError, match="failed acceptance"):
        render_with_measurement(
            tmp_path,
            source_file,
            mocked_pipeline,
            lufs=-12.5,
            peak=-0.5,
            approved=True,
        )

    assert_no_publication(tmp_path)


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_nonfinite_loudness_never_publishes(
    tmp_path, source_file, mocked_pipeline, invalid
):
    with pytest.raises(ValueError, match="failed acceptance"):
        render_with_measurement(
            tmp_path,
            source_file,
            mocked_pipeline,
            lufs=invalid,
        )

    assert_no_publication(tmp_path)


def test_missing_target_fails_before_creating_output(
    tmp_path, source_file, mocked_pipeline
):
    destination = tmp_path / "output.flac"

    with pytest.raises(TypeError, match="target_lufs"):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
        )

    assert_no_publication(tmp_path)
