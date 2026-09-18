# Canonical PR demos controller

This repository contains the controller-based replacement for the legacy
Terraform, per-PR Juju application, Docker, Jenkins, and direct Kubernetes demo
flows.

The implementation includes:

- `controller/`: the FastAPI deployment API, Vault client, desired-state store,
  Kubernetes reconciler, reverse proxy, tests, and workload rock;
- `controller-charm/`: the trusted Kubernetes charm and `haproxy-route`
  integration;
- `.github/workflows/`: reusable PR build/deploy and cleanup workflows;
- `tests/fixtures/demo-app/`: a minimal non-root demo rock;
- `tests/integration/fake_vault.py`: an isolated Vault-compatible integration
  fixture.

Project configuration and application secrets are resolved from externally
managed Vault records. GitHub Actions sends only repository identity, PR and
commit metadata, and an immutable repository-scoped image digest.

See [`controller/README.md`](controller/README.md) and
[`controller-charm/README.md`](controller-charm/README.md) for workload and
operator details.
