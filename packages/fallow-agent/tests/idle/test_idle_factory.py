"""Unit tests for platform dispatch in create_idle_detector."""

import pytest

from fallow_agent.idle.darwin import DarwinIdleDetector
from fallow_agent.idle.factory import ConstantIdleDetector, create_idle_detector
from fallow_agent.idle.linux import LinuxIdleDetector
from fallow_agent.idle.windows import WindowsIdleDetector


def test_windows_dispatch(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    assert isinstance(create_idle_detector(), WindowsIdleDetector)


def test_darwin_dispatch(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    assert isinstance(create_idle_detector(), DarwinIdleDetector)


def test_linux_dispatch(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert isinstance(create_idle_detector(), LinuxIdleDetector)


def test_linux_variant_dispatch(monkeypatch):
    # sys.platform can be "linux2" on some interpreters/embeddings.
    monkeypatch.setattr("sys.platform", "linux2")
    assert isinstance(create_idle_detector(), LinuxIdleDetector)


def test_unknown_platform_raises(monkeypatch):
    monkeypatch.setattr("sys.platform", "freebsd13")
    with pytest.raises(NotImplementedError) as exc:
        create_idle_detector()
    assert "freebsd13" in exc.value.args[0]


def test_force_idle_requires_bench_mode() -> None:
    with pytest.raises(ValueError, match="requires bench mode"):
        create_idle_detector(force_idle=True)


def test_force_idle_returns_finite_constant_detector() -> None:
    detector = create_idle_detector(bench_enabled=True, force_idle=True)
    assert isinstance(detector, ConstantIdleDetector)
    assert detector.seconds_since_input() > 0
    assert detector.seconds_since_input() != float("inf")
