"""Final v0.2.11b treatment and acceptance contract tests."""

import math

import pytest

import reference_leveler_renderer as renderer
from reference_leveler_acceptance import evaluate_output


@pytest.mark.parametrize(
    "parameter",
    [math.nan, math.inf, -math.inf],
)
def test_gain_only_rejects_nonfinite(parameter):
    with pytest.raises(ValueError, match="gain_db"):
        renderer.treatment_filter({
            "name": "gain_only",
            "gain_db": parameter,
        })


@pytest.mark.parametrize(
    "parameter",
    [math.nan, math.inf, -math.inf],
)
def test_limiter_gain_rejects_nonfinite(parameter):
    with pytest.raises(ValueError, match="gain_db"):
        renderer.treatment_filter({
            "name": "gain_plus_limiter",
            "gain_db": parameter,
            "limiter_ceiling_dbtp": -1.0,
        })


@pytest.mark.parametrize(
    "parameter",
    [math.nan, math.inf, -math.inf],
)
def test_limiter_ceiling_rejects_nonfinite(parameter):
    with pytest.raises(ValueError, match="limiter_ceiling_dbtp"):
        renderer.treatment_filter({
            "name": "gain_plus_limiter",
            "gain_db": 2.0,
            "limiter_ceiling_dbtp": parameter,
        })


@pytest.mark.parametrize(
    "parameter",
    [math.nan, math.inf, -math.inf],
)
def test_dynamic_target_rejects_nonfinite(parameter):
    with pytest.raises(ValueError, match="target_lufs"):
        renderer.treatment_filter({
            "name": "dynamic_loudnorm",
            "target_lufs": parameter,
            "ceiling_dbtp": -1.0,
        })


@pytest.mark.parametrize(
    "parameter",
    [math.nan, math.inf, -math.inf],
)
def test_dynamic_ceiling_rejects_nonfinite(parameter):
    with pytest.raises(ValueError, match="ceiling_dbtp"):
        renderer.treatment_filter({
            "name": "dynamic_loudnorm",
            "target_lufs": -11.0,
            "ceiling_dbtp": parameter,
        })


def test_approved_quieter_exception_is_accepted():
    result = evaluate_output(
        {
            "lufs": -12.5,
            "true_peak": -1.2,
            "lra": 5.0,
        },
        target_lufs=-11.0,
        ceiling=-1.0,
        approved_deviation=True,
    )

    assert result.outcome == "accepted_with_deviation"
    assert result.publishable


def test_approved_louder_exception_requires_review():
    result = evaluate_output(
        {
            "lufs": -9.5,
            "true_peak": -1.2,
            "lra": 5.0,
        },
        target_lufs=-11.0,
        ceiling=-1.0,
        approved_deviation=True,
    )

    assert result.outcome == "review_required"
    assert not result.publishable


def test_approved_outside_budget_requires_review():
    result = evaluate_output(
        {
            "lufs": -13.5,
            "true_peak": -1.2,
            "lra": 5.0,
        },
        target_lufs=-11.0,
        ceiling=-1.0,
        approved_deviation=True,
    )

    assert result.outcome == "review_required"
    assert not result.publishable
