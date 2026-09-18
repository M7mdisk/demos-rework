# Canonical demos controller charm

Deploy this Kubernetes charm with `--trust`, attach persistent `state` storage,
and relate `haproxy-route` to the existing HAProxy offer. Configure
`vault-approle-secret` with a Juju user secret containing `role-id` and
`secret-id`. Configure `hmac-credentials-secret` with a `credentials` key whose
value is a JSON map from lowercase `owner/repository` identities to HMAC keys.

The charm publishes `*.demos.canonical.com` (configurable) to its port 8080.
Because HAProxy runs outside the Kubernetes model, the charm creates an owned
NodePort Service selecting the Juju application pods and publishes Kubernetes
node `InternalIP` addresses plus the allocated NodePort through JSON-encoded
`haproxy-route` v2 application data. Cluster-wide node discovery and Service
management require deploying the charm with `--trust`.

The workload stores only non-secret desired state in `/data/controller.db`;
application secret values are read from Vault and projected directly into
controller-owned Kubernetes Secrets.

`image-registry` defaults to `ghcr.io`. Override it only for a trusted private
or local registry; repository matching and immutable digest validation remain
mandatory.
