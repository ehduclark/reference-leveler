# Reference Leveler

**Version:** 0.2.13b3b2
**Status:** Development preview
**License:** MIT

Reference Leveler is a Python audio-processing project designed to match
the loudness characteristics of a reference recording while protecting
source integrity and audio metadata.

Production batch processing is not yet enabled.

## Requirements

- Python 3.12 (tested development environment)
- FFmpeg and ffprobe available on PATH
- Python packages listed in requirements.txt

## Installation

Install FFmpeg on macOS:

    brew install ffmpeg

Create a virtual environment:

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt

## Reference profiling

    python reference_leveler.py profile /path/to/reference.flac --output music-profile.json

## Current capabilities

- Reference audio profiling
- Inventory validation and reconciliation
- Project manifests and revision history
- Immutable metadata publication and verification
- Protected rendering components
- Automated regression testing

## Production limitations

The legacy batch command is retired.
Production batch processing remains disabled until project-aware
orchestration and recovery safeguards are complete.

## Testing

    python -m pip install pytest
    python -m pytest -q

The v0.2.13b3b2 public snapshot contains 457 automated tests.

## License

MIT. See LICENSE.
