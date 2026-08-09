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
from fallow_coordinator.registry.records import ApiKeyInfo, ApiKeyQuotaSnapshot, ModelRecord
from fallow_coordinator.registry.sqlite_registry import SqliteRegistry
from fallow_coordinator.registry.tunnel_mode import EnrollmentMode, Transport, site_eligible

__all__ = [
    "DEFAULT_OFFLINE_AFTER_S",
    "DEFAULT_SUSPECT_AFTER_S",
    "ApiKeyInfo",
    "ApiKeyQuotaSnapshot",
    "EnrollmentMode",
    "EnrollmentTokenError",
    "ModelRecord",
    "ProtocolMismatchError",
    "RegistryConfig",
    "RegistryError",
    "RegistryNotOpenError",
    "SqliteRegistry",
    "Transport",
    "UnknownAgentError",
    "site_eligible",
]
