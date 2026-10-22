# Canonical demos controller charm

Deploy this Kubernetes charm with `--trust`, attach persistent `state` storage,
and relate `haproxy-route` to the existing HAProxy offer. The route publishes
the exact `api-hostname` plus the wildcard beneath `hostname-suffix`. Configure
`vault-approle-secret` with a Juju user secret containing `role-id` and
`secret-id`. Configure `hmac-credentials-secret` with a `credentials` key whose
value is a JSON map from lowercase `owner/repository` identities to HMAC keys.

By default, the charm publishes `demos-controller.canonical.com` and
`*.demos.canonical.com` to its port 8080.
Because HAProxy runs outside the Kubernetes model, the charm creates an owned
NodePort Service selecting the Juju application pods and publishes Kubernetes
node `InternalIP` addresses plus the allocated NodePort through JSON-encoded
`haproxy-route` v2 application data. Cluster-wide node discovery and Service
management require deploying the charm with `--trust`.

Pebble manages FastAPI on `127.0.0.1:8081` and embedded Caddy on port 8080.
Caddy's administration API listens only on `127.0.0.1:2019`. The controller
publishes exact-host routes; Caddy handles all demo traffic without storing
browser sessions. `/data/caddy/autosave.json` retains the last accepted routing
configuration across restarts, independently of Vault and FastAPI availability.
Public traffic must arrive through the HTTPS ingress; Caddy explicitly sets
`X-Forwarded-Proto: https` for upstream applications.

The workload stores only non-secret desired state in `/data/controller.db`;
application secret values are read from Vault and projected directly into
controller-owned Kubernetes Secrets.

`image-registry` defaults to `ghcr.io`. Override it only for a trusted private
or local registry; repository matching and immutable digest validation remain
mandatory.

The Charmhub OCI resource is named `app-image` to match the shared
`canonical/webteam-devops` pack-and-release workflow.
