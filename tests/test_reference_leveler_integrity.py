"""Adversarial source identity and fingerprint tests."""

import os
from pathlib import Path

import pytest

from reference_leveler_integrity import (
    SourceIntegrityError,
    fingerprint_file,
    verify_fingerprint,
    validate_distinct_paths,
)


def test_unchanged_source_verifies(tmp_path):
    source = tmp_path / "source.flac"
    source.write_bytes(b"original recording")

    expected = fingerprint_file(source)

    verify_fingerprint(source, expected)

    assert expected.size == len(b"original recording")
    assert len(expected.sha256) == 64


def test_modified_source_bytes_rejected(tmp_path):
    source = tmp_path / "source.flac"
    source.write_bytes(b"original recording")

    expected = fingerprint_file(source)

    source.write_bytes(b"modified recording")

    with pytest.raises(SourceIntegrityError, match="mismatch"):
        verify_fingerprint(source, expected)


def test_replaced_source_rejected(tmp_path):
    source = tmp_path / "source.flac"
    replacement = tmp_path / "replacement.flac"

    source.write_bytes(b"original recording")
    expected = fingerprint_file(source)

    replacement.write_bytes(b"replacement recording")
    os.replace(replacement, source)

    with pytest.raises(SourceIntegrityError, match="mismatch"):
        verify_fingerprint(source, expected)


def test_identical_source_destination_rejected(tmp_path):
    source = tmp_path / "source.flac"
    source.write_bytes(b"original recording")

    with pytest.raises(SourceIntegrityError):
        validate_distinct_paths(source, source)


def test_destination_symlink_to_source_rejected(tmp_path):
    source = tmp_path / "source.flac"
    destination = tmp_path / "destination.flac"

    source.write_bytes(b"original recording")
    destination.symlink_to(source)

    with pytest.raises(SourceIntegrityError):
        validate_distinct_paths(source, destination)


def test_destination_hardlink_to_source_rejected(tmp_path):
    source = tmp_path / "source.flac"
    destination = tmp_path / "destination.flac"

    source.write_bytes(b"original recording")
    os.link(source, destination)

    with pytest.raises(SourceIntegrityError):
        validate_distinct_paths(source, destination)


def test_symlinked_destination_directory_rejected(tmp_path):
    source = tmp_path / "source.flac"
    source.write_bytes(b"original recording")

    alias_directory = tmp_path / "alias"
    alias_directory.symlink_to(tmp_path, target_is_directory=True)

    destination = alias_directory / "source.flac"

    with pytest.raises(SourceIntegrityError):
        validate_distinct_paths(source, destination)


def test_distinct_paths_accepted(tmp_path):
    source = tmp_path / "source.flac"
    destination = tmp_path / "output.flac"

    source.write_bytes(b"original recording")

    resolved_source, resolved_destination = validate_distinct_paths(
        source,
        destination,
    )

    assert resolved_source == source.resolve()
    assert resolved_destination == destination.absolute()


def test_directory_source_rejected(tmp_path):
    destination = tmp_path / "output.flac"

    with pytest.raises(SourceIntegrityError):
        validate_distinct_paths(tmp_path, destination)


def test_dangling_destination_symlink_rejected(tmp_path):
    source = tmp_path / "source.flac"
    destination = tmp_path / "destination.flac"

    source.write_bytes(b"original recording")
    destination.symlink_to(tmp_path / "nonexistent.flac")

    with pytest.raises(FileExistsError):
        validate_distinct_paths(source, destination)

    assert destination.is_symlink()


def test_existing_unrelated_destination_rejected(tmp_path):
    source = tmp_path / "source.flac"
    destination = tmp_path / "destination.flac"

    source.write_bytes(b"original recording")
    destination.write_bytes(b"existing output")

    with pytest.raises(FileExistsError):
        validate_distinct_paths(source, destination)

    assert destination.read_bytes() == b"existing output"
