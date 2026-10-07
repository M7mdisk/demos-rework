# ADR 0002: Store project configuration and secrets in Vault

- Status: Accepted
- Date: 2025

## Context

Each repository needs settings analogous to the legacy `konf` data, including
plain environment variables and application secrets. Passing these values in
GitHub workflow payloads would expose too much configuration to CI and couple
configuration changes to repositories. A new Juju relation interface would not
fit because demo workloads are not Juju applications.

## Decision

Store one versioned KV v2 record per lowercase `owner/repository` in externally
managed Vault.

Use a Juju secret only for controller bootstrap:

- Vault AppRole role ID and secret ID;
- repository-to-HMAC-key mapping.

Resolve project records during reconciliation. Project plain environment values
become ConfigMaps; secret values become Kubernetes Secrets. Do not persist
application secret values in SQLite.

## Consequences

- Vault is the source of truth for project configuration.
- Vault policy and service-scoped paths enforce administrative boundaries.
- Configuration can change without modifying the target repository.
- The controller requires robust Vault outage behavior.
- Schema changes require explicit versioning.
- Operators must read before writing and must not overwrite existing records
  with defaults.

