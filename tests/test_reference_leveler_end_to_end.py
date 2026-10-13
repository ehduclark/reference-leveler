"""Real FFmpeg/Mutagen renderer integration tests."""

from pathlib import Path

import pytest

from reference_leveler_renderer import render, measure
import subprocess
from reference_leveler_metadata import (
    snapshot,
    verify_metadata,
)

from test_reference_leveler_metadata import (
    populate_metadata,
)


def make_measurable_audio(path):
    """Generate a finite-LUFS fixture without changing metadata tests."""
    codec = {
        ".flac": "flac",
        ".m4a": "alac",
        ".mp3": "libmp3lame",
    }[path.suffix.lower()]

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-af", "volume=-6dB",
            "-c:a", codec,
            "-y", str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize("extension", [".flac", ".m4a", ".mp3"])
def test_real_render_preserves_metadata(tmp_path, extension):
    source = tmp_path / f"source{extension}"
    destination = tmp_path / f"rendered{extension}"

    make_measurable_audio(source)
    populate_metadata(source)

    reference = tmp_path / f"expected{extension}"

    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-y", "-i", str(source),
            "-map", "0:a:0",
            "-af", "volume=-3dB",
            "-c:a",
            {
                ".flac": "flac",
                ".m4a": "alac",
                ".mp3": "libmp3lame",
            }[extension],
            str(reference),
        ],
        check=True,
        capture_output=True,
    )

    expected_lufs = measure(reference)["lufs"]

    result = render(
        source,
        destination,
        {
            "name": "gain_only",
            "gain_db": -3.0,
        },
        ceiling=-1.0,
        target_lufs=expected_lufs,
    )

    assert result["status"] == "completed"
    assert result["acceptance"]["publishable"] is True
    assert destination.exists()

    assert snapshot(source, filter_removed=True) == snapshot(
        destination,
        filter_removed=False,
    )

    verify_metadata(source, destination)

    assert result["measurements"]["true_peak"] <= -0.95

    assert not list(tmp_path.glob(f".rendered.*{extension}"))


@pytest.mark.parametrize("extension", [".flac", ".m4a", ".mp3"])
def test_real_render_does_not_overwrite(tmp_path, extension):
    source = tmp_path / f"source{extension}"
    destination = tmp_path / f"rendered{extension}"

    make_measurable_audio(source)
    populate_metadata(source)

    destination.write_bytes(b"existing destination")

    with pytest.raises(FileExistsError):
        render(
            source,
            destination,
            {
                "name": "gain_only",
                "gain_db": -3.0,
            },
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert destination.read_bytes() == b"existing destination"
    assert not list(tmp_path.glob(f".rendered.*{extension}"))
