"""Protocol versioning.

PROTOCOL_VERSION is bumped on any breaking change to wire types. Agents and the
coordinator exchange it at registration and in every heartbeat; mismatches are
rejected at registration time (no in-place protocol negotiation in v0.1).

v2 added the OCR worker kind: the ``ocr`` ``WorkerKind`` enum value and the
optional ``mmproj`` manifest fields. A pre-OCR agent cannot deserialize an OCR
manifest or lease, so it must not serve an OCR-capable coordinator. The version
is checked at registration and on every heartbeat: a new v1 agent is refused at
registration, and an in-place-upgraded fleet's v1 agent — which keeps its
persisted identity and never re-registers — is fenced the moment its next
heartbeat is rejected, so it is not recorded live or leased OCR work it would
reject in a loop until the units exhaust their attempts.
"""

PROTOCOL_VERSION = 2

__version__ = "0.3.0"
