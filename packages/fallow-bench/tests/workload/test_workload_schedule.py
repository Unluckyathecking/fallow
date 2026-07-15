"""Arrival-schedule determinism and bounds (module B1)."""

from __future__ import annotations

import pytest

from fallow_bench.workload.schedule import build_schedule


def _schedule(seed: int, duration_s: float = 60.0):
    return build_schedule(
        seed=seed,
        rate_per_min=120.0,
        duration_s=duration_s,
        n_prompts=10,
        max_tokens=64,
    )


def test_same_seed_is_identical() -> None:
    assert _schedule(7) == _schedule(7)


def test_different_seed_differs() -> None:
    assert _schedule(1) != _schedule(2)


def test_offsets_strictly_increasing_and_within_duration() -> None:
    schedule = _schedule(3, duration_s=30.0)
    offsets = [a.t_offset_s for a in schedule]
    assert offsets == sorted(offsets)
    assert all(0.0 < o < 30.0 for o in offsets)


def test_indices_contiguous_and_prompt_in_range() -> None:
    schedule = _schedule(11)
    assert [a.idx for a in schedule] == list(range(len(schedule)))
    assert all(0 <= a.prompt_idx < 10 for a in schedule)
    assert all(a.max_tokens == 64 for a in schedule)


def test_rejects_nonpositive_rate() -> None:
    with pytest.raises(ValueError, match="rate_per_min"):
        build_schedule(seed=1, rate_per_min=0.0, duration_s=10.0, n_prompts=5, max_tokens=8)


def test_rejects_zero_prompts() -> None:
    with pytest.raises(ValueError, match="n_prompts"):
        build_schedule(seed=1, rate_per_min=10.0, duration_s=10.0, n_prompts=0, max_tokens=8)
