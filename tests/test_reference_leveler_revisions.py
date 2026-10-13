"""Regression coverage for pure project inventory revisions."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from reference_leveler_inventory import validate_snapshot
from reference_leveler_project import create_manifest
from reference_leveler_revisions import (
    ProjectRevisionError,
    revise_project_inventory,
)


OLD_TIME = "2026-10-12T16:25:36-07:00"
NEW_TIME = "2026-10-13T12:18:05-07:00"


def inventory(root, timestamp, entries):
    files = [
        {"relative_path": name, "size_bytes": size}
        for name, size in entries
    ]

    counts = {}

    for item in files:
        extension = Path(item["relative_path"]).suffix.lower()
        counts[extension] = counts.get(extension, 0) + 1

    return validate_snapshot({
        "schema_version": 1,
        "source_root": str(root),
        "scanned_at": timestamp,
        "file_count": len(files),
        "format_counts": counts,
        "files": files,
    })


@pytest.fixture
def context(tmp_path):
    source = tmp_path / "source"
    output = tmp_path / "output"

    previous = inventory(
        source,
        OLD_TIME,
        [
            ("same.flac", 100),
            ("removed.mp3", 200),
            ("changed.flac", 300),
        ],
    )

    current = inventory(
        source,
        NEW_TIME,
        [
            ("same.flac", 100),
            ("changed.flac", 301),
            ("added.m4a", 400),
        ],
    )

    project = create_manifest(
        name="Revision Test",
        source_root=source,
        output_root=output,
        reference_path=source / "reference.flac",
        reference_sha256="a" * 64,
        reference_size=100,
        target_lufs=-11.0,
        ceiling_dbtp=-1.0,
        profile="consistent",
        inventory_snapshot=tmp_path / "previous.json",
        inventory_count=len(previous.files),
        inventory_scanned_at=previous.scanned_at,
    )

    return {
        "project": project,
        "previous": previous,
        "current": current,
        "current_path": tmp_path / "current.json",
    }


def advance(context, **changes):
    values = {**context, **changes}

    return revise_project_inventory(
        values["project"],
        values["previous"],
        values["current"],
        values["current_path"],
    )


def test_revision_increments_and_preserves_identity(context):
    plan = advance(context)

    assert plan.previous.revision == 1
    assert plan.proposed.revision == 2
    assert plan.proposed.project_id == plan.previous.project_id
    assert plan.proposed.created_utc == plan.previous.created_utc


def test_processing_configuration_is_preserved(context):
    plan = advance(context)

    old = plan.previous.as_dict()
    new = plan.proposed.as_dict()

    for key in old:
        if key not in {
            "revision",
            "inventory_snapshot",
            "inventory_count",
            "inventory_scanned_at",
        }:
            assert new[key] == old[key]


def test_inventory_reconciliation_is_attached(context):
    result = advance(context).reconciliation

    assert result.added == ("added.m4a",)
    assert result.removed == ("removed.mp3",)
    assert result.size_changed == ("changed.flac",)
    assert result.metadata_unchanged == ("same.flac",)


def test_original_project_is_unchanged(context):
    original = copy.deepcopy(context["project"].as_dict())

    advance(context)

    assert context["project"].as_dict() == original
    assert context["project"].revision == 1


def test_revision_is_pure_and_creates_no_files(context):
    advance(context)

    assert not context["current_path"].exists()
    assert not Path(context["project"].inventory_snapshot).exists()


def test_previous_count_mismatch_rejected(context):
    project = replace(context["project"], inventory_count=999)

    with pytest.raises(ProjectRevisionError, match="count"):
        advance(context, project=project)


def test_previous_timestamp_mismatch_rejected(context):
    project = replace(
        context["project"],
        inventory_scanned_at=NEW_TIME,
    )

    with pytest.raises(ProjectRevisionError, match="timestamp"):
        advance(context, project=project)


def test_previous_source_root_mismatch_rejected(context):
    previous = inventory(
        context["project"].output_root,
        OLD_TIME,
        [("song.flac", 100)],
    )

    with pytest.raises(ProjectRevisionError, match="source root"):
        advance(context, previous=previous)


def test_current_source_root_mismatch_rejected(context):
    current = inventory(
        context["project"].output_root,
        NEW_TIME,
        [("song.flac", 100)],
    )

    with pytest.raises(ProjectRevisionError, match="source root"):
        advance(context, current=current)


def test_older_inventory_rejected(context):
    current = inventory(
        context["project"].source_root,
        "2026-10-11T12:00:00-07:00",
        [("song.flac", 100)],
    )

    with pytest.raises(ProjectRevisionError, match="later"):
        advance(context, current=current)


def test_same_timestamp_rejected(context):
    current = inventory(
        context["project"].source_root,
        OLD_TIME,
        [("song.flac", 100)],
    )

    with pytest.raises(ProjectRevisionError, match="later"):
        advance(context, current=current)


def test_relative_snapshot_path_rejected(context):
    with pytest.raises(ProjectRevisionError, match="absolute"):
        advance(context, current_path=Path("current.json"))


def test_reusing_previous_snapshot_path_rejected(context):
    with pytest.raises(ProjectRevisionError, match="different"):
        advance(
            context,
            current_path=Path(
                context["project"].inventory_snapshot
            ),
        )


def test_unchanged_inventory_still_advances_revision(context):
    current = inventory(
        context["project"].source_root,
        NEW_TIME,
        [
            ("same.flac", 100),
            ("removed.mp3", 200),
            ("changed.flac", 300),
        ],
    )

    plan = advance(context, current=current)

    assert plan.proposed.revision == 2
    assert len(plan.reconciliation.metadata_unchanged) == 3


def test_revisions_can_advance_sequentially(context):
    first = advance(context)

    second_snapshot = inventory(
        context["project"].source_root,
        "2026-10-14T12:00:00-07:00",
        [("newer.flac", 500)],
    )

    second = revise_project_inventory(
        first.proposed,
        context["current"],
        second_snapshot,
        context["current_path"].parent / "third.json",
    )

    assert second.proposed.revision == 3
    assert second.proposed.project_id == first.proposed.project_id


def test_processing_budget_is_not_shared_between_revisions(context):
    plan = advance(context)

    assert (
        plan.previous.processing_budget
        is not plan.proposed.processing_budget
    )

    assert (
        plan.previous.processing_budget
        == plan.proposed.processing_budget
    )



def test_forged_inventory_path_rejected(context):
    from reference_leveler_inventory import InventorySnapshot

    forged = InventorySnapshot(
        source_root=context["project"].source_root,
        scanned_at=NEW_TIME,
        files={"../escape.flac": 100},
    )

    with pytest.raises(ProjectRevisionError, match="Invalid inventory"):
        advance(context, current=forged)


def test_forged_inventory_negative_size_rejected(context):
    from reference_leveler_inventory import InventorySnapshot

    forged = InventorySnapshot(
        source_root=context["project"].source_root,
        scanned_at=NEW_TIME,
        files={"song.flac": -1},
    )

    with pytest.raises(ProjectRevisionError, match="Invalid inventory"):
        advance(context, current=forged)


def test_forged_inventory_naive_timestamp_rejected(context):
    from reference_leveler_inventory import InventorySnapshot

    forged = InventorySnapshot(
        source_root=context["project"].source_root,
        scanned_at="2026-10-13T12:00:00",
        files={"song.flac": 100},
    )

    with pytest.raises(ProjectRevisionError, match="Invalid inventory"):
        advance(context, current=forged)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/../current.json",
        "/tmp/./current.json",
        "/tmp//current.json",
        "/tmp/current.json/",
        "/tmp\\\\current.json",
    ],
)
def test_noncanonical_snapshot_path_rejected(context, path):
    with pytest.raises(ProjectRevisionError, match="canonical"):
        advance(context, current_path=path)


def test_large_valid_revision_is_supported(context):
    project = replace(
        context["project"],
        revision=10**100,
    )

    plan = advance(context, project=project)

    assert plan.proposed.revision == 10**100 + 1


def test_forged_previous_inventory_rejected(context):
    from reference_leveler_inventory import InventorySnapshot

    forged = InventorySnapshot(
        source_root=context["project"].source_root,
        scanned_at=OLD_TIME,
        files={"song.flac": True},
    )

    with pytest.raises(ProjectRevisionError, match="Invalid inventory"):
        advance(context, previous=forged)
