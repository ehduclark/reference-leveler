#!/usr/bin/env python3
"""Atomic, verified lossless rendering for Reference Leveler."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from reference_leveler_formats import (
    probe_audio,
    encoding_arguments,
    verify_audio,
)
from reference_leveler_metadata import transfer_metadata
from reference_leveler_acceptance import evaluate_output
from reference_leveler_policy import ProcessingBudget, DEFAULT_BUDGET
from reference_leveler_approvals import (
    ApprovalRecord,
    approval_matches,
    validate_approval,
)
from reference_leveler_project import (
    ProjectManifest,
    validate_manifest,
)
from reference_leveler_treatments import validate_treatment
from reference_leveler_integrity import (
    fingerprint_file,
    verify_fingerprint,
    validate_distinct_paths,
)


SUPPORTED_FORMATS = {
    ".flac",
    ".m4a",
    ".mp3",
}


def run_ffmpeg(arguments):
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", *arguments],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    return result.stderr


def measure(path):
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-nostdin",
            "-i", str(path),
            "-map", "0:a:0",
            "-af", "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])

    matches = re.findall(
        r'\{[^{}]*"input_i"[^{}]*\}',
        result.stderr,
        re.S,
    )

    if not matches:
        raise RuntimeError("No loudness measurement found")

    data = json.loads(matches[-1])

    return {
        "lufs": float(data["input_i"]),
        "true_peak": float(data["input_tp"]),
        "lra": float(data["input_lra"]),
    }


def _finite_treatment_number(spec, key):
    try:
        value = float(spec[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"Treatment parameter {key!r} must be finite"
        ) from exc

    if not math.isfinite(value):
        raise ValueError(
            f"Treatment parameter {key!r} must be finite"
        )

    return value


def treatment_filter(spec):
    name = spec["name"]

    if name == "gain_only":
        gain = _finite_treatment_number(spec, "gain_db")
        return f"volume={gain:.8f}dB"

    if name == "gain_plus_limiter":
        gain = _finite_treatment_number(spec, "gain_db")
        ceiling = _finite_treatment_number(
            spec, "limiter_ceiling_dbtp"
        )
        linear = 10 ** (ceiling / 20)

        return (
            f"volume={gain:.8f}dB,"
            f"alimiter=limit={linear:.8f}:"
            "attack=5:release=80:level=false"
        )

    if name == "dynamic_loudnorm":
        target = _finite_treatment_number(spec, "target_lufs")
        ceiling = _finite_treatment_number(spec, "ceiling_dbtp")
        return (
            f"loudnorm=I={target}:"
            f"TP={ceiling}:"
            "LRA=11:linear=false"
        )

    raise ValueError(f"Unsupported treatment: {name}")




def read_anchored_approval(path):
    """Read and validate approval bytes through one file descriptor."""

    path = Path(path)

    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)

    fd = os.open(path, flags)

    try:
        before = os.fstat(fd)

        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Approval path must be a regular file")

        with os.fdopen(os.dup(fd), "rb") as handle:
            content = handle.read()

        after = os.fstat(fd)

        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError("Approval file changed while reading")

        approval = validate_approval(
            json.loads(content.decode("utf-8"))
        )

        identity = (
            after.st_dev,
            after.st_ino,
            hashlib.sha256(content).hexdigest(),
        )

        return approval, identity

    finally:
        os.close(fd)





def verify_render_authorization(
    *,
    source,
    spec,
    target_lufs,
    ceiling,
    source_fingerprint,
    project=None,
    approval_path=None,
    approval_record=None,
):
    """Authorize exceptional rendering against exact current context."""

    if approval_path is None:
        return False

    if project is None:
        raise ValueError(
            "Approval requires a project manifest"
        )

    if not isinstance(project, ProjectManifest):
        raise TypeError(
            "project must be a ProjectManifest"
        )

    project = validate_manifest(project.as_dict())

    if approval_record is None:
        approval, _ = read_anchored_approval(approval_path)
    else:
        approval = approval_record

    if (
        project.target_lufs != target_lufs
        or project.ceiling_dbtp != ceiling
    ):
        raise ValueError(
            "Render target or ceiling differs from project"
        )

    spec = validate_treatment(
        spec,
        target_lufs=target_lufs,
        ceiling_dbtp=ceiling,
    )

    source = Path(source).resolve(strict=True)
    source_root = Path(project.source_root).resolve(strict=True)

    try:
        relative_source = source.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(
            "Source is outside the approved project root"
        ) from exc

    if not approval_matches(
        approval,
        project=project,
        source_path=relative_source.as_posix(),
        source_sha256=source_fingerprint.sha256,
        source_size=source_fingerprint.size,
        treatment=spec,
    ):
        raise ValueError(
            "Approval does not match current render context"
        )

    return True


def render(
    source,
    destination,
    spec,
    ceiling,
    *,
    target_lufs,
    project=None,
    approval_path=None,
):
    # Validate the acceptance contract before touching the filesystem.
    if not isinstance(target_lufs, (int, float)) or isinstance(target_lufs, bool):
        raise ValueError("Target LUFS must be a finite number")
    if not math.isfinite(target_lufs):
        raise ValueError("Target LUFS must be a finite number")

    if not isinstance(ceiling, (int, float)) or isinstance(ceiling, bool):
        raise ValueError("Ceiling must be a finite number")
    if not math.isfinite(ceiling):
        raise ValueError("Ceiling must be a finite number")

    if spec.get("name") == "dynamic_loudnorm":
        treatment_target = _finite_treatment_number(
            spec, "target_lufs"
        )
        treatment_ceiling = _finite_treatment_number(
            spec, "ceiling_dbtp"
        )

        if treatment_target != target_lufs:
            raise ValueError(
                "Treatment target LUFS differs from render target"
            )

        if treatment_ceiling != ceiling:
            raise ValueError(
                "Treatment ceiling differs from render ceiling"
            )

    source, destination = validate_distinct_paths(
        source,
        destination,
    )

    if destination.suffix.lower() not in SUPPORTED_FORMATS:
        raise ValueError("Unsupported destination audio format")

    destination.parent.mkdir(parents=True, exist_ok=True)

    # Anchor publication to the original destination directory.
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)

    directory_fd = os.open(destination.parent, directory_flags)

    try:
        directory_identity = os.fstat(directory_fd)
        current_identity = destination.parent.stat()

        if (
            directory_identity.st_dev != current_identity.st_dev
            or directory_identity.st_ino != current_identity.st_ino
        ):
            raise RuntimeError("Destination directory identity mismatch")
    except BaseException:
        os.close(directory_fd)
        raise

    temporary = None

    try:
        source_fingerprint = fingerprint_file(source)

        if approval_path is not None:
            approval_record, approval_snapshot = (
                read_anchored_approval(approval_path)
            )
        else:
            approval_record = None
            approval_snapshot = None

        authorized_exception = verify_render_authorization(
            source=source,
            spec=spec,
            target_lufs=target_lufs,
            ceiling=ceiling,
            source_fingerprint=source_fingerprint,
            project=project,
            approval_path=approval_path,
            approval_record=approval_record,
        )

        if project is not None:
            if not isinstance(project, ProjectManifest):
                raise TypeError(
                    "project must be a ProjectManifest"
                )

            validated_project = validate_manifest(
                project.as_dict()
            )

            if (
                validated_project.target_lufs != target_lufs
                or validated_project.ceiling_dbtp != ceiling
            ):
                raise ValueError(
                    "Render target or ceiling differs from project"
                )

            acceptance_budget = ProcessingBudget(
                **validated_project.processing_budget
            )
        else:
            acceptance_budget = DEFAULT_BUDGET

        filtergraph = treatment_filter(spec)
        source_format = probe_audio(source)
        encoder_args = encoding_arguments(source_format, destination)

        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.stem}.",
            suffix=destination.suffix,
            dir=destination.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)

        run_ffmpeg([
            "-y",
            "-i", str(source),
            "-map", "0:a:0",
            "-map_metadata", "-1",
            "-vn",
            "-af", filtergraph,
            *encoder_args,
            str(temporary),
        ])

        # Restore and verify permitted metadata before publication.
        transfer_metadata(source, temporary)

        # Metadata writing may alter the container structure.
        verify_audio(source_format, temporary)

        measured = measure(temporary)

        acceptance = evaluate_output(
            measured,
            target_lufs=target_lufs,
            ceiling=ceiling,
            approved_deviation=authorized_exception,
            budget=acceptance_budget,
        )

        if not acceptance.publishable:
            raise ValueError(
                "Rendered output failed acceptance: "
                f"{acceptance.reason} "
                f"(outcome={acceptance.outcome})"
            )

        # Reject sources modified during rendering.
        verify_fingerprint(source, source_fingerprint)

        if authorized_exception:
            final_approval, final_snapshot = (
                read_anchored_approval(approval_path)
            )

            if final_snapshot != approval_snapshot:
                raise ValueError(
                    "Approval file changed during rendering"
                )

            verify_render_authorization(
                source=source,
                spec=spec,
                target_lufs=target_lufs,
                ceiling=ceiling,
                source_fingerprint=source_fingerprint,
                project=project,
                approval_path=approval_path,
                approval_record=final_approval,
            )

        # Hard-link publication fails if the destination already exists,
        # avoiding the overwrite behavior of os.replace().
        # Verify that the original directory remains at its expected path.
        current_identity = destination.parent.stat()

        if (
            current_identity.st_dev != directory_identity.st_dev
            or current_identity.st_ino != directory_identity.st_ino
        ):
            raise RuntimeError(
                "Destination directory changed during rendering"
            )

        # Publish into the original directory, not a substituted path.
        os.link(
            temporary.name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )

        os.unlink(temporary.name, dir_fd=directory_fd)
        temporary = None

        return {
            "source": str(source),
            "destination": str(destination),
            "treatment": spec,
            "measurements": measured,
            "target_lufs": target_lufs,
            "acceptance": acceptance.as_dict(),
            "source_fingerprint": {
                "device": source_fingerprint.device,
                "inode": source_fingerprint.inode,
                "size": source_fingerprint.size,
                "sha256": source_fingerprint.sha256,
            },
            "status": "completed",
        }

    finally:
        try:
            if temporary is not None:
                try:
                    os.unlink(temporary.name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(directory_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--ceiling", type=float, default=-1.0)
    parser.add_argument("--target-lufs", type=float, required=True)
    parser.add_argument("--report", type=Path)

    args = parser.parse_args()

    if not math.isfinite(args.ceiling):
        parser.error("Ceiling must be finite")
    if not math.isfinite(args.target_lufs):
        parser.error("Target LUFS must be finite")

    spec = json.loads(args.spec.read_text())
    result = render(
        args.source,
        args.destination,
        spec,
        args.ceiling,
        target_lufs=args.target_lufs,
    )

    payload = json.dumps(result, indent=2)

    if args.report:
        args.report.write_text(payload + "\n")

    print(payload)


if __name__ == "__main__":
    main()
