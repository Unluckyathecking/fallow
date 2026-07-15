# Test bench

A small chat UI for exercising a live Fallow deployment by hand. It talks to a running
coordinator over the OpenAI-compatible gateway and the admin API, so you can watch
requests land on one machine or spread across the fleet as agents come and go.

This is a manual testing tool, not part of the shipped product. It has no tests of its
own and isn't wired into CI.

## Run

Start a coordinator and at least one agent, mint a client key (`flw keys new bench`),
then point the bench at them:

```
FALLOW_COORDINATOR_URL=http://<coordinator-host>:8330 \
FALLOW_CLIENT_KEY=<client api key> \
FLW_ADMIN_KEY=<coordinator admin key> \
uv run python testbench/app.py
```

Open http://127.0.0.1:8770. The left panel lists enrolled agents and their replicas and
refreshes every few seconds. Each answer carries a badge showing which machine served it,
time to first byte, total time, and tokens per second — read from the coordinator's
gateway log.

## Config

| Variable | Default | Meaning |
| --- | --- | --- |
| `FALLOW_COORDINATOR_URL` | `http://127.0.0.1:8330` | Coordinator base URL |
| `FALLOW_CLIENT_KEY` | — | Client API key for the gateway |
| `FLW_ADMIN_KEY` | — | Admin key for the fleet panel |
| `FALLOW_GATEWAY_LOG` | `~/.fallow/coord/gateway.jsonl` | Gateway log, read for the routing badge |
| `TESTBENCH_PORT` | `8770` | Local port for the bench |

The admin key comes from the environment only, never a flag, so it stays out of shell
history. The routing badge matches the newest gateway-log line for the model just used;
with several clients hitting the same model at once the label is best-effort.
