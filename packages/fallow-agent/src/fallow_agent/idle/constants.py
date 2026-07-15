"""Constants and operator-facing messages for the idle-detection module.

Centralised so nothing in the OS-specific detectors hardcodes a magic
number or an API symbol name inline.
"""

# Windows tick arithmetic. GetTickCount() and LASTINPUTINFO.dwTime are both
# unsigned 32-bit DWORDs that wrap to zero every 2**32 ms (~49.7 days); elapsed
# time must therefore be computed modulo this value.
DWORD_MODULUS = 2**32
MS_PER_SECOND = 1000.0

# Fake detector.
ZERO_IDLE_S = 0.0

# Quartz (macOS) API symbol names. Resolved lazily by name so the package
# imports cleanly on non-macOS platforms where Quartz is not installed.
QUARTZ_MODULE = "Quartz"
HID_STATE_ATTR = "kCGEventSourceStateHIDSystemState"
ANY_INPUT_EVENT_ATTR = "kCGAnyInputEventType"
SECONDS_SINCE_FN = "CGEventSourceSecondsSinceLastEventType"

# sys.platform tokens used for factory dispatch.
PLATFORM_WINDOWS = "win32"
PLATFORM_DARWIN = "darwin"
PLATFORM_LINUX = "linux"

# Messages.
WINDOWS_ONLY_MSG = "GetLastInputInfo is only available on Windows (sys.platform == 'win32')."
GETLASTINPUTINFO_FAILED_MSG = "GetLastInputInfo returned zero (Win32 call failed)."
LINUX_UNSUPPORTED_MSG = (
    "Idle detection is not implemented on Linux in Fallow v0.1. "
    "X11/Wayland/logind idle sources are out of scope; "
    "see docs/adr/001-idle-detection.md."
)
NEGATIVE_IDLE_MSG = "idle_s must be >= 0."
UNSUPPORTED_PLATFORM_MSG = "No IdleDetector implementation for platform {platform!r}."
