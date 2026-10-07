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

The controller is a control plane and reverse proxy. The demo workloads it
creates are Kubernetes resources, not Juju applications.

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
Controller NodePort
       |
       v
FastAPI controller
  - HMAC authentication
  - replay/rate protection
  - SQLite desired state
  - Vault AppRole/KV client
  - reconciliation loop
  - host-based reverse proxy
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
12. The workflow polls signed status requests until the demo is ready or fails.
13. The workflow creates or updates one persistent pull-request comment.

## Destroy flow

1. Pull-request close/merge invokes `.github/workflows/demo-cleanup.yaml`.
2. The workflow signs `POST /api/v1/destroy`.
3. SQLite changes the desired state to absent.
4. The reconciler deletes the Deployment, Service, ConfigMap, and Secret.
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
controller NodePort. The controller then:

1. reads the incoming `Host`;
2. requires it to end with the configured suffix;
3. finds an active persisted record for the exact hostname;
4. resolves the controller-owned ClusterIP Service;
5. proxies the request inside the model.

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
  but leaves the previous workload routable.
- A failed replacement does not deliberately delete the previous Kubernetes
  resources.
- A deletion failure retains a failed record so reconciliation can retry.
- Expired records are treated as desired absent.

## Trust boundaries

| Boundary | Control |
|---|---|
| GitHub workflow to controller | Repository-specific HMAC, timestamp, nonce, replay fingerprint |
| Repository to image | Exact lowercase repository namespace and immutable digest |
| Vault authorization | AppRole policy plus optional `image_namespace` |
| Controller to Kubernetes | Trusted charm and ownership labels |
| Internet to demo | HAProxy wildcard plus exact host lookup in SQLite |
| Demo pod | Non-root policy, runtime-default seccomp, no privilege escalation, all capabilities dropped |

See the ADRs for why these boundaries were selected.
