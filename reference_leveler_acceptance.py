"""Final-output acceptance using the existing processing budget.

This module evaluates measured output. It does not select treatments,
authorize exceptions, or publish recordings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from reference_leveler_policy import DEFAULT_BUDGET, ProcessingBudget


@dataclass(frozen=True)
class OutputAcceptance:
    outcome: str
    publishable: bool
    review_required: bool
    loudness_class: str | None
    loudness_error: float | None
    peak_compliant: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "publishable": self.publishable,
            "review_required": self.review_required,
            "loudness_class": self.loudness_class,
            "loudness_error": self.loudness_error,
            "peak_compliant": self.peak_compliant,
            "reason": self.reason,
        }


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def evaluate_output(
    measurements: dict[str, Any],
    *,
    target_lufs: float,
    ceiling: float,
    approved_deviation: bool = False,
    budget: ProcessingBudget = DEFAULT_BUDGET,
) -> OutputAcceptance:
    """Classify actual output measurements without authorizing deviations.

    approved_deviation must come from an explicit, external approval.
    This function cannot grant approval itself.

    LRA intervention is deliberately not assessed here because that
    requires a source baseline and an approved treatment contract.
    """

    if not _finite_number(target_lufs):
        raise ValueError("Target LUFS must be finite")

    if not _finite_number(ceiling):
        raise ValueError("True-peak ceiling must be finite")

    if not isinstance(approved_deviation, bool):
        raise TypeError("approved_deviation must be a boolean")

    if not isinstance(measurements, dict):
        return OutputAcceptance(
            outcome="rejected",
            publishable=False,
            review_required=True,
            loudness_class=None,
            loudness_error=None,
            peak_compliant=False,
            reason="Measurements are not a dictionary.",
        )

    required = ("lufs", "true_peak", "lra")

    invalid = [
        name
        for name in required
        if not _finite_number(measurements.get(name))
    ]

    if invalid:
        return OutputAcceptance(
            outcome="rejected",
            publishable=False,
            review_required=True,
            loudness_class=None,
            loudness_error=None,
            peak_compliant=False,
            reason=(
                "Missing or nonfinite measurements: "
                + ", ".join(invalid)
            ),
        )

    actual_lufs = float(measurements["lufs"])
    actual_peak = float(measurements["true_peak"])

    error = abs(actual_lufs - target_lufs)

    peak_compliant = (
        actual_peak <= ceiling + budget.ceiling_tolerance_dbtp
    )

    if not peak_compliant:
        return OutputAcceptance(
            outcome="rejected",
            publishable=False,
            review_required=True,
            loudness_class=None,
            loudness_error=error,
            peak_compliant=False,
            reason="Measured true peak exceeds the permitted ceiling.",
        )

    if error <= budget.preferred_loudness_error:
        loudness_class = "preferred"
    elif error <= budget.acceptable_loudness_error:
        loudness_class = "acceptable"
    elif error <= budget.exception_loudness_error:
        loudness_class = "exception"
    else:
        loudness_class = "outside_budget"

    if loudness_class in ("preferred", "acceptable"):
        return OutputAcceptance(
            outcome="accepted",
            publishable=True,
            review_required=False,
            loudness_class=loudness_class,
            loudness_error=error,
            peak_compliant=True,
            reason="Output meets normal loudness and true-peak budgets.",
        )

    if (
        loudness_class == "exception"
        and approved_deviation
        and actual_lufs < target_lufs
    ):
        return OutputAcceptance(
            outcome="accepted_with_deviation",
            publishable=True,
            review_required=False,
            loudness_class=loudness_class,
            loudness_error=error,
            peak_compliant=True,
            reason="Output deviation was explicitly approved.",
        )

    return OutputAcceptance(
        outcome="review_required",
        publishable=False,
        review_required=True,
        loudness_class=loudness_class,
        loudness_error=error,
        peak_compliant=True,
        reason=(
            "Output loudness requires review or a revised treatment."
        ),
    )
