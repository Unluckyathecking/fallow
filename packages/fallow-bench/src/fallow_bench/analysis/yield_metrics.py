"""Time-to-yield: how fast Fallow gets off the machine when the user returns.

The sample set is the union of two signals — ``user_returned`` event
``yield_ms`` (the agent's own measured preemption) and the B2 churn log's
optional ``flip_latency_ms`` (the injector-observed flip) — so both the
in-process and out-of-band views of a yield count toward the distribution.
"""

from __future__ import annotations

import pandas as pd  # type: ignore[import-untyped]

from fallow_bench.analysis.percentiles import percentile

USER_RETURNED = "user_returned"


def _yield_samples(events: pd.DataFrame, churn: pd.DataFrame) -> list[float]:
    samples: list[float] = []
    for row in events.to_dict("records"):
        if row.get("kind") != USER_RETURNED:
            continue
        yield_ms = row.get("yield_ms")
        if yield_ms is not None and float(yield_ms) >= 0:
            samples.append(float(yield_ms))
    for row in churn.to_dict("records"):
        flip = row.get("flip_latency_ms")
        if flip is not None and float(flip) >= 0:
            samples.append(float(flip))
    return samples


def yield_p50(events: pd.DataFrame, churn: pd.DataFrame) -> float | None:
    """Median time-to-yield in milliseconds."""
    return percentile(_yield_samples(events, churn), 50)


def yield_p99(events: pd.DataFrame, churn: pd.DataFrame) -> float | None:
    """99th-percentile time-to-yield in milliseconds."""
    return percentile(_yield_samples(events, churn), 99)
