"""Timeout configuration for the OpenAI-compatible gateway (module C5).

Frozen so a config value can be shared across requests without any risk of
in-flight mutation. The three timeouts map onto the two enforcement mechanisms:

* ``connect_timeout_s`` / ``inter_chunk_timeout_s`` become the httpx transport
  ``Timeout`` (``connect`` and ``read`` respectively): connect covers dialing the
  replica, read covers the gap between successive body chunks once streaming.
* ``first_byte_timeout_s`` is enforced separately by the router via
  ``asyncio.wait_for`` around the *first* body chunk, because llama-server can
  legitimately take much longer to emit its first token (prompt eval / model
  load) than it takes between subsequent tokens.
"""

from dataclasses import dataclass

import httpx

_DEFAULT_CONNECT_S = 2.0
_DEFAULT_FIRST_BYTE_S = 30.0
_DEFAULT_INTER_CHUNK_S = 15.0
_DEFAULT_ADMISSION_TIMEOUT_S = 10.0
_DEFAULT_ADMISSION_CAPACITY = 64
_DEFAULT_ADMISSION_POLL_S = 0.05
_DEFAULT_AFFINITY_TTL_S = 1800.0
_DEFAULT_AFFINITY_MAX = 10_000


@dataclass(frozen=True)
class GatewayConfig:
    """Per-request timeout budget for proxied inference calls."""

    connect_timeout_s: float = _DEFAULT_CONNECT_S
    first_byte_timeout_s: float = _DEFAULT_FIRST_BYTE_S
    inter_chunk_timeout_s: float = _DEFAULT_INTER_CHUNK_S
    admission_timeout_s: float = _DEFAULT_ADMISSION_TIMEOUT_S
    admission_capacity: int = _DEFAULT_ADMISSION_CAPACITY
    admission_poll_interval_s: float = _DEFAULT_ADMISSION_POLL_S
    affinity_ttl_s: float = _DEFAULT_AFFINITY_TTL_S
    affinity_max: int = _DEFAULT_AFFINITY_MAX

    def __post_init__(self) -> None:
        for name, value in (
            ("connect_timeout_s", self.connect_timeout_s),
            ("first_byte_timeout_s", self.first_byte_timeout_s),
            ("inter_chunk_timeout_s", self.inter_chunk_timeout_s),
            ("admission_poll_interval_s", self.admission_poll_interval_s),
            ("affinity_ttl_s", self.affinity_ttl_s),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.affinity_max <= 0:
            raise ValueError("affinity_max must be positive")
        if self.admission_timeout_s < 0:
            raise ValueError("admission_timeout_s must not be negative")
        if self.admission_capacity <= 0:
            raise ValueError("admission_capacity must be positive")

    def httpx_timeout(self) -> httpx.Timeout:
        """Transport timeout: connect guards dialing; read is a backstop only.

        First-byte and inter-chunk gaps are each enforced at the app layer with
        ``asyncio.wait_for`` (router and streaming module respectively), so the
        transport read timeout must sit ABOVE both — a read timeout at the
        tighter inter-chunk value would fire while legitimately waiting for the
        first token (cold prompt eval) and make the 30s first-byte budget
        unreachable. Found live at Gate 3: a cold llama-server needs >15s to
        first byte.
        """
        read_backstop = max(self.first_byte_timeout_s, self.inter_chunk_timeout_s) + 5.0
        return httpx.Timeout(
            read_backstop,
            connect=self.connect_timeout_s,
        )
