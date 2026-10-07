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

## Charm deployment requirements

- Kubernetes model.
- `--trust`, because the charm manages Services and discovers Nodes.
- Persistent `state` storage mounted at `/data`.
- A `haproxy-route` relation to the external ingress offer.
- A Vault AppRole Juju secret.
- An HMAC credentials Juju secret.
- Network egress to Vault and any other required external services.

The charm workload runs:

```text
/bin/python3 -m uvicorn app:app
```

with working directory `/app`, listening on `0.0.0.0:8080`.

Useful action:

```bash
juju run demos-controller/leader show-endpoint
```

Restart/reconcile:

```bash
juju run demos-controller/leader reconcile
```

## Health and status

Unauthenticated controller probes:

```text
GET /healthz
GET /readyz
```

`/readyz` confirms that SQLite can be queried. It does not guarantee Vault,
Kubernetes, every demo backend, or external ingress are healthy.

Repository demo status is authenticated:

```text
GET /api/v1/demos/<owner>/<repository>/<pr>
```

Lifecycle states:

- `pending`: accepted but not yet reconciled;
- `reconciling`: loading config, applying resources, or waiting for readiness;
- `ready`: Deployment reports available replicas;
- `failed`: the latest reconciliation or deletion attempt failed;
- `deleting`: desired state is absent and resources are being removed.

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
It also appends the exact `KUBERNETES_SERVICE_HOST` value to `NO_PROXY`.

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

This is expected resilience behavior. The status reflects the failed update,
while stored route metadata keeps the previous deployment reachable. Fix the
configuration/image and submit a new delivery.

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
