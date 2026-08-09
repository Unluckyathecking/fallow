# ADR 078: opt-in LAN Site Mode

## Status

Proposed

## Date

2026-08-09

## Related

ADR 000, ADR 009, ADR 052, ADR 059, ADR 060, ADR 063 and ADR 065

## Context

The school network blocks the pilot's Tailscale path to an off-site machine. Binding the existing services to a shared LAN would remove the encryption boundary and expose unauthenticated llama endpoints.

The pilot needs a central on-site coordinator, four Windows agents, quick user-return preemption and low background overhead. It must also leave existing explicit URL and Tailscale deployments alone.

## Decision

Add an opt-in Site Mode with one pinned HTTPS coordinator. The Go Windows agent is the sole Site Mode implementation. It keeps llama on loopback, claims interactive work with a held HTTP request and uploads the raw response as a streaming HTTP body. The coordinator never connects to an agent.

Enrollment uses a trusted local join file with static HTTPS addresses, SPKI pins and one single-use token. The existing per-device bearer remains the application credential. Site clients bypass proxies and never fall back to HTTP.

The existing Windows last-input detector and preemption controller remain authoritative for user presence. Resource telemetry continues to govern model fit and health. Site routing adds a claim waiter and a presence-generation fence to the current fresh, IDLE, unpaused and READY checks.

Static addressing is the pilot baseline. Optional mDNS comes later and never establishes trust. Client certificates, a private CA and a custom WebSocket protocol are outside the MVP.

## Consequences

The school must allow desktop-to-coordinator traffic on one TCP port and provide a stable coordinator address. No inbound desktop rule is needed.

A nominated user must remain logged in for the agent to run and observe input. Installed identity state must survive the school's reboot and reimage policy.

The Go runtime must finish model reconciliation before the Windows pilot can serve a real request. Site Mode cannot be declared ready from unit tests or cross-compilation alone; the acceptance gate includes an actual Windows run.
