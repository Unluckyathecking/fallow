"""Replica readiness probe seam.

`HealthCheck` is injected into the supervisor so unit tests never perform real
HTTP. The default implementation, `http_health_check`, issues a plain stdlib
`http.client` GET against a localhost port — httpx is unnecessary for a
loopback readiness ping.
"""

import http.client
from typing import Protocol

HTTP_OK = 200


class HealthCheck(Protocol):
    """Return True iff the replica reports healthy at ``host:port``."""

    def __call__(self, host: str, port: int, path: str, timeout_s: float) -> bool: ...


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
