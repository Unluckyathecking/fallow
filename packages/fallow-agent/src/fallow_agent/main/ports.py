"""Deterministic port allocator for local inference replicas.

A tiny, pure, in-memory allocator: it hands out the lowest free port in a
contiguous range and reuses ports as soon as they are released. The supervisor
does not allocate ports (ADR 003) — the runtime does, and this is where.
"""

from __future__ import annotations

from fallow_agent.main.errors import PortExhaustedError


class PortAllocator:
    """Allocate/reuse ports from ``[start, start + count)``.

    ``allocate`` always returns the lowest currently-free port so the mapping
    is deterministic and test-friendly; ``release`` returns a port to the pool.
    """

    def __init__(self, start: int, count: int) -> None:
        if start <= 0:
            raise ValueError("port range start must be positive")
        if count <= 0:
            raise ValueError("port range count must be positive")
        self._start = start
        self._count = count
        self._used: set[int] = set()

    def allocate(self) -> int:
        """Return the lowest free port, marking it used.

        Raises :class:`PortExhaustedError` when every port is in use.
        """
        for port in range(self._start, self._start + self._count):
            if port not in self._used:
                self._used.add(port)
                return port
        raise PortExhaustedError(
            f"no free port in range [{self._start}, {self._start + self._count})"
        )

    def release(self, port: int) -> None:
        """Return ``port`` to the pool. Releasing an unknown port is a no-op."""
        self._used.discard(port)

    @property
    def in_use(self) -> frozenset[int]:
        """The set of currently-allocated ports (snapshot)."""
        return frozenset(self._used)
