"""Unit tests for the Linux idle detector and its X11/headless seams."""

import logging
import os
from types import SimpleNamespace

import pytest

from fallow_agent.idle import linux
from fallow_agent.idle.constants import HEADLESS_IDLE_SECONDS
from fallow_agent.idle.linux import (
    LinuxIdleDetector,
    _always_idle,
    _build_x11_reader,
    _load_library,
    _resolve_reader,
    _XScreenSaverReader,
)

_LOG = logging.getLogger("test")


class _FakeQuery:
    """Stand-in for the ctypes-bound XScreenSaverQueryInfo function pointer."""

    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls: list[tuple[object, object, object]] = []

    def __call__(self, display: object, root: object, info: object) -> int:
        self.calls.append((display, root, info))
        return self.result


def _fake_info(idle_ms: int) -> SimpleNamespace:
    """A stand-in for the allocated XScreenSaverInfo pointer."""
    return SimpleNamespace(contents=SimpleNamespace(idle=idle_ms))


def test_detector_returns_reader_value_directly():
    detector = LinuxIdleDetector(reader=lambda: 12.5)
    assert detector.seconds_since_input() == 12.5


def test_xss_reader_converts_milliseconds_to_seconds():
    query = _FakeQuery()
    info = _fake_info(5000)
    reader = _XScreenSaverReader(query=query, display="dpy", root=42, info=info)

    assert reader() == 5.0
    assert query.calls == [("dpy", 42, info)]


def test_xss_reader_raises_when_query_fails():
    reader = _XScreenSaverReader(
        query=_FakeQuery(result=0), display="dpy", root=42, info=_fake_info(0)
    )
    with pytest.raises(OSError, match="XScreenSaverQueryInfo failed"):
        reader()


def test_always_idle_is_the_deterministic_headless_constant():
    assert _always_idle() == HEADLESS_IDLE_SECONDS
    assert _always_idle() == _always_idle()


def test_resolve_reader_headless_without_display():
    reader = _resolve_reader({}, _LOG)
    assert reader is _always_idle
    assert reader() == HEADLESS_IDLE_SECONDS


def test_resolve_reader_uses_x11_when_display_present(monkeypatch):
    def sentinel() -> float:
        return 7.0

    monkeypatch.setattr(linux, "_build_x11_reader", lambda: sentinel)

    reader = _resolve_reader({"DISPLAY": ":0"}, _LOG)

    assert reader is sentinel


def test_resolve_reader_degrades_to_headless_on_oserror(monkeypatch):
    def _boom() -> linux.SecondsReader:
        raise OSError("no libXss")

    monkeypatch.setattr(linux, "_build_x11_reader", _boom)

    reader = _resolve_reader({"DISPLAY": ":0"}, _LOG)

    assert reader is _always_idle


def test_load_library_missing_raises_oserror(monkeypatch):
    monkeypatch.setattr(linux.ctypes.util, "find_library", lambda name: None)
    with pytest.raises(OSError, match="not found"):
        _load_library("Xss")


def test_construct_on_headless_host_does_not_raise(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    detector = LinuxIdleDetector()
    assert detector.seconds_since_input() == HEADLESS_IDLE_SECONDS


@pytest.mark.skipif(
    not os.environ.get("DISPLAY"), reason="requires a live X11 display ($DISPLAY set)"
)
def test_real_x11_read_returns_nonnegative_seconds():
    reader = _build_x11_reader()
    value = reader()
    assert isinstance(value, float)
    assert value >= 0.0
