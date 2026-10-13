"""Adversarial regression for durable renderer authorization."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

import reference_leveler_renderer as renderer

from reference_leveler_integrity import fingerprint_file
from reference_leveler_project import create_manifest
from reference_leveler_approvals import create_approval, save_approval


TREATMENT = {
    "name": "gain_only",
    "gain_db": -3.0,
}


@pytest.fixture
def context(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    source_root.mkdir()

    source = source_root / "source.flac"
    source.write_bytes(b"original source")

    reference = source_root / "reference.flac"
    reference.write_bytes(b"reference fixture")

    inventory = source_root / "inventory.json"
    inventory.write_text("{}")

    destination = tmp_path / "output" / "rendered.flac"

    reference_identity = fingerprint_file(reference)
    source_identity = fingerprint_file(source)

    project = create_manifest(
        name="Renderer authorization regression",
        source_root=source_root,
        output_root=tmp_path / "output",
        reference_path=reference,
        reference_sha256=reference_identity.sha256,
        reference_size=reference_identity.size,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=inventory,
        inventory_count=1,
        inventory_scanned_at="2026-10-12T00:00:00Z",
    )

    approval = create_approval(
        project=project,
        source_path=source.name,
        source_sha256=source_identity.sha256,
        source_size=source_identity.size,
        treatment=TREATMENT,
        approved_by="regression-test",
        reason="Explicit authorization regression fixture.",
    )

    approval_path = tmp_path / "approval.json"
    save_approval(approval, approval_path)

    calls = []

    def fake_ffmpeg(arguments):
        calls.append(arguments)
        Path(arguments[-1]).write_bytes(b"rendered audio")

    monkeypatch.setattr(renderer, "run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(renderer, "transfer_metadata", lambda *args: None)
    monkeypatch.setattr(renderer, "verify_audio", lambda *args: None)
    monkeypatch.setattr(renderer, "probe_audio", lambda *args: object())
    monkeypatch.setattr(
        renderer,
        "encoding_arguments",
        lambda *args: ["-c:a", "flac"],
    )
    monkeypatch.setattr(
        renderer,
        "measure",
        lambda path: {
            "lufs": -12.5,
            "true_peak": -1.2,
            "lra": 4.0,
        },
    )

    return {
        "source": source,
        "destination": destination,
        "project": project,
        "approval": approval,
        "approval_path": approval_path,
        "calls": calls,
    }


def render_case(context, **overrides):
    options = {
        "project": context["project"],
        "approval_path": context["approval_path"],
    }
    options.update(overrides)

    return renderer.render(
        context["source"],
        context["destination"],
        TREATMENT,
        ceiling=-1.0,
        target_lufs=-11.0,
        **options,
    )


def assert_not_published(context):
    assert not context["destination"].exists()

    parent = context["destination"].parent

    if parent.exists():
        assert not list(parent.glob(".rendered.*.flac"))


def test_valid_persisted_approval_publishes(context):
    result = render_case(context)

    assert result["acceptance"]["outcome"] == (
        "accepted_with_deviation"
    )
    assert context["destination"].exists()
    assert len(context["calls"]) == 1


def test_missing_approval_file_fails_before_ffmpeg(context):
    context["approval_path"].unlink()

    with pytest.raises(FileNotFoundError):
        render_case(context)

    assert context["calls"] == []
    assert_not_published(context)


def test_malformed_approval_fails_before_ffmpeg(context):
    context["approval_path"].write_text("{invalid json")

    with pytest.raises((ValueError, json.JSONDecodeError)):
        render_case(context)

    assert context["calls"] == []
    assert_not_published(context)


def test_changed_source_bytes_invalidate_approval(context):
    context["source"].write_bytes(b"changed source")

    with pytest.raises(ValueError, match="Approval does not match"):
        render_case(context)

    assert context["calls"] == []
    assert_not_published(context)


def test_changed_treatment_invalidates_approval(context):
    changed = {
        "name": "gain_only",
        "gain_db": -2.0,
    }

    with pytest.raises(ValueError, match="Approval does not match"):
        renderer.render(
            context["source"],
            context["destination"],
            changed,
            ceiling=-1.0,
            target_lufs=-11.0,
            project=context["project"],
            approval_path=context["approval_path"],
        )

    assert context["calls"] == []
    assert_not_published(context)


def test_changed_project_revision_invalidates_approval(context):
    changed_project = replace(
        context["project"],
        revision=context["project"].revision + 1,
    )

    with pytest.raises(ValueError, match="Approval does not match"):
        render_case(context, project=changed_project)

    assert context["calls"] == []
    assert_not_published(context)


def test_approval_without_project_rejected(context):
    with pytest.raises(ValueError, match="project manifest"):
        render_case(context, project=None)

    assert context["calls"] == []
    assert_not_published(context)


def test_boolean_authorization_bypass_rejected(context):
    with pytest.raises(TypeError, match="approved_deviation"):
        renderer.render(
            context["source"],
            context["destination"],
            TREATMENT,
            ceiling=-1.0,
            target_lufs=-11.0,
            approved_deviation=True,
        )

    assert context["calls"] == []
    assert_not_published(context)


def test_approval_cannot_override_true_peak(context, monkeypatch):
    monkeypatch.setattr(
        renderer,
        "measure",
        lambda path: {
            "lufs": -12.5,
            "true_peak": -0.5,
            "lra": 4.0,
        },
    )

    with pytest.raises(ValueError, match="failed acceptance"):
        render_case(context)

    assert_not_published(context)


def test_approval_cannot_override_loudness_budget(context, monkeypatch):
    monkeypatch.setattr(
        renderer,
        "measure",
        lambda path: {
            "lufs": -14.0,
            "true_peak": -1.2,
            "lra": 4.0,
        },
    )

    with pytest.raises(ValueError, match="failed acceptance"):
        render_case(context)

    assert_not_published(context)


def test_approval_modified_during_rendering_rejected(
    context, monkeypatch
):
    original_measure = renderer.measure

    def change_approval(path):
        data = json.loads(
            context["approval_path"].read_text()
        )
        data["reason"] = "Changed during rendering"
        context["approval_path"].write_text(
            json.dumps(data)
        )
        return original_measure(path)

    monkeypatch.setattr(renderer, "measure", change_approval)

    with pytest.raises(ValueError, match="Approval file changed during rendering"):
        render_case(context)

    assert_not_published(context)


def test_ordinary_rendering_needs_no_approval(context, monkeypatch):
    monkeypatch.setattr(
        renderer,
        "measure",
        lambda path: {
            "lufs": -11.0,
            "true_peak": -1.2,
            "lra": 4.0,
        },
    )

    result = render_case(
        context,
        project=None,
        approval_path=None,
    )

    assert result["acceptance"]["outcome"] == "accepted"
    assert context["destination"].exists()


def test_approval_replaced_with_equivalent_record_rejected(
    context, monkeypatch
):
    """A different valid approval must not replace the authorized one."""

    from reference_leveler_approvals import create_approval

    original_measure = renderer.measure

    def replace_approval(path):
        identity = fingerprint_file(context["source"])

        replacement = create_approval(
            project=context["project"],
            source_path=context["source"].name,
            source_sha256=identity.sha256,
            source_size=identity.size,
            treatment=TREATMENT,
            approved_by="different-approver",
            reason="A different authorization record.",
        )

        replacement_path = (
            context["approval_path"].parent / "replacement.json"
        )

        save_approval(replacement, replacement_path)

        replacement_path.replace(context["approval_path"])

        return original_measure(path)

    monkeypatch.setattr(renderer, "measure", replace_approval)

    with pytest.raises(ValueError, match="Approval file changed"):
        render_case(context)

    assert_not_published(context)


def test_project_budget_mutated_during_render_rejected(
    context, monkeypatch
):
    """Project policy must remain fixed throughout authorized rendering."""

    original_measure = renderer.measure

    def mutate_project(path):
        context["project"].processing_budget[
            "exception_loudness_error"
        ] = 0.25

        return original_measure(path)

    monkeypatch.setattr(renderer, "measure", mutate_project)

    with pytest.raises(ValueError, match="project|policy|budget"):
        render_case(context)

    assert_not_published(context)


def test_renderer_enforces_project_specific_loudness_budget(
    context, monkeypatch
):
    """A valid approval cannot override its project's exception limit."""

    from reference_leveler_policy import ProcessingBudget
    from reference_leveler_project import create_manifest
    from reference_leveler_approvals import create_approval

    old_project = context["project"]

    strict_budget = ProcessingBudget(
        ceiling_tolerance_dbtp=0.02,
        preferred_loudness_error=0.25,
        acceptable_loudness_error=0.5,
        exception_loudness_error=1.0,
        maximum_normal_lra_change=3.0,
        maximum_fallback_lra_change=5.0,
    )

    reference = Path(old_project.reference_path)
    reference_identity = fingerprint_file(reference)

    strict_project = create_manifest(
        name="Strict authorization regression",
        source_root=Path(old_project.source_root),
        output_root=Path(old_project.output_root),
        reference_path=reference,
        reference_sha256=reference_identity.sha256,
        reference_size=reference_identity.size,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=Path(old_project.inventory_snapshot),
        inventory_count=1,
        inventory_scanned_at="2026-10-12T00:00:00Z",
        budget=strict_budget,
    )

    source_identity = fingerprint_file(context["source"])

    strict_approval = create_approval(
        project=strict_project,
        source_path=context["source"].name,
        source_sha256=source_identity.sha256,
        source_size=source_identity.size,
        treatment=TREATMENT,
        approved_by="regression-test",
        reason="Explicit approval under stricter processing budget.",
    )

    strict_approval_path = (
        context["approval_path"].parent / "strict-approval.json"
    )

    save_approval(strict_approval, strict_approval_path)

    # Existing fixture measures -12.5 LUFS:
    # 1.5 LU below target.
    #
    # Default exception budget: 2.0 LU -> publishable.
    # Strict exception budget:  1.0 LU -> not publishable.

    with pytest.raises(ValueError, match="failed acceptance"):
        render_case(
            context,
            project=strict_project,
            approval_path=strict_approval_path,
        )

    assert_not_published(context)



def test_renderer_authorization_does_not_use_legacy_approval_loader(
    context, monkeypatch
):
    """Rendering must authorize from the anchored approval record."""

    def forbidden_loader(*args, **kwargs):
        raise AssertionError(
            "Legacy pathname-based approval loader was called"
        )

    monkeypatch.setattr(
        renderer,
        "load_approval",
        forbidden_loader,
        raising=False,
    )

    render_case(context)
