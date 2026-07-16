# ADR 052: Replica bind-address safety

**Status:** Accepted

**Date:** 2026-07-16

## Context

Each agent starts llama-server replicas on its configured `bind_host` and a
port from its replica range. llama-server does not authenticate clients. A
wildcard address would expose the replica on every network interface, including
interfaces outside the tailnet.

The supervisor already passed `bind_host` to llama-server, but the old guard
only rejected the exact IPv4 value `0.0.0.0`. It did not reject an empty value,
the IPv6 wildcard, or equivalent unspecified IP address spellings.

## Decision

Agent settings and supervisor construction use one bind-address validator. It
rejects empty addresses, wildcard tokens, and any IP address that the standard
library classifies as unspecified. The error explains that an all-interface
bind would expose an unauthenticated llama-server.

The supervisor continues to pass the validated address through the `--host`
argument. Loopback addresses remain valid for development on one machine.
Production deployments use the agent's tailnet IP.

## Alternatives

Relying on deployment guidance would leave a single configuration mistake able
to expose a replica. Adding authentication inside llama-server is outside the
agent's control and would not make a wildcard bind an appropriate default.

## Consequences

Unsafe configurations fail before the agent starts any replica. Existing
loopback and tailnet configurations continue to work. Hostnames remain valid,
but explicit wildcard forms do not.

## Verification

Settings and supervisor tests cover empty values, IPv4 and IPv6 unspecified
addresses, wildcard tokens, loopback, and a tailnet address. Command-building
tests assert that the validated host reaches llama-server's `--host` argument.
