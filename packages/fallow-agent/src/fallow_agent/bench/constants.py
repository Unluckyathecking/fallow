"""Constants for the bench hooks module (A7).

Centralised so neither the injecting detector nor the tiny HTTP listener carries
a magic number or a bare protocol string inline.
"""

from __future__ import annotations

# ── Idle injection ───────────────────────────────────────────────────────────
ZERO_IDLE_S = 0.0

# ── HTTP wire protocol (stdlib listener, no framework) ───────────────────────
HTTP_VERSION = "HTTP/1.1"
CRLF = "\r\n"
HEADER_SEPARATOR = b"\r\n\r\n"
ENCODING = "ascii"
JSON_ENCODING = "utf-8"

METHOD_GET = "GET"
METHOD_POST = "POST"

# A well-formed request line is exactly "METHOD PATH VERSION".
REQUEST_LINE_PARTS = 3

ROUTE_SIMULATE_INPUT = "/simulate_input"
ROUTE_STATE = "/state"

CONTENT_TYPE_JSON = "application/json"
HEADER_CONTENT_LENGTH = "content-length"

# Response status lines (bench-only surface: 204/200/404 and a 400 for junk).
STATUS_OK = (200, "OK")
STATUS_NO_CONTENT = (204, "No Content")
STATUS_BAD_REQUEST = (400, "Bad Request")
STATUS_NOT_FOUND = (404, "Not Found")

# JSON body keys for GET /state (fixed B2 contract).
KEY_STATE = "state"
KEY_IDLE_S = "idle_s"

# Cap on the request head we will buffer; a bench client sends a few bytes.
MAX_HEAD_BYTES = 8192
