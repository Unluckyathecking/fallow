"""Helpers the discovery acceptance scenarios need and the static ones do not.

ADR 092 asks whether optional discovery improves address recovery without
becoming a trust or an availability dependency. Every scenario answers that
against the same real vertical the static acceptance suite drives — a
pinned-HTTPS coordinator on an exact loopback address and the built Go Site
runtime — and varies only what the agent's profile says about addresses and
mDNS. So this module adds three things and reuses the rest of the static
harness: an origin nothing listens on, an origin held by a responder whose key
is not the pinned one, and the daemon's own account of what discovery did.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from site_mode.site_harness import (
    SITE_HOST,
    SiteDaemon,
    reserve_loopback_sockets,
    write_tls_cert,
)

# The two halves of the minimal ASGI signature this module needs; naming them
# keeps the wrong-key responder readable without pulling in a web framework.
Scope = dict[str, object]
Send = Callable[[dict[str, object]], Awaitable[None]]

MDNS_SERVICE = "_fallow._tcp.local."

# The daemon's own discovery reporting, quoted from go-agent/runtime/site.go.
# Every constant is asserted present in at least one scenario and absent in
# another, so a reworded log line fails a test here rather than quietly turning
# an assertion about what the agent did not do into an assertion about nothing.
QUERY_OPENED = "site static coordinators are unreachable; querying " + MDNS_SERVICE
NO_CANDIDATE = "site discovery found no candidate, keeping the static profile"
ORIGIN_UNUSABLE = "is not usable"


def origin(port: int) -> str:
    """The https origin a coordinator publishes for a loopback listener."""
    return f"https://{SITE_HOST}:{port}"


def closed_port() -> int:
    """A loopback port nothing is listening on, for an origin that must be dead."""
    socks, port = reserve_loopback_sockets()
    for sock in socks:
        sock.close()
    return port


def site_settings(
    *,
    site_id: str,
    urls: Sequence[str],
    certfile: Path,
    keyfile: Path,
    mdns: bool = True,
) -> dict[str, object]:
    """A ``[site]`` block publishing ``urls`` in the order an agent must try them."""
    return {
        "enabled": True,
        "site_id": site_id,
        "public_urls": tuple(urls),
        "tls_certfile": certfile,
        "tls_keyfile": keyfile,
        "mdns_service": MDNS_SERVICE if mdns else None,
    }


@dataclass
class WrongKeyResponder:
    """An HTTPS listener holding an origin under a key the profile does not pin."""

    port: int
    certfile: Path
    requests: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return origin(self.port)


def hold_origin() -> tuple[list[socket.socket], int]:
    """Reserve a loopback port so its origin can be published before it is served."""
    return reserve_loopback_sockets()


@contextlib.asynccontextmanager
async def serve_wrong_key_responder(
    directory: Path, socks: list[socket.socket], port: int
) -> AsyncIterator[WrongKeyResponder]:
    """Serve HTTPS on a held port under a freshly generated, unpinned key.

    The key is generated here and never handed to the coordinator, so its SPKI
    cannot match a pin minted from the coordinator's leaf. Every request that
    does arrive is recorded and answered, so a test that finds no request has
    proven the pinned client refused the peer before writing one — not that the
    responder happened to be unreachable.
    """
    directory.mkdir(parents=True, exist_ok=True)
    certfile, keyfile = write_tls_cert(directory)
    responder = WrongKeyResponder(port=port, certfile=certfile)

    async def app(scope: Scope, receive: object, send: Send) -> None:
        if scope.get("type") != "http":
            return
        responder.requests.append(str(scope.get("path", "")))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            interface="asgi3",
            lifespan="off",
            ssl_certfile=str(certfile),
            ssl_keyfile=str(keyfile),
        )
    )
    serve_task = asyncio.create_task(server.serve(sockets=socks))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield responder
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await serve_task


async def stopped_log(daemon: SiteDaemon) -> str:
    """Stop the daemon and return everything it logged.

    The daemon's stderr is captured by the pipe and read when it is reaped, so a
    test asserting on what discovery reported has to stop it first. A daemon that
    already exited on its own is reaped just the same.
    """
    await daemon.stop()
    return daemon.stderr
