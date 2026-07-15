"""Client-trace latency metrics: time-to-first-token and decode throughput.

Both read the B1 per-request trace and count only ``status == "ok"`` rows with
the timestamps they need, so a partial or error-heavy run degrades to fewer
samples (and ``None`` when none survive) rather than to a wrong number.
"""

from __future__ import annotations

import pandas as pd  # type: ignore[import-untyped]

from fallow_bench.analysis.percentiles import percentile

OK_STATUS = "ok"


def _ttft_samples(client: pd.DataFrame) -> list[float]:
    samples: list[float] = []
    for row in client.to_dict("records"):
        if row.get("status") != OK_STATUS:
            continue
        submit, first = row.get("t_submit"), row.get("t_first_token")
        if submit is None or first is None:
            continue
        ttft = float(first) - float(submit)
        if ttft >= 0:
            samples.append(ttft)
    return samples


def ttft_p50(client: pd.DataFrame) -> float | None:
    """Median time-to-first-token in seconds."""
    return percentile(_ttft_samples(client), 50)


def ttft_p95(client: pd.DataFrame) -> float | None:
    """95th-percentile time-to-first-token in seconds."""
    return percentile(_ttft_samples(client), 95)


def _decode_samples(client: pd.DataFrame) -> list[float]:
    samples: list[float] = []
    for row in client.to_dict("records"):
        if row.get("status") != OK_STATUS:
            continue
        first, done, tokens = row.get("t_first_token"), row.get("t_done"), row.get("tokens_out")
        if first is None or done is None or tokens is None:
            continue
        span = float(done) - float(first)
        if span > 0 and float(tokens) > 0:
            samples.append(float(tokens) / span)
    return samples


def decode_tps_p50(client: pd.DataFrame) -> float | None:
    """Median per-request decode throughput in tokens/second."""
    return percentile(_decode_samples(client), 50)
