"""Unit tests for final-output acceptance decisions."""

import math

import pytest

from reference_leveler_acceptance import evaluate_output


def measured(lufs=-11.0, peak=-1.2, lra=4.0):
    return {
        "lufs": lufs,
        "true_peak": peak,
        "lra": lra,
    }


@pytest.mark.parametrize(
    "error,expected_class,expected_outcome",
    [
        (0.0, "preferred", "accepted"),
        (0.5, "preferred", "accepted"),
        (0.75, "acceptable", "accepted"),
        (1.0, "acceptable", "accepted"),
        (1.5, "exception", "review_required"),
        (2.0, "exception", "review_required"),
        (2.1, "outside_budget", "review_required"),
    ],
)
def test_loudness_boundaries(
    error,
    expected_class,
    expected_outcome,
):
    result = evaluate_output(
        measured(lufs=-11.0 - error),
        target_lufs=-11.0,
        ceiling=-1.0,
    )

    assert result.loudness_class == expected_class
    assert result.outcome == expected_outcome
    assert result.publishable == (expected_outcome == "accepted")


def test_explicit_approval_permits_exception():
    result = evaluate_output(
        measured(lufs=-12.5),
        target_lufs=-11.0,
        ceiling=-1.0,
        approved_deviation=True,
    )

    assert result.outcome == "accepted_with_deviation"
    assert result.publishable
    assert not result.review_required


def test_approval_does_not_override_outside_budget():
    result = evaluate_output(
        measured(lufs=-14.0),
        target_lufs=-11.0,
        ceiling=-1.0,
        approved_deviation=True,
    )

    assert result.outcome == "review_required"
    assert not result.publishable


def test_peak_at_tolerance_is_accepted():
    result = evaluate_output(
        measured(peak=-0.95),
        target_lufs=-11.0,
        ceiling=-1.0,
    )

    assert result.outcome == "accepted"
    assert result.peak_compliant


def test_peak_above_tolerance_is_rejected():
    result = evaluate_output(
        measured(peak=-0.94),
        target_lufs=-11.0,
        ceiling=-1.0,
    )

    assert result.outcome == "rejected"
    assert not result.publishable
    assert not result.peak_compliant


@pytest.mark.parametrize(
    "field,value",
    [
        ("lufs", math.nan),
        ("lufs", math.inf),
        ("true_peak", math.nan),
        ("true_peak", -math.inf),
        ("lra", math.nan),
        ("lra", math.inf),
        ("lufs", None),
        ("true_peak", "unknown"),
    ],
)
def test_invalid_measurements_rejected(field, value):
    values = measured()
    values[field] = value

    result = evaluate_output(
        values,
        target_lufs=-11.0,
        ceiling=-1.0,
    )

    assert result.outcome == "rejected"
    assert not result.publishable


@pytest.mark.parametrize("target", [math.nan, math.inf, -math.inf])
def test_invalid_target_rejected(target):
    with pytest.raises(ValueError, match="Target LUFS"):
        evaluate_output(
            measured(),
            target_lufs=target,
            ceiling=-1.0,
        )


@pytest.mark.parametrize("ceiling", [math.nan, math.inf, -math.inf])
def test_invalid_ceiling_rejected(ceiling):
    with pytest.raises(ValueError, match="ceiling"):
        evaluate_output(
            measured(),
            target_lufs=-11.0,
            ceiling=ceiling,
        )


def test_approval_must_be_explicit_boolean():
    with pytest.raises(TypeError, match="approved_deviation"):
        evaluate_output(
            measured(lufs=-12.5),
            target_lufs=-11.0,
            ceiling=-1.0,
            approved_deviation="yes",
        )


def test_decision_serializes_for_future_project_manifest():
    result = evaluate_output(
        measured(),
        target_lufs=-11.0,
        ceiling=-1.0,
    )

    payload = result.as_dict()

    assert payload["outcome"] == "accepted"
    assert payload["publishable"] is True
    assert payload["loudness_error"] == 0.0


def test_invalid_measurement_container_rejected():
    result = evaluate_output(
        None,
        target_lufs=-11.0,
        ceiling=-1.0,
    )

    assert result.outcome == "rejected"
    assert not result.publishable
