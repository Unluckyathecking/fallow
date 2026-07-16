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
LINUX_UNSUPPORTED_MSG = (
    "Idle detection is not implemented on Linux in Fallow v0.1. "
    "X11/Wayland/logind idle sources are out of scope; "
    "see docs/adr/001-idle-detection.md."
)
NEGATIVE_IDLE_MSG = "idle_s must be >= 0."
UNSUPPORTED_PLATFORM_MSG = "No IdleDetector implementation for platform {platform!r}."
