"""Linux idle detection across the three real Linux cases.

Fallow agents run on desktops, headless servers, and VMs; each needs a
different answer to "seconds since the user last touched this machine":

- **X11 desktop.** Read the `idle` field of `XScreenSaverInfo` via the
  MIT-SCREEN-SAVER extension (libXss), called through `ctypes` so the agent
  gains no X client dependency. This is the primary path.
- **Headless server / VM.** With no display there is no input to measure, and a
  dedicated compute node is effectively always available, so the detector
  reports as maximally idle. The gate is the `DISPLAY` environment variable: a
  real desktop always has it set, so it never silently reports always-idle.
- **Degraded X11.** If a display is present but the XScreenSaver read is
  genuinely unavailable (libXss missing, display unreachable, or the extension
  absent), fall back to the headless value and log once that idle detection is
  degraded. A logind/D-Bus path is a documented future option (ADR 044); it is
  deliberately not added here to avoid a D-Bus dependency.

Wayland is not a separate case: GNOME/KDE Wayland sessions run XWayland, so
`DISPLAY` and libXss are present and they take the X11 path. Idle can be
overstated there (XWayland's counter only resets on input to X clients), which
is acceptable here — see ADR 044.

The single I/O seam is the reader callable, chosen once at construction by
`_resolve_reader`. The X11 reader (`_XScreenSaverReader`) holds the display
connection and pre-allocated info struct open for the life of the agent, so
each ~10 Hz poll is one X round-trip rather than a fresh connect. That single
connection assumes the single-threaded poll contract in `IdleDetector`. Note
that Xlib's default I/O error handler calls `exit()` if the connection drops
mid-run; installing a custom handler is a future hardening step, not done here.
"""

import ctypes
import ctypes.util
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from fallow_agent.idle.constants import (
    DISPLAY_ENV,
    HEADLESS_IDLE_SECONDS,
    HEADLESS_NO_DISPLAY_MSG,
    LIBRARY_NOT_FOUND_MSG,
    MS_PER_SECOND,
    X11_DISPLAY_OPEN_FAILED_MSG,
    X11_LIBRARY,
    XSS_DEGRADED_MSG,
    XSS_LIBRARY,
    XSS_QUERY_FAILED_MSG,
)
from fallow_protocol.interfaces import IdleDetector

_LOG = logging.getLogger(__name__)

SecondsReader = Callable[[], float]

# XScreenSaverQueryInfo(display, drawable, info) -> Status.
XssQueryFn = Callable[[Any, int, Any], int]


class XScreenSaverInfo(ctypes.Structure):
    """Mirror of the C `XScreenSaverInfo` struct; only `idle` is read.

    `idle` is milliseconds since the last input event. Field order and widths
    match Xlib: `Window` and the trailing `unsigned long`s are `c_ulong`.
    """

    _fields_ = (
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),
        ("event_mask", ctypes.c_ulong),
    )


class _XScreenSaverReader:
    """Reads seconds-since-input from the X server via XScreenSaverQueryInfo.

    Holds the display connection, root window, and pre-allocated info struct so
    each call is a single query. Converts the `idle` milliseconds field to
    seconds; raises `OSError` if the query fails.
    """

    def __init__(self, query: XssQueryFn, display: Any, root: int, info: Any) -> None:
        self._query = query
        self._display = display
        self._root = root
        self._info = info

    def __call__(self) -> float:
        if not self._query(self._display, self._root, self._info):
            raise OSError(XSS_QUERY_FAILED_MSG)
        return float(self._info.contents.idle) / MS_PER_SECOND


def _always_idle() -> float:
    """Headless reader: a dedicated compute node is always available."""
    return HEADLESS_IDLE_SECONDS


def _load_library(name: str) -> ctypes.CDLL:
    """Locate and load a shared library, or raise a clear OSError."""
    path = ctypes.util.find_library(name)
    if path is None:
        raise OSError(LIBRARY_NOT_FOUND_MSG.format(name=name))
    return ctypes.CDLL(path)


def _build_x11_reader() -> SecondsReader:
    """Open the X display and bind the XScreenSaver idle read.

    Raises OSError when a library is missing, the display cannot be opened, or
    the extension is absent (surfaced by a probe query). Callers degrade to the
    headless reader on OSError.
    """
    xlib = _load_library(X11_LIBRARY)
    xss = _load_library(XSS_LIBRARY)

    xlib.XOpenDisplay.argtypes = (ctypes.c_char_p,)
    xlib.XOpenDisplay.restype = ctypes.c_void_p
    display = xlib.XOpenDisplay(None)
    if not display:
        raise OSError(X11_DISPLAY_OPEN_FAILED_MSG)

    xlib.XDefaultRootWindow.argtypes = (ctypes.c_void_p,)
    xlib.XDefaultRootWindow.restype = ctypes.c_ulong
    root = xlib.XDefaultRootWindow(display)

    xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(XScreenSaverInfo)
    info = xss.XScreenSaverAllocInfo()

    query = xss.XScreenSaverQueryInfo
    query.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XScreenSaverInfo))
    query.restype = ctypes.c_int

    reader = _XScreenSaverReader(query=query, display=display, root=root, info=info)
    reader()  # Probe once so a missing extension degrades at construction.
    return reader


def _resolve_reader(env: Mapping[str, str], logger: logging.Logger) -> SecondsReader:
    """Choose the idle reader for this host, logging the outcome once.

    No DISPLAY → headless. DISPLAY set but the X11 read is unavailable →
    degraded, log a warning, fall back to headless. DISPLAY set and libXss
    usable → the real XScreenSaver reader.
    """
    if not env.get(DISPLAY_ENV):
        logger.info(HEADLESS_NO_DISPLAY_MSG)
        return _always_idle
    try:
        return _build_x11_reader()
    except OSError as exc:
        logger.warning(XSS_DEGRADED_MSG, exc)
        return _always_idle


class LinuxIdleDetector(IdleDetector):
    """`IdleDetector` backed by XScreenSaver, with a headless fallback.

    Safe to construct and call on a machine with no display: construction never
    raises, and a headless or degraded host reports as always-idle.
    """

    def __init__(self, reader: SecondsReader | None = None) -> None:
        self._reader: SecondsReader = reader or _resolve_reader(os.environ, _LOG)

    def seconds_since_input(self) -> float:
        return self._reader()
