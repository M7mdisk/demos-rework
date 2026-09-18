from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import ops
import pytest
from charm import DemosControllerCharm
from kubernetes import (
    MANAGED_BY_LABEL,
    OWNER_ANNOTATION,
    KubernetesAdapter,
)
from lightkube import ApiError
from lightkube.resources.core_v1 import Service
from ops.testing import Harness

ROOT = Path(__file__).parents[1]


def make_harness(client: MagicMock | None = None) -> Harness[DemosControllerCharm]:
    harness = Harness(
        DemosControllerCharm,
        meta=(ROOT / "metadata.yaml").read_text(),
        config=(ROOT / "config.yaml").read_text(),
        actions=(ROOT / "actions.yaml").read_text(),
    )
    with patch("charm.Client", return_value=client or MagicMock()):
        harness.begin()
    return harness


def nodeport_client() -> MagicMock:
    client = MagicMock()
    service = MagicMock()
    service.spec.ports = [MagicMock(nodePort=32080)]
    client.get.return_value = service
    node = MagicMock()
    node.status.addresses = [
        MagicMock(type="InternalIP", address="10.10.0.2"),
        MagicMock(type="Hostname", address="worker-0"),
    ]
    client.list.return_value = [node]
    return client


def test_missing_secrets_blocks_charm() -> None:
    harness = make_harness()
    harness.container_pebble_ready("controller")
    assert isinstance(harness.model.unit.status, ops.BlockedStatus)
    harness.cleanup()


def test_configures_workload_and_publishes_wildcard_route() -> None:
    client = nodeport_client()
    harness = make_harness(client)
    harness.set_leader(True)
    relation_id = harness.add_relation("haproxy-route", "haproxy")
    with patch.object(
        DemosControllerCharm,
        "_secret_content",
        side_effect=[
            {"role-id": "role", "secret-id": "secret"},
            {"credentials": '{"canonical/example":"hmac-key"}'},
        ],
    ):
        harness.container_pebble_ready("controller")
    plan = harness.get_container_pebble_plan("controller")
    assert (
        plan.services["fastapi"].environment["DATABASE_PATH"] == "/data/controller.db"
    )
    assert plan.services["fastapi"].environment["UVICORN_HOST"] == "0.0.0.0"
    assert plan.services["fastapi"].environment["UVICORN_PORT"] == "8080"
    data = harness.get_relation_data(relation_id, harness.model.app.name)
    assert {key: json.loads(value) for key, value in data.items()} == {
        "service": "demos-controller-nodeport",
        "ports": [32080],
        "protocol": "http",
        "hosts": ["10.10.0.2"],
        "hostname": "*.demos.canonical.com",
        "paths": ["/"],
        "check": {
            "interval": 10,
            "rise": 2,
            "fall": 3,
            "path": "/readyz",
            "port": 32080,
        },
    }
    service = client.apply.call_args.args[0]
    assert service.spec.selector == {"app.kubernetes.io/name": "demos-controller"}
    assert service.spec.type == "NodePort"
    assert service.metadata.labels[MANAGED_BY_LABEL] == "demos-controller"
    assert service.metadata.annotations[OWNER_ANNOTATION] == "demos-controller"
    assert isinstance(harness.model.unit.status, ops.ActiveStatus)
    harness.cleanup()


def test_forbidden_kubernetes_access_blocks_with_trust_message() -> None:
    client = MagicMock()
    client.apply.side_effect = ApiError(
        status={"code": 403, "message": "forbidden", "status": "Failure"}
    )
    harness = make_harness(client)
    harness.set_leader(True)
    harness.add_relation("haproxy-route", "haproxy")
    assert harness.charm._publish_route(None) is False
    assert isinstance(harness.model.unit.status, ops.BlockedStatus)
    assert "--trust" in harness.model.unit.status.message
    harness.cleanup()


def test_adapter_rejects_non_permission_api_errors() -> None:
    client = MagicMock()
    client.apply.side_effect = ApiError(
        status={"code": 500, "message": "failed", "status": "Failure"}
    )
    adapter = KubernetesAdapter(client, "testing", "demos-controller")
    with pytest.raises(ApiError):
        adapter.reconcile()


def test_cleanup_deletes_only_the_owned_nodeport() -> None:
    client = MagicMock()
    owned = MagicMock()
    owned.metadata.annotations = {OWNER_ANNOTATION: "demos-controller"}
    client.get.return_value = owned
    adapter = KubernetesAdapter(client, "testing", "demos-controller")
    adapter.cleanup()
    client.delete.assert_called_once_with(
        Service, name="demos-controller-nodeport", namespace="testing"
    )

    client.reset_mock()
    foreign = MagicMock()
    foreign.metadata.annotations = {OWNER_ANNOTATION: "another-charm"}
    client.get.return_value = foreign
    adapter.cleanup()
    client.delete.assert_not_called()
