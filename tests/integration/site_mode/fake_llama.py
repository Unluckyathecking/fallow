#!/usr/bin/env python3
"""A controlled, loopback-only fake llama-server for the Site Mode acceptance harness.

It stands in for the real ``llama-server`` binary the Go supervisor spawns: it
accepts the same argv (``-m MODEL --port P --host H --parallel N -c N`` plus any
manifest default args), binds the requested address, and answers the health probe
and the two OpenAI routes the relay carries. Behaviour is deterministic and can be
steered per request so the harness can prove buffered JSON, raw SSE, and the
mid-stream boundary without any real model.

The server refuses to bind anything but a loopback host, mirroring the daemon's
own fail-closed rule: a Site Mode replica is never exposed on the LAN.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-m", "--model", dest="model")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("-c", "--ctx-size", dest="ctx", type=int, default=0)
    # Absorb any manifest default args (e.g. -ngl / --flash-attn) without failing.
    args, _unknown = parser.parse_known_args(argv)
    return args


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a: object) -> None:  # keep the harness output clean
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def do_POST(self) -> None:
        raw = self._read_body()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}

        if self.path == "/v1/embeddings":
            self._buffered_json(
                {
                    "object": "list",
                    "data": [{"embedding": [0.5, 0.25], "index": 0}],
                    "echo": payload.get("input"),
                }
            )
            return
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return

        mode = str(payload.get("_fake_mode", ""))
        if payload.get("stream"):
            self._sse(payload, mode)
        else:
            self._buffered_json(
                {
                    "id": "fake-chat",
                    "object": "chat.completion",
                    "choices": [
                        {"message": {"role": "assistant", "content": payload.get("_echo", "hello")}}
                    ],
                }
            )

    def _buffered_json(self, obj: dict) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, payload: dict, mode: str) -> None:
        echo = str(payload.get("_echo", "Hello"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if mode == "truncate":
            # Emit one complete, deliberately large first event so the relaying
            # agent flushes it across the relay (a small chunk can linger in the
            # client's request buffer), then drop the connection with no
            # terminator: the first byte is already out, so the client must see a
            # clean truncation and never a retry.
            padding = "x" * 16000
            first = f'data: {{"choices":[{{"delta":{{"content":"{padding}"}}}}]}}\n\n'.encode()
            self._chunk(first)
            # Hold the connection briefly so the relaying agent forwards this first
            # event across the relay and the client receives it before the drop;
            # then return, closing the socket with no terminating 0-chunk.
            time.sleep(0.5)
            return
        events = [
            f'data: {{"choices":[{{"delta":{{"content":"{echo[:2]}"}}}}]}}\n\n'.encode(),
            f'data: {{"choices":[{{"delta":{{"content":"{echo[2:]}"}}}}]}}\n\n'.encode(),
            b"data: [DONE]\n\n",
        ]
        for ev in events:
            self._chunk(ev)
            if mode == "slow":
                time.sleep(0.02)
        self._chunk(b"")  # terminating zero-length chunk
        with contextlib.suppress(OSError):
            self.wfile.flush()

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if not _is_loopback(args.host):
        sys.stderr.write(f"fake llama refuses non-loopback host {args.host!r}\n")
        return 2
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
