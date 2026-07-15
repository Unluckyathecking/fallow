# ADR 027: Gated benchmark fleet

## Context

The canonical experiment needs three to five distributed agents. Linux is useful for a
temporary CPU fleet, but the ordinary Linux idle detector is deliberately an honest stub.
Pretending that a headless host has real user activity would weaken Fallow's core safety
property if the setting escaped into a normal deployment.

Fleet bootstrap also crosses a trust boundary. Cloud-init and rendered machine bundles
may appear in provider logs, image caches, or build artifacts. Tailscale credentials and
Fallow enrollment tokens must not enter those files.

## Decision

Add `bench.force_idle`, defaulting to `false`. Configuration validation rejects it unless
`bench.enabled` is also `true`, and the idle factory repeats that check before returning a
finite constant-idle detector. Agent startup emits one warning when the setting is active.
The constant detector is still wrapped in `BenchIdleDetector`, so the churn controller can
simulate a user return and exercise the normal preemption path.

Add a provider-neutral scaffold under `experiments/fleet/`. It renders public cloud-init,
agent, and systemd files, validates the bundle, and supports local setup and cleanup on a
host the operator already controls. The scaffold does not call a cloud API or enroll a
machine into a tailnet.

Cloud-init installs public Ubuntu packages only. Tailscale admission happens through an
authenticated operator channel. The one-time Fallow enrollment token is supplied from the
host environment during setup and stored in a root-readable runtime environment file.

Provisioning and spend remain outside the repository workflow. They require an explicit
maintainer decision that names the provider, fleet size, region, machine shape, budget,
teardown deadline, and tailnet-only network boundary.

## Consequences

- Headless Linux agents can participate in a controlled benchmark without claiming to
  detect real user input.
- Two independent checks prevent forced idle outside bench mode.
- Simulated returns still travel through the production preemption state machine.
- Rendered bundles are safe to treat as public, but operators must protect runtime secrets.
- The repository can prepare and test a fleet bundle without creating resources or spend.
- Real provisioning, enrollment, and teardown remain operator responsibilities.

## Verification

Settings, factory, assembly, warning, and simulated-input tests cover the forced-idle
boundary. The fleet scaffold tests reject placeholders and embedded credentials, stage a
runtime token with mode `0600`, and run with network commands replaced by failing stubs.
