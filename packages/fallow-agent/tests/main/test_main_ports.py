"""Port allocator: lowest-free allocation, reuse of released ports, exhaustion."""

from __future__ import annotations

import pytest

from fallow_agent.main import PortAllocator, PortExhaustedError


def test_allocates_lowest_free_first() -> None:
    alloc = PortAllocator(8100, 3)
    assert alloc.allocate() == 8100
    assert alloc.allocate() == 8101
    assert alloc.allocate() == 8102


def test_reuses_released_port() -> None:
    alloc = PortAllocator(8100, 3)
    a, b, c = alloc.allocate(), alloc.allocate(), alloc.allocate()
    assert (a, b, c) == (8100, 8101, 8102)
    alloc.release(8101)
    # The released port is the lowest free one, so it comes back next.
    assert alloc.allocate() == 8101
    assert alloc.in_use == frozenset({8100, 8101, 8102})


def test_exhaustion_raises() -> None:
    alloc = PortAllocator(8100, 2)
    alloc.allocate()
    alloc.allocate()
    with pytest.raises(PortExhaustedError):
        alloc.allocate()


def test_releasing_unknown_port_is_noop() -> None:
    alloc = PortAllocator(8100, 2)
    alloc.release(9999)  # never allocated; must not raise
    assert alloc.allocate() == 8100


def test_invalid_range_rejected() -> None:
    with pytest.raises(ValueError):
        PortAllocator(0, 4)
    with pytest.raises(ValueError):
        PortAllocator(8100, 0)
