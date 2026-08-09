"""A loopback stand-in for the pilot site's TLS-inspecting middlebox.

The school network terminates HTTPS and re-presents it under its own key. This
module rebuilds that on loopback: ``intercept_origin`` takes the coordinator's
exact port and answers with a certificate carrying the coordinator's hostname but
a freshly generated key, so a client that checks the SPKI pin must refuse it.
``cleartext_sink`` is the companion trap — a plain TCP listener used as a poisoned
proxy target, so a downgrade or a dial around the pin cannot pass unnoticed.

Both listeners keep every byte a client sent them. That is the point: a test can
assert not merely that the handshake failed but that no request and no credential
ever left the agent. Sockets, certificate minting and port binding all come from
``site_harness`` so there is one way to hold a loopback origin in this suite.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from site_mode.site_harness import (
    LOOPBACK,
    bind_fixed_loopback,
    reserve_loopback_sockets,
    write_tls_cert,
)

# A TLS record always opens with the handshake content type followed by a legacy
# major version of 3; anything else on the wire is a cleartext client.
_TLS_RECORD_PREFIX = b"\x16\x03"
_ACCEPT_POLL_S = 0.2
_READ_TIMEOUT_S = 2.0
_READ_LIMIT = 65536


@dataclass(frozen=True)
class Connection:
    """One accepted connection: how the client opened it and what it sent.

    ``kind`` is ``tls`` for a TLS ClientHello, ``cleartext`` for anything else on
    the wire, and ``silent`` for a client that connected and sent nothing.
    ``payload`` holds the bytes the client wrote — after decryption for a TLS
    connection whose handshake completed, raw otherwise. ``error`` carries the
    handshake failure, which for an honest pinned client is the expected outcome.
    """

    kind: str
    payload: bytes
    error: str | None = None


class _Recorder:
    """Accept loop shared by the listeners: one thread per socket, one per client."""

    def __init__(self, socks: list[socket.socket]) -> None:
        self._socks = socks
        self._lock = threading.Lock()
        self._connections: list[Connection] = []
        self._threads: list[threading.Thread] = []
        self._closing = threading.Event()
        self.port = int(socks[0].getsockname()[1])

    def start(self) -> None:
        for sock in self._socks:
            sock.listen(8)
            sock.settimeout(_ACCEPT_POLL_S)
            self._spawn(self._accept_loop, sock)

    def close(self) -> None:
        self._closing.set()
        for sock in self._socks:
            with contextlib.suppress(OSError):
                sock.close()
        for thread in list(self._threads):
            thread.join(timeout=5.0)

    @property
    def connections(self) -> list[Connection]:
        with self._lock:
            return list(self._connections)

    @property
    def payload(self) -> bytes:
        """Every byte every client wrote to this listener, in arrival order."""
        return b"".join(c.payload for c in self.connections)

    def _spawn(self, target: Callable[..., None], *args: object) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _accept_loop(self, sock: socket.socket) -> None:
        while not self._closing.is_set():
            try:
                conn, _addr = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._spawn(self._serve, conn)

    def _serve(self, conn: socket.socket) -> None:
        raise NotImplementedError

    def _record(self, connection: Connection) -> None:
        with self._lock:
            self._connections.append(connection)

    def _sniff(self, conn: socket.socket) -> bytes:
        """Peek at the opening bytes without consuming them."""
        try:
            return conn.recv(len(_TLS_RECORD_PREFIX), socket.MSG_PEEK)
        except (TimeoutError, OSError):
            return b""

    def _drain(self, sock: socket.socket) -> bytes:
        """Read what the client sends until it stops, bounded in time and size."""
        chunks: list[bytes] = []
        total = 0
        while total < _READ_LIMIT:
            try:
                chunk = sock.recv(min(4096, _READ_LIMIT - total))
            except (TimeoutError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)


class CleartextSink(_Recorder):
    """A plain TCP listener that records anything sent to it and answers nothing."""

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(_READ_TIMEOUT_S)
            head = self._sniff(conn)
            payload = self._drain(conn)
            kind = "silent" if not head else "tls" if head == _TLS_RECORD_PREFIX else "cleartext"
            self._record(Connection(kind=kind, payload=payload))


class TlsInterceptor(_Recorder):
    """A TLS terminator on the coordinator's origin, keyed by an impostor."""

    def __init__(self, socks: list[socket.socket], context: ssl.SSLContext) -> None:
        super().__init__(socks)
        self._context = context

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(_READ_TIMEOUT_S)
        head = self._sniff(conn)
        if head != _TLS_RECORD_PREFIX:
            with conn:
                kind = "silent" if not head else "cleartext"
                self._record(Connection(kind=kind, payload=self._drain(conn)))
            return
        try:
            tls = self._context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            with contextlib.suppress(OSError):
                conn.close()
            # A pinned client rejects the impostor certificate and aborts here,
            # which is the expected outcome, so the failure is recorded not raised.
            self._record(Connection(kind="tls", payload=b"", error=str(exc)))
            return
        with tls:
            self._record(Connection(kind="tls", payload=self._drain(tls)))


@contextlib.contextmanager
def intercept_origin(port: int, workdir: Path) -> Iterator[TlsInterceptor]:
    """Hold ``port`` with the coordinator's hostname under a different key.

    ``write_tls_cert`` mints a fresh EC key on every call, so the leaf served here
    carries the same names as the coordinator's and a public key the join bundle's
    pin cannot match — a transparent inspection proxy, in one loopback port.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    certfile, keyfile = write_tls_cert(workdir)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certfile), str(keyfile))
    # Offer no post-handshake session tickets. A client that writes and closes
    # without reading leaves them unread, and Windows answers a close over
    # unread bytes with an RST, which discards whatever the client sent that
    # this listener has not read yet — the recording the suite rests on.
    context.num_tickets = 0
    server = TlsInterceptor(bind_fixed_loopback(port), context)
    server.start()
    try:
        yield server
    finally:
        server.close()


@contextlib.contextmanager
def cleartext_sink() -> Iterator[CleartextSink]:
    """A cleartext listener on a spare loopback port, used as a fallback trap."""
    socks, _port = reserve_loopback_sockets()
    server = CleartextSink(socks)
    server.start()
    try:
        yield server
    finally:
        server.close()
