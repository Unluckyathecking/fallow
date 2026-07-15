"""Unit tests for the immutable WorkerRegistry."""

import pytest
from workers_helpers import FakeWorker

from fallow_agent.workers import (
    Worker,
    WorkerNotRegisteredError,
    WorkerRegistry,
    WorkerUnavailableError,
)
from fallow_protocol.capabilities import WorkerKind


def test_registry_create_returns_factory_result() -> None:
    sentinel: Worker = FakeWorker()
    registry = WorkerRegistry().register(WorkerKind.EMBED, lambda: sentinel)

    assert registry.create(WorkerKind.EMBED) is sentinel


def test_registry_unknown_kind_raises() -> None:
    with pytest.raises(WorkerNotRegisteredError):
        WorkerRegistry().create(WorkerKind.EMBED)


def test_registry_propagates_unavailable_from_factory() -> None:
    def factory() -> Worker:
        raise WorkerUnavailableError("whisper missing")

    registry = WorkerRegistry().register(WorkerKind.TRANSCRIBE, factory)

    with pytest.raises(WorkerUnavailableError):
        registry.create(WorkerKind.TRANSCRIBE)


def test_register_is_immutable_and_returns_new_registry() -> None:
    base = WorkerRegistry()
    extended = base.register(WorkerKind.EMBED, FakeWorker)

    assert base.kinds == frozenset()
    assert extended.kinds == frozenset({WorkerKind.EMBED})


def test_register_last_write_wins() -> None:
    first: Worker = FakeWorker()
    second: Worker = FakeWorker()
    registry = (
        WorkerRegistry()
        .register(WorkerKind.EMBED, lambda: first)
        .register(WorkerKind.EMBED, lambda: second)
    )

    assert registry.create(WorkerKind.EMBED) is second
