"""Fallow test bench — a small chat UI that shows which machine served each request.

It talks to a running coordinator over the OpenAI-compatible gateway and the admin
API, so you can watch requests land on the Mac agent, the PC agent, or spread across
both as the fleet changes. Nothing here is part of the shipped product; it exists to
exercise a live deployment by hand.

Run it against a coordinator you already have up:

    FALLOW_COORDINATOR_URL=http://<coordinator-host>:8330 \
    FALLOW_CLIENT_KEY=<a client api key from `flw keys new`> \
    FLW_ADMIN_KEY=<the coordinator admin key> \
    uv run python testbench/app.py

Then open http://127.0.0.1:8770.

Config comes from the environment, never flags — the admin key must not reach shell
history. The gateway log path defaults to the coordinator default and only matters for
the per-request routing badge.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

COORDINATOR_URL = os.environ.get("FALLOW_COORDINATOR_URL", "http://127.0.0.1:8330").rstrip("/")
CLIENT_KEY = os.environ.get("FALLOW_CLIENT_KEY", "")
ADMIN_KEY = os.environ.get("FLW_ADMIN_KEY", "")
GATEWAY_LOG = Path(
    os.environ.get("FALLOW_GATEWAY_LOG", "~/.fallow/coord/gateway.jsonl")
).expanduser()

_STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Fallow test bench")


class ChatRequest(BaseModel):
    model: str
    prompt: str
    max_tokens: int = 256


def _last_routing(model_id: str) -> dict[str, Any] | None:
    """Return the newest gateway-log entry for this model, or None.

    The gateway appends one line per request. For a single-user bench that line is
    almost always the request we just made; we still match on model_id so a stray
    concurrent request for another model can't mislabel the badge.
    """
    try:
        lines = GATEWAY_LOG.read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("model_id") == model_id:
            return entry
    return None


async def _fleet(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    resp = await client.get(
        f"{COORDINATOR_URL}/v1/admin/agents",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()


def _label(agent: dict[str, Any]) -> str:
    caps = agent.get("caps") or {}
    host = caps.get("hostname") or agent.get("host") or "unknown"
    os_name = (caps.get("os") or "").lower()
    if "windows" in os_name or "win" in os_name:
        kind = "PC"
    elif "mac" in os_name or "darwin" in os_name:
        kind = "Mac"
    else:
        kind = os_name or "host"
    return f"{kind} · {host.split('.')[0]}"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/api/fleet")
async def fleet() -> JSONResponse:
    async with httpx.AsyncClient() as client:
        try:
            agents = await _fleet(client)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"coordinator unreachable: {exc}") from exc
    out = []
    for a in agents:
        out.append(
            {
                "agent_id": a["agent_id"],
                "label": _label(a),
                "host": a.get("host"),
                "state": a.get("state"),
                "gpus": (a.get("caps") or {}).get("gpus") or [],
                "replicas": [
                    {"model_id": r["model_id"], "state": r["state"], "gpu": r.get("gpu", False)}
                    for r in a.get("replicas", [])
                ],
                "user_idle_s": a.get("user_idle_s"),
            }
        )
    return JSONResponse(out)


@app.post("/api/chat")
async def chat(req: ChatRequest) -> JSONResponse:
    body = {
        "model": req.model,
        "messages": [{"role": "user", "content": req.prompt}],
        "max_tokens": req.max_tokens,
        "stream": False,
    }
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{COORDINATOR_URL}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {CLIENT_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=120.0,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        completion = resp.json()
        try:
            agents = await _fleet(client)
        except httpx.HTTPError:
            agents = []

    entry = _last_routing(req.model)
    routing: dict[str, Any] = {}
    if entry:
        by_id = {a["agent_id"]: a for a in agents}
        served = by_id.get(entry.get("agent_id"))
        submit = entry.get("t_submit")
        first = entry.get("t_first_byte")
        done = entry.get("t_done")
        routing = {
            "agent_id": entry.get("agent_id"),
            "label": _label(served) if served else entry.get("agent_id", "?")[:8],
            "host": served.get("host") if served else None,
            "status": entry.get("status"),
            "retried": entry.get("retried"),
            "ttfb_ms": _ms(submit, first),
            "total_ms": _ms(submit, done),
        }
    usage = completion.get("usage", {})
    content = ""
    if completion.get("choices"):
        content = completion["choices"][0].get("message", {}).get("content", "")
    tok_per_s = None
    total_ms = routing.get("total_ms")
    if total_ms and usage.get("completion_tokens"):
        tok_per_s = round(usage["completion_tokens"] / (total_ms / 1000.0), 1)
    return JSONResponse(
        {"content": content, "routing": routing, "usage": usage, "tok_per_s": tok_per_s}
    )


def _ms(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    from datetime import datetime

    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((e - s).total_seconds() * 1000.0, 1)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("TESTBENCH_PORT", "8770"))
    uvicorn.run(app, host="127.0.0.1", port=port)
