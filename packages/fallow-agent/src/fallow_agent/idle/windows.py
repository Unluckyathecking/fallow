"""Windows idle detection via GetLastInputInfo.

The user must run the agent inside their own interactive desktop session:
a Session 0 service reads *nothing* from GetLastInputInfo and would report the
machine as permanently idle. Deployment must place the agent in the console
session (documented in the module README).

The single OS seam is `_read_input_ticks`; the wraparound arithmetic
(`_elapsed_ms`) is pure and unit-tested directly. Both GetTickCount() and
LASTINPUTINFO.dwTime are unsigned 32-bit DWORDs, so their difference is taken
modulo 2**32 to survive the ~49.7-day tick rollover. The seam pins the C
signatures (restype=DWORD) so GetTickCount() is read as an unsigned tick rather
than ctypes' default signed int, which would go negative past ~24.8 days of
uptime and report a garbage idle time (issue #35).
"""

import sys
from collections.abc import Callable
from typing import NamedTuple

from fallow_agent.idle.constants import (
    DWORD_MODULUS,
    GETLASTINPUTINFO_FAILED_MSG,
    MS_PER_SECOND,
    WINDOWS_ONLY_MSG,
)
from fallow_protocol.interfaces import IdleDetector


class InputTicks(NamedTuple):
    """A single co-temporal reading of the tick counter and last-input tick."""

    now_ms: int
    last_input_ms: int


def _elapsed_ms(ticks: InputTicks) -> int:
    """Milliseconds since last input, with unsigned 32-bit wraparound."""
    return (ticks.now_ms - ticks.last_input_ms) % DWORD_MODULUS


def _read_input_ticks() -> InputTicks:
    """OS seam: read GetTickCount() and the last-input tick together.

    Isolated so tests can substitute a deterministic reader. All ctypes /
    windll access lives inside the win32 branch so importing this module never
    touches Windows-only symbols on other platforms.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class _LastInputInfo(ctypes.Structure):
            _fields_ = (
                ("cbSize", wintypes.UINT),
                ("dwTime", wintypes.DWORD),
            )

        # Pin the C signatures. ctypes defaults an unbound function's restype to
        # signed int, so GetTickCount() would surface as a negative Python int
        # once uptime passes 2**31 ms (~24.8 days); binding restype=DWORD keeps
        # now_ms an honest unsigned tick (matching the darwin seam, which binds
        # its C signature too).
        get_last_input = ctypes.windll.user32.GetLastInputInfo
        get_last_input.argtypes = (ctypes.POINTER(_LastInputInfo),)
        get_last_input.restype = wintypes.BOOL
        get_tick_count = ctypes.windll.kernel32.GetTickCount
        get_tick_count.argtypes = ()
        get_tick_count.restype = wintypes.DWORD

        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        if not get_last_input(ctypes.byref(info)):
            raise OSError(GETLASTINPUTINFO_FAILED_MSG)
        return InputTicks(now_ms=int(get_tick_count()), last_input_ms=int(info.dwTime))
    raise NotImplementedError(WINDOWS_ONLY_MSG)


TicksReader = Callable[[], InputTicks]


class WindowsIdleDetector(IdleDetector):
    """`IdleDetector` backed by the Win32 GetLastInputInfo API."""

    def __init__(self, reader: TicksReader | None = None) -> None:
        self._reader: TicksReader = reader or _read_input_ticks

    def seconds_since_input(self) -> float:
        return _elapsed_ms(self._reader()) / MS_PER_SECOND
