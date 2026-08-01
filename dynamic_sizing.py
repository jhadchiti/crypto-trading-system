"""
Dynamic position sizing based on regime states.
================================================

Pure function returning a multiplier on the base risk-per-trade. Hand-coded
from quant priors; no fitting to historical performance.

Rules:
  - High vol regime               -> 0.7x (vol expansion = position-sizing risk)
  - Concentrated correlation      -> 0.7x (effective single-factor exposure)
  - Heavy positive funding + LONG -> 0.8x (carry headwind)
  - Heavy negative funding + SHORT -> 0.8x (carry headwind)
  - Low vol AND diversified       -> 1.25x (favorable regime)

Multipliers compound multiplicatively, clamped to [0.4, 1.5].

Two preset configurations:
  - vol_only: respects vol regime only (simpler)
  - full:     all three regimes (vol + correlation + funding)
"""

from __future__ import annotations


DEFAULT_MIN = 0.4
DEFAULT_MAX = 1.5


def risk_multiplier(vol_state: str = "NORMAL",
                    corr_state: str = "MIXED",
                    funding_state: str = "NEUTRAL",
                    is_long: bool = True,
                    use_vol: bool = True,
                    use_corr: bool = True,
                    use_funding: bool = True,
                    ) -> float:
    """
    Return the multiplier in [DEFAULT_MIN, DEFAULT_MAX] for current regime.
    """
    mult = 1.0

    if use_vol:
        if vol_state == "HIGH":
            mult *= 0.7
        elif vol_state == "LOW":
            mult *= 1.15

    if use_corr:
        if corr_state == "CONCENTRATED":
            mult *= 0.7
        elif corr_state == "DIVERSIFIED":
            mult *= 1.1

    if use_funding:
        if funding_state == "POSITIVE_HEAVY" and is_long:
            mult *= 0.8
        elif funding_state == "NEGATIVE" and (not is_long):
            mult *= 0.8

    return max(DEFAULT_MIN, min(DEFAULT_MAX, mult))


def describe_multiplier(vol_state: str, corr_state: str, funding_state: str,
                        is_long: bool = True) -> dict:
    """Return both the multiplier and a human-readable reason string."""
    m = risk_multiplier(vol_state, corr_state, funding_state, is_long)
    reasons = []
    if vol_state == "HIGH":     reasons.append("HIGH vol -0.3x")
    elif vol_state == "LOW":    reasons.append("LOW vol +0.15x")
    if corr_state == "CONCENTRATED": reasons.append("CONCENTRATED corr -0.3x")
    elif corr_state == "DIVERSIFIED": reasons.append("DIVERSIFIED corr +0.1x")
    if funding_state == "POSITIVE_HEAVY" and is_long:
        reasons.append("crowded long funding -0.2x")
    if funding_state == "NEGATIVE" and not is_long:
        reasons.append("crowded short funding -0.2x")
    return {"multiplier": m, "reasons": reasons or ["neutral regime"]}
