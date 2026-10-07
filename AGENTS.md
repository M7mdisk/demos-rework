# Agent guide

This repository implements the Canonical pull-request demos controller. Read
this file before changing code, deployment configuration, GitHub workflows, or
Vault records.

## Non-negotiable rules

- Never commit, print, or document Vault AppRole credentials, HMAC keys, Juju
  secret contents, GitHub tokens, or Vault tokens.
- Do not change external systems unless the user explicitly authorizes it.
  Local-only requests mean no GitHub repository, GitHub secret, Vault,
  Charmhub, Juju, ingress, or DNS changes.
- Do not overwrite an existing Vault record. Read it first, then create only a
  missing record or make the specifically requested field-level change.
- Demo images must remain immutable digest references and must match the
  authenticated repository namespace.
- Preserve an existing ready demo when a replacement cannot be reconciled.
  Vault or Kubernetes failures must not silently remove the last working route.
- Deploy the charm with Kubernetes trust. It creates a NodePort Service,
  discovers node addresses, and creates demo resources through the Kubernetes
  API.
- Keep the exact in-cluster Kubernetes API host in `NO_PROXY`. CIDR-only
  exclusions were insufficient in the production environment.

## Repository map

- `app.py`: ASGI entry point used by the FastAPI Rockcraft extension.
- `src/demo_controller/`: API, authentication, persistence, Vault access,
  reconciliation, Kubernetes rendering, and reverse proxy.
- `charm/`: Kubernetes charm, NodePort adapter, actions, tests, and metadata.
- `.github/workflows/`: reusable deploy and cleanup workflows.
- `.github/workflows/publish-charm.yaml`: main-branch Charmhub publisher using
  the SHA-pinned `canonical/webteam-devops` reusable workflow; it does not
  deploy the charm.
- `.github/scripts/demo_api.py`: dependency-free signed API client.
- `tests/`: controller tests, fake Vault, and demo workload fixture.
- `docs/architecture.md`: end-to-end architecture and data flow.
- `docs/configuration.md`: controller, Juju secret, and Vault schemas.
- `docs/operations.md`: deployment, verification, and troubleshooting runbook.
- `docs/history.md`: implementation history and lessons learned.
- `docs/adr/`: accepted architectural decisions.

## Architectural invariants

1. GitHub Actions builds the target repository's rock and publishes it to GHCR.
2. The workflow resolves the tag to a digest before calling the controller.
3. Every controller API request is authenticated with a repository-specific
   HMAC credential.
4. Repository configuration and application secrets come from Vault KV v2.
5. The controller stores desired state and replay fingerprints in SQLite, but
   never stores application secret values there.
6. The controller owns plain Kubernetes Deployments, Services, ConfigMaps, and
   Secrets. Demo workloads are not Juju applications.
7. HAProxy has one API hostname and one wildcard demo route. The controller
   reverse-proxies each exact demo hostname to its ClusterIP Service.
8. Closing or merging a pull request requests resource deletion and attempts
   safe deletion of the single-tag PR image version.
9. Demo lifetime is fixed at three days. It is not configurable through Vault,
   charm config, or controller environment.

## Development workflow

Use the smallest relevant validation first:

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

The test dependencies are an optional project extra. Plain `uv run pytest` may
invoke a pytest that cannot import the project dependencies; use
`uv run --extra test pytest`.

Package from the repository root:

```bash
ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS=1 rockcraft pack
(cd charm && charmcraft pack --quiet)
```

Generated `.rock`, `.charm`, `.db`, virtual-environment, and cache files are
ignored and must not be committed.

## Change guidance

- API/authentication changes require tests for valid signatures, stale
  signatures, replay behavior, idempotent retries, and repository isolation.
- Vault schema changes require strict model validation and migration/versioning
  consideration. Unknown fields currently fail closed.
- Kubernetes changes must keep the non-root pod policy, dropped capabilities,
  disabled privilege escalation, resource bounds, readiness probe, ownership
  labels, and idempotent deletion.
- Proxy changes must preserve the original path/query, remove hop-by-hop
  headers, set `X-Forwarded-Host`, and never route an unknown hostname.
- Workflow changes must pin third-party actions, check out the exact PR commit,
  avoid persisting credentials, and retain least-privilege permissions.
- Charm publishing requires the `CHARMHUB_TOKEN` Actions secret plus
  `CHARM_NAME` and `CHARMHUB_CHANNEL` repository variables. The shared
  publisher expects the OCI resource name `app-image`.
- Charm relation values for `haproxy-route` are JSON encoded. The unit
  `address` is also JSON encoded.

## Known caveats

- Vault lookup errors intentionally avoid returning secret material, but some
  validation failures are summarized generically. Inspect workload logs for
  the exact failure class.
- Rate limiting is in-memory per controller process; replay protection and
  desired state are persistent in SQLite.
- The reverse proxy buffers request and response bodies. It is suitable for
  review applications, not large streaming or websocket workloads.
- Historical production names and verification evidence are recorded in
  `docs/history.md`. Treat them as observations, not guaranteed current state.
