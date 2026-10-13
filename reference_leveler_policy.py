"""Explicit, deterministic candidate selection policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProcessingBudget:
    ceiling_tolerance_dbtp: float = 0.05
    preferred_loudness_error: float = 0.5
    acceptable_loudness_error: float = 1.0
    exception_loudness_error: float = 2.0

    # Provisional intervention limits, not perceptual guarantees.
    maximum_normal_lra_change: float = 3.0
    maximum_fallback_lra_change: float = 5.0


DEFAULT_BUDGET = ProcessingBudget()


def candidate_class(candidate: dict[str, Any], budget=DEFAULT_BUDGET):
    """Classify a candidate independently of its existing status."""
    if candidate.get("status") == "error":
        return "error"

    error = candidate["loudness_error"]
    lra_change = candidate["lra_change"]

    if error <= budget.preferred_loudness_error:
        loudness_class = "preferred"
    elif error <= budget.acceptable_loudness_error:
        loudness_class = "acceptable"
    elif error <= budget.exception_loudness_error:
        loudness_class = "exception"
    else:
        loudness_class = "outside_budget"

    if lra_change > budget.maximum_fallback_lra_change:
        intervention_class = "excessive"
    elif lra_change > budget.maximum_normal_lra_change:
        intervention_class = "elevated"
    else:
        intervention_class = "normal"

    return {
        "loudness_class": loudness_class,
        "intervention_class": intervention_class,
    }


def select_candidate(
    candidates: list[dict[str, Any]],
    ceiling: float,
    budget: ProcessingBudget = DEFAULT_BUDGET,
) -> dict[str, Any]:
    """Return an explicit decision without modifying candidate data."""

    classified = []

    for candidate in candidates:
        classification = candidate_class(candidate, budget)

        if classification == "error":
            continue

        measured = candidate["measurements"]

        peak_ok = (
            measured["true_peak"]
            <= ceiling + budget.ceiling_tolerance_dbtp
        )

        if not peak_ok:
            continue

        classified.append((candidate, classification))

    normal = [
        (candidate, cls)
        for candidate, cls in classified
        if cls["loudness_class"]
            in ("preferred", "acceptable", "exception")
        and cls["intervention_class"] == "normal"
    ]

    if normal:
        winner, classification = min(
            normal,
            key=lambda item: item[0]["score"],
        )

        return {
            "selected_candidate": winner["name"],
            "outcome": "accepted",
            "review_required": (
                classification["loudness_class"] == "exception"
            ),
            "loudness_class": classification["loudness_class"],
            "intervention_class": classification["intervention_class"],
            "reason": (
                "Candidate satisfies true-peak and normal "
                "intervention budgets."
            ),
        }

    fallback = [
        (candidate, cls)
        for candidate, cls in classified
        if candidate["name"] != "dynamic_loudnorm"
        and cls["intervention_class"] != "excessive"
    ]

    if fallback:
        winner, classification = min(
            fallback,
            key=lambda item: item[0]["score"],
        )

        return {
            "selected_candidate": winner["name"],
            "outcome": "accepted_with_deviation",
            "review_required": True,
            "loudness_class": classification["loudness_class"],
            "intervention_class": classification["intervention_class"],
            "reason": (
                "No candidate met normal acceptance requirements; "
                "selected a peak-compliant fallback within the "
                "provisional intervention budget."
            ),
        }

    return {
        "selected_candidate": None,
        "outcome": "review_required",
        "review_required": True,
        "loudness_class": None,
        "intervention_class": None,
        "reason": (
            "No candidate satisfied true-peak and intervention budgets."
        ),
    }
