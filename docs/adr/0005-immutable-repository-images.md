# ADR 0005: Require immutable repository-scoped image digests

- Status: Accepted
- Date: 2025

## Context

Mutable tags can change after authorization and make it impossible to know what
was deployed. A repository credential must not be able to deploy an image from
another namespace.

## Decision

Accept only images shaped as:

```text
<configured-registry>/<authenticated-owner>/<authenticated-repository>@sha256:<digest>
```

The workflow may push a PR-specific tag, but it must inspect that tag and send
the resolved digest to the controller. A Vault record may further constrain the
allowed `image_namespace`.

## Consequences

- Deployment identity is stable and auditable.
- Repository credentials cannot deploy cross-repository images.
- The registry can be overridden for isolated testing, but matching remains
  exact.
- Cleanup must reason about tags separately from the digest deployment record.
- Rebuilding the same commit can produce a new digest and therefore a new
  explicit deployment request.

