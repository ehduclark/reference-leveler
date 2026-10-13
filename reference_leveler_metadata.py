#!/usr/bin/env python3
"""Metadata preservation and verification for Reference Leveler."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import COMM, PRIV, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4


class MetadataFidelityError(ValueError):
    """Metadata could not be preserved or verified."""


SUPPORTED = {
    ".flac": FLAC,
    ".m4a": MP4,
    ".mp3": MP3,
}

FLAC_REMOVE = {
    "replaygain_track_gain",
    "replaygain_track_peak",
    "replaygain_album_gain",
    "replaygain_album_peak",
    "replaygain_reference_loudness",
}

MP4_REMOVE = {
    "----:com.apple.iTunes:iTunNORM",
    "----:com.apple.iTunes:Encoding Params",
}


def load_audio(path: Path):
    path = Path(path)
    extension = path.suffix.lower()

    if extension not in SUPPORTED:
        raise MetadataFidelityError(
            f"Unsupported metadata format: {extension}"
        )

    try:
        return SUPPORTED[extension](path)
    except Exception as exc:
        raise MetadataFidelityError(
            f"Cannot read metadata: {path}"
        ) from exc


def is_removed_id3(frame):
    if isinstance(frame, TXXX):
        return frame.desc.upper().startswith("REPLAYGAIN_")

    if isinstance(frame, COMM):
        return frame.desc.lower() == "itunnorm"

    if isinstance(frame, PRIV):
        return frame.owner == "PeakValue"

    return False


def normalized_tags(audio, extension):
    """Return a deep copy of the metadata permitted in output."""

    if extension == ".flac":
        return {
            key: list(values)
            for key, values in (audio.tags or {}).items()
            if key.lower() not in FLAC_REMOVE
        }

    if extension == ".m4a":
        return {
            key: copy.deepcopy(values)
            for key, values in (audio.tags or {}).items()
            if key not in MP4_REMOVE
        }

    if extension == ".mp3":
        if audio.tags is None:
            return []

        return [
            copy.deepcopy(frame)
            for frame in audio.tags.values()
            if not is_removed_id3(frame)
        ]

    raise MetadataFidelityError(
        f"Unsupported metadata format: {extension}"
    )


def canonical_value(value):
    """Canonicalize metadata without discarding binary payloads."""

    if isinstance(value, bytes):
        return (
            type(value).__name__,
            hashlib.sha256(value).hexdigest(),
            getattr(value, "imageformat", None),
        )

    if isinstance(value, (str, int, float, bool, type(None))):
        return value

    if isinstance(value, (list, tuple)):
        return tuple(canonical_value(item) for item in value)

    if isinstance(value, dict):
        return tuple(sorted(
            (key, canonical_value(item))
            for key, item in value.items()
        ))

    if hasattr(value, "data"):
        return (
            type(value).__name__,
            hashlib.sha256(bytes(value.data)).hexdigest(),
            getattr(value, "imageformat", None),
        )

    return repr(value)


def id3_frame_snapshot(frame):
    """Capture ID3 frame identity and all serialized field values."""

    fields = tuple(sorted(
        (
            name,
            canonical_value(value),
        )
        for name, value in vars(frame).items()
        if not name.startswith("_")
    ))

    return (
        frame.FrameID,
        frame.HashKey,
        fields,
    )


def picture_snapshot(picture):
    """Capture FLAC picture structure and image content."""

    return (
        picture.type,
        picture.mime,
        picture.desc,
        picture.width,
        picture.height,
        picture.depth,
        picture.colors,
        hashlib.sha256(picture.data).hexdigest(),
    )


def snapshot(path: Path, *, filter_removed=True):
    """Capture metadata, optionally excluding obsolete source fields."""

    path = Path(path)
    extension = path.suffix.lower()
    audio = load_audio(path)

    if filter_removed:
        tags = normalized_tags(audio, extension)
    elif extension == ".mp3":
        tags = list(audio.tags.values()) if audio.tags else []
    else:
        tags = dict(audio.tags or {})

    if extension == ".flac":
        tag_snapshot = tuple(sorted(
            (
                key.lower(),
                tuple(sorted(values)),
            )
            for key, values in tags.items()
        ))

        pictures = tuple(sorted(
            picture_snapshot(picture)
            for picture in audio.pictures
        ))

    elif extension == ".m4a":
        tag_snapshot = tuple(sorted(
            (
                key,
                canonical_value(values),
            )
            for key, values in tags.items()
        ))

        pictures = ()

    else:
        tag_snapshot = tuple(sorted(
            id3_frame_snapshot(frame)
            for frame in tags
        ))

        pictures = ()

    return {
        "extension": extension,
        "tags": tag_snapshot,
        "pictures": pictures,
    }

def transfer_metadata(source: Path, destination: Path):
    """Copy permitted source metadata to an existing output file."""

    source = Path(source)
    destination = Path(destination)

    if source.resolve() == destination.resolve():
        raise MetadataFidelityError(
            "Source and destination must differ"
        )

    if source.suffix.lower() != destination.suffix.lower():
        raise MetadataFidelityError(
            "Source and destination formats must match"
        )

    extension = source.suffix.lower()

    original = load_audio(source)
    output = load_audio(destination)

    tags = normalized_tags(original, extension)

    try:
        if extension == ".flac":
            output.clear()

            for key, values in tags.items():
                output[key] = values

            output.clear_pictures()

            for picture in original.pictures:
                output.add_picture(copy.deepcopy(picture))

        elif extension == ".m4a":
            output.clear()

            for key, values in tags.items():
                output[key] = copy.deepcopy(values)

        elif extension == ".mp3":
            if output.tags is None:
                output.add_tags()
            else:
                output.tags.clear()

            for frame in tags:
                output.tags.add(copy.deepcopy(frame))

        output.save()

    except Exception as exc:
        raise MetadataFidelityError(
            f"Metadata transfer failed: {destination}"
        ) from exc

    verify_metadata(source, destination)


def verify_metadata(source: Path, destination: Path):
    """Reject output when preserved metadata differs from source."""

    expected = snapshot(source, filter_removed=True)
    actual = snapshot(destination, filter_removed=False)

    if expected != actual:
        raise MetadataFidelityError(
            f"Metadata verification failed: {destination}"
        )

    return actual
