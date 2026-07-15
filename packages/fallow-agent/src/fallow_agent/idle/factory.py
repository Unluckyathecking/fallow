"""Platform dispatch for the concrete `IdleDetector` implementation."""

import sys

from fallow_agent.idle.constants import (
    PLATFORM_DARWIN,
    PLATFORM_LINUX,
    PLATFORM_WINDOWS,
    UNSUPPORTED_PLATFORM_MSG,
)
from fallow_agent.idle.darwin import DarwinIdleDetector
from fallow_agent.idle.linux import LinuxIdleDetector
from fallow_agent.idle.windows import WindowsIdleDetector
from fallow_protocol.interfaces import IdleDetector


def create_idle_detector() -> IdleDetector:
    """Return the `IdleDetector` for the current OS.

    Raises NotImplementedError on platforms with no implementation (the Linux
    detector is returned for Linux hosts but itself raises on use).
    """
    platform = sys.platform
    if platform == PLATFORM_WINDOWS:
        return WindowsIdleDetector()
    if platform == PLATFORM_DARWIN:
        return DarwinIdleDetector()
    if platform.startswith(PLATFORM_LINUX):
        return LinuxIdleDetector()
    raise NotImplementedError(UNSUPPORTED_PLATFORM_MSG.format(platform=platform))
