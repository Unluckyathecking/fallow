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


@dataclass(frozen=True)
class GatewayConfig:
    """Per-request timeout budget for proxied inference calls."""

    connect_timeout_s: float = _DEFAULT_CONNECT_S
    first_byte_timeout_s: float = _DEFAULT_FIRST_BYTE_S
    inter_chunk_timeout_s: float = _DEFAULT_INTER_CHUNK_S

    def httpx_timeout(self) -> httpx.Timeout:
        """Transport timeout: connect guards dialing, read guards inter-chunk gaps.

        Write/pool reuse the same budgets so a stuck upstream can never hang a
        request indefinitely.
        """
        return httpx.Timeout(
            self.inter_chunk_timeout_s,
            connect=self.connect_timeout_s,
        )
