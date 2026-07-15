#!/usr/bin/env python3
"""Spike S1.1 — suspend latency: THE critical number for ADR-000 decision #3.

Question: when the user touches the machine, how long until every
fallow-owned child process is psutil-suspended? ADR-000 promises p99 < 300ms.

Model (honest to the real hot path):
  * N child "replica" processes each spin a CPU core (busy, like llama.cpp).
  * A poll thread wakes every ``--poll-ms`` (default 100, matching
    AgentConfig.poll_interval_ms). Real input can land at any phase within a
    poll window, so we fire a synthetic trigger at random gaps and let the poll
    thread discover it, exactly as IdleDetector.seconds_since_input would.
  * On discovery the poll thread suspends all children and verifies each is
    STATUS_STOPPED (status checked), then records timings.

Reported per trial (over M trials, default 100):
  * total_ms   = trigger -> all children verified suspended (user-facing).
  * detect_ms  = trigger -> poll thread noticed it (poll-phase latency).
  * suspend_ms = notice -> all verified suspended (raw psutil.suspend cost;
                 expected < 15ms).

Prints ONE JSON summary line at the end so results are machine-collectable.

Windows note: psutil.suspend() calls SuspendThread synchronously but status()
cannot report a "stopped" state, so verification trusts the synchronous call on
Windows and checks STATUS_STOPPED elsewhere (macOS/Linux). Documented, not hidden.
"""

import argparse
import contextlib
import json
import platform
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import psutil

SPIN_SRC = "while True:\n    pass\n"
DEFAULT_CHILDREN = 4
DEFAULT_TRIALS = 100
DEFAULT_POLL_MS = 100
DEFAULT_MIN_GAP_S = 0.15
DEFAULT_MAX_GAP_S = 0.60
VERIFY_TIMEOUT_S = 0.5
VERIFY_SLEEP_S = 0.0005
TARGET_P99_MS = 300.0
MS_PER_S = 1000.0
_IS_WINDOWS = sys.platform.startswith("win")


@dataclass(frozen=True)
class Trial:
    total_ms: float
    detect_ms: float
    suspend_ms: float
    verified: bool


def _is_suspended(proc: psutil.Process) -> bool:
    """True if the process is confirmed yielded (or gone)."""
    if _IS_WINDOWS:
        return True  # SuspendThread is synchronous; status() cannot confirm here
    try:
        return proc.status() == psutil.STATUS_STOPPED
    except psutil.Error:
        return True  # vanished == not competing for the machine


def _suspend_all(procs: list[psutil.Process]) -> None:
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.suspend()


def _resume_all(procs: list[psutil.Process]) -> None:
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.resume()


class PollWorker(threading.Thread):
    """Wakes every poll interval; on an armed trigger, suspends + verifies."""

    def __init__(self, procs: list[psutil.Process], poll_s: float, verify_s: float) -> None:
        super().__init__(daemon=True)
        self._procs = procs
        self._poll_s = poll_s
        self._verify_s = verify_s
        self._armed = threading.Event()
        self._done = threading.Event()
        self._stop = threading.Event()
        self._trigger_t = 0.0
        self.result: Trial | None = None

    def arm(self, trigger_t: float) -> None:
        self.result = None
        self._trigger_t = trigger_t
        self._done.clear()
        self._armed.set()

    def wait_done(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def shutdown(self) -> None:
        self._stop.set()

    def _verify(self) -> bool:
        deadline = time.perf_counter() + self._verify_s
        pending = list(self._procs)
        while pending and time.perf_counter() < deadline:
            pending = [p for p in pending if not _is_suspended(p)]
            if pending:
                time.sleep(VERIFY_SLEEP_S)
        return not pending

    def run(self) -> None:
        while not self._stop.is_set():
            time.sleep(self._poll_s)
            if not self._armed.is_set():
                continue
            detect_t = time.perf_counter()
            _suspend_all(self._procs)
            verified = self._verify()
            done_t = time.perf_counter()
            self.result = Trial(
                total_ms=(done_t - self._trigger_t) * MS_PER_S,
                detect_ms=(detect_t - self._trigger_t) * MS_PER_S,
                suspend_ms=(done_t - detect_t) * MS_PER_S,
                verified=verified,
            )
            self._armed.clear()
            self._done.set()


def spawn_spinner() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", SPIN_SRC],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def run_trials(
    worker: PollWorker, procs: list[psutil.Process], args: argparse.Namespace
) -> list[Trial]:
    rng = random.Random(args.seed)
    trials: list[Trial] = []
    per_trial_budget = VERIFY_TIMEOUT_S + args.poll_ms / MS_PER_S + 1.0
    for _ in range(args.trials):
        time.sleep(rng.uniform(args.min_gap_s, args.max_gap_s))
        worker.arm(time.perf_counter())
        if not worker.wait_done(per_trial_budget) or worker.result is None:
            continue
        trials.append(worker.result)
        _resume_all(procs)
    return trials


def build_summary(trials: list[Trial], args: argparse.Namespace) -> dict[str, object]:
    totals = [t.total_ms for t in trials]
    suspends = [t.suspend_ms for t in trials]
    detects = [t.detect_ms for t in trials]
    p99_total = percentile(totals, 99)
    return {
        "spike": "suspend_latency",
        "platform": platform.system(),
        "children": args.children,
        "load": args.load,
        "poll_ms": args.poll_ms,
        "trials_requested": args.trials,
        "trials_ok": len(trials),
        "verified_all": all(t.verified for t in trials) if trials else False,
        "total_ms_p50": round(percentile(totals, 50), 3),
        "total_ms_p95": round(percentile(totals, 95), 3),
        "total_ms_p99": round(p99_total, 3),
        "suspend_ms_p50": round(percentile(suspends, 50), 3),
        "suspend_ms_p95": round(percentile(suspends, 95), 3),
        "suspend_ms_p99": round(percentile(suspends, 99), 3),
        "detect_ms_p50": round(percentile(detects, 50), 3),
        "detect_ms_p99": round(percentile(detects, 99), 3),
        "target_p99_ms": TARGET_P99_MS,
        "verdict": "PASS" if trials and p99_total < TARGET_P99_MS else "FAIL",
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--children", type=int, default=DEFAULT_CHILDREN, help="busy child spinners")
    p.add_argument("--trials", type=int, default=DEFAULT_TRIALS, help="number of input triggers")
    p.add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS, help="poll thread period (ms)")
    p.add_argument("--load", action="store_true", help="also saturate all CPUs with spinners")
    p.add_argument(
        "--min-gap-s", type=float, default=DEFAULT_MIN_GAP_S, help="min gap between triggers"
    )
    p.add_argument(
        "--max-gap-s", type=float, default=DEFAULT_MAX_GAP_S, help="max gap between triggers"
    )
    p.add_argument("--seed", type=int, default=None, help="RNG seed for trigger gaps")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    spinners = [spawn_spinner() for _ in range(args.children)]
    load: list[subprocess.Popen[bytes]] = []
    if args.load:
        load = [spawn_spinner() for _ in range(psutil.cpu_count() or 1)]
    procs = [psutil.Process(s.pid) for s in spinners]
    worker = PollWorker(procs, args.poll_ms / MS_PER_S, VERIFY_TIMEOUT_S)
    worker.start()
    try:
        trials = run_trials(worker, procs, args)
    finally:
        worker.shutdown()
        _resume_all(procs)
        for proc_handle in (*spinners, *load):
            with contextlib.suppress(Exception):
                proc_handle.kill()
    print(json.dumps(build_summary(trials, args)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
