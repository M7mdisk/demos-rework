# ADR 0006: Persist desired state in SQLite and reconcile

- Status: Accepted
- Date: 2025

## Context

API requests should return promptly, Kubernetes operations may take time, and
the controller must recover after restarts and transient Vault/Kubernetes
failures. A fully external database would add operational complexity for a
single-controller service.

## Decision

Persist non-secret desired state and replay fingerprints in SQLite on attached
persistent storage. Return `202 Accepted` from mutation APIs and reconcile
records periodically.

Use WAL mode and deterministic `(repository, pr)` identity. Persist enough
successful route metadata to keep a previous deployment available when an
update fails.

## Consequences

- Controller restarts resume reconciliation.
- Mutation APIs are asynchronous and clients must poll status.
- Persistent storage is required.
- The design assumes a single active SQLite writer/controller workload; scaling
  replicas requires a different coordination and persistence design.
- SQLite must never contain application secret values.
- Expiry and deletion are handled by the same reconciliation loop.

