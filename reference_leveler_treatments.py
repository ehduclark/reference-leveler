#!/usr/bin/env python3
"""Canonical executable treatment specifications and identities."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


class TreatmentSpecError(ValueError):
    """Invalid or incompatible executable treatment specification."""


TREATMENT_FIELDS = {
    "gain_only": frozenset({"name", "gain_db"}),
    "gain_plus_limiter": frozenset({
        "name", "gain_db", "limiter_ceiling_dbtp",
    }),
    "dynamic_loudnorm": frozenset({
        "name", "target_lufs", "ceiling_dbtp",
    }),
}


def _number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise TreatmentSpecError(
            f"{label} must be a finite number"
        )
    return float(value)


def validate_treatment(
    spec: Any,
    *,
    target_lufs: float,
    ceiling_dbtp: float,
) -> dict:
    """Return a normalized, strictly validated executable treatment."""

    target = _number(target_lufs, "target_lufs")
    ceiling = _number(ceiling_dbtp, "ceiling_dbtp")

    if not isinstance(spec, dict):
        raise TreatmentSpecError("Treatment must be an object")

    name = spec.get("name")

    if not isinstance(name, str) or name not in TREATMENT_FIELDS:
        raise TreatmentSpecError(
            f"Unsupported treatment: {name!r}"
        )

    expected = TREATMENT_FIELDS[name]

    if set(spec) != expected:
        raise TreatmentSpecError(
            f"Treatment fields mismatch for {name}"
        )

    result = {"name": name}

    for key in sorted(expected - {"name"}):
        result[key] = _number(spec[key], key)

    if name == "dynamic_loudnorm":
        if result["target_lufs"] != target:
            raise TreatmentSpecError(
                "Treatment target differs from project target"
            )
        if result["ceiling_dbtp"] != ceiling:
            raise TreatmentSpecError(
                "Treatment ceiling differs from project ceiling"
            )

    if name == "gain_plus_limiter":
        if result["limiter_ceiling_dbtp"] > ceiling:
            raise TreatmentSpecError(
                "Limiter setting exceeds project ceiling"
            )

    return result


def treatment_fingerprint(
    spec: Any,
    *,
    target_lufs: float,
    ceiling_dbtp: float,
) -> str:
    """SHA-256 of a validated canonical executable treatment."""

    canonical = validate_treatment(
        spec,
        target_lufs=target_lufs,
        ceiling_dbtp=ceiling_dbtp,
    )

    payload = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    return hashlib.sha256(payload).hexdigest()
