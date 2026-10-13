"""Tests for canonical treatment specification identity."""

import copy
import math

import pytest

from reference_leveler_treatments import (
    TreatmentSpecError,
    validate_treatment,
    treatment_fingerprint,
)


TARGET = -11.0
CEILING = -1.0


@pytest.mark.parametrize(
    "spec",
    [
        {"name": "gain_only", "gain_db": -2.5},
        {
            "name": "gain_plus_limiter",
            "gain_db": 3.0,
            "limiter_ceiling_dbtp": -1.5,
        },
        {
            "name": "dynamic_loudnorm",
            "target_lufs": TARGET,
            "ceiling_dbtp": CEILING,
        },
    ],
)
def test_supported_treatments_validate(spec):
    result = validate_treatment(
        spec,
        target_lufs=TARGET,
        ceiling_dbtp=CEILING,
    )

    assert result["name"] == spec["name"]
    assert result is not spec


@pytest.mark.parametrize(
    "spec",
    [
        {"name": "conservative_gain", "gain_db": -2.0},
        {"name": "gain_only"},
        {"name": "gain_only", "gain_db": 1.0, "extra": 7},
        {"name": "gain_only", "gain_db": True},
        {"name": "gain_only", "gain_db": float("nan")},
        {"name": "gain_only", "gain_db": float("inf")},
        {"name": "gain_only", "gain_db": "2.0"},
        {
            "name": "dynamic_loudnorm",
            "target_lufs": -12.0,
            "ceiling_dbtp": CEILING,
        },
        {
            "name": "dynamic_loudnorm",
            "target_lufs": TARGET,
            "ceiling_dbtp": -2.0,
        },
        {
            "name": "gain_plus_limiter",
            "gain_db": 2.0,
            "limiter_ceiling_dbtp": 0.0,
        },
    ],
)
def test_invalid_treatments_rejected(spec):
    with pytest.raises(TreatmentSpecError):
        validate_treatment(
            spec,
            target_lufs=TARGET,
            ceiling_dbtp=CEILING,
        )


def test_fingerprint_is_independent_of_dictionary_order():
    first = {
        "name": "gain_plus_limiter",
        "gain_db": 3.0,
        "limiter_ceiling_dbtp": -1.5,
    }

    second = {
        "limiter_ceiling_dbtp": -1.5,
        "name": "gain_plus_limiter",
        "gain_db": 3.0,
    }

    assert treatment_fingerprint(
        first,
        target_lufs=TARGET,
        ceiling_dbtp=CEILING,
    ) == treatment_fingerprint(
        second,
        target_lufs=TARGET,
        ceiling_dbtp=CEILING,
    )


def test_parameter_change_invalidates_fingerprint():
    first = {"name": "gain_only", "gain_db": -2.5}
    second = {"name": "gain_only", "gain_db": -2.4}

    assert treatment_fingerprint(
        first,
        target_lufs=TARGET,
        ceiling_dbtp=CEILING,
    ) != treatment_fingerprint(
        second,
        target_lufs=TARGET,
        ceiling_dbtp=CEILING,
    )


def test_validation_does_not_mutate_input():
    spec = {"name": "gain_only", "gain_db": -2.5}
    original = copy.deepcopy(spec)

    validate_treatment(
        spec,
        target_lufs=TARGET,
        ceiling_dbtp=CEILING,
    )

    assert spec == original


@pytest.mark.parametrize(
    "target,ceiling",
    [
        (math.nan, CEILING),
        (TARGET, math.inf),
        (True, CEILING),
        (TARGET, False),
    ],
)
def test_invalid_project_numbers_rejected(target, ceiling):
    with pytest.raises(TreatmentSpecError):
        validate_treatment(
            {"name": "gain_only", "gain_db": 1.0},
            target_lufs=target,
            ceiling_dbtp=ceiling,
        )
