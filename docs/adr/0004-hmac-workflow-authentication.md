# ADR 0004: Authenticate workflows with repository HMAC credentials

- Status: Accepted
- Date: 2025

## Context

GitHub Actions must request deploy, destroy, and status operations without
granting a global controller credential or trusting request body repository
claims. Retries must be safe, and intercepted requests must not be replayable.

## Decision

Assign one HMAC key per lowercase repository identity. Sign:

```text
METHOD\nPATH\nTIMESTAMP\nNONCE\nRAW_BODY
```

Send repository, timestamp, nonce, and lowercase hex HMAC-SHA256 in request
headers. Require deploy/destroy nonces to equal `delivery_id`.

Persist nonce fingerprints. Allow a repeated nonce only when repository,
method, path, nonce, and raw body are identical. A retry may use a fresh
timestamp and signature.

## Consequences

- A credential authorizes only its mapped repository.
- Request bodies cannot substitute a different repository.
- Delivery retries are idempotent.
- Reusing a delivery ID for a changed request fails closed.
- GitHub and Juju copies of a repository key must be rotated together.
- Timestamp validation requires reasonably synchronized clocks.
- The in-memory rate limit resets on process restart, while replay protection
  persists.

