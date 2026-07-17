# ADR 059: Transport-security documentation reconciliation

**Status:** Accepted

**Date:** 2026-07-17

## Context

Two documents described the worker bind model in a way that contradicted the
implemented design. `SECURITY.md` and `docs/compatibility.md` said worker
inference servers must remain bound to loopback. The accepted
[ADR 052](052-replica-bind-address-safety.md) and the architecture baseline
([ADR 000 §6](000-architecture-baseline.md)) describe the design that is
actually implemented: loopback for single-machine development, and the agent's
tailnet IP in production, with the supervisor rejecting wildcard binds.

The contradiction matters most to IT reviewers, who read `SECURITY.md` and
`docs/compatibility.md` first and could conclude either that cross-host
inference is unsupported or that the code diverges from its stated policy.

## Decision

Update `SECURITY.md` and `docs/compatibility.md` to match ADR 052 and the
implemented architecture:

- Workers bind the agent's tailnet IP in production and loopback only for local
  development. The supervisor rejects wildcard binds.
- `llama-server` is unauthenticated. Transport confidentiality comes from the
  tailnet (Tailscale or WireGuard), not from Fallow.
- There is no application-layer TLS or mTLS yet. This is stated plainly as a
  known limitation so reviewers treat the tailnet as the encryption and
  access-control boundary. mTLS remains planned (ADR 000 §6).

This ADR records the reconciliation only. It changes no code and no design; it
brings the two lagging documents into line with a decision already made.

## Consequences

`SECURITY.md`, `docs/compatibility.md`, and ADR 052 now describe the same bind
model. Reviewers get one consistent, honest account of where transport security
lives and what is still missing. No behaviour changes.

## References

- [ADR 052: Replica bind-address safety](052-replica-bind-address-safety.md)
- [ADR 000 §6: Transport security delegated to the tailnet](000-architecture-baseline.md)
