"""Unit tests for the Windows idle detector and its wraparound arithmetic."""

import sys

import pytest

from fallow_agent.idle.constants import WINDOWS_ONLY_MSG
from fallow_agent.idle.windows import (
    InputTicks,
    WindowsIdleDetector,
    _elapsed_ms,
    _read_input_ticks,
)


def test_elapsed_ms_no_wrap():
    assert _elapsed_ms(InputTicks(now_ms=5000, last_input_ms=2000)) == 3000


def test_elapsed_ms_wraparound_tick_after_rollover():
    # last input recorded just before the DWORD rolled over, tick read after.
    ticks = InputTicks(now_ms=100, last_input_ms=2**32 - 50)
    assert _elapsed_ms(ticks) == 150


def test_elapsed_ms_wraparound_last_input_near_max():
    ticks = InputTicks(now_ms=0, last_input_ms=2**32 - 1)
    assert _elapsed_ms(ticks) == 1


def test_elapsed_ms_zero_when_equal():
    assert _elapsed_ms(InputTicks(now_ms=42, last_input_ms=42)) == 0


def test_elapsed_ms_high_uptime_recent_input_issue_35():
    # Regression for #35: past ~24.8 days of uptime the raw tick counter exceeds
    # 2**31 — the regime where a signed misread of GetTickCount reported a
    # garbage idle_s. A recent input (known 10 s delta) must still convert to a
    # small elapsed time, whichever way the seam surfaced the tick.
    now_ms = 2_592_000_000  # ~30 days of uptime, above the signed-int boundary
    assert _elapsed_ms(InputTicks(now_ms=now_ms, last_input_ms=now_ms - 10_000)) == 10_000


def test_detector_converts_ms_to_seconds():
    detector = WindowsIdleDetector(reader=lambda: InputTicks(now_ms=7500, last_input_ms=0))
    assert detector.seconds_since_input() == 7.5


def test_detector_uses_wraparound_for_seconds():
    detector = WindowsIdleDetector(reader=lambda: InputTicks(now_ms=250, last_input_ms=2**32 - 250))
    assert detector.seconds_since_input() == 0.5


@pytest.mark.skipif(sys.platform == "win32", reason="tests the non-Windows guard")
def test_read_input_ticks_raises_off_windows():
    with pytest.raises(NotImplementedError) as exc:
        _read_input_ticks()
    assert exc.value.args[0] == WINDOWS_ONLY_MSG
