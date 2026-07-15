"""Named constants for the churn injector (module B2).

No churn logic hardcodes a magic number: every default lives here so scripted
YAML scenarios and the seeded generator share one source of truth.
"""

from typing import Final

# ── Agent bench-mode HTTP contract (module A7, fixed) ────────────────────────
DEFAULT_BENCH_PORT: Final = 9411
SIMULATE_INPUT_PATH: Final = "/simulate_input"
STATE_PATH: Final = "/state"
SIMULATE_INPUT_OK_STATUS: Final = 204
ACTIVE_STATE: Final = "active"  # AgentState.ACTIVE value the /state endpoint reports
HTTP_TIMEOUT_S: Final = 5.0

# ── Output ───────────────────────────────────────────────────────────────────
CHURN_JSONL_NAME: Final = "churn.jsonl"

# ── Schedule-model defaults (lognormal params are dimensionless mu/sigma) ─────
DEFAULT_TAP_INTERVAL_S: Final = 30.0  # keep a machine "active" for a whole session
DEFAULT_KILL_RATE_PER_S: Final = 0.0  # rare extras OFF by default
DEFAULT_NET_DROP_RATE_PER_S: Final = 0.0
SCHEDULE_TIME_DP: Final = 6  # round offsets so seeds replay byte-identically

# ── Verification-poll defaults ───────────────────────────────────────────────
DEFAULT_VERIFY_ENABLED: Final = True
DEFAULT_VERIFY_MAX_WAIT_S: Final = 2.0
DEFAULT_VERIFY_POLL_S: Final = 0.05
MS_PER_S: Final = 1000.0

# ── Config parsing ───────────────────────────────────────────────────────────
CHURN_SECTION_KEY: Final = "churn"  # B1's experiment YAML embeds us under this key

# ── Record detail messages ───────────────────────────────────────────────────
NO_COMMAND_MSG: Final = "no command template configured for kind"
POSITIVE_TAP_MSG: Final = "tap_interval_s must be > 0"
