"""Unit tests for the macOS idle detector and its Quartz seam."""

import importlib

from fallow_agent.idle import darwin
from fallow_agent.idle.darwin import DarwinIdleDetector, _read_seconds_since_input


def test_detector_returns_reader_value_directly():
    detector = DarwinIdleDetector(reader=lambda: 12.5)
    assert detector.seconds_since_input() == 12.5


def test_detector_default_reader_is_the_os_seam():
    detector = DarwinIdleDetector()
    assert detector._reader is _read_seconds_since_input


class _FakeQuartz:
    """Stand-in for the pyobjc Quartz module."""

    kCGEventSourceStateHIDSystemState = 1
    kCGAnyInputEventType = 0xFFFFFFFF

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def CGEventSourceSecondsSinceLastEventType(self, state, event_type):
        self.calls.append((state, event_type))
        return 3  # int on purpose: the seam must coerce to float


def test_read_seconds_since_input_uses_quartz(monkeypatch):
    fake = _FakeQuartz()
    monkeypatch.setattr(importlib, "import_module", lambda name: fake)

    result = _read_seconds_since_input()

    assert result == 3.0
    assert isinstance(result, float)
    assert fake.calls == [(1, 0xFFFFFFFF)]


def test_read_seconds_since_input_imports_quartz_by_name(monkeypatch):
    seen: list[str] = []

    def fake_import(name: str):
        seen.append(name)
        return _FakeQuartz()

    monkeypatch.setattr(importlib, "import_module", fake_import)
    _read_seconds_since_input()
    assert seen == [darwin.QUARTZ_MODULE]
