"""Shared constants and upstream scripts for model-cache tests.

Everything is in-process: an ``httpx.MockTransport`` stands in for the
coordinator, and the cache directory is a pytest ``tmp_path``. No network, no
real llama-server, no GPU.

Import helpers from this module (never from ``conftest``): pytest imports each
``conftest.py`` under a private internal name, so ``from conftest import X`` is
resolved by sys.path insertion order and breaks when test trees are mixed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import httpx

BASE_URL = "http://coordinator.test"
DEVICE_TOKEN = "tok-abc123"
MODEL_ID = "qwen2.5-7b-instruct-q4km"
FILE_NAME = "model.gguf"


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def blob_handler(
    body: bytes,
    *,
    ignore_range: bool = False,
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve ``body`` as a coordinator blob endpoint, honouring Range."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        rng = request.headers.get("Range")
        if rng and not ignore_range:
            start = int(rng.removeprefix("bytes=").split("-", 1)[0])
            chunk = body[start:]
            headers = {"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}
            return httpx.Response(206, content=chunk, headers=headers)
        return httpx.Response(200, content=body)

    return _handler
