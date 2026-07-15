#!/usr/bin/env python3
"""Spike S1.2 — CUDA suspend/resume durability (Windows / RTX box).

Question: does repeatedly suspending a live llama-server mid-generation and
resuming it leave the CUDA context healthy, or does it corrupt output / wedge
the process? ADR-000 decision #3 escalates suspended GPU replicas to *kill* only
after vram_evict_after_s; between suspend and kill the server must survive
suspend/resume cleanly. This spike measures how often that holds over --cycles.

Requires an ALREADY-RUNNING llama-server (llama.cpp native /completion API).
Pass its --server-url and --pid. Nothing is spawned here.

Each cycle:
  1. Stream a deterministic (temperature 0, fixed seed) completion.
  2. Mid-stream, suspend the server PID, sleep --suspend-ms, resume it.
  3. Finish reading the stream.
  4. Fire a FRESH deterministic completion and compare its first
     --compare-chars against a reference captured before the loop.
Any exception, non-200, or first-token mismatch is a failure (kind-tallied).

Prints ONE JSON summary line at the end. Optionally samples VRAM via nvidia-smi.
"""

import argparse
import json
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx
import psutil

DEFAULT_CYCLES = 500
DEFAULT_N_PREDICT = 32
DEFAULT_SUSPEND_MS = 50
DEFAULT_COMPARE_CHARS = 24
DEFAULT_PROMPT = "The capital of France is"
DEFAULT_TIMEOUT_S = 30.0
NVIDIA_SMI = "nvidia-smi"
NVIDIA_QUERY = "--query-gpu=memory.used,memory.total"
NVIDIA_FORMAT = "--format=csv,noheader,nounits"


@dataclass
class Tally:
    cycles_run: int = 0
    failures: int = 0
    corrupted: int = 0
    error_kinds: Counter[str] = field(default_factory=Counter)


def completion_payload(prompt: str, n_predict: int, *, stream: bool) -> dict[str, object]:
    return {
        "prompt": prompt,
        "temperature": 0,
        "seed": 0,
        "n_predict": n_predict,
        "stream": stream,
        "cache_prompt": False,
    }


def parse_sse_content(line: str) -> str | None:
    """Extract the 'content' field from one llama.cpp SSE 'data:' line."""
    if not line.startswith("data:"):
        return None
    body = line[len("data:") :].strip()
    if not body or body == "[DONE]":
        return None
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return None
    content = obj.get("content")
    return content if isinstance(content, str) else None


def reference_text(client: httpx.Client, url: str, args: argparse.Namespace) -> str:
    resp = client.post(url, json=completion_payload(args.prompt, args.n_predict, stream=False))
    resp.raise_for_status()
    content = resp.json().get("content", "")
    return content if isinstance(content, str) else ""


def stream_with_suspend(
    client: httpx.Client, url: str, proc: psutil.Process, args: argparse.Namespace
) -> str:
    """Stream a completion, suspending/resuming the server after first content."""
    collected: list[str] = []
    suspended = False
    payload = completion_payload(args.prompt, args.n_predict, stream=True)
    with client.stream("POST", url, json=payload) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            chunk = parse_sse_content(line)
            if chunk is None:
                continue
            collected.append(chunk)
            if not suspended:
                proc.suspend()
                time.sleep(args.suspend_ms / 1000.0)
                proc.resume()
                suspended = True
    return "".join(collected)


def sample_vram() -> tuple[int | None, int | None]:
    try:
        out = subprocess.run(
            [NVIDIA_SMI, NVIDIA_QUERY, NVIDIA_FORMAT],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    first = out.strip().splitlines()[0] if out.strip() else ""
    parts = [p.strip() for p in first.split(",")]
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None, None
    return int(parts[0]), int(parts[1])


def run_cycle(
    client: httpx.Client, url: str, proc: psutil.Process, args: argparse.Namespace, ref: str
) -> str | None:
    """Run one suspend/resume cycle. Returns an error-kind string, or None on success."""
    try:
        stream_with_suspend(client, url, proc, args)
        verify = reference_text(client, url, args)
    except (httpx.HTTPError, psutil.Error) as exc:
        return type(exc).__name__
    if verify[: args.compare_chars] != ref[: args.compare_chars]:
        return "OutputMismatch"
    return None


def run_all(
    client: httpx.Client, url: str, proc: psutil.Process, args: argparse.Namespace, ref: str
) -> Tally:
    tally = Tally()
    for _ in range(args.cycles):
        tally.cycles_run += 1
        kind = run_cycle(client, url, proc, args, ref)
        if kind is None:
            continue
        tally.failures += 1
        tally.error_kinds[kind] += 1
        if kind == "OutputMismatch":
            tally.corrupted += 1
    return tally


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--server-url", required=True, help="base URL of the running llama-server")
    p.add_argument("--pid", type=int, required=True, help="PID of the llama-server process")
    p.add_argument("--cycles", type=int, default=DEFAULT_CYCLES, help="suspend/resume cycles")
    p.add_argument("--n-predict", type=int, default=DEFAULT_N_PREDICT, help="tokens per completion")
    p.add_argument(
        "--suspend-ms", type=int, default=DEFAULT_SUSPEND_MS, help="mid-stream suspend hold"
    )
    p.add_argument(
        "--compare-chars", type=int, default=DEFAULT_COMPARE_CHARS, help="first-chars compared"
    )
    p.add_argument("--prompt", default=DEFAULT_PROMPT, help="deterministic prompt")
    p.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, help="per-request timeout")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    url = args.server_url.rstrip("/") + "/completion"
    proc = psutil.Process(args.pid)
    vram_used_before, vram_total = sample_vram()
    with httpx.Client(timeout=args.timeout_s) as client:
        ref = reference_text(client, url, args)
        tally = run_all(client, url, proc, args, ref)
    vram_used_after, _ = sample_vram()
    summary = {
        "spike": "cuda_suspend_cycles",
        "cycles_run": tally.cycles_run,
        "failures": tally.failures,
        "corrupted": tally.corrupted,
        "error_kinds": dict(tally.error_kinds),
        "reference_len": len(ref),
        "vram_used_before_mb": vram_used_before,
        "vram_used_after_mb": vram_used_after,
        "vram_total_mb": vram_total,
        "verdict": "PASS" if tally.failures == 0 else "FAIL",
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
