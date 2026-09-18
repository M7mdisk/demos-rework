# Demo app fixture

This directory is a self-contained Rockcraft project for exercising the demos
controller without modifying an application repository. Pebble starts a
dependency-free Python HTTP service on port `8000`.

The root endpoint reports `PLAIN_MESSAGE`, `DEMO_REVISION`, and whether
`SECRET_MESSAGE` was supplied. It exposes only a short SHA-256 fingerprint of
the secret, never the value. `/healthz` and `/readyz` return readiness status.

Run the source-level contract check:

```bash
python3 verify.py
```

Build, import, and smoke-test the rock with:

```bash
./run-local.sh
```

For controller testing, configure the fixture repository's Vault record with
port `8000`, readiness path `/readyz`, a plain `PLAIN_MESSAGE`, and a secret
`SECRET_MESSAGE`. Set `DEMO_REVISION` differently between builds to verify
rollouts.

Projects call the reusable workflows from small event wrappers. The deploy
wrapper should grant only the permissions the called workflow needs:

```yaml
name: PR demo
on:
  pull_request:
    types: [opened, reopened, synchronize]
permissions:
  contents: read
  packages: write
  pull-requests: write
jobs:
  demo:
    uses: canonical/demos-rework/.github/workflows/demo-deploy.yaml@main
    with:
      pr-number: ${{ github.event.pull_request.number }}
      commit-sha: ${{ github.event.pull_request.head.sha }}
      api-url: ${{ vars.DEMOS_API_URL }}
    secrets:
      demos-hmac-key: ${{ secrets.DEMOS_HMAC_KEY }}
```

Use a separate closed-event wrapper for cleanup:

```yaml
name: PR demo cleanup
on:
  pull_request:
    types: [closed]
permissions:
  contents: read
  packages: write
  pull-requests: write
jobs:
  cleanup:
    uses: canonical/demos-rework/.github/workflows/demo-cleanup.yaml@main
    with:
      pr-number: ${{ github.event.pull_request.number }}
      api-url: ${{ vars.DEMOS_API_URL }}
    secrets:
      demos-hmac-key: ${{ secrets.DEMOS_HMAC_KEY }}
```

Repository secrets are unavailable to pull requests from forks. Those pull
requests therefore cannot publish or deploy demos unless the repository adopts
a separately reviewed, trusted workflow.
