# Operations runbook

This runbook records the deployment and debugging knowledge learned during the
initial implementation. Commands are examples; verify controller, model,
application, offer, and secret names before executing them.

## Build and test locally

```bash
uv run --extra test pytest -q \
  tests/test_auth_store.py \
  tests/test_models_and_resources.py \
  tests/test_reconciler.py \
  tests/test_vault.py

uv run \
  --with-requirements charm/requirements.txt \
  --with-requirements charm/requirements-test.txt \
  pytest -q charm/tests

uvx --from ruff ruff format --check \
  src tests charm/src charm/tests .github/scripts
uvx --from ruff ruff check \
  src tests charm/src charm/tests .github/scripts
```

Package:

```bash
ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS=1 rockcraft pack
(cd charm && charmcraft pack --quiet)
```

The FastAPI framework extension was experimental when implemented, so the
Rockcraft environment variable is required.

## Publish to Charmhub from GitHub

`.github/workflows/publish-charm.yaml` runs on every push to `main` and can also
be started manually. It calls the SHA-pinned
`canonical/webteam-devops/.github/workflows/pack_release.yaml` reusable
workflow. It packs the root rock, publishes it to GHCR, uploads the OCI resource
to Charmhub, uploads a new charm revision when `charm/` changed, and releases
the selected charm revision and resource to the configured channel.

It does not deploy or refresh a Juju application.

Configure these repository Actions values:

| Type | Name | Example | Purpose |
|---|---|---|---|
| Secret | `CHARMHUB_TOKEN` | redacted | Charmcraft authorization exported by `charmcraft login` |
| Variable | `CHARM_NAME` | `demos-controller` | Charmhub charm name |
| Variable | `CHARMHUB_CHANNEL` | `latest/edge` | Channel receiving each successful release |

The shared workflow expects a generic OCI resource named `app-image`; the charm
metadata uses that name. Charmhub historically had only `controller-image` for
this charm. On the first configured run, the workflow uploads the new charm
revision declaring `app-image` before uploading that resource. Existing
released revisions keep their historical resource attachment.

The manual `force-charm-build` input uploads a new charm revision even when no
file under `charm/` changed. A normal push always packs and publishes the rock,
but the shared workflow reuses the latest charm revision when `charm/` is
unchanged.

## Charm deployment requirements

- Kubernetes model.
- `--trust`, because the charm manages Services and discovers Nodes.
- Persistent `state` storage mounted at `/data`.
- A `haproxy-route` relation to the external ingress offer.
- A Vault AppRole Juju secret.
- An HMAC credentials Juju secret.
- Network egress to Vault and any other required external services.

The charm runs two Pebble services with working directory `/app`:

- `fastapi`: `/bin/python3 -m uvicorn app:app`, bound to `127.0.0.1:8081`;
- `caddy`: `/bin/python3 -m demo_controller.routing`, which generates a bootstrap
  config and execs `/usr/bin/caddy run --resume --config /data/caddy-bootstrap.json`.

Caddy owns port 8080; the NodePort and ingress relation do not change. Its admin
API binds only to `127.0.0.1:2019`. Accepted configurations are autosaved to
`/data/caddy/autosave.json`. Do not delete this file during a control-plane outage:
it lets Caddy resume retained demo routes without SQLite, Vault, or FastAPI.
Caddy receives no Vault/HMAC credentials or egress proxy variables.

Upgrade the charm and workload image together: the old charm expects FastAPI
on port 8080 and does not supervise Caddy. A rollback also requires a matching
charm/image pair. Keep external ingress HTTPS-only; Caddy sends an explicit
`X-Forwarded-Proto: https` to upstreams.

Useful action:

```bash
juju run demos-controller/leader show-endpoint
```

Tail a demo workload's Pebble logs (demo output never reaches the container
log API, because Pebble keeps service output in its in-container store):

```bash
juju run demos-controller/leader demo-logs \
    repository=canonical/snapcraft.io pr=5932 tail=200
```

The action execs `pebble logs` into the demo pod through the Kubernetes
exec API, so it requires the same model access as any other action. `tail`
is capped at 2000 lines and the result is size limited.

Restart/reconcile:

```bash
juju run demos-controller/leader reconcile
```

## Health and status

Unauthenticated controller probes:

```text
GET /healthz
GET /readyz
GET /statusz
```

These endpoints serve different purposes:

- `/healthz`: the HTTP process is alive.
- `/readyz` on the API hostname: SQLite can be queried and FastAPI can serve
  traffic. A database failure returns a sanitized HTTP 503.
- `/readyz` with an empty, localhost, or loopback Host: Caddy's independent
  readiness response, used by HAProxy's hostless probe and Pebble. It remains
  healthy even if FastAPI/SQLite/Vault fail, so retained demo routes stay in
  ingress. On a demo hostname `/readyz` is forwarded to that demo unchanged.
- `/statusz`: cached controller diagnostics, returning HTTP 503 when the
  reconciliation task has stopped, SQLite is unavailable, a dependency's last
  observation failed, or any persisted demo is failed. Otherwise it returns
  HTTP 200. It includes task state, the last completed loop timestamp, aggregate
  demo-state counts, and Vault/Kubernetes/Caddy observation status, timestamp, and
  sanitized error category. It contains no repository identities or secrets.

Diagnostics requests do not authenticate to Vault or call Kubernetes. Dependency
status starts as `unknown` until reconciliation exercises that dependency;
`ok`/`error` describes the last observation, not a new live probe or a guarantee
that every demo backend is healthy. A failed repository lookup does not prove
that the whole Vault service is down. Check `checked_at` and
`last_completed_at` when interpreting old observations.

The charm polls FastAPI's loopback `/statusz` during configuration and
`update-status`. A Vault, Kubernetes, or Caddy publication failure becomes a
Juju waiting status; a stopped
reconciliation task becomes maintenance. These status changes do not remove or
alter the ingress route, whose check remains `/readyz`.

Repository demo status is authenticated:

```text
GET /api/v1/demos/<owner>/<repository>/<pr>
```

Lifecycle states:

- `pending`: accepted but not yet reconciled;
- `reconciling`: loading config, applying resources, or waiting for readiness;
- `ready`: Deployment reports available replicas and its Caddy route was accepted;
- `failed`: the latest reconciliation or deletion attempt failed;
- `deleting`: desired state is absent and resources are being removed.

## Caddy routing verification

Inspect services with `juju ssh --container controller demos-controller/leader
pebble services`. Inspect proxy errors with `pebble logs caddy` in that container.
Do not expose port 2019 or enable verbose request logging containing cookies.

For local protocol regression tests, install Caddy from the Ubuntu archive or
set `CADDY_BINARY` to an extracted executable, then run:

```bash
CADDY_BINARY=/path/to/caddy uv run --extra test pytest -q tests/test_routing.py
```

These tests use local subprocesses and synthetic sessions only. They verify
multiple visitors, repeated cookies, login/logout, original paths and queries,
forwarding headers, WebSockets, rejected config updates, and persisted restart
recovery. Without Caddy the subprocess cases are explicitly skipped.

## Verify through ingress without DNS

The production ingress endpoint observed during implementation was:

```text
ingress-ps7-webdesign.dynamic.admin.canonical.com
```

Controller health:

```bash
curl -k \
  --connect-to demos-controller.canonical.com:443:ingress-ps7-webdesign.dynamic.admin.canonical.com:443 \
  https://demos-controller.canonical.com/healthz
```

Demo:

```bash
curl -k \
  --connect-to <demo-host>:443:ingress-ps7-webdesign.dynamic.admin.canonical.com:443 \
  https://<demo-host>/
```

`-k` was used only for this direct ingress proof. It is not a recommendation to
disable TLS verification in the controller or workflows.

## Proxy-constrained environments

The production Kubernetes model used during implementation blocked direct
internet egress and required:

```text
http://egress.ps7.internal:3128
```

Juju exposed proxy settings to the charm process as:

```text
JUJU_CHARM_HTTP_PROXY
JUJU_CHARM_HTTPS_PROXY
JUJU_CHARM_NO_PROXY
```

The charm maps them to workload `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`.
It also includes `localhost`, `127.0.0.1`, and the exact
`KUBERNETES_SERVICE_HOST` value in `NO_PROXY`. These egress settings apply only
to FastAPI; Caddy uses direct in-cluster upstream connections.

This exact-host bypass is essential. CIDR entries in `NO_PROXY` were not
reliably honored by the HTTP stack, causing Lightkube to send in-cluster
Kubernetes API requests through the egress proxy and receive HTTP 403.

When Vault works but Kubernetes reconciliation reports `ProxyError` or 403:

1. inspect the workload environment;
2. find `KUBERNETES_SERVICE_HOST`;
3. confirm that exact value appears in `NO_PROXY`;
4. restart the Pebble service after correcting the environment.

## Troubleshooting map

### Charm is blocked waiting for secrets

- Confirm `vault-approle-secret` is configured and granted to the application.
- Confirm it contains `role-id` and `secret-id`.
- Confirm `hmac-credentials-secret` is configured and granted.
- Confirm its `credentials` field is valid, non-empty JSON.

### Charm says Kubernetes API access denied

The charm was not deployed with sufficient trust. It needs cluster access to
manage the NodePort adapter, discover node InternalIPs, and create demo
resources.

### HAProxy relation exists but route is unavailable

- Confirm the leader can create/read the NodePort Service.
- Confirm node InternalIP discovery succeeds.
- Confirm all application databag values are JSON strings.
- Confirm unit `address` is present and JSON encoded.
- Confirm `hostname` is the API hostname and `additional_hostnames` contains
  the wildcard.
- Confirm the health check points to `/readyz` and the allocated NodePort.

### Vault authentication fails

- Probe DNS, TCP/443, and HTTPS from inside the workload container.
- Check whether the model requires an egress proxy.
- Confirm the AppRole is valid and its policy permits the service-scoped path.
- Confirm KV v2 mount and base path are not confused with `/data/` API syntax.
- Check logs for the underlying HTTP exception or status.

### Vault requests repeatedly report `PoolTimeout`

A pool timeout means the HTTP client could not obtain a connection; it does not
by itself establish a Vault server outage or rejected credentials. In particular,
failed TLS setup after an HTTP proxy CONNECT can strand unusable pool slots in
httpcore 1.0.9.

The controller serializes Vault requests and replaces its owned HTTP client on
transport failure, closing the failed pool. Pool/connect/proxy setup failures
get at most one retry after a short delay. HTTP error responses are not retried
by this recovery mechanism. Ambiguous read/write failures reset the client for
the next reconciliation but do not replay the current login or renewal. Token
renewal transport failures do not fall through to an additional login.

Replacement preserves TLS verification and the process's proxy/`NO_PROXY`
configuration. A valid cached Vault token is retained across pool replacement.
Persistent failures still produce a sanitized error and a degraded `/statusz`;
the normal reconciliation interval bounds subsequent attempts. Increasing pool
limits or restarting Vault is not a substitute for pool recovery.

The Vault and local Caddy administration clients owned by the controller are
closed on shutdown. Caddy itself remains an independent Pebble service; the
`reconcile` action restarts FastAPI without restarting the proxy.
Injected clients remain the caller's responsibility.

### Vault returns 404

The repository record does not exist at the configured mount/base/versioned
path. Check the exact lowercase owner and repository. Read before creating; do
not overwrite a nearby record.

### Vault returns 403

The AppRole policy does not authorize that path. Do not work around it by
writing into another team's namespace. Use the service-scoped namespace
assigned to the controller or request a policy change.

### Deployment remains reconciling

- Inspect the generated Deployment and pod events.
- Confirm the image can be pulled.
- Confirm the rock runs as non-root.
- Confirm its service port and `health_path` match the Vault record.
- Confirm resource values pass both controller validation and cluster policy.
- Confirm Deployment `observedGeneration` and available replicas.

### Deployment fails after an update but old demo still responds

The controller retains previous resources and route metadata when a Vault
update fails, but this does not verify that the retained backend is healthy.
The failure message includes the sanitized Vault failure and explicitly says
backend availability is not verified. Check Deployment availability and the
actual backend before claiming the previous demo still works. Fix the failure;
the reconciliation loop retries persisted desired state without requiring a new
delivery.

### API rejects a retry as replayed

A repeated nonce is accepted only when repository, method, path, nonce, and raw
body match. The timestamp and signature may be refreshed. A nonce reused with a
different body is rejected.

### Cleanup succeeds but GHCR image remains

Image deletion is best effort and deliberately conservative. The workflow
deletes only versions with exactly one PR tag. Check package permissions and
whether the version has additional tags.

## Recovery behavior

- Controller restart: SQLite desired state is reloaded and reconciled.
- Vault outage: existing ready routes remain available; new deployments fail.
- Kubernetes API outage: records remain for retry.
- Expiry: every accepted deployment is deleted after the fixed three-day TTL
  if pull-request cleanup has not already removed it.
- Missing ConfigMap/Secret data: stale resources are deleted when the new Vault
  version no longer contains the corresponding values.
- Revoked Vault token: one reauthentication attempt is made on HTTP 401/403.

## Production evidence from initial implementation

The full lifecycle was verified with
`canonical/webteam-juju-demos-testing`:

- build and publish immutable GHCR image;
- signed deployment request;
- Vault lookup;
- Kubernetes reconciliation;
- wildcard ingress proxying;
- controller restart recovery;
- Vault outage preservation;
- signed destroy;
- Kubernetes cleanup;
- safe GHCR version deletion;
- route returning inactive after cleanup.

See `docs/history.md` for specific historical PRs and revisions. Do not assume
those resources are still live.
