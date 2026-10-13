#!/usr/bin/env python3
"""Source-aware audio encoding configuration for Reference Leveler."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FormatFidelityError(ValueError):
    """A source cannot be safely represented by the requested destination."""


@dataclass(frozen=True)
class AudioFormat:
    codec: str
    sample_rate: int
    channels: int
    sample_format: str | None
    bits_per_raw_sample: int | None
    bit_rate: int | None
    channel_layout: str | None = None


def probe_audio(path: Path) -> AudioFormat:
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_streams",
            "-show_format",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode:
        raise FormatFidelityError(
            result.stderr.strip() or f"Cannot inspect {path}"
        )

    data = json.loads(result.stdout)

    streams = [
        stream
        for stream in data.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]

    if len(streams) != 1:
        raise FormatFidelityError(
            f"Expected exactly one audio stream; found {len(streams)}"
        )

    stream = streams[0]

    try:
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FormatFidelityError(
            "Missing or invalid audio stream properties"
        ) from exc

    if sample_rate <= 0 or channels <= 0:
        raise FormatFidelityError("Invalid sample rate or channel count")

    def optional_int(value):
        if value in (None, "N/A"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return AudioFormat(
        codec=stream.get("codec_name", ""),
        sample_rate=sample_rate,
        channels=channels,
        sample_format=stream.get("sample_fmt"),
        bits_per_raw_sample=optional_int(
            stream.get("bits_per_raw_sample")
        ),
        bit_rate=optional_int(
            stream.get("bit_rate")
            or data.get("format", {}).get("bit_rate")
        ),
        channel_layout=stream.get("channel_layout") or None,
    )


def encoding_arguments(
    source: AudioFormat,
    destination: Path,
) -> list[str]:
    extension = destination.suffix.lower()

    expected = {
        ".flac": "flac",
        ".m4a": "alac",
        ".mp3": "mp3",
        ".wav": "pcm",
    }

    if extension not in expected:
        raise FormatFidelityError(
            f"Unsupported destination extension: {extension}"
        )

    if extension == ".flac":
        if source.codec != "flac":
            raise FormatFidelityError(
                "FLAC destination requires FLAC source"
            )

        codec = "flac"

        if source.bits_per_raw_sample == 24:
            sample_format = "s32"
        elif source.bits_per_raw_sample == 16:
            sample_format = "s16"
        else:
            raise FormatFidelityError(
                "FLAC source bit depth must be explicitly 16 or 24"
            )

        arguments = [
            "-c:a", codec,
            "-sample_fmt", sample_format,
        ]

    elif extension == ".m4a":
        if source.codec != "alac":
            raise FormatFidelityError(
                "M4A destination requires lossless ALAC source"
            )

        codec = "alac"

        if source.bits_per_raw_sample in (16, 24):
            sample_format = (
                "s16p"
                if source.bits_per_raw_sample == 16
                else "s32p"
            )
        else:
            raise FormatFidelityError(
                "ALAC bit depth must be explicitly 16 or 24"
            )

        arguments = [
            "-c:a", codec,
            "-sample_fmt", sample_format,
        ]

    elif extension == ".mp3":
        if source.codec != "mp3":
            raise FormatFidelityError(
                "MP3 destination requires MP3 source"
            )

        arguments = [
            "-c:a", "libmp3lame",
        ]

        if source.bit_rate is not None:
            bitrate = source.bit_rate

            if not 32000 <= bitrate <= 320000:
                raise FormatFidelityError(
                    f"Unsupported MP3 bitrate: {bitrate}"
                )

            arguments += ["-b:a", str(bitrate)]

        else:
            raise FormatFidelityError(
                "MP3 bitrate unavailable; refusing implicit encoding"
            )

    else:
        if not source.codec.startswith("pcm_"):
            raise FormatFidelityError(
                "WAV destination requires PCM source"
            )

        codec = source.codec

        arguments = [
            "-c:a", codec,
        ]

    arguments += [
        "-ar", str(source.sample_rate),
        "-ac", str(source.channels),
    ]

    if source.channel_layout is not None:
        arguments += [
            "-channel_layout", source.channel_layout,
        ]

    return arguments


def verify_audio(
    source: AudioFormat,
    destination: Path,
) -> AudioFormat:
    output = probe_audio(destination)

    expected_codec = {
        ".flac": "flac",
        ".m4a": "alac",
        ".mp3": "mp3",
        ".wav": source.codec,
    }[destination.suffix.lower()]

    if output.codec != expected_codec:
        raise FormatFidelityError(
            f"Codec changed: {expected_codec} -> {output.codec}"
        )

    if output.sample_rate != source.sample_rate:
        raise FormatFidelityError(
            "Output sample rate differs from source"
        )

    if output.channels != source.channels:
        raise FormatFidelityError(
            "Output channel count differs from source"
        )

    if source.channel_layout is not None:
        if output.channel_layout != source.channel_layout:
            raise FormatFidelityError(
                "Output channel layout differs from source: "
                f"{source.channel_layout!r} -> "
                f"{output.channel_layout!r}"
            )

    if destination.suffix.lower() in {".flac", ".m4a"}:
        if source.bits_per_raw_sample not in (16, 24):
            raise FormatFidelityError(
                "Source lossless bit depth is unknown"
            )

        if output.bits_per_raw_sample != source.bits_per_raw_sample:
            raise FormatFidelityError(
                "Output bit depth differs from source"
            )

    return output
