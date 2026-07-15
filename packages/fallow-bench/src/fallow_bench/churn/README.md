# churn — fleet-churn injector (bench module B2)

Reproducible disruption of a Fallow fleet for the Wave-4 scheduling study.
Generates a **seeded, replay-deterministic** schedule of user-return / kill /
net-drop events (or loads a scripted one from YAML) and replays it against
bench-mode agents, timing the injected-input → replica-yield flip.

Depends only on `fallow_protocol` (+ `httpx`, `numpy`, `pyyaml`) per the bench
layer rule. It talks to agents over HTTP — it does **not** import `fallow_agent`.

## Agent contract (module A7, fixed)

Each bench-mode agent exposes, on `http://{host}:{bench_port}` (default `9411`):

- `POST /simulate_input` → `204` — inject a synthetic user input.
- `GET /state` → `{"state": "idle|active|draining", "idle_s": <float>}`.

## Public API

Re-exported from `fallow_bench.churn`:

| Symbol | Purpose |
| --- | --- |
| `ChurnSection` | The churn slice of an experiment config (B1 embeds it under `churn:`). |
| `ChurnModel` | Lognormal idle/active renewal params + optional kill/net-drop rates. |
| `ChurnEvent` | One scheduled disruption: `{t_offset_s, agent_name, kind, params}`. |
| `ChurnRecord` | One executed event with absolute and replay-relative time, written to `churn.jsonl`. |
| `ChurnKind` | `user_return` \| `agent_kill` \| `net_drop`. |
| `AgentTarget`, `VerifyConfig`, `RunResult` | Config / result value types. |
| `build_schedule` / `resolve_schedule` | Seeded generator; scripted takes precedence. |
| `load_churn_section` | YAML loader (standalone doc or B1 experiment config). |
| `ChurnInjector` | Async replay engine over injected clock/sleeper/HTTP/runner. |
| `ChurnLog` | Serialised append-only `churn.jsonl` sink. |
| `measure_flip` | Bounded `GET /state` poll → input→yield latency (ms). |
| `run_shell` | Subprocess `Runner` — wired only in `__main__`. |

## Determinism

All randomness flows from one seeded `numpy.random.default_rng(seed)`; the same
seed yields a byte-identical schedule. The injector owns no clock and no
sleeper — both are injected (`time.monotonic` / `asyncio.sleep` in `__main__`,
a fake clock in tests), so a replay is fully reproducible.

Each record stores `t`, the UTC epoch time captured when execution starts, and
`t_executed`, the monotonic offset from the replay start. Recovery analysis uses
`t` so it shares a time scale with coordinator unit transitions. Schedule tests
and replay audits use `t_executed`.

## Kill / net-drop commands

Never hardcoded. Kill and net-drop events run a config-supplied shell template
per kind, rendered with `{name}`, `{host}`, `{bench_port}`, e.g.

```yaml
commands:
  agent_kill: "ssh {host} taskkill /F /IM llama-server.exe"
```

A failing endpoint, missing template, or broken runner is recorded with
`ok=false` and never aborts the run.

## Entry point

```bash
python -m fallow_bench churn --config experiment.yaml --out ./run-out
```

Writes `run-out/churn.jsonl`. See `docs/adr/020-bench-churn.md`.
