#!/usr/bin/env python3
"""Collection-wide loudness planning without modifying audio."""

from __future__ import annotations

import argparse
from reference_leveler_decisions import choose_strategy, fingerprint, policy_fingerprint
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = "reference_leveler.plan"
SCHEMA_VERSION = 1

DEFAULT_CEILING = -1.0
DEFAULT_REFERENCE = -9.58

# These are planning heuristics, not predictions of audible damage.
PROFILES = {
    "transparent": {
        "peak_weight": 1.6,
        "target_weight": 0.35,
        "severe_weight": 4.0,
    },
    "balanced": {
        "peak_weight": 1.0,
        "target_weight": 0.8,
        "severe_weight": 2.5,
    },
    "consistent": {
        "peak_weight": 0.65,
        "target_weight": 1.5,
        "severe_weight": 1.5,
    },
}


def finite_number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def load_measurements(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Preflight JSON must contain a list")

    tracks = []
    seen = set()

    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Track {index} is not an object")

        filename = item.get("file")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError(f"Track {index} has no valid filename")
        if filename in seen:
            raise ValueError(f"Duplicate filename: {filename}")
        seen.add(filename)

        tracks.append({
            "file": filename,
            "lufs": finite_number(item.get("lufs"), f"{filename}: lufs"),
            "true_peak": finite_number(
                item.get("true_peak"), f"{filename}: true_peak"
            ),
            "lra": finite_number(item.get("lra"), f"{filename}: lra"),
        })

    if not tracks:
        raise ValueError("Preflight contains no tracks")

    return tracks


def evaluate_track(track, target, ceiling):
    gain = target - track["lufs"]
    projected_peak = track["true_peak"] + gain
    excess = max(0.0, projected_peak - ceiling)

    if excess <= 1e-9:
        classification = "gain_only"
    elif excess <= 1.0:
        classification = "minor_peak_control"
    elif excess <= 3.0:
        classification = "moderate_peak_control"
    elif excess <= 6.0:
        classification = "substantial_peak_control"
    else:
        classification = "high_risk"

    return {
        **track,
        "target_lufs": target,
        "gain_needed_db": gain,
        "projected_true_peak_dbtp": projected_peak,
        "peak_excess_db": excess,
        "risk_class": classification,
    }


def evaluate_target(tracks, target, ceiling, reference, profile):
    evaluated = [evaluate_track(t, target, ceiling) for t in tracks]
    counts = {
        key: sum(t["risk_class"] == key for t in evaluated)
        for key in (
            "gain_only",
            "minor_peak_control",
            "moderate_peak_control",
            "substantial_peak_control",
            "high_risk",
        )
    }

    excesses = [t["peak_excess_db"] for t in evaluated]

    # Softly penalize overall peak-control pressure.
    peak_pressure = sum(
        min(excess, 3.0) ** 1.5 for excess in excesses
    ) / len(excesses)

    # Stronger penalty for the most demanding recordings.
    severe_pressure = sum(
        max(0.0, excess - 3.0) ** 1.5 for excess in excesses
    ) / len(excesses)

    # Discourage drifting unnecessarily far from the reference.
    target_distance = abs(target - reference)

    weights = PROFILES[profile]

    score = (
        weights["peak_weight"] * peak_pressure
        + weights["severe_weight"] * severe_pressure
        + weights["target_weight"] * target_distance
    )

    return {
        "target_lufs": round(target, 2),
        "score": round(score, 5),
        "counts": counts,
        "peak_control_count": len(tracks) - counts["gain_only"],
        "over_3_db_count": sum(e > 3.0 for e in excesses),
        "over_6_db_count": sum(e > 6.0 for e in excesses),
        "maximum_peak_excess_db": round(max(excesses), 3),
        "tracks": evaluated,
    }


def candidate_targets(reference):
    # Explore a useful range below the reference, including the reference.
    lower = math.floor((reference - 5.0) * 2) / 2
    values = []
    current = lower

    while current < reference - 1e-9:
        values.append(round(current, 2))
        current += 0.5

    values.append(round(reference, 2))
    return sorted(set(values))


def build_plan(tracks, reference, ceiling, profile):
    candidates = [
        evaluate_target(tracks, target, ceiling, reference, profile)
        for target in candidate_targets(reference)
    ]

    selected = min(
        candidates,
        key=lambda c: (c["score"], abs(c["target_lufs"] - reference)),
    )

    selected_tracks = []
    for track in selected["tracks"]:
        selected_tracks.append({
            **track,
            "decision": choose_strategy(track, profile),
        })

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "measurement_fingerprint": fingerprint(tracks),
        "policy_fingerprint": policy_fingerprint(),
        "profile": profile,
        "reference_target_lufs": reference,
        "recommended_target_lufs": selected["target_lufs"],
        "true_peak_ceiling_dbtp": ceiling,
        "track_count": len(tracks),
        "median_source_lufs": round(
            statistics.median(t["lufs"] for t in tracks), 2
        ),
        "candidate_summary": [
            {key: value for key, value in c.items() if key != "tracks"}
            for c in candidates
        ],
        "selected_summary": {
            key: value for key, value in selected.items() if key != "tracks"
        },
        "tracks": selected_tracks,
        "limitations": [
            "Peak excess is a screening metric, not predicted limiter reduction.",
            "No transient analysis or perceptual quality assessment performed.",
            "Processing strategies have not yet been selected or executed.",
            "Failed source analyses are absent from the original preflight JSON.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path.home() / "Desktop/reference_leveler_preflight.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / "Desktop/reference_leveler_plan.json",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default="balanced",
    )
    parser.add_argument("--reference", type=float, default=DEFAULT_REFERENCE)
    parser.add_argument("--ceiling", type=float, default=DEFAULT_CEILING)

    args = parser.parse_args()

    reference = finite_number(args.reference, "reference")
    ceiling = finite_number(args.ceiling, "ceiling")

    if args.input.resolve() == args.output.resolve():
        parser.error("Input and output must be different files")

    tracks = load_measurements(args.input)
    plan = build_plan(tracks, reference, ceiling, args.profile)

    args.output.write_text(
        json.dumps(plan, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    print("\nREFERENCE LEVELER v0.2 - COLLECTION INTELLIGENCE")
    print("=" * 65)
    print(f"Tracks analyzed:       {plan['track_count']}")
    print(f"Source median:         {plan['median_source_lufs']:.2f} LUFS")
    print(f"Reference target:      {reference:.2f} LUFS")
    print(f"Recommended target:    {plan['recommended_target_lufs']:.2f} LUFS")
    print(f"Processing philosophy: {args.profile}")
    print(f"True-peak ceiling:     {ceiling:.2f} dBTP")

    print("\nCANDIDATE TARGET COMPARISON")
    print(
        f"{'TARGET':>9} {'SCORE':>10} {'GAIN ONLY':>11}"
        f" {'PEAK CTRL':>11} {'>3 dB':>8} {'>6 dB':>8}"
    )

    for candidate in plan["candidate_summary"]:
        print(
            f"{candidate['target_lufs']:9.2f}"
            f" {candidate['score']:10.4f}"
            f" {candidate['counts']['gain_only']:11d}"
            f" {candidate['peak_control_count']:11d}"
            f" {candidate['over_3_db_count']:8d}"
            f" {candidate['over_6_db_count']:8d}"
        )

    print("\nRECOMMENDED TARGET CLASSIFICATION")
    for key, count in plan["selected_summary"]["counts"].items():
        print(f"  {key:28s} {count:4d}")

    print(f"\nPlan saved: {args.output}")
    print("\nNo audio files were modified.")


if __name__ == "__main__":
    main()
