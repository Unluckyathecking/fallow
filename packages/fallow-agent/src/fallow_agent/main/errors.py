"""Typed errors for the agent runtime (module I2).

Every failure the composition root can raise before or during assembly has a
named type so callers (the CLI, tests) branch on class, not on strings. Loop
bodies never raise these out to the caller — they log and continue — but the
one-shot setup path (settings, identity, manifest fetch) fails loudly.
"""

from __future__ import annotations


class AgentRuntimeError(Exception):
    """Base class for all agent-runtime failures."""


class SettingsError(AgentRuntimeError):
    """The agent configuration (TOML + env) was missing or invalid."""


class IdentityError(AgentRuntimeError):
    """The persisted identity could not be loaded, or enrollment is impossible."""


class ManifestFetchError(AgentRuntimeError):
    """A model manifest could not be fetched from the coordinator."""


class PortExhaustedError(AgentRuntimeError):
    """The configured port range has no free port left to allocate."""
