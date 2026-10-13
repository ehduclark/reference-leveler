"""Metadata fidelity tests using disposable audio files."""

import subprocess
from pathlib import Path

import pytest

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, PRIV, TALB, TIT2, TPE1, TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from reference_leveler_metadata import (
    MetadataFidelityError,
    snapshot,
    transfer_metadata,
    verify_metadata,
)


def make_audio(path):
    codec = {
        ".flac": ["-c:a", "flac"],
        ".m4a": ["-c:a", "alac"],
        ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    }[path.suffix]

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "lavfi",
            "-i", "sine=frequency=440:duration=0.3",
            *codec,
            "-y",
            str(path),
        ],
        check=True,
    )


@pytest.fixture(params=[".flac", ".m4a", ".mp3"])
def audio_pair(tmp_path, request):
    extension = request.param

    source = tmp_path / f"source{extension}"
    destination = tmp_path / f"destination{extension}"

    make_audio(source)
    make_audio(destination)

    return source, destination


def populate_metadata(path):
    extension = path.suffix

    if extension == ".flac":
        audio = FLAC(path)

        audio["artist"] = ["Test Artist"]
        audio["title"] = ["Test Title"]
        audio["musicbrainz_trackid"] = ["test-identifier"]
        audio["replaygain_track_gain"] = ["-5.00 dB"]

        picture = Picture()
        picture.type = 3
        picture.mime = "image/png"
        picture.desc = "Front Cover"
        picture.data = b"test-artwork-payload"
        audio.add_picture(picture)

        audio.save()

    elif extension == ".m4a":
        audio = MP4(path)

        audio["\xa9ART"] = ["Test Artist"]
        audio["\xa9nam"] = ["Test Title"]
        audio["----:com.apple.iTunes:iTunNORM"] = [
            b"obsolete-normalization"
        ]
        audio["----:com.apple.iTunes:Encoding Params"] = [
            b"obsolete-encoder-parameters"
        ]
        audio["covr"] = [
            MP4Cover(
                b"test-artwork-payload",
                imageformat=MP4Cover.FORMAT_PNG,
            )
        ]

        audio.save()

    else:
        audio = MP3(path)

        if audio.tags is None:
            audio.add_tags()

        audio.tags.add(TPE1(encoding=3, text=["Test Artist"]))
        audio.tags.add(TIT2(encoding=3, text=["Test Title"]))
        audio.tags.add(TALB(encoding=3, text=["Test Album"]))

        audio.tags.add(
            TXXX(
                encoding=3,
                desc="REPLAYGAIN_TRACK_GAIN",
                text=["-5.00 dB"],
            )
        )

        audio.tags.add(
            COMM(
                encoding=3,
                lang="eng",
                desc="iTunNORM",
                text=["obsolete-normalization"],
            )
        )

        audio.tags.add(
            PRIV(
                owner="PeakValue",
                data=b"\x01\x02\x03\x04",
            )
        )

        audio.tags.add(
            PRIV(
                owner="PreserveThis",
                data=b"\x00\x01\x02\x03",
            )
        )

        audio.tags.add(
            APIC(
                encoding=3,
                mime="image/png",
                type=3,
                desc="Front Cover",
                data=b"test-artwork-payload",
            )
        )

        audio.save()


def test_metadata_round_trip(audio_pair):
    source, destination = audio_pair

    populate_metadata(source)

    transfer_metadata(source, destination)

    assert snapshot(source, filter_removed=True) == snapshot(
        destination,
        filter_removed=False,
    )


def test_corrupted_metadata_is_rejected(audio_pair):
    source, destination = audio_pair

    populate_metadata(source)
    transfer_metadata(source, destination)

    if destination.suffix == ".flac":
        audio = FLAC(destination)
        audio["artist"] = ["Corrupted Artist"]

    elif destination.suffix == ".m4a":
        audio = MP4(destination)
        audio["\xa9ART"] = ["Corrupted Artist"]

    else:
        audio = MP3(destination)
        audio.tags.add(
            TPE1(encoding=3, text=["Corrupted Artist"])
        )

    audio.save()

    with pytest.raises(MetadataFidelityError):
        verify_metadata(source, destination)


def test_corrupted_artwork_is_rejected(audio_pair):
    source, destination = audio_pair

    populate_metadata(source)
    transfer_metadata(source, destination)

    if destination.suffix == ".flac":
        audio = FLAC(destination)
        pictures = list(audio.pictures)

        audio.clear_pictures()

        picture = pictures[0]
        picture.data = b"corrupted-artwork"
        audio.add_picture(picture)

    elif destination.suffix == ".m4a":
        audio = MP4(destination)
        audio["covr"] = [
            MP4Cover(
                b"corrupted-artwork",
                imageformat=MP4Cover.FORMAT_PNG,
            )
        ]

    else:
        audio = MP3(destination)

        for frame in audio.tags.values():
            if isinstance(frame, APIC):
                frame.data = b"corrupted-artwork"

    audio.save()

    with pytest.raises(MetadataFidelityError):
        verify_metadata(source, destination)


def test_multiple_flac_pictures_preserved(tmp_path):
    source = tmp_path / "source.flac"
    destination = tmp_path / "destination.flac"

    make_audio(source)
    make_audio(destination)

    audio = FLAC(source)
    audio["artist"] = ["Multiple Artwork Artist"]

    for picture_type, description, payload in [
        (3, "Front Cover", b"front-cover-image"),
        (4, "Back Cover", b"back-cover-image"),
        (8, "Artist", b"artist-image"),
    ]:
        picture = Picture()
        picture.type = picture_type
        picture.mime = "image/png"
        picture.desc = description
        picture.width = 600
        picture.height = 600
        picture.depth = 24
        picture.data = payload
        audio.add_picture(picture)

    audio.save()

    transfer_metadata(source, destination)

    result = FLAC(destination)

    assert len(result.pictures) == 3
    assert snapshot(source) == snapshot(
        destination,
        filter_removed=False,
    )


def test_complex_mp3_frames_preserved(tmp_path):
    source = tmp_path / "source.mp3"
    destination = tmp_path / "destination.mp3"

    make_audio(source)
    make_audio(destination)

    audio = MP3(source)

    if audio.tags is None:
        audio.add_tags()

    audio.tags.add(
        COMM(
            encoding=3,
            lang="eng",
            desc="Production Notes",
            text=["Original recording notes"],
        )
    )

    audio.tags.add(
        COMM(
            encoding=3,
            lang="fra",
            desc="Notes",
            text=["Notes supplémentaires"],
        )
    )

    audio.tags.add(
        PRIV(
            owner="ReferenceLevelerTest",
            data=b"\x00\xff\x01\xfe\x02\xfd",
        )
    )

    for description, payload in [
        ("Front Cover", b"front-artwork"),
        ("Back Cover", b"back-artwork"),
    ]:
        audio.tags.add(
            APIC(
                encoding=3,
                mime="image/png",
                type=3 if description == "Front Cover" else 4,
                desc=description,
                data=payload,
            )
        )

    audio.save()

    transfer_metadata(source, destination)

    result = MP3(destination)

    assert len(result.tags.getall("APIC")) == 2
    assert len(result.tags.getall("COMM")) == 2

    assert snapshot(source) == snapshot(
        destination,
        filter_removed=False,
    )


def test_obsolete_normalization_metadata_rejected(audio_pair):
    source, destination = audio_pair

    populate_metadata(source)
    transfer_metadata(source, destination)

    if destination.suffix == ".flac":
        audio = FLAC(destination)
        audio["replaygain_track_gain"] = ["-8.00 dB"]

    elif destination.suffix == ".m4a":
        audio = MP4(destination)
        audio["----:com.apple.iTunes:iTunNORM"] = [
            b"obsolete-normalization"
        ]

    else:
        audio = MP3(destination)
        audio.tags.add(
            TXXX(
                encoding=3,
                desc="REPLAYGAIN_TRACK_GAIN",
                text=["-8.00 dB"],
            )
        )

    audio.save()

    with pytest.raises(MetadataFidelityError):
        verify_metadata(source, destination)


def test_corrupted_private_mp3_payload_rejected(tmp_path):
    source = tmp_path / "source.mp3"
    destination = tmp_path / "destination.mp3"

    make_audio(source)
    make_audio(destination)

    audio = MP3(source)

    if audio.tags is None:
        audio.add_tags()

    audio.tags.add(
        PRIV(
            owner="PreserveBinaryData",
            data=b"\x00\x01\x02\xff",
        )
    )

    audio.save()

    transfer_metadata(source, destination)

    output = MP3(destination)

    for frame in output.tags.getall("PRIV"):
        if frame.owner == "PreserveBinaryData":
            frame.data = b"\x00\x01\x02\x00"

    output.save()

    with pytest.raises(MetadataFidelityError):
        verify_metadata(source, destination)


def test_mp4_artwork_format_change_is_rejected(tmp_path):
    """Identical artwork bytes with different format declarations must fail."""

    source = tmp_path / "source.m4a"
    destination = tmp_path / "destination.m4a"

    make_audio(source)
    make_audio(destination)

    populate_metadata(source)
    transfer_metadata(source, destination)

    output = MP4(destination)

    original_cover = output["covr"][0]

    output["covr"] = [
        MP4Cover(
            bytes(original_cover),
            imageformat=MP4Cover.FORMAT_JPEG,
        )
    ]

    output.save()

    with pytest.raises(MetadataFidelityError):
        verify_metadata(source, destination)
