"""Protocol versioning.

PROTOCOL_VERSION is bumped on any breaking change to wire types. Agents and the
coordinator exchange it at registration and in every heartbeat; mismatches are
rejected at registration time (no in-place protocol negotiation in v0.1).

v2 added the OCR worker kind: the ``ocr`` ``WorkerKind`` enum value and the
optional ``mmproj`` manifest fields. A pre-OCR agent cannot deserialize an OCR
manifest or lease, so the registration check refuses a v1 agent against a v2
coordinator. Per the v0.1 model above there is no in-place protocol upgrade:
the fleet is upgraded together, so an agent enrolls fresh at the coordinator's
version. Fencing an already-enrolled agent that keeps its identity across an
in-place coordinator upgrade would need graceful protocol rollout (versioned
work leasing / snapshot invalidation), which is out of scope for v0.1.
"""

PROTOCOL_VERSION = 2

__version__ = "0.3.0"
