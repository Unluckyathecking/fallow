"""macOS idle detection via Quartz CGEventSourceSecondsSinceLastEventType.

Quartz is imported lazily (by name, through importlib) inside the OS seam so
this module imports cleanly on non-macOS hosts where the pyobjc Quartz
framework is not installed. The Quartz call already returns seconds since the
last HID event across the whole system, so no arithmetic is needed here.
"""

import importlib
from collections.abc import Callable

from fallow_agent.idle.constants import (
    ANY_INPUT_EVENT_ATTR,
    HID_STATE_ATTR,
    QUARTZ_MODULE,
    SECONDS_SINCE_FN,
)
from fallow_protocol.interfaces import IdleDetector


def _read_seconds_since_input() -> float:
    """OS seam: seconds since the last system-wide HID event, via Quartz.

    Isolated so tests can substitute a deterministic reader without importing
    pyobjc. Symbols are resolved by name to keep the import lazy.
    """
    quartz = importlib.import_module(QUARTZ_MODULE)
    state = getattr(quartz, HID_STATE_ATTR)
    any_input = getattr(quartz, ANY_INPUT_EVENT_ATTR)
    seconds_since = getattr(quartz, SECONDS_SINCE_FN)
    return float(seconds_since(state, any_input))


SecondsReader = Callable[[], float]


class DarwinIdleDetector(IdleDetector):
    """`IdleDetector` backed by the Quartz HID event source."""

    def __init__(self, reader: SecondsReader | None = None) -> None:
        self._reader: SecondsReader = reader or _read_seconds_since_input

    def seconds_since_input(self) -> float:
        return self._reader()
