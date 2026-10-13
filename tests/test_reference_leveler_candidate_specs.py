"""Candidate-to-renderer treatment specification contracts."""

import pytest

from reference_leveler_candidates import build_treatment_specs
from reference_leveler_treatments import (
    validate_treatment,
    treatment_fingerprint,
)


TARGET = -11.0
CEILING = -1.0


@pytest.fixture
def original():
    return {
        "lufs": -15.0,
        "true_peak": -3.0,
        "lra": 6.0,
    }


def test_all_four_candidate_strategies_present(original):
    specs = build_treatment_specs(original, TARGET, CEILING)

    assert set(specs) == {
        "gain_only",
        "conservative_gain",
        "gain_plus_limiter",
        "dynamic_loudnorm",
    }


def test_conservative_gain_maps_to_executable_gain_only(original):
    specs = build_treatment_specs(original, TARGET, CEILING)

    assert specs["conservative_gain"] == {
        "name": "gain_only",
        "gain_db": 1.8,
    }


def test_gain_only_preserves_requested_gain(original):
    specs = build_treatment_specs(original, TARGET, CEILING)

    assert specs["gain_only"] == {
        "name": "gain_only",
        "gain_db": 4.0,
    }


def test_limiter_preserves_headroom(original):
    specs = build_treatment_specs(original, TARGET, CEILING)

    assert specs["gain_plus_limiter"] == {
        "name": "gain_plus_limiter",
        "gain_db": 4.0,
        "limiter_ceiling_dbtp": -1.5,
    }


def test_dynamic_loudnorm_matches_project(original):
    specs = build_treatment_specs(original, TARGET, CEILING)

    assert specs["dynamic_loudnorm"] == {
        "name": "dynamic_loudnorm",
        "target_lufs": TARGET,
        "ceiling_dbtp": CEILING,
    }


def test_every_candidate_is_renderer_compatible(original):
    specs = build_treatment_specs(original, TARGET, CEILING)

    for spec in specs.values():
        assert validate_treatment(
            spec,
            target_lufs=TARGET,
            ceiling_dbtp=CEILING,
        ) == spec


def test_candidate_fingerprints_are_stable(original):
    first = build_treatment_specs(original, TARGET, CEILING)
    second = build_treatment_specs(original, TARGET, CEILING)

    for name in first:
        assert treatment_fingerprint(
            first[name],
            target_lufs=TARGET,
            ceiling_dbtp=CEILING,
        ) == treatment_fingerprint(
            second[name],
            target_lufs=TARGET,
            ceiling_dbtp=CEILING,
        )


def test_conservative_gain_never_exceeds_requested_gain(original):
    specs = build_treatment_specs(original, TARGET, CEILING)

    assert (
        specs["conservative_gain"]["gain_db"]
        <= specs["gain_only"]["gain_db"]
    )
