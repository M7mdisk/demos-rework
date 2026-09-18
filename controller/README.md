# Demo controller workload

The workload serves the signed deployment API on port 8080, persists non-secret
desired state in SQLite, resolves project configuration through Vault AppRole
and KV v2, reconciles resources with Lightkube, and proxies active demo hosts.

Requests provide `X-Demos-Repository`, `X-Demos-Timestamp`, `X-Demos-Nonce`,
and `X-Demos-Signature`. The signature is lowercase hex HMAC-SHA256 over:

```text
METHOD\nPATH\nTIMESTAMP\nNONCE\nRAW_BODY
```

`POST /api/v1/deploy` and `/api/v1/destroy` require the nonce to equal the
payload delivery ID. Status is available at
`GET /api/v1/demos/{owner}/{repository}/{pr}`. Every API request is signed.

Vault records at `<mount>/data/<base>/v1/<owner>/<repository>` use schema
version 1 and distinguish `environment` from `secrets`. Secret values are never
written to SQLite.

Images must use immutable digests beneath the authenticated repository path.
The registry defaults to `ghcr.io` and can be overridden with `IMAGE_REGISTRY`
for isolated development registries.
