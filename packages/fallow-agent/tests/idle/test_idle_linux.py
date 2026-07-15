"""Unit tests for the honest Linux stub."""

import pytest

from fallow_agent.idle.constants import LINUX_UNSUPPORTED_MSG
from fallow_agent.idle.linux import LinuxIdleDetector


def test_linux_detector_raises_not_implemented():
    detector = LinuxIdleDetector()
    with pytest.raises(NotImplementedError) as exc:
        detector.seconds_since_input()
    assert exc.value.args[0] == LINUX_UNSUPPORTED_MSG


def test_linux_message_points_at_the_adr():
    assert "001-idle-detection.md" in LINUX_UNSUPPORTED_MSG
