"""Fetch Pebble service logs from a demo workload pod.

Demo workloads are plain Kubernetes Deployments, so their service output is
kept in each pod's in-container Pebble log store and never reaches the
container stdout that the log API exposes. This module execs ``pebble logs``
into the demo pod through the Kubernetes exec websocket API, using the
charm's in-cluster service-account credentials.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import urlencode

import httpx
from httpx_ws import HTTPXWSException, WebSocketDisconnect, connect_ws
from lightkube.models.core_v1 import Pod
from lightkube.resources.core_v1 import Pod as PodResource

PR_LABEL = "demos.canonical.com/pr"
REPOSITORY_HASH_LABEL = "demos.canonical.com/repository-hash"
WORKLOAD_CONTAINER = "workload"
DEFAULT_TAIL = 200
MAX_TAIL = 2000
# Juju action results are size limited; stop well below the RPC limit.
MAX_RESULT_BYTES = 64_000
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class DemoLogsError(RuntimeError):
    """Raised when demo workload logs cannot be fetched."""


class _PodLister(Protocol):
    def list(self, resource: type[PodResource], *, labels: dict[str, str]) -> Iterable[Pod]: ...


def repository_hash(repository: str) -> str:
    """Match the controller's demos.canonical.com/repository-hash label."""
    return hashlib.sha256(repository.encode()).hexdigest()[:16]


def demo_pod_selector(repository: str, pr: int) -> dict[str, str]:
    return {REPOSITORY_HASH_LABEL: repository_hash(repository), PR_LABEL: str(pr)}


def select_demo_pod(pods: Iterable[Pod]) -> Pod | None:
    """Return the newest running, not-deleting pod of a demo rollout."""
    candidates = [
        pod
        for pod in pods
        if pod.status
        and pod.status.phase == "Running"
        and pod.status.startTime
        and pod.metadata.deletionTimestamp is None
    ]
    return max(candidates, key=lambda pod: pod.status.startTime, default=None)


def exec_url(namespace: str, pod_name: str, tail: int, api_host: str, api_port: str) -> str:
    params = urlencode(
        [
            ("command", "pebble"),
            ("command", "logs"),
            ("command", "-n"),
            ("command", str(tail)),
            ("container", WORKLOAD_CONTAINER),
            ("stdin", "false"),
            ("stdout", "true"),
            ("stderr", "true"),
            ("tty", "false"),
        ]
    )
    path = f"/api/v1/namespaces/{namespace}/pods/{pod_name}/exec"
    return f"wss://{api_host}:{api_port}{path}?{params}"


def decode_log_frame(data: bytes) -> str | None:
    """Decode one exec channel frame, dropping protocol control output.

    Channel 3 carries the final exec status object instead of log output,
    and some API servers deliver that status on a data channel as well.
    """
    output = data[1:].decode(errors="replace")
    if data[0] == 3 or output.startswith('{"metadata"'):
        return None
    return output


def fetch_demo_logs(client: _PodLister, repository: str, pr: int, tail: int = DEFAULT_TAIL) -> str:
    """Return the last ``tail`` Pebble log lines of a repository's demo pod."""
    tail = min(max(tail, 1), MAX_TAIL)
    pods = list(client.list(PodResource, labels=demo_pod_selector(repository, pr)))
    pod = select_demo_pod(pods)
    if pod is None:
        raise DemoLogsError(f"no running demo pod for {repository}#{pr}")
    return _exec_pebble_logs(pod.metadata.namespace, pod.metadata.name, tail)


def _exec_pebble_logs(namespace: str, pod_name: str, tail: int) -> str:
    try:
        with open(f"{SA_DIR}/token") as stream:
            token = stream.read()
        api_host = os.environ["KUBERNETES_SERVICE_HOST"]
        api_port = os.environ["KUBERNETES_SERVICE_PORT"]
    except (OSError, KeyError) as error:
        raise DemoLogsError(
            f"in-cluster Kubernetes credentials are unavailable: {error}"
        ) from error
    url = exec_url(namespace, pod_name, tail, api_host, api_port)
    chunks: list[str] = []
    total = 0
    try:
        http = httpx.Client(verify=f"{SA_DIR}/ca.crt")
        with connect_ws(
            url,
            client=http,
            headers={"Authorization": f"Bearer {token}"},
            subprotocols=["v4.channel.k8s.io"],
        ) as websocket:
            while total < MAX_RESULT_BYTES:
                try:
                    data = websocket.receive_bytes()
                except WebSocketDisconnect:
                    break
                if (output := decode_log_frame(data)) is None:
                    continue
                chunks.append(output)
                total += len(output)
    except (HTTPXWSException, httpx.HTTPError, OSError) as error:
        raise DemoLogsError(f"unable to exec into the demo pod {pod_name}: {error}") from error
    return "".join(chunks)
