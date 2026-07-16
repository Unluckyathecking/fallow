"""macOS idle detection via CoreGraphics CGEventSourceSecondsSinceLastEventType.

The CoreGraphics function is bound through ctypes against the framework, not
through a pyobjc lazy module attribute. Some pyobjc builds do not expose
`CGEventSourceSecondsSinceLastEventType` as a module attribute, so the old
`getattr(Quartz, ...)` path raised at runtime (issue #34); the C export is
always present in the framework. This also keeps the dependency surface to the
standard library.

The framework is loaded lazily (and memoised, since the poll thread calls this
at ~10 Hz) and only inside the darwin branch, so importing this module never
touches macOS-only symbols on other platforms. The C call already returns
seconds since the last system-wide HID event, so no arithmetic is needed here.
"""

import ctypes
import ctypes.util
import functools
import sys
from collections.abc import Callable
from typing import Any

from fallow_agent.idle.constants import (
    ANY_INPUT_EVENT_TYPE,
    COREGRAPHICS_FRAMEWORK,
    COREGRAPHICS_MISSING_MSG,
    COREGRAPHICS_NOT_FOUND_MSG,
    DARWIN_ONLY_MSG,
    HID_SYSTEM_STATE,
    PLATFORM_DARWIN,
    SECONDS_SINCE_FN,
)
from fallow_protocol.interfaces import IdleDetector

SecondsSinceFn = Callable[[int, int], float]


def _resolve_seconds_since_fn(lib: Any) -> SecondsSinceFn:
    """Bind CGEventSourceSecondsSinceLastEventType with its C signature.

    Raises OSError with a clear message when the framework does not export the
    symbol, instead of leaking a raw ctypes AttributeError.
    """
    try:
        fn = getattr(lib, SECONDS_SINCE_FN)
    except AttributeError as exc:
        raise OSError(COREGRAPHICS_MISSING_MSG) from exc
    fn.restype = ctypes.c_double
    fn.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
    return fn  # type: ignore[no-any-return]


@functools.lru_cache(maxsize=1)
def _load_seconds_since_fn() -> SecondsSinceFn:
    """Load CoreGraphics once and return the bound idle-timer function."""
    path = ctypes.util.find_library(COREGRAPHICS_FRAMEWORK)
    if path is None:
        raise OSError(COREGRAPHICS_NOT_FOUND_MSG)
    return _resolve_seconds_since_fn(ctypes.CDLL(path))


def _read_seconds_since_input() -> float:
    """OS seam: seconds since the last system-wide HID event, via CoreGraphics.

    Isolated so tests can substitute a deterministic reader. Gated on
    sys.platform so the framework is only loaded on macOS.
    """
    if sys.platform == PLATFORM_DARWIN:
        fn = _load_seconds_since_fn()
        return float(fn(HID_SYSTEM_STATE, ANY_INPUT_EVENT_TYPE))
    raise NotImplementedError(DARWIN_ONLY_MSG)


SecondsReader = Callable[[], float]


class DarwinIdleDetector(IdleDetector):
    """`IdleDetector` backed by the CoreGraphics HID event source."""

    def __init__(self, reader: SecondsReader | None = None) -> None:
        self._reader: SecondsReader = reader or _read_seconds_since_input

    def seconds_since_input(self) -> float:
        return self._reader()
