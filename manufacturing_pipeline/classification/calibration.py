"""Score → probability calibration."""

from __future__ import annotations

import math


def softmax(scores: dict[str, float], T: float = 1.0) -> dict[str, float]:
    m = max(scores.values())
    exps = {k: math.exp((v - m) / T) for k, v in scores.items()}
    Z = sum(exps.values())
    return {k: v / Z for k, v in exps.items()}
