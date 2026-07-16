"""Constants and operator-facing messages for the idle-detection module.

Centralised so nothing in the OS-specific detectors hardcodes a magic
number or an API symbol name inline.
"""

import sys

# Windows tick arithmetic. GetTickCount() and LASTINPUTINFO.dwTime are both
# unsigned 32-bit DWORDs that wrap to zero every 2**32 ms (~49.7 days); elapsed
# time must therefore be computed modulo this value.
DWORD_MODULUS = 2**32
MS_PER_SECOND = 1000.0

# Fake detector.
ZERO_IDLE_S = 0.0

# CoreGraphics (macOS) idle-timer API. Bound through ctypes against the
# framework rather than a pyobjc lazy module attribute: some pyobjc builds do
# not expose CGEventSourceSecondsSinceLastEventType as a module attribute
# (issue #34), whereas the C export is always present in the framework. The
# framework is loaded lazily so the package still imports cleanly off macOS.
COREGRAPHICS_FRAMEWORK = "CoreGraphics"
SECONDS_SINCE_FN = "CGEventSourceSecondsSinceLastEventType"
# The two C-call arguments: CGEventSourceStateID.kCGEventSourceStateHIDSystemState
# and the kCGAnyInputEventType sentinel (~0 as a uint32).
HID_SYSTEM_STATE = 1
ANY_INPUT_EVENT_TYPE = 0xFFFFFFFF

# sys.platform tokens used for factory dispatch.
PLATFORM_WINDOWS = "win32"
PLATFORM_DARWIN = "darwin"
PLATFORM_LINUX = "linux"

# Messages.
WINDOWS_ONLY_MSG = "GetLastInputInfo is only available on Windows (sys.platform == 'win32')."
GETLASTINPUTINFO_FAILED_MSG = "GetLastInputInfo returned zero (Win32 call failed)."
DARWIN_ONLY_MSG = (
    "CGEventSourceSecondsSinceLastEventType is only available on macOS (sys.platform == 'darwin')."
)
COREGRAPHICS_NOT_FOUND_MSG = "CoreGraphics framework not found (is this macOS?)."
COREGRAPHICS_MISSING_MSG = "CoreGraphics does not export CGEventSourceSecondsSinceLastEventType."

# Linux X11 idle detection via the MIT-SCREEN-SAVER extension (libXss). The
# `idle` field of XScreenSaverInfo is milliseconds since the last input; it is
# read through ctypes so the agent needs no X client dependency. libX11 opens
# the display connection, libXss allocates and queries the info struct.
X11_LIBRARY = "X11"
XSS_LIBRARY = "Xss"
DISPLAY_ENV = "DISPLAY"
# Headless nodes (servers, VMs) have no input to measure and are dedicated
# compute, so they report as maximally idle. Reusing the largest finite float
# keeps this consistent with the bench ConstantIdleDetector.
HEADLESS_IDLE_SECONDS = sys.float_info.max

LIBRARY_NOT_FOUND_MSG = "Shared library {name!r} not found (is it installed?)."
X11_DISPLAY_OPEN_FAILED_MSG = "XOpenDisplay(NULL) returned NULL; no X display is reachable."
XSS_QUERY_FAILED_MSG = "XScreenSaverQueryInfo failed (is the MIT-SCREEN-SAVER extension present?)."
HEADLESS_NO_DISPLAY_MSG = "No DISPLAY set; Linux idle detection is headless (always-idle)."
XSS_DEGRADED_MSG = (
    "X display present but the XScreenSaver idle read is unavailable (%s); "
    "falling back to always-idle. Idle-based preemption is degraded on this host."
)

NEGATIVE_IDLE_MSG = "idle_s must be >= 0."
UNSUPPORTED_PLATFORM_MSG = "No IdleDetector implementation for platform {platform!r}."
