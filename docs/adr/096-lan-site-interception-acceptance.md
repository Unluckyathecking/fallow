# gate Site Mode against TLS interception

## Status

Proposed

## Date

2026-08-09

## Related

#109, #115, #120

## Goal

Rehearse the school network's TLS interception before pilot day. The pilot site inspects HTTPS; the acceptance suite must prove an intercepted connection is refused before any secret leaves the agent, is diagnosed by name, and recovers once the real coordinator is reachable again.

## Owned paths

- `tests/integration/site_mode/test_interception.py`
- `tests/integration/site_mode/interceptor.py`
- `docs/adr/096-lan-site-interception-acceptance.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

The harness gains a loopback TLS terminator that presents a certificate with the coordinator's hostname but a different key, standing in for a transparent inspection proxy. Against it, the agent must send no request bytes and no authorization after the pin check fails, must not fall back to cleartext or a direct dial, and must report a pin mismatch distinctly from an unreachable coordinator. When the interceptor is removed and the real coordinator returns, enrollment state is untouched and claims resume without re-enrollment.

The cases run through the production broker, transport and Go runtime from the existing `tests/integration/site_mode` harness. Latency and packet-loss injection are excluded; this PR is about interception only.

## Verification

The new cases run in the same CI lanes as the existing site suite, on the exact-head built agent binary. Local runs report pass counts with zero unexpected skips.

## Compatibility

Test-only. No production code changes; if building the interceptor exposes a production defect, the fix is a separate PR with its own specification.
