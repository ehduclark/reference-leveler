"""Renderer publication and failure-safety regression tests."""

from pathlib import Path

import pytest

import reference_leveler_renderer as renderer


@pytest.fixture
def source_file(tmp_path):
    path = tmp_path / "source.flac"
    path.write_bytes(b"test source")
    return path


@pytest.fixture
def mocked_pipeline(monkeypatch):
    calls = []

    monkeypatch.setattr(
        renderer,
        "fingerprint_file",
        lambda path: type(
            "Fingerprint",
            (),
            {
                "device": 1,
                "inode": 2,
                "size": 11,
                "sha256": "test-fingerprint",
            },
        )(),
    )

    monkeypatch.setattr(
        renderer,
        "verify_fingerprint",
        lambda path, expected: None,
    )

    monkeypatch.setattr(
        renderer,
        "probe_audio",
        lambda path: object(),
    )

    monkeypatch.setattr(
        renderer,
        "encoding_arguments",
        lambda source, destination: ["-c:a", "flac"],
    )

    def fake_ffmpeg(arguments):
        calls.append(arguments)
        Path(arguments[-1]).write_bytes(b"rendered audio")
        return ""

    monkeypatch.setattr(renderer, "run_ffmpeg", fake_ffmpeg)

    monkeypatch.setattr(
        renderer,
        "transfer_metadata",
        lambda source, destination: None,
    )

    monkeypatch.setattr(
        renderer,
        "verify_audio",
        lambda source, destination: None,
    )

    monkeypatch.setattr(
        renderer,
        "measure",
        lambda path: {
            "lufs": -11.0,
            "true_peak": -1.2,
            "lra": 4.0,
        },
    )

    return calls


def test_successful_render_publishes_destination(
    tmp_path, source_file, mocked_pipeline
):
    destination = tmp_path / "output.flac"

    result = renderer.render(
        source_file,
        destination,
        {"name": "gain_only", "gain_db": -3.0},
        ceiling=-1.0,
        target_lufs=-11.0,
    )

    assert destination.exists()
    assert destination.read_bytes() == b"rendered audio"
    assert result["status"] == "completed"
    assert len(mocked_pipeline) == 1

    assert not list(tmp_path.glob(".output.*.flac"))


def test_failed_format_verification_never_publishes(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    destination = tmp_path / "output.flac"

    def reject_format(source, path):
        raise ValueError("Deliberate format verification failure")

    monkeypatch.setattr(
        renderer,
        "verify_audio",
        reject_format,
    )

    with pytest.raises(
        ValueError,
        match="Deliberate format verification failure",
    ):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".output.*.flac"))


def test_true_peak_violation_never_publishes(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    destination = tmp_path / "output.flac"

    monkeypatch.setattr(
        renderer,
        "measure",
        lambda path: {
            "lufs": -11.0,
            "true_peak": 0.5,
            "lra": 4.0,
        },
    )

    with pytest.raises(ValueError, match="failed acceptance"):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".output.*.flac"))


def test_existing_destination_cannot_be_overwritten(
    tmp_path, source_file, mocked_pipeline
):
    destination = tmp_path / "output.flac"
    destination.write_bytes(b"original destination")

    with pytest.raises(FileExistsError):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert destination.read_bytes() == b"original destination"
    assert mocked_pipeline == []


def test_unsupported_destination_rejected(
    tmp_path, source_file, mocked_pipeline
):
    destination = tmp_path / "output.ogg"

    with pytest.raises(ValueError, match="Unsupported"):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert not destination.exists()
    assert mocked_pipeline == []


def test_metadata_failure_never_publishes(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    destination = tmp_path / "output.flac"

    def reject_metadata(source, temporary):
        raise ValueError("Deliberate metadata failure")

    monkeypatch.setattr(
        renderer,
        "transfer_metadata",
        reject_metadata,
    )

    with pytest.raises(ValueError, match="Deliberate metadata failure"):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".output.*.flac"))


def test_metadata_precedes_format_and_measurement(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    destination = tmp_path / "output.flac"
    operations = []

    def metadata(source, temporary):
        operations.append("metadata")

    def verify(source_format, temporary):
        operations.append("format")

    def measure(temporary):
        operations.append("measurement")
        return {
            "lufs": -11.0,
            "true_peak": -1.2,
            "lra": 4.0,
        }

    monkeypatch.setattr(renderer, "transfer_metadata", metadata)
    monkeypatch.setattr(renderer, "verify_audio", verify)
    monkeypatch.setattr(renderer, "measure", measure)

    renderer.render(
        source_file,
        destination,
        {"name": "gain_only", "gain_db": -3.0},
        ceiling=-1.0,
        target_lufs=-11.0,
    )

    assert operations == [
        "metadata",
        "format",
        "measurement",
    ]

    assert destination.exists()
    assert not list(tmp_path.glob(".output.*.flac"))


def test_wav_rejected_before_encoding(
    tmp_path, source_file, mocked_pipeline
):
    destination = tmp_path / "output.wav"

    with pytest.raises(ValueError, match="Unsupported"):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert not destination.exists()
    assert mocked_pipeline == []


def test_source_modified_during_render_never_publishes(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    from reference_leveler_integrity import (
        fingerprint_file,
        verify_fingerprint,
        SourceIntegrityError,
    )

    destination = tmp_path / "output.flac"

    monkeypatch.setattr(
        renderer,
        "fingerprint_file",
        fingerprint_file,
    )
    monkeypatch.setattr(
        renderer,
        "verify_fingerprint",
        verify_fingerprint,
    )

    def modifying_ffmpeg(arguments):
        source_file.write_bytes(b"modified during encoding")
        Path(arguments[-1]).write_bytes(b"rendered audio")
        return ""

    monkeypatch.setattr(
        renderer,
        "run_ffmpeg",
        modifying_ffmpeg,
    )

    with pytest.raises(SourceIntegrityError, match="mismatch"):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".output.*.flac"))


def test_source_replaced_during_render_never_publishes(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    import os

    from reference_leveler_integrity import (
        fingerprint_file,
        verify_fingerprint,
        SourceIntegrityError,
    )

    destination = tmp_path / "output.flac"

    monkeypatch.setattr(
        renderer,
        "fingerprint_file",
        fingerprint_file,
    )
    monkeypatch.setattr(
        renderer,
        "verify_fingerprint",
        verify_fingerprint,
    )

    def replacing_ffmpeg(arguments):
        replacement = tmp_path / "replacement.flac"
        replacement.write_bytes(b"replacement recording")
        os.replace(replacement, source_file)

        Path(arguments[-1]).write_bytes(b"rendered audio")
        return ""

    monkeypatch.setattr(
        renderer,
        "run_ffmpeg",
        replacing_ffmpeg,
    )

    with pytest.raises(SourceIntegrityError, match="mismatch"):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".output.*.flac"))


def test_successful_render_reports_source_fingerprint(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    from reference_leveler_integrity import (
        fingerprint_file,
        verify_fingerprint,
    )

    destination = tmp_path / "output.flac"

    expected = fingerprint_file(source_file)

    monkeypatch.setattr(
        renderer,
        "fingerprint_file",
        fingerprint_file,
    )
    monkeypatch.setattr(
        renderer,
        "verify_fingerprint",
        verify_fingerprint,
    )

    result = renderer.render(
        source_file,
        destination,
        {"name": "gain_only", "gain_db": -3.0},
        ceiling=-1.0,
        target_lufs=-11.0,
    )

    reported = result["source_fingerprint"]

    assert reported["sha256"] == expected.sha256
    assert reported["size"] == expected.size
    assert reported["device"] == expected.device
    assert reported["inode"] == expected.inode

    assert destination.exists()


def test_integrity_failure_cleans_temporary_output(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    from reference_leveler_integrity import SourceIntegrityError

    destination = tmp_path / "output.flac"

    def reject_integrity(source, expected):
        raise SourceIntegrityError(
            "Deliberate integrity verification failure"
        )

    monkeypatch.setattr(
        renderer,
        "verify_fingerprint",
        reject_integrity,
    )

    with pytest.raises(
        SourceIntegrityError,
        match="Deliberate integrity verification failure",
    ):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".output.*.flac"))


def test_destination_created_during_render_is_not_overwritten(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    destination = tmp_path / "output.flac"

    def competing_ffmpeg(arguments):
        Path(arguments[-1]).write_bytes(b"rendered audio")
        destination.write_bytes(b"competing output")
        return ""

    monkeypatch.setattr(renderer, "run_ffmpeg", competing_ffmpeg)

    with pytest.raises(FileExistsError):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert destination.read_bytes() == b"competing output"
    assert source_file.read_bytes() == b"test source"
    assert not list(tmp_path.glob(".output.*.flac"))


def test_destination_symlink_created_during_render_is_not_followed(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    destination = tmp_path / "output.flac"
    protected = tmp_path / "protected.flac"
    protected.write_bytes(b"protected original")

    def competing_ffmpeg(arguments):
        Path(arguments[-1]).write_bytes(b"rendered audio")
        destination.symlink_to(protected)
        return ""

    monkeypatch.setattr(renderer, "run_ffmpeg", competing_ffmpeg)

    with pytest.raises(FileExistsError):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert destination.is_symlink()
    assert protected.read_bytes() == b"protected original"
    assert source_file.read_bytes() == b"test source"
    assert not list(tmp_path.glob(".output.*.flac"))


def test_destination_directory_replacement_never_publishes(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    import os

    output_directory = tmp_path / "output"
    output_directory.mkdir()

    destination = output_directory / "rendered.flac"

    displaced_directory = tmp_path / "displaced"
    replacement_directory = tmp_path / "replacement"
    replacement_directory.mkdir()

    def replacing_ffmpeg(arguments):
        Path(arguments[-1]).write_bytes(b"rendered audio")

        os.rename(output_directory, displaced_directory)
        output_directory.symlink_to(
            replacement_directory,
            target_is_directory=True,
        )

        return ""

    monkeypatch.setattr(renderer, "run_ffmpeg", replacing_ffmpeg)

    with pytest.raises(
        RuntimeError,
        match="Destination directory changed during rendering",
    ):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert not (replacement_directory / "rendered.flac").exists()
    assert not (displaced_directory / "rendered.flac").exists()
    assert source_file.read_bytes() == b"test source"
    assert not list(displaced_directory.glob(".rendered.*.flac"))


@pytest.mark.parametrize(
    "failing_operation",
    ["fingerprint_file", "probe_audio"],
)
def test_preflight_failure_closes_directory_descriptor(
    tmp_path,
    source_file,
    mocked_pipeline,
    monkeypatch,
    failing_operation,
):
    import os

    destination = tmp_path / "output.flac"

    real_open = renderer.os.open
    real_close = renderer.os.close

    opened = []
    closed = []

    def tracked_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == destination.parent:
            opened.append(descriptor)
        return descriptor

    def tracked_close(descriptor):
        closed.append(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(renderer.os, "open", tracked_open)
    monkeypatch.setattr(renderer.os, "close", tracked_close)

    def reject_preflight(*args, **kwargs):
        raise ValueError("Deliberate preflight failure")

    monkeypatch.setattr(
        renderer,
        failing_operation,
        reject_preflight,
    )

    with pytest.raises(ValueError, match="Deliberate preflight failure"):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert len(opened) == 1
    assert opened[0] in closed

    with pytest.raises(OSError):
        os.fstat(opened[0])

    assert not destination.exists()
    assert not list(tmp_path.glob(".output.*.flac"))


def test_publication_failure_cleans_temporary_and_preserves_source(
    tmp_path, source_file, mocked_pipeline, monkeypatch
):
    import errno

    destination = tmp_path / "output.flac"
    original_source = source_file.read_bytes()

    real_link = renderer.os.link

    def reject_publication(source, target, *args, **kwargs):
        if target == destination.name and "dst_dir_fd" in kwargs:
            raise PermissionError(
                errno.EACCES,
                "Deliberate publication failure",
            )
        return real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(renderer.os, "link", reject_publication)

    with pytest.raises(
        PermissionError,
        match="Deliberate publication failure",
    ):
        renderer.render(
            source_file,
            destination,
            {"name": "gain_only", "gain_db": -3.0},
            ceiling=-1.0,
            target_lufs=-11.0,
        )

    assert source_file.read_bytes() == original_source
    assert not destination.exists()
    assert not list(tmp_path.glob(".output.*.flac"))
