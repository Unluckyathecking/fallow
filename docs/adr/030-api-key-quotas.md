# ADR 030: API key request quotas

Status: accepted · Date: 2026-07-15

## Context

Client API keys already carry a model allowlist, but one key can still consume every
gateway request slot. Operators need simple limits that work without an external rate
limiter and remain deterministic in tests.

## Decision

Each client key may have a requests-per-minute limit, a requests-per-UTC-day limit, both,
or neither. Omitted limits mean unrestricted access. Limits must be positive integers.
The bootstrap admin key remains unrestricted.

The gateway checks quotas after bearer authentication and before request parsing or
routing. The minute limit is a token bucket whose capacity equals the configured RPM.
It starts full and refills continuously at `rpm / 60` tokens per second. This permits a
burst up to the minute limit while preserving the average rate. The daily counter resets
at 00:00 UTC. All calculations use the coordinator's injected aware clock.

A rejected request returns HTTP 429 in the normal OpenAI error envelope. `Retry-After`
is an integer number of seconds until both configured limits can accept another request.
Rejected requests do not consume either counter.

Quota counters live in memory on the request path. The coordinator writes all active
counters to the registry every `quota_snapshot_interval_s`, which defaults to 30 seconds.
It also attempts a final snapshot during graceful shutdown. Startup restores the latest
snapshot before the gateway begins serving. Refill and UTC-day reset are applied from the
restored timestamps.

The limits are nullable columns on `registry_api_keys`. Registry startup checks the table
with `PRAGMA table_info` and adds either column when it is missing. This is the project's
first in-place schema change. Later additive registry migrations should use the same
startup check.

## Restart semantics

A graceful restart preserves accepted-request counts up to shutdown. A crash can lose
usage recorded after the most recent snapshot, so a key may receive extra requests after
recovery. The loss window is bounded by the snapshot interval, but the number of requests
inside that window depends on the key's limits and traffic. This design favors a small,
nonblocking request path over a SQLite write for every request.

Changing the snapshot interval changes that recovery tradeoff. Deployments that require
strict accounting across crashes should enforce quotas at a durable edge proxy instead.

## Consequences

- Existing keys remain unrestricted because migration leaves both limits null.
- `flw keys new` accepts `--rpm` and `--per-day`; the admin request stores the values with
  the hashed key identity.
- Every authenticated gateway route, including `GET /v1/models`, consumes one request.
- Quota snapshots contain only hashed key identities and counters, never plaintext keys.
