# ADR 0008: Embed Caddy for workload HTTP forwarding

- Status: Accepted
- Supersedes: Python forwarding implementation in ADR 0003; the existing
  wildcard ingress and NodePort design is unchanged.

## Context

The FastAPI controller used HTTPX to forward browser traffic. A shared client
cookie jar could reuse one visitor's session on another visitor's request.
Fixing that bug also exposed the need to preserve repeated Set-Cookie headers.
A general-purpose API client is not a reverse-proxy implementation: buffering,
compression, connection headers, streaming, and WebSocket upgrades otherwise
remain our responsibility.

## Decision

Package Ubuntu's Caddy package in the controller rock and run it as a second
Pebble service in the same workload container. No extra Juju application,
container resource, external ingress change, or per-demo relation is required.

- Caddy owns the existing public port 8080.
- The exact API hostname routes to FastAPI on `127.0.0.1:8081`.
- Each previously ready demo has an explicit hostname-to-Service route.
- Unknown hosts receive an empty 404. No upstream is derived from arbitrary
  request input.
- Caddy preserves the public Host and sets X-Forwarded-Host and the HTTPS
  scheme. External HAProxy remains the TLS terminator. Public HTTP traffic
  must be redirected/rejected at that ingress.
- Caddy's administration API binds only to `127.0.0.1:2019`. It is not routed
  through the public listener. Administration calls bypass egress proxies.
- The reconciler publishes complete configurations using atomic `POST /load`.
  Caddy validates/provisions updates before replacing a working configuration.
- Caddy autosaves accepted configuration under the existing `/data` volume and
  starts with `--resume`. First boot falls back to a generated bootstrap config.
- Restore persisted route metadata before any Vault lookup. Keep an old route
  while a replacement is unready or reconciliation fails. Publish a new port
  only after Kubernetes readiness and successful Caddy publication.
- Withdraw a route before deleting its Kubernetes backend. If withdrawal
  fails, retain the backend and retry.

The public ingress readiness probe is answered by Caddy, independently of the
Python control plane. It matches only an empty/localhost/loopback Host and
`/readyz`; demo requests for `/readyz` still reach their workload. The public
API hostname's `/readyz` continues to report SQLite/API readiness. The charm
queries FastAPI's `/statusz` directly for control-plane diagnostics, including
Caddy publication failures, without withdrawing working ingress routes.

## Consequences

Python no longer sees or stores browser session cookies and has no workload
request forwarding implementation. Caddy handles HTTP transport, repeated
headers, streaming, and WebSockets. A Caddy configuration contains routing
metadata only, never Vault/HMAC credentials or application secrets.

There is one additional supervised process and an Ubuntu package to maintain.
Route changes take effect during reconciliation rather than a per-request
SQLite lookup. Expiry and deliberate cleanup require a running reconciler;
retained configuration prioritizes serving the last working demos during a
control-plane outage. Persisted routes alone do not guarantee backend health.
Changing the ingress hostname configuration is reconciled after startup; the
autosaved configuration remains authoritative until the next accepted update.

An upgrade must pair the new rock with the new charm: the listener port owner
and Pebble services change together. The old `PROXY_TIMEOUT` option no longer
applies. A rollback must likewise restore a matching charm/image pair.

## Verification

Unit tests cover route generation, first readiness, retained ready ports,
publication failures, restoration before Vault access, withdrawal-before-delete,
and client shutdown. Protocol tests launch the actual packaged Caddy binary
against a local ASGI workload, covering login/session isolation, concurrency,
multiple Set-Cookie headers and logout, redirects, original escaped path/query,
public host/HTTPS headers, hop-by-hop removal, compressed responses, streaming,
WebSockets, rejected updates, and autosaved restart recovery.

Run protocol tests with Caddy installed or set `CADDY_BINARY` to its executable.
Without a binary these subprocess tests are explicitly skipped, not mocked.

References:

- https://caddyserver.com/docs/api
- https://caddyserver.com/docs/command-line#caddy-run
