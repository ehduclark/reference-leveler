#!/usr/bin/env python3
"""Read-only media fidelity audit for Reference Leveler."""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
from pathlib import Path


AUDIO_EXTENSIONS = {
    ".flac", ".wav", ".mp3", ".m4a",
    ".aac", ".aiff", ".aif", ".ogg", ".opus",
}


def probe(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_format",
            "-show_streams",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")

    return json.loads(result.stdout)


def describe(path, root):
    info = probe(path)

    streams = info.get("streams", [])
    audio = [
        stream for stream in streams
        if stream.get("codec_type") == "audio"
    ]

    video = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
    ]

    if not audio:
        raise ValueError("No audio stream found")

    primary = audio[0]
    format_info = info.get("format", {})

    artwork = [
        stream for stream in video
        if stream.get("disposition", {}).get("attached_pic") == 1
    ]

    return {
        "file": str(path.relative_to(root)),
        "extension": path.suffix.lower(),
        "size_bytes": path.stat().st_size,
        "container": format_info.get("format_name"),
        "duration_seconds": format_info.get("duration"),
        "codec": primary.get("codec_name"),
        "sample_rate": primary.get("sample_rate"),
        "channels": primary.get("channels"),
        "channel_layout": primary.get("channel_layout"),
        "sample_format": primary.get("sample_fmt"),
        "bits_per_sample": primary.get("bits_per_sample"),
        "bits_per_raw_sample": primary.get("bits_per_raw_sample"),
        "audio_stream_count": len(audio),
        "video_stream_count": len(video),
        "artwork_stream_count": len(artwork),
        "other_stream_count": len(streams) - len(audio) - len(video),
        "global_tags": format_info.get("tags", {}),
        "audio_tags": primary.get("tags", {}),
        "stream_types": [
            stream.get("codec_type")
            for stream in streams
        ],
    }


def summarize(records, errors):
    def count(field):
        return dict(collections.Counter(
            str(record.get(field))
            for record in records
        ).most_common())

    return {
        "successful": len(records),
        "failed": len(errors),
        "extensions": count("extension"),
        "codecs": count("codec"),
        "sample_rates": count("sample_rate"),
        "channel_layouts": count("channel_layout"),
        "sample_formats": count("sample_format"),
        "bits_per_raw_sample": count("bits_per_raw_sample"),
        "files_with_artwork": sum(
            bool(record["artwork_stream_count"])
            for record in records
        ),
        "files_with_multiple_audio_streams": sum(
            record["audio_stream_count"] > 1
            for record in records
        ),
        "files_with_global_tags": sum(
            bool(record["global_tags"])
            for record in records
        ),
        "files_with_audio_tags": sum(
            bool(record["audio_tags"])
            for record in records
        ),
    }


def audit(root):
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
    )

    records = []
    errors = []

    print(f"Discovered {len(files)} audio files.", flush=True)

    for index, path in enumerate(files, 1):
        try:
            records.append(describe(path, root))
            status = "OK"
        except Exception as exc:
            errors.append({
                "file": str(path.relative_to(root)),
                "error": str(exc),
            })
            status = "ERROR"

        if index % 25 == 0 or index == len(files):
            print(
                f"[{index}/{len(files)}] "
                f"inspected | "
                f"{len(records)} OK | "
                f"{len(errors)} errors",
                flush=True,
            )

    return {
        "root": str(root),
        "discovered": len(files),
        "summary": summarize(records, errors),
        "records": records,
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve(strict=True)

    if not root.is_dir():
        parser.error("Root must be a directory")

    result = audit(root)

    args.output.write_text(
        json.dumps(result, indent=2) + "\n"
    )

    print("\nREFERENCE LEVELER COLLECTION AUDIT")
    print("=" * 65)
    print(f"Discovered: {result['discovered']}")
    print(f"Successful: {result['summary']['successful']}")
    print(f"Failed:     {result['summary']['failed']}")
    print(f"Artwork:    {result['summary']['files_with_artwork']}")
    print(f"Report:     {args.output}")

    if result["errors"]:
        print("\nERRORS:")
        for error in result["errors"]:
            print(f"  {error['file']}: {error['error']}")


if __name__ == "__main__":
    main()
