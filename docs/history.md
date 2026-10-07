# Implementation history and lessons

This document preserves context that does not belong in normative architecture
documents. It describes what was built and observed during the initial
implementation. External state may have changed since then.

## Original problem

The previous review-app flow combined GitHub hooks, Jenkins,
`start-demo.sh`, Docker, `konf`, Terraform, and direct Kubernetes operations.
The requested replacement needed to use rocks, charms, Juju, and an existing
offered HAProxy ingress while requiring minimal target-repository changes.

Early exploration considered:

- one Juju application per PR;
- adding one HAProxy relation entry per PR;
- a new configuration relation interface;
- a wildcard ingress with a controller-side proxy.

The accepted design was one trusted controller charm, one wildcard route, plain
controller-owned Kubernetes demo resources, and Vault-based project records.

## Major implementation milestones

1. Built FastAPI deploy, destroy, and status APIs.
2. Added repository-specific HMAC authentication and persistent replay data.
3. Added strict request and Vault configuration models.
4. Added SQLite desired state and a periodic reconciliation loop.
5. Added Vault AppRole login, token renewal, and KV v2 lookup.
6. Added typed Lightkube rendering and reconciliation.
7. Added host-based reverse proxying.
8. Added the Kubernetes charm, Pebble service, persistent storage, and
   `haproxy-route` integration.
9. Added reusable GitHub deploy/cleanup workflows and signed client.
10. Built fake Vault and non-root demo fixtures.
11. Verified locally, then through a real production-style GitHub and ingress
    lifecycle.
12. Flattened the controller Python project into the repository root and
    renamed `controller-charm/` to `charm/`.

## Bugs found through testing

The following details are worth retaining because each represents a plausible
regression:

- The FastAPI extension generated a Pebble service named `fastapi`; the charm
  must manage that service name.
- Uvicorn must bind to `0.0.0.0:8080`, not its loopback/default port.
- `haproxy-route` application and unit values must be JSON encoded.
- The provider expected a unit `address`, not only application route data.
- Kubernetes memory quantities such as `1Gi` need validation that understands
  GiB, not only MiB.
- Lightkube typed security-context models should be used instead of raw nested
  dictionaries.
- The installed Lightkube version did not support a
  `raise_if_not_found` argument on `Client.delete`; explicit 404 handling is
  required.
- Demo workloads run under `runAsNonRoot`; the test rock had to be compatible.
- Retry identity cannot include the changing timestamp/signature. It must
  fingerprint stable request identity while still verifying each signature.
- Vault image namespace checks must accept digest references, not only tags.
- Registry validation must be configurable for isolated local testing while
  remaining exact and repository scoped.
- Direct outbound TCP could time out even when DNS worked; the model's egress
  proxy had to be propagated into the workload.
- CIDR-only `NO_PROXY` was insufficient for Lightkube. The exact
  `KUBERNETES_SERVICE_HOST` fixed Kubernetes API requests being proxied.

## Production verification

The deployed application was `demos-controller` in the
`k8s-marketplace-demos-default` model. It used:

- API hostname `demos-controller.canonical.com`;
- demo suffix `demos.canonical.com`;
- the `ingress-ps7-webdesign` HAProxy route;
- service-scoped Vault configuration;
- a repository-specific HMAC key stored in both Juju and GitHub secret stores.

The testing repository was:

```text
canonical/webteam-juju-demos-testing
```

### Failed and corrective iterations

- PR #15 built successfully but the controller could not reach/authenticate to
  Vault. Pod-side probes showed external HTTPS required the model egress proxy.
- Charmhub revision 4 propagated Juju proxy variables into Pebble.
- Vault then worked, but Kubernetes API access was sent through the proxy and
  failed with HTTP 403.
- Charmhub revision 5 appended the exact Kubernetes API service host to
  `NO_PROXY`.

### Successful lifecycle

PR #19 was used for the complete create-and-cleanup proof:

- immutable rock published;
- demo became ready;
- response verified through HAProxy with `curl --connect-to`;
- PR closed without merging;
- cleanup workflow succeeded;
- single-tag GHCR version deleted;
- old route returned `404 {"detail":"demo is not active"}`.

PR #20 was opened afterward as a persistent manual demonstration and was left
open at the end of the implementation session. Its historical hostname was:

```text
webteam-juju-demos-testing-pr-20.demos.canonical.com
```

Do not assume PR #20, its image, or its demo still exists.

## Repository and remote history

Relevant implementation commits:

```text
b26a747 Implement Vault-backed PR demos controller
edf1fc8 tests
5f6bed3 Fix production controller routing and proxy access
bc9ac8e Bypass proxy for Kubernetes API
b6ee498 Flatten controller repository layout
```

During an explicitly local-only phase, external GitHub changes were made too
early. They were rolled back where possible and the user required future work
to respect local-only boundaries literally. This is why `AGENTS.md` treats
external-system authorization as a hard rule.

At the end of the implementation:

- `canonical/demos-rework` was archived;
- `M7mdisk/demos-rework` was the active private development repository;
- the local `main` branch tracked `personal/main`.

The reusable workflows still referenced `canonical/demos-rework` as their
checkout source. That needs an explicitly authorized migration before future
production use.

## Validation evidence

After the repository layout was flattened:

- 13 controller tests passed;
- 5 charm tests passed;
- Ruff formatting/checks passed;
- the controller rock packed successfully from the repository root;
- the charm packed successfully from `charm/`;
- no stale `controller/` or `controller-charm/` references remained.

Deprecation warnings were observed under Python 3.14 for FastAPI/Starlette
test-client and asyncio test APIs. The declared runtime target is Python 3.12;
these warnings did not fail the suite but should be revisited during dependency
upgrades.

