"""Named constants for the heartbeat/client module.

No magic numbers or literal paths live in the logic modules: every wire path,
status code, and default tunable is named here.
"""

# ── Coordinator v1 endpoints (agent-initiated) ──────────────────────────────
REGISTER_PATH = "/v1/agents/register"
HEARTBEAT_PATH_TEMPLATE = "/v1/agents/{agent_id}/heartbeat"
EVENTS_PATH_TEMPLATE = "/v1/agents/{agent_id}/events"
WORK_PATH_TEMPLATE = "/v1/agents/{agent_id}/work"
RESULT_PATH_TEMPLATE = "/v1/agents/{agent_id}/work_units/{unit_id}/result"

# Query parameter used by the long-poll work acquisition call.
WORK_TIMEOUT_PARAM = "timeout"

# ── HTTP status handling ─────────────────────────────────────────────────────
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_ACCEPTED = 202
HTTP_NO_CONTENT = 204
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
SERVER_ERROR_MIN = 500

# Statuses that carry a parseable success body.
OK_CODES = frozenset({HTTP_OK, HTTP_CREATED})
# Statuses accepted for fire-and-forget writes (events, results).
ACCEPT_CODES = frozenset({HTTP_OK, HTTP_CREATED, HTTP_ACCEPTED, HTTP_NO_CONTENT})
# Statuses that mean "your token is not accepted".
AUTH_CODES = frozenset({HTTP_UNAUTHORIZED, HTTP_FORBIDDEN})

# ── Retry / backoff defaults ─────────────────────────────────────────────────
DEFAULT_CLIENT_MAX_RETRIES = 3
DEFAULT_CLIENT_BACKOFF_S = 0.5

DEFAULT_MAX_PUSH_ATTEMPTS = 3
DEFAULT_EVENT_BACKOFF_S = 0.5

# ── System-probe helpers ─────────────────────────────────────────────────────
BYTES_PER_MB = 1024 * 1024
MIN_CPU_CORES = 1
MAX_CPU_PERCENT = 100.0
MIN_CPU_PERCENT = 0.0
UNKNOWN_CPU_MODEL = "unknown"
NVIDIA_VENDOR = "nvidia"
