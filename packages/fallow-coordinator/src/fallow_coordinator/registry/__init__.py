"""Registry / auth module (C2).

Public API: the :class:`SqliteRegistry` store plus its config, typed errors, and
the value objects it returns. Everything else in the package is private.
"""

from fallow_coordinator.registry.config import (
    DEFAULT_OFFLINE_AFTER_S,
    DEFAULT_SUSPECT_AFTER_S,
    RegistryConfig,
)
from fallow_coordinator.registry.errors import (
    EnrollmentTokenError,
    ProtocolMismatchError,
    RegistryError,
    RegistryNotOpenError,
    UnknownAgentError,
)
from fallow_coordinator.registry.records import ApiKeyInfo, ModelRecord
from fallow_coordinator.registry.sqlite_registry import SqliteRegistry

__all__ = [
    "DEFAULT_OFFLINE_AFTER_S",
    "DEFAULT_SUSPECT_AFTER_S",
    "ApiKeyInfo",
    "EnrollmentTokenError",
    "ModelRecord",
    "ProtocolMismatchError",
    "RegistryConfig",
    "RegistryError",
    "RegistryNotOpenError",
    "SqliteRegistry",
    "UnknownAgentError",
]
