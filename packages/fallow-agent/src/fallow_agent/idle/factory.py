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


class ConstantIdleDetector(IdleDetector):
    """Report the largest finite idle duration for a headless bench agent."""

    def seconds_since_input(self) -> float:
        return sys.float_info.max


def create_idle_detector(*, bench_enabled: bool = False, force_idle: bool = False) -> IdleDetector:
    """Return the `IdleDetector` for the current OS.

    ``force_idle`` is reserved for an explicitly enabled bench. Keeping this
    guard in the factory prevents any caller from selecting the constant
    detector for an ordinary agent.

    Raises NotImplementedError on platforms with no implementation (the Linux
    detector is returned for Linux hosts but itself raises on use).
    """
    if force_idle:
        if not bench_enabled:
            raise ValueError("force_idle requires bench mode")
        return ConstantIdleDetector()

    platform = sys.platform
    if platform == PLATFORM_WINDOWS:
        return WindowsIdleDetector()
    if platform == PLATFORM_DARWIN:
        return DarwinIdleDetector()
    if platform.startswith(PLATFORM_LINUX):
        return LinuxIdleDetector()
    raise NotImplementedError(UNSUPPORTED_PLATFORM_MSG.format(platform=platform))
