"""Linux idle detection — an honest stub (out of scope for Fallow v0.1).

A correct Linux implementation must span X11 (XScreenSaver), Wayland (per
compositor idle-notify protocols), and headless/logind sessions, with no single
microsecond-cost API covering all three. Rather than ship a silently-wrong
detector, v0.1 raises so callers fail loudly on unsupported hosts.
"""

from fallow_agent.idle.constants import LINUX_UNSUPPORTED_MSG
from fallow_protocol.interfaces import IdleDetector


class LinuxIdleDetector(IdleDetector):
    """`IdleDetector` placeholder that refuses to guess."""

    def seconds_since_input(self) -> float:
        raise NotImplementedError(LINUX_UNSUPPORTED_MSG)
