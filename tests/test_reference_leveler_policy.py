"""Regression tests for candidate decision policy."""

from reference_leveler_policy import select_candidate


def candidate(name, lufs, peak, lra_change, score, target=-11.0):
    return {
        "name": name,
        "status": "eligible",
        "loudness_error": abs(lufs - target),
        "lra_change": lra_change,
        "score": score,
        "measurements": {
            "lufs": lufs,
            "true_peak": peak,
        },
    }


def test_cramps_prefers_limiter():
    result = select_candidate([
        candidate("gain_only", -11.00, 3.87, 0.0, 0.0),
        candidate("gain_plus_limiter", -11.55, -1.15, 0.5, 0.675),
        candidate("dynamic_loudnorm", -10.94, -1.0, 2.2, 1.58),
    ], -1.0)

    assert result["selected_candidate"] == "gain_plus_limiter"
    assert result["outcome"] == "accepted"
    assert result["review_required"] is False


def test_perturbator_prefers_gain_only():
    result = select_candidate([
        candidate("gain_only", -11.0, -6.49, 0.1, 0.05),
        candidate("conservative_gain", -11.0, -6.49, 0.1, 0.05),
        candidate("dynamic_loudnorm", -11.0, -1.0, 0.4, 0.65),
    ], -1.0)

    assert result["selected_candidate"] == "gain_only"
    assert result["outcome"] == "accepted"


def test_megadrive_uses_reviewed_fallback():
    result = select_candidate([
        candidate("gain_only", -11.01, 7.06, 0.9, 0.455),
        candidate("conservative_gain", -19.26, -1.20, 0.9, 4.58),
        candidate("gain_plus_limiter", -14.17, -1.40, 3.3, 3.385),
        candidate("dynamic_loudnorm", -15.62, -1.0, 11.3, 8.41),
    ], -1.0)

    assert result["selected_candidate"] == "gain_plus_limiter"
    assert result["outcome"] == "accepted_with_deviation"
    assert result["review_required"] is True


def test_excessive_intervention_is_rejected():
    result = select_candidate([
        candidate("dynamic_loudnorm", -11.0, -1.0, 8.0, 0.5),
    ], -1.0)

    assert result["selected_candidate"] is None
    assert result["review_required"] is True


def test_peak_violation_is_always_rejected():
    result = select_candidate([
        candidate("gain_only", -11.0, 0.0, 0.0, 0.0),
    ], -1.0)

    assert result["selected_candidate"] is None
    assert result["review_required"] is True
