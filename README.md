# Canonical PR demos controller

This repository contains the controller-based replacement for the legacy
Terraform, per-PR Juju application, Docker, Jenkins, and direct Kubernetes demo
flows.

The repository root contains the FastAPI deployment API, Vault client,
desired-state store, Kubernetes reconciler, reverse proxy, tests, and workload
rock. The remaining components are:

- `charm/`: the trusted Kubernetes charm and `haproxy-route` integration;
- `.github/workflows/`: reusable PR build/deploy and cleanup workflows;
- `tests/fixtures/demo-app/`: a minimal non-root demo rock;
- `tests/integration/fake_vault.py`: an isolated Vault-compatible integration
  fixture.

Project configuration and application secrets are resolved from externally
managed Vault records. GitHub Actions sends only repository identity, PR and
commit metadata, and an immutable repository-scoped image digest.

## Controller API

The workload serves the signed deployment API on port 8080, persists non-secret
desired state in SQLite, resolves project configuration through Vault AppRole
and KV v2, reconciles resources with Lightkube, and proxies active demo hosts.

Requests provide `X-Demos-Repository`, `X-Demos-Timestamp`, `X-Demos-Nonce`,
and `X-Demos-Signature`. The signature is lowercase hex HMAC-SHA256 over:

```text
METHOD\nPATH\nTIMESTAMP\nNONCE\nRAW_BODY
```

`POST /api/v1/deploy` and `/api/v1/destroy` require the nonce to equal the
payload delivery ID. Status is available at
`GET /api/v1/demos/{owner}/{repository}/{pr}`. Every API request is signed.

Vault records at `<mount>/data/<base>/v1/<owner>/<repository>` use schema
version 1 and distinguish `environment` from `secrets`. Secret values are never
written to SQLite.

Images must use immutable digests beneath the authenticated repository path.
The registry defaults to `ghcr.io` and can be overridden with `IMAGE_REGISTRY`
for isolated development registries.

See [`charm/README.md`](charm/README.md) for operator details.
