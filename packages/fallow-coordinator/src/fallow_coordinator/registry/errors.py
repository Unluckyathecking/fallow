"""Typed errors raised by the registry.

Callers (the coordinator HTTP layer, built later) translate these into wire
responses. They are never swallowed inside the registry.
"""


class RegistryError(Exception):
    """Base class for all registry failures."""


class RegistryNotOpenError(RegistryError):
    """A method was called before ``open()`` (or after ``close()``)."""


class ProtocolMismatchError(RegistryError):
    """Registration rejected: agent speaks a different protocol version."""

    def __init__(self, got: int, expected: int) -> None:
        super().__init__(f"protocol_version {got} != coordinator {expected}")
        self.got = got
        self.expected = expected


class EnrollmentTokenError(RegistryError):
    """The presented enrollment token is unknown or already consumed."""


class UnknownAgentError(RegistryError):
    """A heartbeat referenced an agent_id that is not registered."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"unknown agent_id: {agent_id}")
        self.agent_id = agent_id
