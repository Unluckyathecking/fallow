"""``flw site status``: transport, JSON shape, and the human table.

Offline throughout: the admin transport seam is an ``httpx.MockTransport``, the
same seam every other CLI test uses.
"""

from __future__ import annotations

import json

import httpx
import pytest

from fallow_cli import main
from fallow_cli.errors import EXIT_AUTH, CliError
from fallow_cli.site.status import (
    COLUMNS,
    FIELDS,
    STATUS_PATH,
    fetch_fleet_status,
    render_fleet_status,
)

COORD_URL = "http://coordinator.test"

SERVING = {
    "agent_id": "agent-1",
    "enrollment_mode": "site",
    "transport": "site_relay",
    "heartbeat_age_s": 1.5,
    "presence_state": "idle",
    "presence_generation": 3,
    "available": True,
    "ready_replicas": 2,
    "last_claim": "finished",
    "last_claim_code": None,
}
FAILED = {
    **SERVING,
    "agent_id": "agent-2",
    "presence_state": "reclaimed",
    "available": False,
    "ready_replicas": 0,
    "last_claim": "failed",
    "last_claim_code": "became_active",
}
NEVER_CLAIMED = {**SERVING, "agent_id": "agent-3", "last_claim": "none", "last_claim_code": None}

FLEET = [SERVING, FAILED, NEVER_CLAIMED]


def transport(status=200, body=None, *, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"agents": FLEET} if body is None else body)

    return httpx.MockTransport(handler)


def client(**kw):
    return httpx.Client(base_url=COORD_URL, **kw)


def test_fetch_sends_the_admin_bearer_to_the_status_route():
    seen: list[httpx.Request] = []
    with client(transport=transport(seen=seen)) as http:
        agents = fetch_fleet_status(http, "secret")
    assert [a.agent_id for a in agents] == ["agent-1", "agent-2", "agent-3"]
    assert seen[0].url.path == STATUS_PATH
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert agents[1].last_claim_code == "became_active"
    assert agents[2].last_claim == "none" and agents[2].last_claim_code is None


@pytest.mark.parametrize(("status", "exit_code"), [(401, EXIT_AUTH), (403, EXIT_AUTH), (500, 1)])
def test_fetch_translates_http_failures(status, exit_code):
    with client(transport=transport(status, body={})) as http:  # noqa: SIM117
        with pytest.raises(CliError) as excinfo:
            fetch_fleet_status(http, "secret")
    assert excinfo.value.exit_code == exit_code


def test_fetch_rejects_a_malformed_body():
    with client(transport=transport(body={"agents": [{"agent_id": "a"}]})) as http:  # noqa: SIM117
        with pytest.raises(CliError, match="malformed"):
            fetch_fleet_status(http, "secret")


def test_fetch_reports_an_unreachable_coordinator():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with client(transport=httpx.MockTransport(boom)) as http:  # noqa: SIM117
        with pytest.raises(CliError, match="unreachable"):
            fetch_fleet_status(http, "secret")


def test_json_and_human_output_carry_the_same_fields(capsys):
    with client(transport=transport()) as http:
        agents = fetch_fleet_status(http, "secret")

    render_fleet_status(agents, True)
    payload = json.loads(capsys.readouterr().out)
    assert [set(row) for row in payload] == [set(FIELDS)] * 3
    assert payload[1]["last_claim_code"] == "became_active"

    render_fleet_status(agents, False)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3, "one row per Site Mode agent"
    assert lines[0].startswith("agent-1 ")
    for _field, label in COLUMNS:
        assert f"{label}=" in lines[0], "every JSON field is labelled in the human line"
    assert "claim_code=became_active" in lines[1]
    assert "claim_code=-" in lines[0], "a clean claim has no failure code"


def test_command_renders_json(runner, env, monkeypatch):
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", transport())
    result = runner.invoke(
        main.app, ["--coordinator-url", COORD_URL, "--json", "site", "status"], env=env
    )
    assert result.exit_code == 0, result.output
    assert [row["agent_id"] for row in json.loads(result.output)] == [
        "agent-1",
        "agent-2",
        "agent-3",
    ]


def test_command_ignores_proxy_environment(runner, env, monkeypatch):
    """The status call takes the same direct, no-proxy path as join-bundles."""
    seen: list[httpx.Request] = []
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", transport(seen=seen))
    result = runner.invoke(
        main.app,
        ["--coordinator-url", COORD_URL, "site", "status"],
        env={**env, "HTTPS_PROXY": "http://proxy.invalid:3128", "ALL_PROXY": "socks5://nope:1080"},
    )
    assert result.exit_code == 0, result.output
    assert str(seen[0].url).startswith(COORD_URL)


def test_command_requires_an_admin_key(runner, env, monkeypatch):
    monkeypatch.setattr(main, "_ADMIN_TRANSPORT", transport())
    result = runner.invoke(
        main.app,
        ["--coordinator-url", COORD_URL, "site", "status"],
        env={k: v for k, v in env.items() if k != "FLW_ADMIN_KEY"},
    )
    assert result.exit_code == EXIT_AUTH
    assert "no admin key configured" in result.output
