# ADR 0007: Propagate egress proxy settings and bypass the Kubernetes API

- Status: Accepted
- Date: 2025

## Context

The production Kubernetes model did not permit direct external HTTPS egress.
Juju provided proxy information to the charm process, but the controller
workload did not inherit it. After proxy propagation fixed Vault connectivity,
Lightkube sent the in-cluster Kubernetes API request through the proxy because
CIDR `NO_PROXY` entries were not reliably interpreted, resulting in HTTP 403.

## Decision

Map:

```text
JUJU_CHARM_HTTP_PROXY  -> HTTP_PROXY
JUJU_CHARM_HTTPS_PROXY -> HTTPS_PROXY
JUJU_CHARM_NO_PROXY    -> NO_PROXY
```

in the Pebble workload environment. Append the exact
`KUBERNETES_SERVICE_HOST` value to `NO_PROXY`, deduplicating entries.

## Consequences

- Vault and other external HTTPS traffic can use the model egress proxy.
- Kubernetes API traffic stays in-cluster.
- Proxy environment changes require Pebble replanning/restart.
- Removing the exact host bypass can reproduce misleading Kubernetes 403 or
  `ProxyError` failures even while Vault remains healthy.
- Environment-specific proxy URLs remain deployment configuration and must not
  be hardcoded in application logic.

