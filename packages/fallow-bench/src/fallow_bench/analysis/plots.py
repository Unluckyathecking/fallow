"""Matplotlib (Agg, headless) figures for the report.

Three PNGs: TTFT and time-to-yield CDFs (one line per arm) and a requests/min
timeline with churn-kill markers. PNGs are intentionally exempt from the
byte-identical guarantee; they are visual aids, not data of record.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import numpy as np
from matplotlib import pyplot as plt

from fallow_bench.analysis.latency import _ttft_samples
from fallow_bench.analysis.models import RunFrames
from fallow_bench.analysis.recovery import KILL_ACTION
from fallow_bench.analysis.yield_metrics import _yield_samples

SECONDS_PER_MINUTE = 60.0


def _plot_cdf(samples: list[float], label: str) -> None:
    if not samples:
        return
    ordered = np.sort(np.asarray(samples, dtype=float))
    ys = np.arange(1, len(ordered) + 1, dtype=float) / len(ordered)
    plt.step(ordered, ys, where="post", label=label)


def _finish(xlabel: str, ylabel: str, title: str, out_path: Path) -> None:
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.title(title)
    if plt.gca().get_legend_handles_labels()[0]:  # only if something was drawn
        plt.legend()
    plt.savefig(out_path)
    plt.close()


def write_ttft_cdf(runs: Mapping[str, RunFrames], out_path: Path) -> None:
    plt.figure()
    for arm in sorted(runs):
        _plot_cdf(_ttft_samples(runs[arm].client), arm)
    _finish("TTFT (s)", "CDF", "Time-to-first-token", out_path)


def write_yield_cdf(runs: Mapping[str, RunFrames], out_path: Path) -> None:
    plt.figure()
    for arm in sorted(runs):
        _plot_cdf(_yield_samples(runs[arm].events, runs[arm].churn), arm)
    _finish("Time-to-yield (ms)", "CDF", "Time-to-yield", out_path)


def _requests_per_minute(client: RunFrames) -> tuple[np.ndarray, np.ndarray, float] | None:
    times = sorted(float(t) for t in client.client["t_submit"].tolist() if t is not None)
    if len(times) < 2:
        return None
    t0 = times[0]
    minutes = np.asarray([(t - t0) / SECONDS_PER_MINUTE for t in times], dtype=float)
    span = max(minutes[-1], 1.0)
    edges = np.arange(0.0, span + 1.0, 1.0)
    counts, _ = np.histogram(minutes, bins=edges)
    return edges[:-1], counts.astype(float), t0


def _churn_marker_minutes(run: RunFrames, t0: float) -> list[float]:
    marks: list[float] = []
    for row in run.churn.to_dict("records"):
        if row.get("action") == KILL_ACTION and row.get("t") is not None:
            marks.append((float(row["t"]) - t0) / SECONDS_PER_MINUTE)
    return marks


def write_throughput_timeline(runs: Mapping[str, RunFrames], out_path: Path) -> None:
    plt.figure()
    for arm in sorted(runs):
        series = _requests_per_minute(runs[arm])
        if series is None:
            continue
        xs, ys, t0 = series
        plt.plot(xs, ys, label=arm)
        for mark in _churn_marker_minutes(runs[arm], t0):
            plt.axvline(mark, color="grey", linestyle="--", alpha=0.4)
    _finish(
        "Minutes since first request",
        "Requests / min",
        "Interactive load (dashed: churn kills)",
        out_path,
    )
