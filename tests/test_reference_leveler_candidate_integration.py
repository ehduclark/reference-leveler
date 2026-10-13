"""Integration tests for executable candidate treatment contracts."""

from pathlib import Path

import pytest

import reference_leveler_candidates as candidates
from reference_leveler_treatments import (
    validate_treatment,
    treatment_fingerprint,
)


TARGET = -11.0
CEILING = -1.0


@pytest.fixture
def mocked_evaluator(monkeypatch):
    """Run the full candidate evaluator without FFmpeg or real audio."""

    original = {
        "lufs": -15.0,
        "true_peak": -3.0,
        "lra": 6.0,
    }

    outputs = {
        "gain_only": {
            "lufs": -11.0,
            "true_peak": 1.0,
            "lra": 6.0,
        },
        "conservative_gain": {
            "lufs": -13.2,
            "true_peak": -1.2,
            "lra": 6.0,
        },
        "gain_plus_limiter": {
            "lufs": -11.1,
            "true_peak": -1.1,
            "lra": 5.8,
        },
        "dynamic_loudnorm": {
            "lufs": -11.0,
            "true_peak": -1.0,
            "lra": 5.0,
        },
    }

    calls = []

    def fake_measure(path):
        path = Path(path)

        if path.name == "source.flac":
            return original

        return outputs[path.stem]

    def fake_ffmpeg(arguments):
        calls.append(arguments)
        return ""

    monkeypatch.setattr(candidates, "measure", fake_measure)
    monkeypatch.setattr(candidates, "run_ffmpeg", fake_ffmpeg)

    return calls


def test_evaluate_returns_four_executable_candidates(
    mocked_evaluator,
):
    result = candidates.evaluate(
        Path("source.flac"),
        TARGET,
        CEILING,
    )

    assert len(result["candidates"]) == 4
    assert len(mocked_evaluator) == 4

    for candidate in result["candidates"]:
        assert "treatment_spec" in candidate
        assert "treatment_fingerprint" in candidate

        spec = candidate["treatment_spec"]

        assert validate_treatment(
            spec,
            target_lufs=TARGET,
            ceiling_dbtp=CEILING,
        ) == spec

        assert candidate["treatment_fingerprint"] == (
            treatment_fingerprint(
                spec,
                target_lufs=TARGET,
                ceiling_dbtp=CEILING,
            )
        )


def test_conservative_candidate_uses_gain_only_renderer(
    mocked_evaluator,
):
    result = candidates.evaluate(
        Path("source.flac"),
        TARGET,
        CEILING,
    )

    conservative = next(
        candidate
        for candidate in result["candidates"]
        if candidate["name"] == "conservative_gain"
    )

    assert conservative["treatment_spec"] == {
        "name": "gain_only",
        "gain_db": 1.8,
    }


def test_selection_behavior_preserved(mocked_evaluator):
    result = candidates.evaluate(
        Path("source.flac"),
        TARGET,
        CEILING,
    )

    assert result["recommended_candidate"] == "gain_plus_limiter"
    assert result["target_lufs"] == TARGET
    assert result["ceiling_dbtp"] == CEILING

    statuses = {
        item["name"]: item["status"]
        for item in result["candidates"]
    }

    assert statuses["gain_only"] == "peak_violation"
    assert statuses["conservative_gain"] == "review_required"
    assert statuses["gain_plus_limiter"] == "eligible"
    assert statuses["dynamic_loudnorm"] == "eligible"


def test_failed_candidate_has_no_executable_authorization(
    mocked_evaluator,
    monkeypatch,
):
    original_run = candidates.run_ffmpeg

    def fail_limiter(arguments):
        output = Path(arguments[-1])

        if output.stem == "gain_plus_limiter":
            raise RuntimeError("Simulated limiter failure")

        return original_run(arguments)

    monkeypatch.setattr(
        candidates,
        "run_ffmpeg",
        fail_limiter,
    )

    result = candidates.evaluate(
        Path("source.flac"),
        TARGET,
        CEILING,
    )

    failed = next(
        item
        for item in result["candidates"]
        if item["name"] == "gain_plus_limiter"
    )

    assert failed["status"] == "error"
    assert "treatment_spec" not in failed
    assert "treatment_fingerprint" not in failed
    assert "Simulated limiter failure" in failed["error"]
