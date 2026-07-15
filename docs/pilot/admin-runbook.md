# Administrator runbook — Fallow school pilot

For the person who runs the coordinator day to day: enrolling agents, assigning
models, watching the fleet, and pulling a model out of service. Everything here
goes through the `flw` CLI or the coordinator admin API (`/v1/admin/*`), which is
specified in [`docs/admin-api.md`](../admin-api.md). Command names below are the
ones the CLI actually ships; see [`packages/fallow-cli`](../../packages/fallow-cli/src/fallow_cli/README.md).

Before you start, read the [responsible-use scope](../ai-act-scoping.md). The pilot
is limited to uses that are not high-risk under the EU AI Act. Grading, admissions,
behaviour monitoring, proctoring and biometrics are out of scope — do not enable
them.

## Access

- The coordinator runs as `python -m fallow_coordinator serve --config coordinator.toml`
  on a machine that stays up, bound to its tailnet address.
- `flw` authenticates with a single static **admin key** — the `admin_key` in
  `coordinator.toml`, or `FALLOW_COORD_ADMIN_KEY` on the coordinator side. On the
  operator side set it as `FLW_ADMIN_KEY` (there is no admin-key flag, so it never
  lands in shell history), and point `flw` at the coordinator with
  `--coordinator-url` or `FLW_COORDINATOR_URL`.
- The admin key is a shared secret with full control of the fleet. Hold it like one.

## Enrollment

Agents register once with a one-time token, then persist their own identity.

1. Mint a token: `flw enroll new-token`. It is shown once.
2. Put it in the agent's `agent.toml` as `enrollment_token`, or hand it to the
   installer via `FALLOW_ENROLLMENT_TOKEN` so it is not written to disk.
3. On first run the agent registers and receives a **device token**; after that it
   ignores the enrollment token. Enrollment tokens are single-use.

Confirm the agent arrived: `flw agents list`.

## Assignment

A model only runs where it is assigned. Registration and assignment are separate
steps.

- Register a model on the coordinator: `flw models register --file <blob> --model-id
  <id> --family <f> --quant <q>` (or `flw models pull <url> …` to download first).
  `flw models list` shows what is registered.
- Assign it to the agents that should serve it:

  ```
  flw assign <model-id> <agent-id> [<agent-id> ...]
  ```

  This is an **exact replace**, not an append: the model afterwards runs on exactly
  the agents you name and no others. Re-run it with the full list to change the set.
  The change reaches an agent on its **next heartbeat**, not instantly.

## Monitoring

- `flw status` — one-glance summary of agents and registered models.
- `flw agents list` — each enrolled agent with its capability caps and latest
  heartbeat, so you can see which are online.
- `flw models list` — the registered model catalogue.
- `flw jobs status <job-id>` — progress of a submitted batch job.
- **Audit trails on the coordinator:** every gateway request is written to
  `gateway.jsonl` and every agent event (including preemption yields) to
  `events.jsonl`. Their paths are set by `gateway_log_path` and `events_jsonl_path`
  in `coordinator.toml`. These are the record of which model served what, and when.
- **Agent logs** (macOS): `~/.fallow/logs/agent.out.log` and `agent.err.log`.

There is no live dashboard in v0.1 — monitoring is the CLI plus the JSONL sinks.

## Kill switch

The kill switch is **un-assignment**: remove a model from every agent so no replica
serves it. There is no "delete model" route in v0.1, and removing a model's
assignments is the fleet-wide off switch described in the
[responsible-use scope](../ai-act-scoping.md).

At the admin API this is a `PUT /v1/admin/assignments` with an empty agent list —
which is a tested, idempotent operation:

```
PUT /v1/admin/assignments
Authorization: Bearer <admin key>
{"model_id": "<model-id>", "agent_ids": []}
```

Every agent that currently holds the model drops it on its next heartbeat.

**Caveat — the CLI cannot express the empty set.** `flw assign <model-id>` with no
agent IDs is rejected (`Missing argument 'AGENT_IDS...'`). So the fleet-wide kill is
done by calling the admin API directly — for example with `curl` using the admin
bearer token — not through `flw assign`. To narrow rather than cut, `flw assign
<model-id> <one-agent>` reassigns it down to a single machine.

What the kill switch does **not** do in v0.1:

- It does not delete the model blob or its manifest; the model stays registered and
  can be reassigned.
- It does not revoke a client API key or an agent's device token. There is no
  per-token revocation route; the un-assignment is the blunt instrument. Plan key
  hygiene accordingly.
- It takes effect on the next heartbeat, so it is prompt but not instantaneous.

## Scope reminder

Fallow gives the deploying institution a model inventory, per-request audit records,
this fleet-wide off switch, and data locality (prompts, documents and weights stay
on your infrastructure). It is infrastructure for documented, inspectable
deployment. It is **not** marketed as AI Act compliance and does not certify
anything. The compliance judgement is the institution's — see the
[sign-off template](./data-policy-signoff.md).
