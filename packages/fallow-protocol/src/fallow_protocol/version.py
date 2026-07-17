"""Protocol versioning.

PROTOCOL_VERSION is bumped on any breaking change to wire types. Agents and the
coordinator exchange it at registration and in every heartbeat; mismatches are
rejected at registration time (no in-place protocol negotiation in v0.1).
"""

PROTOCOL_VERSION = 1

__version__ = "0.3.0"
