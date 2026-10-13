"""Real FFmpeg regression tests for lossless channel identity."""

import hashlib
import subprocess

import pytest

from reference_leveler_formats import (
    encoding_arguments,
    probe_audio,
    verify_audio,
)


def run_ffmpeg(*arguments):
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *arguments],
        check=True,
        capture_output=True,
    ).stdout


def decoded_channels(path, channel_count):
    pcm = run_ffmpeg(
        "-i", str(path),
        "-map", "0:a:0",
        "-f", "s16le",
        "-c:a", "pcm_s16le",
        "-",
    )

    sample_width = 2
    frame_width = channel_count * sample_width

    assert len(pcm) % frame_width == 0

    return [
        b"".join(
            pcm[offset + channel * sample_width:
                offset + (channel + 1) * sample_width]
            for offset in range(0, len(pcm), frame_width)
        )
        for channel in range(channel_count)
    ]


@pytest.mark.parametrize(
    "layout,frequencies",
    [
        ("stereo", [220, 330]),
        ("5.1", [220, 330, 440, 550, 660, 770]),
        ("5.1(side)", [220, 330, 440, 550, 660, 770]),
    ],
)
def test_lossless_channel_identity(
    tmp_path,
    layout,
    frequencies,
):
    source = tmp_path / "source.flac"
    destination = tmp_path / "output.flac"

    inputs = []

    for frequency in frequencies:
        inputs.extend([
            "-f", "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration=1",
        ])

    channel_count = len(frequencies)

    run_ffmpeg(
        *inputs,
        "-filter_complex",
        "".join(f"[{index}:a]" for index in range(channel_count))
        + f"join=inputs={channel_count}:channel_layout={layout}[out]",
        "-map", "[out]",
        "-c:a", "flac",
        str(source),
    )

    source_format = probe_audio(source)

    assert source_format.channel_layout == layout

    run_ffmpeg(
        "-i", str(source),
        "-map", "0:a:0",
        *encoding_arguments(source_format, destination),
        str(destination),
    )

    verify_audio(source_format, destination)

    original_channels = decoded_channels(source, channel_count)
    output_channels = decoded_channels(destination, channel_count)

    assert len(original_channels) == len(output_channels)

    for original, rendered in zip(
        original_channels,
        output_channels,
        strict=True,
    ):
        assert hashlib.sha256(original).digest() == (
            hashlib.sha256(rendered).digest()
        )
