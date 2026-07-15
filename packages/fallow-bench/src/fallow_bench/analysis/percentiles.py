"""The single percentile definition used across every latency-like metric.

Using one helper (numpy linear interpolation) everywhere keeps the headline
table internally consistent and hand-checkable: TTFT, decode, yield and recovery
all agree on what ``p50`` means.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def percentile(samples: Sequence[float], q: float) -> float | None:
    """Return the ``q``-th percentile (0-100) of ``samples`` via linear
    interpolation, or ``None`` when there are no samples."""
    if len(samples) == 0:
        return None
    arr = np.asarray(samples, dtype=float)
    return float(np.percentile(arr, q, method="linear"))
