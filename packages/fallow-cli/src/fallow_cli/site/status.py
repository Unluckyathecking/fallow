"""Read-only Site Mode fleet status: fetch, validate, render.

Self-contained on purpose. ADR 094 owns this module, ``main.py`` and the site
router; it does not own ``client.py`` or ``render.py``, so the one request this
command makes and the one line it prints live here rather than as another
``AdminClient`` method and another renderer. The request is deliberately the
same shape as the join-bundle path: admin bearer, direct transport.

The human form is one labelled ``key=value`` line per agent, like the sibling
``site join-bundles`` output. Ten fields do not survive a rich table at any
sane terminal width — ids and typed failure codes are exactly what gets
truncated, and those are the two things an operator came to read.

``COLUMNS`` is the single source of the field set, so the printed line and the
``--json`` payload cannot drift apart: same fields, same order, only the labels
abbreviated.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx
from pydantic import ValidationError

from fallow_cli.errors import EXIT_AUTH, CliError
from fallow_cli.render import print_json
from fallow_protocol import FallowModel

STATUS_PATH = "/v1/admin/site/status"

COLUMNS = (
    ("enrollment_mode", "mode"),
    ("transport", "transport"),
    ("heartbeat_age_s", "hb_age_s"),
    ("presence_state", "presence"),
    ("presence_generation", "gen"),
    ("available", "avail"),
    ("ready_replicas", "ready"),
    ("last_claim", "last_claim"),
    ("last_claim_code", "claim_code"),
)
FIELDS = ("agent_id", *(field for field, _label in COLUMNS))


class SiteAgentStatus(FallowModel):
    """One Site Mode agent's row, exactly as the coordinator reports it."""

    agent_id: str
    enrollment_mode: str
    transport: str
    heartbeat_age_s: float
    presence_state: str
    presence_generation: int
    available: bool
    ready_replicas: int
    last_claim: str
    last_claim_code: str | None = None


class SiteFleetStatus(FallowModel):
    agents: tuple[SiteAgentStatus, ...]


def fetch_fleet_status(client: httpx.Client, admin_key: str) -> tuple[SiteAgentStatus, ...]:
    """GET the fleet status, translating every HTTP failure into a CliError."""
    try:
        response = client.get(STATUS_PATH, headers={"Authorization": f"Bearer {admin_key}"})
    except httpx.RequestError as exc:
        raise CliError(f"coordinator unreachable at {client.base_url}") from exc
    if response.status_code in (401, 403):
        raise CliError("admin key rejected", exit_code=EXIT_AUTH)
    if response.status_code != 200:
        raise CliError(f"coordinator error {response.status_code}")
    try:
        return SiteFleetStatus.model_validate(response.json()).agents
    except (ValidationError, ValueError) as exc:
        raise CliError("coordinator returned malformed Site Mode fleet status") from exc


def render_fleet_status(agents: Sequence[SiteAgentStatus], as_json: bool) -> None:
    """Print the same fields as one line per agent, or as JSON."""
    if as_json:
        print_json([agent.model_dump(mode="json") for agent in agents])
        return
    for agent in agents:
        labelled = " ".join(f"{label}={_cell(getattr(agent, field))}" for field, label in COLUMNS)
        print(f"{agent.agent_id} {labelled}")


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)
