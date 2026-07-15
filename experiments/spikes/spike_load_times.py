#!/usr/bin/env python3
"""Spike S1.3 — llama-server time-to-ready across cache conditions.

Question: how long from spawning llama-server to /health returning 200? This
bounds how fast the coordinator can (re)spin a replica after a kill-for-VRAM
escalation (ADR-000 decision #3) or a churned machine rejoining. If cold starts
are tens of seconds, the scheduler must avoid kill/restart thrash.

Three conditions, --reps each (default 3), reporting seconds per launch:
  * cold        - first launch. A per-OS drop-caches HINT is printed (NOT
                  enforced; dropping page cache needs privileges). Only the
                  first cold rep is truly cold unless you drop caches between.
  * warm        - relaunched back-to-back so the model file is in page cache.
  * post_kill   - killed uncleanly (SIGKILL) then relaunched; checks restart
                  after an ungraceful exit (the escalation path).

Spawns/kills the given --binary itself. Prints ONE JSON summary line at the end.
"""

import argparse
import json
import platform
import subprocess
import time
from dataclasses import dataclass

import httpx

DEFAULT_REPS = 3
DEFAULT_READY_TIMEOUT_S = 120.0
DEFAULT_HOST = "127.0.0.1"
HEALTH_POLL_S = 0.05
HEALTH_REQ_TIMEOUT_S = 1.0
STOP_GRACE_S = 10.0
DROP_CACHE_HINTS = {
    "Linux": "sync && echo 3 | sudo tee /proc/sys/vm/drop_caches",
    "Darwin": "sudo purge",
    "Windows": "Use RAMMap (EmptyStandbyList) or Sysinternals to clear the standby list",
}


@dataclass(frozen=True)
class Launch:
    ready_s: float
    ok: bool


def build_argv(args: argparse.Namespace) -> list[str]:
    return [
        args.binary,
        "-m",
        args.model,
        "--host",
        args.host,
        "--port",
        str(args.port),
        *args.extra,
    ]


def wait_for_health(port: int, host: str, timeout_s: float) -> bool:
    url = f"http://{host}:{port}/health"
    deadline = time.perf_counter() + timeout_s
    with httpx.Client(timeout=HEALTH_REQ_TIMEOUT_S) as client:
        while time.perf_counter() < deadline:
            try:
                if client.get(url).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(HEALTH_POLL_S)
    return False


def stop(proc: subprocess.Popen[bytes], *, hard: bool) -> None:
    if proc.poll() is not None:
        return
    if hard:
        proc.kill()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=STOP_GRACE_S)


def launch_and_time(args: argparse.Namespace) -> tuple[Launch, subprocess.Popen[bytes]]:
    argv = build_argv(args)
    start = time.perf_counter()
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = wait_for_health(args.port, args.host, args.ready_timeout_s)
    return Launch(ready_s=time.perf_counter() - start, ok=ok), proc


def run_condition(args: argparse.Namespace, *, kill_between: bool) -> list[Launch]:
    launches: list[Launch] = []
    for _ in range(args.reps):
        launch, proc = launch_and_time(args)
        launches.append(launch)
        stop(proc, hard=kill_between)
    return launches


def summarise(name: str, launches: list[Launch]) -> dict[str, object]:
    secs = [round(x.ready_s, 3) for x in launches]
    return {
        "condition": name,
        "ready_s": secs,
        "mean_s": round(sum(secs) / len(secs), 3) if secs else None,
        "all_ready": all(x.ok for x in launches),
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", required=True, help="path to llama-server")
    p.add_argument("--model", required=True, help="path to the .gguf model")
    p.add_argument("--port", type=int, required=True, help="port to bind")
    p.add_argument("--host", default=DEFAULT_HOST, help="host to bind")
    p.add_argument("--reps", type=int, default=DEFAULT_REPS, help="reps per condition")
    p.add_argument(
        "--ready-timeout-s",
        type=float,
        default=DEFAULT_READY_TIMEOUT_S,
        help="max wait for /health",
    )
    p.add_argument(
        "--extra", nargs=argparse.REMAINDER, default=[], help="extra args passed to llama-server"
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    hint = DROP_CACHE_HINTS.get(platform.system(), "(no drop-caches hint for this OS)")
    print(f"# cold-cache hint (run manually, not enforced): {hint}")
    conditions = {
        "cold": run_condition(args, kill_between=False),
        "warm": run_condition(args, kill_between=False),
        "post_kill": run_condition(args, kill_between=True),
    }
    summary = {
        "spike": "load_times",
        "platform": platform.system(),
        "reps": args.reps,
        "conditions": [summarise(name, launches) for name, launches in conditions.items()],
        "verdict": "INFORMATIONAL",
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
