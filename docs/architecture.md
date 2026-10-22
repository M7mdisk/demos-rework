# Architecture

## Purpose

The demos controller replaces a legacy Jenkins, Docker, `konf`, Terraform, and
per-PR Juju deployment flow with Canonical tooling:

- GitHub Actions builds rocks.
- A trusted Kubernetes charm operates the controller.
- Juju supplies bootstrap secrets and ingress integration.
- Vault owns per-repository configuration and application secrets.
- The controller creates short-lived Kubernetes resources for pull requests.
- An existing HAProxy deployment exposes both the API and demo hostnames.

The charm embeds a Python control plane and a Caddy reverse proxy. The demo
workloads it creates are Kubernetes resources, not Juju applications. Browser
traffic never passes through Python.

## Components

```text
Pull request event
       |
       v
GitHub Actions
  - checkout exact commit
  - rockcraft pack
  - push to GHCR
  - resolve immutable digest
  - sign API request
       |
       v
HAProxy route
  demos-controller.canonical.com
  *.demos.canonical.com
       |
       v
Controller NodePort :8080
       |
       v
Embedded Caddy
  - exact demo hosts -> ClusterIP Services
  - API hostname -> FastAPI on 127.0.0.1:8081
  - unknown hosts -> empty 404
       |
       v
FastAPI controller
  - HMAC authentication
  - replay/rate protection
  - SQLite desired state
  - Vault AppRole/KV client
  - reconciliation loop
  - Caddy route publication via loopback admin API
       |
       +--------------------+
       |                    |
       v                    v
Vault KV v2          Kubernetes API
per-repository       Deployment
configuration        ClusterIP Service
and secrets          ConfigMap / Secret
```

## Deploy flow

1. A target repository workflow calls `.github/workflows/demo-deploy.yaml`.
2. The workflow checks out the exact 40-character pull-request commit SHA.
3. Rockcraft packs the workload.
4. `rockcraft.skopeo` pushes a PR-specific tag to
   `ghcr.io/<owner>/<repository>`.
5. The workflow inspects the remote image and obtains its `sha256` digest.
6. The standard-library client signs `POST /api/v1/deploy` with the
   repository-specific HMAC key.
7. The API validates:
   - the credential repository;
   - timestamp age and signature;
   - nonce/replay fingerprint;
   - strict request schema;
   - exact repository identity;
   - configured image registry;
   - immutable digest format;
   - nonce equality with `delivery_id`.
8. SQLite records the desired deployment and returns `202 Accepted`.
9. The reconciliation loop reads the repository's Vault KV v2 record.
10. The controller validates configuration and the optional Vault-authorized
    image namespace.
11. It applies a Deployment, ClusterIP Service, and optional ConfigMap/Secret.
12. Once the Deployment is ready, the controller atomically publishes its
    Caddy route before reporting ready. The workflow polls signed status
    requests until the demo is ready or fails.
13. The workflow creates or updates one persistent pull-request comment.

## Destroy flow

1. Pull-request close/merge invokes `.github/workflows/demo-cleanup.yaml`.
2. The workflow signs `POST /api/v1/destroy`.
3. SQLite changes the desired state to absent.
4. The reconciler withdraws the Caddy route, then deletes the Deployment,
   Service, ConfigMap, and Secret. Failed withdrawal defers backend deletion.
5. The record is removed after Kubernetes deletion succeeds.
6. The workflow attempts to delete only GHCR versions that have exactly one tag
   matching `pr-<number>-sha-*`. Multi-tag versions are deliberately retained.

Destroy is idempotent, including when no deployment record exists.
Every accepted deployment also has a fixed three-day safety TTL. Pull-request
close or merge deletes it immediately; the TTL handles missing cleanup events.

## Hostnames and routing

Demo hostnames use:

```text
<repository-name>-pr-<number>.<hostname-suffix>
```

The production suffix used during implementation was `demos.canonical.com`,
producing names such as:

```text
webteam-juju-demos-testing-pr-20.demos.canonical.com
```

The charm publishes one exact API hostname and one wildcard hostname through a
single `haproxy-route` relation. HAProxy sends all matching requests to the
controller NodePort, where Caddy:

1. matches the controller API hostname and forwards it to loopback FastAPI;
2. matches each explicit demo hostname and forwards it to the recorded Service;
3. returns an empty 404 for every unknown hostname.

The reconciler restores routes from SQLite before consulting Vault and publishes
updates through Caddy's loopback-only `POST /load` API. Failed configuration
updates leave the last accepted configuration running. Caddy autosaves that
configuration to `/data/caddy/autosave.json` and starts with `--resume`, so
restarting FastAPI or Caddy does not depend on Vault for route restoration.
No credentials or application secret values are included in routing config.

Caddy retains the public `Host`, sets `X-Forwarded-Host`, and explicitly sets
`X-Forwarded-Proto: https` because external HAProxy terminates HTTPS. This
requires HTTPS-only public ingress; browser-supplied forwarding headers are not
trusted to choose the scheme. Cookies, redirects, compression, streaming,
WebSockets, and hop-by-hop headers are handled by Caddy, not Python.

The upstream `Host` header must stay the public hostname, not the ClusterIP
service DNS name: demo applications that do not trust `X-Forwarded-Host`
build absolute redirect URLs, such as OpenID login return URLs, from the
`Host` header, and a cluster-internal value would break the login flow after
the SSO round-trip.

This avoids trying to mutate relation data for every pull request and avoids
creating one Juju application per demo.

## Persistence and failure semantics

SQLite uses WAL mode and persists:

- desired presence;
- repository, PR, commit, and image identity;
- delivery ID and request hash;
- lifecycle state and status message;
- Vault version and workload port;
- creation, update, and expiry times;
- nonce fingerprints for replay handling.

Application secret values are not persisted in SQLite.

The reconciliation loop revisits pending, reconciling, ready, failed, and
deleting records. This enables restart recovery and automatic expiry.

Important failure behavior:

- A Vault failure before the first successful deployment marks the record
  failed and exposes no route.
- A Vault failure after a deployment has a known port marks the update failed
  but retains the previous resources and route metadata. Retention is not a
  claim that the backend is available; the error message makes this explicit.
- A failed replacement does not deliberately delete the previous Kubernetes
  resources.
- A route publication failure does not mark a new demo ready or replace the
  last known ready port. A not-yet-ready Deployment never gains a new route.
- A deletion failure retains a failed record so reconciliation can retry.
- Expired records are treated as desired absent.
- Vault transport failures discard the owned HTTP client/pool. Only pre-send
  pool/connect/proxy failures get one immediate retry; ambiguous failures defer
  to the next reconciliation without replaying authentication requests.
- `/statusz` exposes sanitized, cached control-plane diagnostics independently
  of ingress `/readyz`. The charm uses diagnostics for Juju status without
  withdrawing retained routes during a dependency failure.

## Trust boundaries

| Boundary | Control |
|---|---|
| GitHub workflow to controller | Repository-specific HMAC, timestamp, nonce, replay fingerprint |
| Repository to image | Exact lowercase repository namespace and immutable digest |
| Vault authorization | AppRole policy plus optional `image_namespace` |
| Controller to Kubernetes | Trusted charm and ownership labels |
| Internet to demo | HAProxy wildcard plus exact host routes in Caddy |
| Controller to Caddy | Loopback-only admin API, no egress proxy; durable atomic configuration |
| Demo pod | Non-root policy, runtime-default seccomp, no privilege escalation, all capabilities dropped |

See the ADRs for why these boundaries were selected.
