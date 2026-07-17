# ADR 073: modelmesh bandwidth limiting and topology-aware peer selection

Status: accepted · Date: 2026-07-17 · Related: [ADR 072](072-modelmesh-peer-exchange.md), [ADR 071](071-modelmesh-core.md), [ADR 070](070-moe-fabric-experimental-track.md)

## Context

ADR 072 gave modelmesh a peer layer that fetches a store's missing chunks from
whoever holds them, verified against the signed manifest. It left two things
crude on purpose. Transfer ran flat out, with no regard for the person sitting
at the machine. And it fetched from the first holder in discovery order, with no
regard for whether that holder was next to it on the LAN or across the WAN.

This increment addresses both, and both run into the same constraint: modelmesh
is a leaf in the import DAG. It must not import the agent that knows whether the
user is active, and it must not import the coordinator or agent that measure the
network. So the fix cannot be to reach out and read that state. It has to be
handed in.

## Decision

Add two small modules. The package stays standard-library only and stays a leaf.
Neither module changes the existing core or peer code; the peer layer can adopt
them later, and until it does they are inert.

**Bandwidth limiting while the user is active.** The reason to spread a model
over the LAN is to keep it off the uplink, but LAN transfer still competes with
whatever the person is doing on that machine. So transfer runs at full rate while
the machine is idle and drops to a trickle while the user is active. The pacer is
a token bucket that starts full, refills at the rate in force, and caps the
allowance at one second of that rate so an idle gap cannot bank an unbounded
burst. A transfer larger than the allowance waits for the shortfall to refill.

The active/idle signal is injected, not read. modelmesh does not decide what idle
means; ADR 002 and the agent's idle detection do, and a leaf cannot import them.
So the limiter takes a callable that reports the state, plus the clock and the
sleep, all as constructor arguments. That keeps the pacer pure: a fake clock and
a recording sleep make every wait exact and deterministic in tests, with no real
time and no real network. Reading the rate per call means a machine that goes
active partway through a download slows on the next chunk, not the next download.

**Topology-aware peer selection.** When several peers hold a chunk, they are not
equal. A peer on the same LAN costs nothing on the uplink, a nearer peer answers
sooner, and a peer with more spare bandwidth finishes faster. So selection orders
holders by LAN before WAN, then lower latency, then higher bandwidth, and returns
the best. With no holders it returns nothing, the same signal an empty holder
list already gives, so the caller falls back the same way.

The topology is injected for the same reason the idle state is. modelmesh does
not measure latency or learn the LAN layout; the caller supplies a function from
a peer to its metadata (LAN-or-WAN, measured latency, advertised bandwidth), and
tests pin it exactly. The policy is pluggable: it is a function from that metadata
to a sort key, with the LAN-latency-bandwidth ordering as the default, so a
deployment that wants a different preference passes its own without touching the
selector. Sorting is stable, so holders that tie keep discovery order and the
choice stays deterministic.

**Why both take injected inputs.** This is the leaf discipline from ADR 071 and
ADR 072, held again. The import-linter contract fails the build if modelmesh
imports the agent, the coordinator, or the serving path. Idle state and network
topology live behind exactly those boundaries. Injection is how a leaf uses that
knowledge without depending on it: the policy is here, the measurement stays with
whoever owns it, and the seam between them is a plain callable a test can fake.

## Consequences

- The core, the peer layer, the coordinator, and the agent are untouched. The
  leaf contract still fails the build if modelmesh imports any of them.
- Nothing wires these in yet. The limiter is a pacer the transport can call
  around each fetch, and the selector is a ranking the exchange can use to choose
  a holder, but connecting them to a real download belongs with the agent wiring,
  behind its own ADR. This ADR approves the policies, not a production rollout.
- The limiter paces on bytes, so the caller controls granularity by how much it
  hands over per call. Per chunk is the obvious unit and needs no support here.
- Selection reads richer metadata than discovery records today. Where that
  metadata comes from — measured at discovery, cached, or refreshed — is the
  caller's concern, kept out of the leaf on purpose.
