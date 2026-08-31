"""Protocol versioning.

PROTOCOL_VERSION is bumped on any breaking change to wire types. Agents and the
coordinator exchange it at registration and in every heartbeat; mismatches are
rejected at registration time (no in-place protocol negotiation in v0.1).

v2 added the OCR worker kind: the ``ocr`` ``WorkerKind`` enum value and the
optional ``mmproj`` manifest fields. A pre-OCR agent cannot deserialize an OCR
manifest or lease, so it must not enroll against an OCR-capable coordinator —
the bump is what turns it away at registration instead of letting it lease OCR
units it would reject in a loop until they exhaust their attempts.
"""

PROTOCOL_VERSION = 2

__version__ = "0.3.0"
