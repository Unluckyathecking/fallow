"""Precomputed open-loop arrival schedule.

The full list of arrivals is drawn from ``random.Random(seed)`` **before** the
run starts, so identical seeds produce byte-identical schedules across arms
(see ADR 019 §1). Inter-arrival gaps are exponential (a Poisson process at
``rate_per_min``); each arrival picks a prompt index uniformly at random.
"""

from __future__ import annotations

import random

from fallow_protocol import FallowModel

_SECONDS_PER_MINUTE = 60.0


class Arrival(FallowModel):
    """One scheduled interactive request, fixed before the run."""

    idx: int
    t_offset_s: float  # seconds after run start
    prompt_idx: int
    max_tokens: int


def build_schedule(
    *,
    seed: int,
    rate_per_min: float,
    duration_s: float,
    n_prompts: int,
    max_tokens: int,
) -> tuple[Arrival, ...]:
    """Return the fixed arrival schedule for one run.

    Draws exponential inter-arrival gaps until the cumulative offset would pass
    ``duration_s``. Deterministic given ``seed``.
    """
    if rate_per_min <= 0:
        raise ValueError("rate_per_min must be > 0")
    if n_prompts <= 0:
        raise ValueError("n_prompts must be > 0")
    rng = random.Random(seed)
    rate_per_s = rate_per_min / _SECONDS_PER_MINUTE
    arrivals: list[Arrival] = []
    offset = 0.0
    while True:
        offset += rng.expovariate(rate_per_s)
        if offset >= duration_s:
            break
        arrivals.append(
            Arrival(
                idx=len(arrivals),
                t_offset_s=offset,
                prompt_idx=rng.randrange(n_prompts),
                max_tokens=max_tokens,
            )
        )
    return tuple(arrivals)
