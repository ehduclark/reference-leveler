#!/usr/bin/env python3
"""Dynamics-aware loudness candidate evaluation."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import tempfile
from pathlib import Path

from reference_leveler_policy import select_candidate
from reference_leveler_treatments import (
    validate_treatment,
    treatment_fingerprint,
)


LOUDNESS_TOLERANCE = 0.5
PEAK_TOLERANCE = 0.05
LRA_WEIGHT = 0.35

# alimiter constrains sample peaks, not guaranteed true peaks.
# Leave additional headroom for intersample peaks.
LIMITER_HEADROOM_DB = 0.5


def run_ffmpeg(arguments):
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-y", *arguments],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-2500:])
    return result.stderr


def measure(path):
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats",
            "-i", str(path),
            "-af", "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode:
        raise RuntimeError(result.stderr[-2500:])

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



def classify_loudness(error):
    if error <= 0.5:
        return "preferred"
    if error <= 1.0:
        return "acceptable"
    if error <= 2.0:
        return "exception"
    return "outside_budget"



def build_treatment_specs(original, target, ceiling):
    """Construct executable specifications for all candidate strategies."""

    gain = target - original["lufs"]

    conservative_gain = min(
        gain,
        ceiling - original["true_peak"] - 0.2,
    )

    raw = {
        "gain_only": {
            "name": "gain_only",
            "gain_db": gain,
        },
        "conservative_gain": {
            "name": "gain_only",
            "gain_db": conservative_gain,
        },
        "gain_plus_limiter": {
            "name": "gain_plus_limiter",
            "gain_db": gain,
            "limiter_ceiling_dbtp": (
                ceiling - LIMITER_HEADROOM_DB
            ),
        },
        "dynamic_loudnorm": {
            "name": "dynamic_loudnorm",
            "target_lufs": target,
            "ceiling_dbtp": ceiling,
        },
    }

    return {
        name: validate_treatment(
            spec,
            target_lufs=target,
            ceiling_dbtp=ceiling,
        )
        for name, spec in raw.items()
    }


def evaluate(source, target, ceiling):
    original = measure(source)
    specs = build_treatment_specs(original, target, ceiling)

    gain = specs["gain_only"]["gain_db"]
    conservative_gain = specs["conservative_gain"]["gain_db"]

    limiter_ceiling = specs[
        "gain_plus_limiter"
    ]["limiter_ceiling_dbtp"]
    limiter_linear = 10 ** (limiter_ceiling / 20)

    treatments = {
        "gain_only": f"volume={gain:.8f}dB",

        "conservative_gain": (
            f"volume={conservative_gain:.8f}dB"
        ),

        "gain_plus_limiter": (
            f"volume={gain:.8f}dB,"
            f"alimiter=limit={limiter_linear:.8f}:"
            "attack=5:release=80:level=false"
        ),

        "dynamic_loudnorm": (
            f"loudnorm=I={target}:TP={ceiling}:"
            "LRA=11:linear=false"
        ),
    }

    candidates = []

    with tempfile.TemporaryDirectory(
        prefix="reference-leveler-candidates-"
    ) as temp:
        temp = Path(temp)

        for name, filtergraph in treatments.items():
            output = temp / f"{name}.wav"

            try:
                run_ffmpeg([
                    "-i", str(source),
                    "-map", "0:a:0",
                    "-vn",
                    "-af", filtergraph,
                    "-c:a", "pcm_f32le",
                    str(output),
                ])

                measured = measure(output)

                loudness_error = abs(measured["lufs"] - target)
                lra_change = abs(
                    measured["lra"] - original["lra"]
                )

                peak_ok = (
                    measured["true_peak"]
                    <= ceiling + PEAK_TOLERANCE
                )

                tier = classify_loudness(loudness_error)

                if not peak_ok:
                    status = "peak_violation"
                elif tier == "outside_budget":
                    status = "review_required"
                else:
                    status = "eligible"

                # These weights are provisional engineering policy,
                # not a validated model of perceived audio quality.
                processing_penalty = {
                    "gain_only": 0.0,
                    "conservative_gain": 0.0,
                    "gain_plus_limiter": 0.15,
                    "dynamic_loudnorm": 0.45,
                }[name]

                score = (
                    loudness_error * 0.5
                    + lra_change * 0.5
                    + processing_penalty
                )

                candidates.append({
                    "name": name,
                    "treatment_spec": specs[name],
                    "treatment_fingerprint": treatment_fingerprint(
                        specs[name],
                        target_lufs=target,
                        ceiling_dbtp=ceiling,
                    ),
                    "status": status,
                    "tier": tier,
                    "score": round(score, 4),
                    "loudness_error": round(
                        loudness_error, 3
                    ),
                    "lra_change": round(lra_change, 3),
                    "measurements": measured,
                })

            except Exception as exc:
                candidates.append({
                    "name": name,
                    "status": "error",
                    "error": str(exc),
                })

    decision = select_candidate(candidates, ceiling)

    selected = decision["selected_candidate"]
    tier = decision["loudness_class"] or "review_required"
    reason = decision["reason"]

    return {
        "source": str(source),
        "source_measurements": original,
        "target_lufs": target,
        "ceiling_dbtp": ceiling,
        "candidates": candidates,
        "recommended_candidate": selected,
        "outcome": decision["outcome"],
        "review_required": decision["review_required"],
        "intervention_class": decision["intervention_class"],
        "recommendation_tier": tier,
        "selection_reason": reason,
        "limitations": [
            "Candidate ranking uses provisional heuristic weights.",
            "LRA is not a direct measure of perceptual transparency.",
            "alimiter is not a guaranteed true-peak limiter.",
            "Temporary candidates are deleted after evaluation.",
            "Final encoded outputs require separate verification.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--target", type=float, default=-11.0)
    parser.add_argument("--ceiling", type=float, default=-1.0)
    parser.add_argument("--output", type=Path)

    args = parser.parse_args()

    if not args.source.is_file():
        parser.error("Source file not found")

    if not all(math.isfinite(v) for v in (args.target, args.ceiling)):
        parser.error("Target and ceiling must be finite")

    result = evaluate(args.source, args.target, args.ceiling)
    payload = json.dumps(result, indent=2, allow_nan=False)

    if args.output:
        args.output.write_text(payload + "\n")

    print(payload)


if __name__ == "__main__":
    main()
