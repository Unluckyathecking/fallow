"""In-memory inflight-request accounting keyed by ``(host, port)``.

The coordinator runs a single asyncio event loop, so a plain ``dict`` mutated
between ``await`` points is race-free (no thread crosses into it). A count is
held for the *entire* duration of a proxied request — including the whole body
of a streaming response — via an :class:`InflightHold` whose ``release`` is
idempotent, so double-release (e.g. retry then finally) can never underflow.

``get_inflight`` is the seam the app layer reads to enrich
``ReplicaEndpoint.inflight`` with a *live* count before calling the scheduler's
``pick_replica`` — fresher than the ~5s-old count carried on heartbeats.
"""


class InflightHold:
    """A single held inflight slot; releasing it decrements the counter once."""

    __slots__ = ("_key", "_released", "_tracker")

    def __init__(self, tracker: "InflightTracker", key: tuple[str, int]) -> None:
        self._tracker = tracker
        self._key = key
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._tracker._decrement(self._key)


class InflightTracker:
    """Live per-replica inflight counts for load-aware routing."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, int], int] = {}

    def hold(self, host: str, port: int) -> InflightHold:
        key = (host, port)
        self._counts[key] = self._counts.get(key, 0) + 1
        return InflightHold(self, key)

    def count(self, host: str, port: int) -> int:
        return self._counts.get((host, port), 0)

    def get_inflight(self) -> dict[tuple[str, int], int]:
        """A copy of the current counts; safe for the caller to keep or mutate."""
        return dict(self._counts)

    def _decrement(self, key: tuple[str, int]) -> None:
        remaining = self._counts.get(key, 0) - 1
        if remaining <= 0:
            self._counts.pop(key, None)
        else:
            self._counts[key] = remaining
