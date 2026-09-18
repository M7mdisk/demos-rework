#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

rockcraft pack
rock_file="$(find . -maxdepth 1 -name 'demos-fixture_*.rock' -print -quit)"
test -n "$rock_file"

rockcraft.skopeo --insecure-policy copy \
  "oci-archive:${rock_file}" \
  docker-daemon:demos-fixture:local

container_id="$(
  docker run --detach --rm \
    --publish 8000:8000 \
    --env PLAIN_MESSAGE="local plain value" \
    --env SECRET_MESSAGE="local secret value" \
    --env DEMO_REVISION="local-rock" \
    demos-fixture:local
)"
trap 'docker rm --force "$container_id" >/dev/null 2>&1 || true' EXIT

curl --fail --retry 30 --retry-delay 1 --retry-connrefused \
  http://127.0.0.1:8000/readyz
curl --fail http://127.0.0.1:8000/
