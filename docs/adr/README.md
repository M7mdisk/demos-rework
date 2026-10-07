# Architecture decision records

Accepted decisions:

- [ADR 0001: Use one trusted controller for Kubernetes demo resources](0001-controller-owned-kubernetes-resources.md)
- [ADR 0002: Store project configuration and secrets in Vault](0002-vault-project-configuration.md)
- [ADR 0003: Route one wildcard through the controller proxy](0003-wildcard-controller-proxy.md)
- [ADR 0004: Authenticate workflows with repository HMAC credentials](0004-hmac-workflow-authentication.md)
- [ADR 0005: Require immutable repository-scoped image digests](0005-immutable-repository-images.md)
- [ADR 0006: Persist desired state in SQLite and reconcile](0006-sqlite-desired-state.md)
- [ADR 0007: Propagate egress proxy settings and bypass the Kubernetes API](0007-egress-proxy-and-kubernetes-bypass.md)

ADRs describe decisions and consequences, not necessarily current deployment
state. Supersede an ADR rather than silently rewriting its decision.

