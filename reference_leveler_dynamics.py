#!/usr/bin/env python3
"""Streaming, fixed-window audio dynamics analysis."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from array import array
from pathlib import Path


SAMPLE_RATE = 48000
CHANNELS = 2
WINDOW_SECONDS = 0.4


def percentile(values, fraction):
    if not values:
        return None

    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)

    if lower == upper:
        return ordered[lower]

    weight = position - lower
    return (
        ordered[lower] * (1 - weight)
        + ordered[upper] * weight
    )


def analyze(path, target_lufs, source_lufs, ceiling, window_seconds):
    if window_seconds <= 0:
        raise ValueError("Window duration must be positive")

    gain_db = target_lufs - source_lufs
    threshold_dbfs = ceiling - gain_db
    threshold_linear = 10 ** (threshold_dbfs / 20)

    frames_per_window = round(SAMPLE_RATE * window_seconds)
    samples_per_window = frames_per_window * CHANNELS

    command = [
        "ffmpeg",
        "-v", "error",
        "-i", str(path),
        "-map", "0:a:0",
        "-vn",
        "-ac", str(CHANNELS),
        "-ar", str(SAMPLE_RATE),
        "-f", "f32le",
        "pipe:1",
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    window_peaks = []
    window_rms = []
    window_exceedance = []

    total_samples = 0
    exceeding_samples = 0
    pending = bytearray()

    try:
        while True:
            chunk = process.stdout.read(262144)

            if not chunk:
                break

            pending.extend(chunk)

            while len(pending) >= samples_per_window * 4:
                block = bytes(pending[:samples_per_window * 4])
                del pending[:samples_per_window * 4]

                samples = array("f")
                samples.frombytes(block)

                absolute = [abs(value) for value in samples]
                peak = max(absolute)
                rms = math.sqrt(
                    sum(value * value for value in samples)
                    / len(samples)
                )

                count = sum(
                    value > threshold_linear
                    for value in absolute
                )

                window_peaks.append(peak)
                window_rms.append(rms)
                window_exceedance.append(count / len(samples))

                total_samples += len(samples)
                exceeding_samples += count

        if pending:
            usable = len(pending) - len(pending) % 4
            samples = array("f")
            samples.frombytes(pending[:usable])

            if samples:
                absolute = [abs(value) for value in samples]
                peak = max(absolute)
                rms = math.sqrt(
                    sum(value * value for value in samples)
                    / len(samples)
                )
                count = sum(
                    value > threshold_linear
                    for value in absolute
                )

                window_peaks.append(peak)
                window_rms.append(rms)
                window_exceedance.append(count / len(samples))
                total_samples += len(samples)
                exceeding_samples += count

    finally:
        process.stdout.close()

    stderr = process.stderr.read().decode(
        "utf-8", errors="replace"
    )
    returncode = process.wait()

    if returncode != 0:
        raise RuntimeError(stderr[-2000:])

    if not window_peaks:
        raise RuntimeError("No audio samples decoded")

    maximum_peak = max(window_peaks)
    relative_threshold = maximum_peak * 10 ** (-3 / 20)

    elevated = [
        peak >= relative_threshold
        for peak in window_peaks
    ]

    longest_run = 0
    current_run = 0

    for value in elevated:
        current_run = current_run + 1 if value else 0
        longest_run = max(longest_run, current_run)

    rms_db = [
        20 * math.log10(max(value, 1e-12))
        for value in window_rms
    ]

    # Exclude windows more than 35 dB below the loudest RMS window.
    active_threshold = max(rms_db) - 35
    active_rms = [
        value for value in rms_db
        if value >= active_threshold
    ]

    return {
        "file": str(path),
        "window_seconds": window_seconds,
        "window_count": len(window_peaks),
        "duration_seconds": total_samples / (SAMPLE_RATE * CHANNELS),
        "source_lufs": source_lufs,
        "target_lufs": target_lufs,
        "required_gain_db": gain_db,
        "ceiling_dbtp": ceiling,
        "sample_threshold_dbfs": threshold_dbfs,
        "maximum_sample_peak_dbfs": (
            20 * math.log10(max(maximum_peak, 1e-12))
        ),
        "peak_density_within_3db": sum(elevated) / len(elevated),
        "longest_elevated_run_seconds": longest_run * window_seconds,
        "sample_exceedance_fraction": (
            exceeding_samples / total_samples
        ),
        "windows_with_exceedance_fraction": (
            sum(value > 0 for value in window_exceedance)
            / len(window_exceedance)
        ),
        "active_rms_p10_dbfs": percentile(active_rms, 0.10),
        "active_rms_p50_dbfs": percentile(active_rms, 0.50),
        "active_rms_p90_dbfs": percentile(active_rms, 0.90),
        "limitations": [
            "Sample exceedance is not true-peak exceedance.",
            "Window peaks do not describe limiter gain reduction.",
            "Stereo channels are analyzed together.",
            "RMS levels are not perceptually weighted loudness.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--source-lufs", type=float, required=True)
    parser.add_argument("--target-lufs", type=float, default=-11.0)
    parser.add_argument("--ceiling", type=float, default=-1.0)
    parser.add_argument("--window", type=float, default=WINDOW_SECONDS)
    parser.add_argument("--output", type=Path)

    args = parser.parse_args()

    result = analyze(
        args.file,
        args.target_lufs,
        args.source_lufs,
        args.ceiling,
        args.window,
    )

    payload = json.dumps(result, indent=2, allow_nan=False)

    if args.output:
        args.output.write_text(payload + "\n")

    print(payload)


if __name__ == "__main__":
    main()
