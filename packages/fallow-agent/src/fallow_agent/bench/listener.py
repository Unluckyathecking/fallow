"""``BenchListener``: a tiny stdlib HTTP control surface for the churn injector.

The agent layer may not import ``fastapi``/``aiosqlite`` (import-linter), so this
is a hand-rolled ``asyncio.start_server`` listener with just enough HTTP/1.1 to
serve two routes for module B2:

    POST /simulate_input  -> 204   (calls ``BenchIdleDetector.simulate_input``)
    GET  /state           -> 200   {"state": "idle|active|draining", "idle_s": float}

Anything else is ``404``; an unparseable request line is ``400``. There is **no
auth** — this is a benchmark-only surface, enabled solely when the operator sets
``[bench] enabled = true`` and bound to the agent's ``bind_host`` (never
``0.0.0.0``; the settings guard still applies), so it is reachable only on
loopback or the tailnet.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Protocol

from fallow_agent.bench.constants import (
    CONTENT_TYPE_JSON,
    CRLF,
    ENCODING,
    HEADER_CONTENT_LENGTH,
    HEADER_SEPARATOR,
    HTTP_VERSION,
    JSON_ENCODING,
    KEY_IDLE_S,
    KEY_STATE,
    MAX_HEAD_BYTES,
    METHOD_GET,
    METHOD_POST,
    REQUEST_LINE_PARTS,
    ROUTE_SIMULATE_INPUT,
    ROUTE_STATE,
    STATUS_BAD_REQUEST,
    STATUS_NO_CONTENT,
    STATUS_NOT_FOUND,
    STATUS_OK,
)
from fallow_protocol.messages import AgentState


class SimulatableDetector(Protocol):
    """The idle-injection side of the bench surface."""

    def simulate_input(self) -> None: ...

    def seconds_since_input(self) -> float: ...


class StateSource(Protocol):
    """Anything exposing the agent's current preemption state (the Preemptor)."""

    @property
    def state(self) -> AgentState: ...


class BenchListener:
    """Serves the two B2 bench routes over a stdlib asyncio server."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        detector: SimulatableDetector,
        state_source: StateSource,
    ) -> None:
        self._host = host
        self._port = port
        self._detector = detector
        self._state_source = state_source
        self._server: asyncio.AbstractServer | None = None
        self._bind_task: asyncio.Task[int] | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def bind(self) -> int:
        """Bind and begin accepting; returns the actual bound port.

        Used directly in tests (with ``port=0`` for an ephemeral port). The
        wiring path uses :meth:`start` instead.
        """
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        sock = self._server.sockets[0]
        return int(sock.getsockname()[1])

    def start(self) -> None:
        """Schedule the bind on the running loop (composition-root wiring)."""
        self._bind_task = asyncio.ensure_future(self.bind())

    async def stop(self) -> None:
        """Stop accepting and close the listening socket. Idempotent."""
        if self._bind_task is not None:
            self._bind_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await self._bind_task
            self._bind_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ── Connection handling ──────────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            method, path = await self._read_request(reader)
            await self._dispatch(method, path, writer)
        except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await self._respond(writer, STATUS_BAD_REQUEST)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str]:
        head = await reader.readuntil(HEADER_SEPARATOR)
        if len(head) > MAX_HEAD_BYTES:
            raise ValueError("request head too large")
        text = head.decode(ENCODING)
        lines = text.split(CRLF)
        method, path = self._parse_request_line(lines[0])
        await self._drain_body(reader, lines[1:])
        return method, path

    @staticmethod
    def _parse_request_line(line: str) -> tuple[str, str]:
        parts = line.split(" ")
        if len(parts) != REQUEST_LINE_PARTS:
            raise ValueError("malformed request line")
        method, target, _version = parts
        path = target.split("?", 1)[0]
        return method, path

    @staticmethod
    async def _drain_body(reader: asyncio.StreamReader, header_lines: list[str]) -> None:
        length = 0
        for line in header_lines:
            name, sep, value = line.partition(":")
            if sep and name.strip().lower() == HEADER_CONTENT_LENGTH:
                length = int(value.strip())
                break
        if length > 0:
            await reader.readexactly(length)

    # ── Routing ──────────────────────────────────────────────────────────────

    async def _dispatch(self, method: str, path: str, writer: asyncio.StreamWriter) -> None:
        if method == METHOD_POST and path == ROUTE_SIMULATE_INPUT:
            self._detector.simulate_input()
            await self._respond(writer, STATUS_NO_CONTENT)
        elif method == METHOD_GET and path == ROUTE_STATE:
            await self._respond(writer, STATUS_OK, self._state_body())
        else:
            await self._respond(writer, STATUS_NOT_FOUND)

    def _state_body(self) -> bytes:
        payload = {
            KEY_STATE: self._state_source.state.value,
            KEY_IDLE_S: self._detector.seconds_since_input(),
        }
        return json.dumps(payload).encode(JSON_ENCODING)

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: tuple[int, str],
        body: bytes = b"",
    ) -> None:
        code, reason = status
        head = f"{HTTP_VERSION} {code} {reason}{CRLF}"
        head += f"Content-Length: {len(body)}{CRLF}"
        if body:
            head += f"Content-Type: {CONTENT_TYPE_JSON}{CRLF}"
        head += f"Connection: close{CRLF}{CRLF}"
        writer.write(head.encode(ENCODING) + body)
        await writer.drain()
