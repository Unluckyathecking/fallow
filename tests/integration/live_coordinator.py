"""Boot the real coordinator app on a loopback ephemeral port for goagent tests.

The in-process ASGI harness (``conftest.make_harness``) is fast because it skips
the socket. A Go agent needs a real TCP endpoint, so the parity scenarios run
uvicorn on ``127.0.0.1:0`` in the *same* event loop and share the injected
:class:`~integration_helpers.FakeClock`, so a test can still advance time to
drive the coordinator's eviction and requeue loops.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import uvicorn
from fastapi import FastAPI
from integration_helpers import FakeClock

from fallow_coordinator.app import CoordinatorConfig

LOOPBACK = "127.0.0.1"


@dataclass
class LiveCoordinator:
    """One coordinator served over loopback HTTP, with its clock and config."""

    base_url: str
    client: httpx.AsyncClient
    clock: FakeClock
    config: CoordinatorConfig


@asynccontextmanager
async def serve_app(app: FastAPI) -> AsyncIterator[str]:
    """Serve ``app`` on an ephemeral loopback port; yield its base URL.

    The socket is bound with port 0 and the assigned port read back, so parallel
    runs never collide. uvicorn runs as a task on the caller's event loop and is
    asked to exit on teardown.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((LOOPBACK, 0))
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    serve_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield f"http://{LOOPBACK}:{port}"
    finally:
        server.should_exit = True
        await serve_task
