"""Replica readiness and slot-occupancy probe seams.

`HealthCheck` is injected into the supervisor so unit tests never perform real
HTTP. The default implementation, `http_health_check`, issues a plain stdlib
`http.client` GET against a localhost port — httpx is unnecessary for a
loopback readiness ping.
"""

import http.client
import json
from typing import Protocol

HTTP_OK = 200
SLOTS_PATH = "/slots"


class HealthCheck(Protocol):
    """Return True iff the replica reports healthy at ``host:port``."""

    def __call__(self, host: str, port: int, path: str, timeout_s: float) -> bool: ...


class SlotsCheck(Protocol):
    """Return busy llama-server slots, or ``None`` when the probe is unavailable."""

    def __call__(self, host: str, port: int, timeout_s: float) -> int | None: ...


def http_health_check(host: str, port: int, path: str, timeout_s: float) -> bool:
    """GET ``http://host:port/path`` and return True on a 200 response.

    Any connection error, timeout, or non-200 status yields False; the caller
    keeps polling until the startup timeout elapses.
    """
    conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        response.read()  # drain so the connection can be reused/closed cleanly
        return response.status == HTTP_OK
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def parse_busy_slots(payload: bytes) -> int | None:
    """Count b4589 slot objects whose ``is_processing`` field is true."""
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, list):
        return None
    busy = 0
    for slot in decoded:
        if not isinstance(slot, dict) or type(slot.get("is_processing")) is not bool:
            return None
        busy += int(slot["is_processing"])
    return busy


def http_busy_slot_count(host: str, port: int, timeout_s: float) -> int | None:
    """Read the optional b4589 ``/slots`` endpoint without raising."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    try:
        conn.request("GET", SLOTS_PATH)
        response = conn.getresponse()
        payload = response.read()
        if response.status != HTTP_OK:
            return None
        return parse_busy_slots(payload)
    except (OSError, http.client.HTTPException):
        return None
    finally:
        conn.close()
