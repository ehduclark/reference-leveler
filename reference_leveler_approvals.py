#!/usr/bin/env python3
"""Durable, context-bound approval records for Reference Leveler."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import secrets
import stat
import uuid
from dataclasses import asdict, dataclass
from types import MappingProxyType
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reference_leveler_project import (
    ProjectManifest,
    validate_manifest,
)
from reference_leveler_treatments import (
    validate_treatment,
    treatment_fingerprint,
)


SCHEMA = "reference_leveler.approval"
SCHEMA_VERSION = 1
SCOPE = "quieter_loudness_exception"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ApprovalError(ValueError):
    """Malformed or incompatible approval record."""


class ApprovalDurabilityError(OSError):
    """Approval published, but durable persistence is unconfirmed."""

    def __init__(self, destination: Path, cause: OSError):
        self.destination = Path(destination)
        self.cause = cause
        super().__init__(
            f"Approval published but durability is unconfirmed: "
            f"{destination}: {cause}"
        )


@dataclass(frozen=True)
class ApprovalRecord:
    schema: str
    schema_version: int
    approval_id: str
    project_id: str
    project_revision: int
    source_path: str
    source_sha256: str
    source_size: int
    treatment: dict
    treatment_fingerprint: str
    target_lufs: float
    ceiling_dbtp: float
    processing_budget: dict
    policy_fingerprint: str
    approved_by: str
    approved_utc: str
    reason: str
    scope: str

    def __post_init__(self):
        object.__setattr__(
            self,
            "treatment",
            MappingProxyType(copy.deepcopy(dict(self.treatment))),
        )
        object.__setattr__(
            self,
            "processing_budget",
            MappingProxyType(copy.deepcopy(dict(self.processing_budget))),
        )

    def as_dict(self) -> dict:
        return {
            field: copy.deepcopy(
                dict(value)
                if isinstance(value, MappingProxyType)
                else value
            )
            for field, value in vars(self).items()
        }


def _number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ApprovalError(f"{label} must be a finite number")
    return float(value)


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ApprovalError(f"{label} must be lowercase SHA-256")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApprovalError(f"{label} must be nonempty text")
    if value != value.strip():
        raise ApprovalError(f"{label} must not have outer whitespace")
    return value


def _source_path(value: Any) -> str:
    text = _text(value, "source_path")
    path = Path(text)

    if (
        path.is_absolute()
        or text.startswith("./")
        or "\\" in text
        or any(part in ("", ".", "..") for part in text.split("/"))
        or path.as_posix() != text
    ):
        raise ApprovalError("source_path must be canonical and relative")

    return text


def _timestamp(value: Any) -> str:
    text = _text(value, "approved_utc")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalError("Invalid approval timestamp") from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApprovalError("Approval timestamp must be timezone-aware")

    if parsed.utcoffset().total_seconds() != 0:
        raise ApprovalError("Approval timestamp must be UTC")

    return text


def _budget(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ApprovalError("processing_budget must be an object")

    from reference_leveler_policy import ProcessingBudget

    expected = set(asdict(ProcessingBudget()))

    if set(value) != expected:
        raise ApprovalError("processing_budget fields mismatch")

    result = {
        key: _number(value[key], f"processing_budget.{key}")
        for key in sorted(expected)
    }

    if any(number < 0 for number in result.values()):
        raise ApprovalError("processing_budget cannot be negative")

    if not (
        result["preferred_loudness_error"]
        <= result["acceptable_loudness_error"]
        <= result["exception_loudness_error"]
    ):
        raise ApprovalError("Loudness budget thresholds are unordered")

    return result


def validate_approval(data: Any) -> ApprovalRecord:
    """Strictly validate a standalone approval record."""

    if not isinstance(data, dict):
        raise ApprovalError("Approval must be an object")

    expected = set(ApprovalRecord.__dataclass_fields__)

    if set(data) != expected:
        raise ApprovalError("Approval fields mismatch")

    if data["schema"] != SCHEMA:
        raise ApprovalError("Unsupported approval schema")

    if type(data["schema_version"]) is not int:
        raise ApprovalError("Invalid approval schema version")

    if data["schema_version"] != SCHEMA_VERSION:
        raise ApprovalError("Unsupported approval schema version")

    try:
        approval_id = str(uuid.UUID(data["approval_id"]))
        project_id = str(uuid.UUID(data["project_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApprovalError("Invalid approval or project UUID") from exc

    if (
        approval_id != data["approval_id"]
        or project_id != data["project_id"]
    ):
        raise ApprovalError("UUIDs must be canonical")

    revision = data["project_revision"]

    if type(revision) is not int or revision < 1:
        raise ApprovalError("Invalid project revision")

    source_size = data["source_size"]

    if type(source_size) is not int or source_size < 0:
        raise ApprovalError("Invalid source size")

    target = _number(data["target_lufs"], "target_lufs")
    ceiling = _number(data["ceiling_dbtp"], "ceiling_dbtp")

    try:
        treatment = validate_treatment(
            data["treatment"],
            target_lufs=target,
            ceiling_dbtp=ceiling,
        )
        actual_fingerprint = treatment_fingerprint(
            treatment,
            target_lufs=target,
            ceiling_dbtp=ceiling,
        )
    except ValueError as exc:
        raise ApprovalError("Invalid treatment specification") from exc

    if data["treatment_fingerprint"] != actual_fingerprint:
        raise ApprovalError("Treatment fingerprint mismatch")

    if data["scope"] != SCOPE:
        raise ApprovalError("Unsupported approval scope")

    return ApprovalRecord(
        schema=SCHEMA,
        schema_version=SCHEMA_VERSION,
        approval_id=approval_id,
        project_id=project_id,
        project_revision=revision,
        source_path=_source_path(data["source_path"]),
        source_sha256=_sha256(
            data["source_sha256"], "source_sha256"
        ),
        source_size=source_size,
        treatment=treatment,
        treatment_fingerprint=actual_fingerprint,
        target_lufs=target,
        ceiling_dbtp=ceiling,
        processing_budget=_budget(data["processing_budget"]),
        policy_fingerprint=_sha256(
            data["policy_fingerprint"], "policy_fingerprint"
        ),
        approved_by=_text(data["approved_by"], "approved_by"),
        approved_utc=_timestamp(data["approved_utc"]),
        reason=_text(data["reason"], "reason"),
        scope=SCOPE,
    )


def create_approval(
    *,
    project: ProjectManifest,
    source_path: str,
    source_sha256: str,
    source_size: int,
    treatment: dict,
    approved_by: str,
    reason: str,
) -> ApprovalRecord:
    """Construct an approval bound to a validated project manifest."""

    project = validate_manifest(project.as_dict())

    record = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "approval_id": str(uuid.uuid4()),
        "project_id": project.project_id,
        "project_revision": project.revision,
        "source_path": source_path,
        "source_sha256": source_sha256,
        "source_size": source_size,
        "treatment": treatment,
        "treatment_fingerprint": treatment_fingerprint(
            treatment,
            target_lufs=project.target_lufs,
            ceiling_dbtp=project.ceiling_dbtp,
        ),
        "target_lufs": project.target_lufs,
        "ceiling_dbtp": project.ceiling_dbtp,
        "processing_budget": project.processing_budget,
        "policy_fingerprint": project.policy_fingerprint,
        "approved_by": approved_by,
        "approved_utc": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "scope": SCOPE,
    }

    return validate_approval(record)


def approval_matches(
    approval: ApprovalRecord,
    *,
    project: ProjectManifest,
    source_path: str,
    source_sha256: str,
    source_size: int,
    treatment: dict,
) -> bool:
    """Return whether approval remains valid for the exact context."""

    try:
        approval = validate_approval(approval.as_dict())
        project = validate_manifest(project.as_dict())
        source_path = _source_path(source_path)
        source_sha256 = _sha256(source_sha256, "source_sha256")

        if type(source_size) is not int or source_size < 0:
            return False

        treatment = validate_treatment(
            treatment,
            target_lufs=project.target_lufs,
            ceiling_dbtp=project.ceiling_dbtp,
        )
    except (ApprovalError, ValueError, TypeError, AttributeError):
        return False

    return all((
        approval.project_id == project.project_id,
        approval.project_revision == project.revision,
        approval.source_path == source_path,
        approval.source_sha256 == source_sha256,
        approval.source_size == source_size,
        approval.treatment == treatment,
        approval.target_lufs == project.target_lufs,
        approval.ceiling_dbtp == project.ceiling_dbtp,
        approval.processing_budget == project.processing_budget,
        approval.policy_fingerprint == project.policy_fingerprint,
    ))


def load_approval(path: Path) -> ApprovalRecord:
    """Load and strictly validate a persisted approval."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_approval(data)


def save_approval(
    approval: ApprovalRecord,
    destination: Path,
) -> None:
    """Publish an approval exclusively through an anchored directory."""

    approval = validate_approval(approval.as_dict())
    destination = Path(destination)
    parent = destination.parent

    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | os.O_CLOEXEC
    )

    directory_fd = os.open(parent, flags)
    temporary_name = None

    try:
        directory_identity = os.fstat(directory_fd)

        if not stat.S_ISDIR(directory_identity.st_mode):
            raise NotADirectoryError(parent)

        pathname_identity = os.stat(
            parent,
            follow_symlinks=False,
        )

        if (
            pathname_identity.st_dev != directory_identity.st_dev
            or pathname_identity.st_ino != directory_identity.st_ino
        ):
            raise ApprovalError(
                "Approval directory changed during publication"
            )

        payload = (
            json.dumps(
                approval.as_dict(),
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

        for _ in range(32):
            candidate = (
                f".{destination.name}."
                f"{secrets.token_hex(16)}.tmp"
            )

            try:
                temporary_fd = os.open(
                    candidate,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue

            temporary_name = candidate
            break
        else:
            raise ApprovalError(
                "Unable to allocate exclusive temporary approval"
            )

        try:
            with os.fdopen(temporary_fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            current_identity = os.stat(
                parent,
                follow_symlinks=False,
            )

            if (
                current_identity.st_dev != directory_identity.st_dev
                or current_identity.st_ino != directory_identity.st_ino
            ):
                raise ApprovalError(
                    "Approval directory changed before publication"
                )

            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )

            try:
                os.fsync(directory_fd)
            except OSError as exc:
                raise ApprovalDurabilityError(
                    destination,
                    exc,
                ) from exc

        finally:
            if temporary_name is not None:
                os.unlink(
                    temporary_name,
                    dir_fd=directory_fd,
                )

    finally:
        os.close(directory_fd)
