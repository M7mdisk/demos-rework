# ADR 0001: Use one trusted controller for Kubernetes demo resources

- Status: Accepted
- Date: 2025

## Context

Each pull request needs an isolated, short-lived workload. Creating a complete
Juju application per pull request would add controller/model churn, relations,
and slow lifecycle operations. The demos need only a Deployment, Service, and
optional configuration objects.

## Decision

Deploy one trusted Kubernetes charm containing a long-running controller. The
controller uses Lightkube to own Kubernetes Deployments, ClusterIP Services,
ConfigMaps, and Secrets for demos.

Demo workloads are not Juju applications and do not expose a custom Juju
configuration relation.

## Consequences

- The charm must be deployed with `--trust`.
- The controller must label and name resources deterministically.
- Reconciliation and cleanup are controller responsibilities.
- Juju remains responsible for the controller workload, storage, secrets, and
  ingress relation, not each demo.
- Kubernetes API availability and RBAC are part of controller health.
- Target repository onboarding is smaller and demo startup is faster than
  creating one Juju application per PR.

