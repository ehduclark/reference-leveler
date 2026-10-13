"""Explainable per-track processing recommendations."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path


STRATEGY_POLICY_VERSION = 1

# Screening thresholds. These are not calibrated audio-quality guarantees.
POLICIES = {
    "transparent": {
        "minor_peak_db": 0.5,
        "moderate_peak_db": 1.5,
        "maximum_peak_db": 3.0,
    },
    "balanced": {
        "minor_peak_db": 1.0,
        "moderate_peak_db": 3.0,
        "maximum_peak_db": 6.0,
    },
    "consistent": {
        "minor_peak_db": 1.5,
        "moderate_peak_db": 4.0,
        "maximum_peak_db": 8.0,
    },
}


def fingerprint(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def choose_strategy(track, profile):
    policy = POLICIES[profile]
    excess = track["peak_excess_db"]

    if excess <= 0:
        strategy = "gain_only"
        reason = "Gain adjustment fits below the true-peak ceiling."

    elif excess <= policy["minor_peak_db"]:
        strategy = "gain_plus_limiter"
        reason = "Small projected peak excess; limiter trial recommended."

    elif excess <= policy["moderate_peak_db"]:
        strategy = "gentle_compression_trial"
        reason = (
            "Moderate projected peak excess; compare compression "
            "against limiting before selecting an output."
        )

    elif excess <= policy["maximum_peak_db"]:
        strategy = "conservative_exception"
        reason = (
            "Substantial projected peak excess; permit a quieter "
            "result rather than force aggressive processing."
        )

    else:
        strategy = "review_required"
        reason = (
            "Projected peak excess exceeds the approved processing "
            "budget; preserve source and flag for review."
        )

    return {
        "strategy": strategy,
        "reason": reason,
        "policy_version": STRATEGY_POLICY_VERSION,
    }


def policy_fingerprint():
    return fingerprint({
        "version": STRATEGY_POLICY_VERSION,
        "policies": POLICIES,
        "implementation": inspect.getsource(choose_strategy),
    })
