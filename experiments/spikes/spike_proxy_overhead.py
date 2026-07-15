#!/usr/bin/env python3
"""Spike S1.4 — gateway proxy TTFT overhead (no llama-server needed).

Question: ADR-000 decision #2 makes the coordinator proxy every interactive
request to a replica port. How much time-to-first-token (TTFT) does a naive
httpx streaming passthrough add versus talking to the replica directly? This
de-risks the gateway design before any real inference exists.

Setup (all localhost, stdlib http.server + httpx):
  * origin  - a threaded HTTP server streaming SSE chunks with known timing
              (first chunk after --first-delay-ms, then --inter-delay-ms each).
  * proxy   - a threaded HTTP server whose handler opens an httpx stream to the
              origin and forwards raw bytes as they arrive (the naive gateway).
  * client  - httpx, measures TTFT direct vs through the proxy, paired per rep
              (direct then proxy) so origin-side jitter cancels.

Reports added TTFT (proxy - direct) p50/p95 in ms. Prints ONE JSON summary line.
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

DEFAULT_REPS = 100
DEFAULT_CHUNKS = 8
DEFAULT_FIRST_DELAY_MS = 40
DEFAULT_INTER_DELAY_MS = 10
DEFAULT_TARGET_ADDED_P95_MS = 10.0
LOCALHOST = "127.0.0.1"
REQ_TIMEOUT_S = 30.0
SSE_HEADER = ("Content-Type", "text/event-stream")


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


class OriginHandler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:  # silence stderr access log
        pass

    def do_GET(self) -> None:
        cfg = self.server.cfg  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header(*SSE_HEADER)
        self.end_headers()
        for i in range(cfg["chunks"]):
            delay = cfg["first_delay_ms"] if i == 0 else cfg["inter_delay_ms"]
            time.sleep(delay / 1000.0)
            self.wfile.write(f"data: chunk{i}\n\n".encode())
            self.wfile.flush()


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        pass

    def do_GET(self) -> None:
        origin_url = self.server.origin_url  # type: ignore[attr-defined]
        with (
            httpx.Client(timeout=REQ_TIMEOUT_S) as client,
            client.stream("GET", origin_url) as upstream,
        ):
            self.send_response(200)
            self.send_header(*SSE_HEADER)
            self.end_headers()
            for raw in upstream.iter_raw():
                if raw:
                    self.wfile.write(raw)
                    self.wfile.flush()


def start_server(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer((LOCALHOST, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def measure_ttft(client: httpx.Client, url: str) -> float:
    start = time.perf_counter()
    with client.stream("GET", url) as resp:
        for raw in resp.iter_raw():
            if raw:
                return (time.perf_counter() - start) * 1000.0
    return (time.perf_counter() - start) * 1000.0


def run_reps(
    direct_url: str, proxy_url: str, reps: int
) -> tuple[list[float], list[float], list[float]]:
    direct: list[float] = []
    proxied: list[float] = []
    added: list[float] = []
    with httpx.Client(timeout=REQ_TIMEOUT_S) as client:
        for _ in range(reps):
            d = measure_ttft(client, direct_url)
            p = measure_ttft(client, proxy_url)
            direct.append(d)
            proxied.append(p)
            added.append(p - d)
    return direct, proxied, added


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--reps", type=int, default=DEFAULT_REPS, help="paired measurements")
    p.add_argument("--chunks", type=int, default=DEFAULT_CHUNKS, help="SSE chunks per response")
    p.add_argument(
        "--first-delay-ms", type=int, default=DEFAULT_FIRST_DELAY_MS, help="delay to first chunk"
    )
    p.add_argument(
        "--inter-delay-ms", type=int, default=DEFAULT_INTER_DELAY_MS, help="delay between chunks"
    )
    p.add_argument(
        "--target-added-p95-ms",
        type=float,
        default=DEFAULT_TARGET_ADDED_P95_MS,
        help="soft budget for added TTFT p95",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    origin, origin_port = start_server(OriginHandler)
    origin.cfg = {  # type: ignore[attr-defined]
        "chunks": args.chunks,
        "first_delay_ms": args.first_delay_ms,
        "inter_delay_ms": args.inter_delay_ms,
    }
    direct_url = f"http://{LOCALHOST}:{origin_port}/"
    proxy, proxy_port = start_server(ProxyHandler)
    proxy.origin_url = direct_url  # type: ignore[attr-defined]
    proxy_url = f"http://{LOCALHOST}:{proxy_port}/"
    try:
        direct, proxied, added = run_reps(direct_url, proxy_url, args.reps)
    finally:
        origin.shutdown()
        proxy.shutdown()
    added_p95 = percentile(added, 95)
    summary = {
        "spike": "proxy_overhead",
        "reps": args.reps,
        "direct_ms_p50": round(percentile(direct, 50), 3),
        "proxy_ms_p50": round(percentile(proxied, 50), 3),
        "added_ms_p50": round(percentile(added, 50), 3),
        "added_ms_p95": round(added_p95, 3),
        "target_added_p95_ms": args.target_added_p95_ms,
        "verdict": "PASS" if added_p95 < args.target_added_p95_ms else "REVIEW",
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
