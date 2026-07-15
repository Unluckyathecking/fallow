"""Fixtures for module A5 (heartbeat/client) tests.

Non-fixture fakes and builders live in ``heartbeat_helpers``; conftest is
fixtures-only. Everything is in-process: no network, no llama-server, no GPU.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import pytest
from heartbeat_helpers import FIXED_NOW


@pytest.fixture
def fixed_now() -> Callable[[], datetime]:
    return lambda: FIXED_NOW
