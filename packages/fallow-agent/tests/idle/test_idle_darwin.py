"""Unit tests for the macOS idle detector and its CoreGraphics seam."""

import ctypes
import ctypes.util
import sys

import pytest

from fallow_agent.idle import darwin
from fallow_agent.idle.darwin import DarwinIdleDetector, _read_seconds_since_input


def test_detector_returns_reader_value_directly():
    detector = DarwinIdleDetector(reader=lambda: 12.5)
    assert detector.seconds_since_input() == 12.5


def test_detector_default_reader_is_the_os_seam():
    detector = DarwinIdleDetector()
    assert detector._reader is _read_seconds_since_input


class _FakeSecondsFn:
    """Stand-in for the ctypes-bound CoreGraphics function pointer."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self.restype: object = None
        self.argtypes: object = None

    def __call__(self, state, event_type):
        self.calls.append((state, event_type))
        return 3  # int on purpose: the seam must coerce to float


class _FakeCoreGraphics:
    """Stand-in for the loaded CoreGraphics CDLL."""

    def __init__(self, fn: _FakeSecondsFn | None = None) -> None:
        if fn is not None:
            self.CGEventSourceSecondsSinceLastEventType = fn


def test_resolve_binds_c_signature():
    fn = _FakeSecondsFn()

    bound = darwin._resolve_seconds_since_fn(_FakeCoreGraphics(fn))

    assert bound is fn
    assert fn.restype is ctypes.c_double
    assert fn.argtypes == (ctypes.c_uint32, ctypes.c_uint32)


def test_resolve_missing_symbol_raises_oserror():
    # The bug in issue #34: the symbol is absent on the loaded framework.
    # It must surface as a clear OSError, not a raw AttributeError/KeyError.
    with pytest.raises(OSError, match="CGEventSourceSecondsSinceLastEventType"):
        darwin._resolve_seconds_since_fn(_FakeCoreGraphics())


def test_read_seconds_since_input_calls_fn_with_hid_constants(monkeypatch):
    fn = _FakeSecondsFn()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(darwin, "_load_seconds_since_fn", lambda: fn)

    result = _read_seconds_since_input()

    assert result == 3.0
    assert isinstance(result, float)
    assert fn.calls == [(darwin.HID_SYSTEM_STATE, darwin.ANY_INPUT_EVENT_TYPE)]


def test_read_seconds_since_input_raises_off_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(NotImplementedError):
        _read_seconds_since_input()


def test_load_raises_when_framework_missing(monkeypatch):
    darwin._load_seconds_since_fn.cache_clear()
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
    try:
        with pytest.raises(OSError, match="CoreGraphics framework not found"):
            darwin._load_seconds_since_fn()
    finally:
        darwin._load_seconds_since_fn.cache_clear()
