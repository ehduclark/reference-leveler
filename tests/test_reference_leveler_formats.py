"""Regression tests for Reference Leveler format-aware encoding."""

from pathlib import Path

import pytest

import reference_leveler_formats as formats


def audio(codec, depth=None, bitrate=None):
    return formats.AudioFormat(
        codec=codec,
        sample_rate=44100,
        channels=2,
        sample_format=None,
        bits_per_raw_sample=depth,
        bit_rate=bitrate,
    )


@pytest.mark.parametrize(
    "source,extension,encoder,expected_format",
    [
        (audio("flac", 16), ".flac", "flac", "s16"),
        (audio("flac", 24), ".flac", "flac", "s32"),
        (audio("alac", 16), ".m4a", "alac", "s16p"),
        (audio("alac", 24), ".m4a", "alac", "s32p"),
        (audio("mp3", bitrate=192000), ".mp3", "libmp3lame", None),
        (audio("pcm_s24le"), ".wav", "pcm_s24le", None),
    ],
)
def test_encoding_configuration(
    source, extension, encoder, expected_format
):
    args = formats.encoding_arguments(
        source,
        Path("output" + extension),
    )

    assert args[args.index("-c:a") + 1] == encoder
    assert args[args.index("-ar") + 1] == "44100"
    assert args[args.index("-ac") + 1] == "2"

    if expected_format is not None:
        assert args[args.index("-sample_fmt") + 1] == expected_format


@pytest.mark.parametrize(
    "source,extension",
    [
        (audio("mp3", bitrate=192000), ".flac"),
        (audio("aac", bitrate=192000), ".m4a"),
        (audio("flac", 16), ".mp3"),
        (audio("alac", 16), ".flac"),
        (audio("flac", 16), ".ogg"),
        (audio("flac", None), ".flac"),
        (audio("mp3", bitrate=None), ".mp3"),
    ],
)
def test_unsupported_format_combinations_rejected(source, extension):
    with pytest.raises(formats.FormatFidelityError):
        formats.encoding_arguments(
            source,
            Path("output" + extension),
        )


def test_verify_rejects_codec_change(monkeypatch):
    source = audio("flac", 16)
    output = audio("mp3", bitrate=192000)

    monkeypatch.setattr(
        formats,
        "probe_audio",
        lambda path: output,
    )

    with pytest.raises(formats.FormatFidelityError, match="Codec changed"):
        formats.verify_audio(source, Path("output.flac"))


def test_verify_rejects_sample_rate_change(monkeypatch):
    source = audio("flac", 16)

    output = formats.AudioFormat(
        codec="flac",
        sample_rate=48000,
        channels=2,
        sample_format="s16",
        bits_per_raw_sample=16,
        bit_rate=None,
    )

    monkeypatch.setattr(
        formats,
        "probe_audio",
        lambda path: output,
    )

    with pytest.raises(formats.FormatFidelityError, match="sample rate"):
        formats.verify_audio(source, Path("output.flac"))


def test_verify_rejects_channel_change(monkeypatch):
    source = audio("flac", 16)

    output = formats.AudioFormat(
        codec="flac",
        sample_rate=44100,
        channels=1,
        sample_format="s16",
        bits_per_raw_sample=16,
        bit_rate=None,
    )

    monkeypatch.setattr(
        formats,
        "probe_audio",
        lambda path: output,
    )

    with pytest.raises(formats.FormatFidelityError, match="channel count"):
        formats.verify_audio(source, Path("output.flac"))


def test_verify_rejects_bit_depth_change(monkeypatch):
    source = audio("flac", 24)
    output = audio("flac", 16)

    monkeypatch.setattr(
        formats,
        "probe_audio",
        lambda path: output,
    )

    with pytest.raises(formats.FormatFidelityError, match="bit depth"):
        formats.verify_audio(source, Path("output.flac"))



def test_verify_rejects_missing_output_bit_depth(monkeypatch):
    source = audio("flac", 24)
    output = audio("flac", None)

    monkeypatch.setattr(
        formats,
        "probe_audio",
        lambda path: output,
    )

    with pytest.raises(formats.FormatFidelityError, match="bit depth"):
        formats.verify_audio(source, Path("output.flac"))


def test_channel_layout_mismatch_rejected(monkeypatch):
    source = formats.AudioFormat(
        codec="flac",
        sample_rate=48000,
        channels=6,
        sample_format="s16",
        bits_per_raw_sample=16,
        bit_rate=None,
        channel_layout="5.1",
    )

    output = formats.AudioFormat(
        codec="flac",
        sample_rate=48000,
        channels=6,
        sample_format="s16",
        bits_per_raw_sample=16,
        bit_rate=None,
        channel_layout="5.1(side)",
    )

    monkeypatch.setattr(formats, "probe_audio", lambda _: output)

    with pytest.raises(
        formats.FormatFidelityError,
        match="channel layout differs",
    ):
        formats.verify_audio(source, Path("output.flac"))


def test_missing_output_channel_layout_rejected(monkeypatch):
    source = formats.AudioFormat(
        codec="flac",
        sample_rate=48000,
        channels=2,
        sample_format="s16",
        bits_per_raw_sample=16,
        bit_rate=None,
        channel_layout="stereo",
    )

    output = formats.AudioFormat(
        codec="flac",
        sample_rate=48000,
        channels=2,
        sample_format="s16",
        bits_per_raw_sample=16,
        bit_rate=None,
        channel_layout=None,
    )

    monkeypatch.setattr(formats, "probe_audio", lambda _: output)

    with pytest.raises(
        formats.FormatFidelityError,
        match="channel layout differs",
    ):
        formats.verify_audio(source, Path("output.flac"))


def test_unknown_source_channel_layout_is_not_invented(monkeypatch):
    source = formats.AudioFormat(
        codec="flac",
        sample_rate=48000,
        channels=2,
        sample_format="s16",
        bits_per_raw_sample=16,
        bit_rate=None,
        channel_layout=None,
    )

    output = formats.AudioFormat(
        codec="flac",
        sample_rate=48000,
        channels=2,
        sample_format="s16",
        bits_per_raw_sample=16,
        bit_rate=None,
        channel_layout="stereo",
    )

    monkeypatch.setattr(formats, "probe_audio", lambda _: output)

    assert formats.verify_audio(
        source,
        Path("output.flac"),
    ) == output
