# ADR 0003: Route one wildcard through the controller proxy

- Status: Accepted
- Date: 2025

## Context

All traffic must pass through an existing HAProxy deployment exposed by a
cross-model `haproxy-route` offer. Pull-request hostnames are dynamic. A single
static relation is not a natural interface for advertising arbitrary backend
Services per PR, and creating relations/applications per PR would be expensive.

## Decision

Publish:

- one exact controller API hostname;
- one wildcard hostname beneath the configured demo suffix.

Route both through the controller NodePort. The controller inspects the exact
incoming `Host` and proxies active demo hostnames to controller-owned ClusterIP
Services.

## Consequences

- HAProxy relation data changes only when controller ingress configuration
  changes, not for every PR.
- The controller becomes part of the data path.
- The proxy must reject unknown hosts and preserve relevant HTTP semantics.
- The current proxy buffers bodies and does not provide websocket/streaming
  behavior.
- HAProxy can remain outside the Kubernetes model; the charm advertises node
  InternalIPs and the owned NodePort.

