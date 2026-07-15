"""A tiny loopback HTTP/1.1 + SSE stub that role-plays a llama-server replica.

The coordinator's *own* httpx client makes a real loopback TCP call to whatever
``host:port`` a READY replica advertises, so the gateway E2E scenario needs a
real (not mocked) server on ``127.0.0.1``. This raw-``asyncio`` stub answers one
request with either a buffered JSON body or a streamed SSE body, and can close
mid-stream to model an upstream that dies. Ports are ephemeral (``port 0``);
``reserve_ordered_ports`` hands out a deterministic ``(dead, live)`` pair for the
retry scenario.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from types import TracebackType

LOOPBACK = "127.0.0.1"
_HEADER_TERMINATOR = b"\r\n\r\n"


def _free_port() -> int:
    """Bind ``127.0.0.1:0``, read the assigned port, release it immediately."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return int(sock.getsockname()[1])


def reserve_dead_port() -> int:
    """A currently-free (nothing listening) loopback port → connection refused."""
    return _free_port()


def reserve_ordered_ports() -> tuple[int, int]:
    """Return ``(dead, live)`` free ports, same digit-length, ``dead < live``.

    The gateway's tie-break sorts endpoints by the ``"host:port"`` string; equal
    length makes that string order match numeric order, so ``dead`` is always
    picked first (and refused) and the live stub serves the retry.
    """
    for _ in range(200):
        a, b = _free_port(), _free_port()
        low, high = min(a, b), max(a, b)
        if low != high and len(str(low)) == len(str(high)):
            return low, high
    raise RuntimeError("could not reserve two equal-length ordered ports")


def _buffered_head(body: bytes, content_type: str) -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"content-type: " + content_type.encode() + b"\r\n"
        b"content-length: " + str(len(body)).encode() + b"\r\n"
        b"connection: close\r\n\r\n"
    )


def _stream_head(content_type: str) -> bytes:
    # No content-length: the client reads until EOF, so an early close is a
    # clean truncation rather than a framing error.
    return (
        b"HTTP/1.1 200 OK\r\n"
        b"content-type: " + content_type.encode() + b"\r\n"
        b"connection: close\r\n\r\n"
    )


class StubServer:
    """One-shot-per-connection HTTP stub bound to a loopback port."""

    def __init__(
        self,
        *,
        buffered_body: bytes | None = None,
        chunks: tuple[bytes, ...] = (),
        content_type: str = "application/json",
        chunk_delay_s: float = 0.0,
    ) -> None:
        self._buffered_body = buffered_body
        self._chunks = chunks
        self._content_type = content_type
        self._chunk_delay_s = chunk_delay_s
        self._server: asyncio.Server | None = None
        self.port: int = 0
        self.hits = 0

    async def __aenter__(self) -> StubServer:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self, port: int = 0) -> None:
        self._server = await asyncio.start_server(self._handle, LOOPBACK, port)
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.hits += 1
        try:
            await self._drain_request(reader)
            if self._buffered_body is not None:
                await self._write_buffered(writer)
            else:
                await self._write_stream(writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._close(writer)

    async def _drain_request(self, reader: asyncio.StreamReader) -> None:
        # Read only the request head; the body (if any) is left unread.
        await reader.readuntil(_HEADER_TERMINATOR)

    async def _write_buffered(self, writer: asyncio.StreamWriter) -> None:
        assert self._buffered_body is not None
        writer.write(_buffered_head(self._buffered_body, self._content_type))
        writer.write(self._buffered_body)
        await writer.drain()

    async def _write_stream(self, writer: asyncio.StreamWriter) -> None:
        writer.write(_stream_head(self._content_type))
        await writer.drain()
        for chunk in self._chunks:
            if self._chunk_delay_s:
                await asyncio.sleep(self._chunk_delay_s)
            writer.write(chunk)
            await writer.drain()

    @staticmethod
    def _close(writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            writer.close()
