# Configuration and secrets

## Configuration layers

Configuration is split deliberately:

| Layer | Contains | Storage |
|---|---|---|
| Charm config | Hostnames, Vault location/path, limits, registry | Juju application config |
| Bootstrap secrets | Vault AppRole and repository HMAC keys | Juju user secrets |
| Project config | Port, health path, resources, plain environment | Vault KV v2 |
| Project secrets | Application secret environment values | Vault KV v2, projected to Kubernetes Secret |
| Desired state | Repository, PR, image, state, expiry | SQLite |

Do not move application secrets into charm config, GitHub variables, workflow
payloads, or SQLite.

## Charm configuration

The authoritative schema is `charm/config.yaml`.

Important options:

- `api-hostname`: exact public controller hostname.
- `hostname-suffix`: suffix beneath which the wildcard route is advertised.
- `image-registry`: only registry accepted by the API; defaults to `ghcr.io`.
- `vault-address`: Vault HTTPS base URL.
- `vault-namespace`: optional Vault Enterprise namespace.
- `vault-kv-mount`: KV v2 mount name, not an API path.
- `vault-base-path`: base beneath the KV mount.
- `vault-approle-secret`: Juju secret URI containing `role-id` and `secret-id`.
- `hmac-credentials-secret`: Juju secret URI containing a JSON credential map.
- `reconcile-interval`: desired-state loop period.
- `request-max-bytes`: deploy/destroy API body limit.
- `signature-max-age`: accepted clock skew/signature age.
- `rate-limit-per-minute`: authenticated requests per repository per process.

The charm converts these options into controller environment variables and
adds proxy settings supplied by Juju.

## Juju secret shapes

Vault AppRole secret:

```text
role-id=<redacted>
secret-id=<redacted>
```

HMAC credentials secret:

```json
{
  "credentials": "{\"canonical/example-repository\":\"<redacted>\"}"
}
```

The inner value is a JSON object mapping lowercase `owner/repository` names to
HMAC keys. Never include real values in documentation, tests, commits, shell
history intended for sharing, or issue comments.

## Vault path

The logical repository path is:

```text
<vault-base-path>/v1/<owner>/<repository>
```

For KV v2 HTTP access, the complete API path is:

```text
<vault-address>/v1/<vault-kv-mount>/data/<vault-base-path>/v1/<owner>/<repository>
```

The production convention discovered during implementation used a
service-scoped base path:

```text
services/k8s-webteam-demos-default/demos-controller
```

Therefore the testing repository record was located logically at:

```text
secret/services/k8s-webteam-demos-default/demos-controller/v1/canonical/webteam-juju-demos-testing
```

Vault UI links generally use:

```text
<vault-address>/ui/vault/secrets/<mount>/show/<path-without-mount>
```

For the historical testing record:

```text
https://vault.ps7.admin.canonical.com/ui/vault/secrets/secret/show/services/k8s-webteam-demos-default/demos-controller/v1/canonical/webteam-juju-demos-testing
```

Treat this as environment-specific and verify it before use.

## Vault record schema

```json
{
  "schema_version": 1,
  "enabled": true,
  "environment": {
    "PUBLIC_SETTING": "value"
  },
  "secrets": {
    "PRIVATE_SETTING": "<redacted>"
  },
  "port": 8000,
  "health_path": "/",
  "resources": {
    "cpu_request": "50m",
    "cpu_limit": "1",
    "memory_request": "128Mi",
    "memory_limit": "1Gi"
  },
  "image_namespace": "canonical/example-repository"
}
```

Validation is strict:

- unknown fields are rejected;
- `schema_version` must be `1`;
- disabled records are rejected;
- environment names must match `[A-Z_][A-Z0-9_]*`;
- plain and secret names cannot overlap;
- the port must be in `1024..65535`;
- the health path must be absolute and cannot contain `..`;
- CPU and memory values are bounded by controller policy;
- `image_namespace`, when set, must match the immutable image namespace.

Plain environment values become a ConfigMap. Secret values become an Opaque
Kubernetes Secret and are exposed with `envFrom`.

Demo lifetime is not configurable. Each accepted deployment expires exactly
three days after the request unless pull-request cleanup deletes it first.

## Target repository configuration

A target repository needs:

- a GitHub Actions secret containing its repository-specific HMAC key;
- an API URL variable or workflow input;
- pull-request workflows that call the reusable deploy and cleanup workflows;
- a `rockcraft.yaml` at the configured `rockcraft-path`;
- a matching Vault record.

During production verification the conventional names were:

```text
Secret:   DEMOS_HMAC_KEY
Variable: DEMOS_API_URL
```

The HMAC key in GitHub and the matching entry in the Juju HMAC secret must be
identical. Rotate them together.

## Safe Vault change procedure

1. Read the current record and metadata version.
2. Confirm the owner/repository and service-scoped path.
3. Do not write if the task is local-only.
4. Preserve unknown operationally significant values by making a field-level
   update rather than replacing the whole document.
5. Never overwrite an existing record merely to install defaults.
6. Validate the proposed document against `ProjectConfig`.
7. Write the new version.
8. Observe controller status and workload readiness.
9. Roll back by restoring the prior KV version if necessary.
