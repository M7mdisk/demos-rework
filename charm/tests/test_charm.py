from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import ops
import pytest
from demo_logs import MAX_TAIL, DemoLogsError
from kubernetes import (
    MANAGED_BY_LABEL,
    OWNER_ANNOTATION,
    KubernetesAdapter,
)
from lightkube import ApiError
from lightkube.resources.core_v1 import Service
from ops.testing import ActionFailed, ExecResult, Harness

from charm import DemosControllerCharm

ROOT = Path(__file__).parents[1]


def healthy_status():
    return {
        "status": "ok",
        "database": "ok",
        "reconciliation": {"state": "running"},
        "dependencies": {
            "vault": {"status": "ok"},
            "kubernetes": {"status": "ok"},
            "caddy": {"status": "ok"},
        },
    }


def make_harness(client: MagicMock | None = None) -> Harness[DemosControllerCharm]:
    harness = Harness(
        DemosControllerCharm,
        meta=(ROOT / "metadata.yaml").read_text(),
        config=(ROOT / "config.yaml").read_text(),
        actions=(ROOT / "actions.yaml").read_text(),
    )
    with patch("charm.Client", return_value=client or MagicMock()):
        harness.begin()
    harness.handle_exec(
        "controller", ["/bin/python3"], result=ExecResult(stdout=json.dumps(healthy_status()))
    )
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


def test_configures_workload_and_publishes_api_and_wildcard_route(
    monkeypatch,
) -> None:
    monkeypatch.setenv("JUJU_CHARM_HTTPS_PROXY", "http://egress.internal:3128")
    monkeypatch.setenv("JUJU_CHARM_NO_PROXY", "localhost,.svc")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.86.0.1")
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
    assert plan.services["fastapi"].environment["DATABASE_PATH"] == "/data/controller.db"
    assert plan.services["fastapi"].environment["UVICORN_HOST"] == "127.0.0.1"
    assert plan.services["fastapi"].environment["UVICORN_PORT"] == "8081"
    assert "MAX_DEMO_LIFETIME" not in plan.services["fastapi"].environment
    assert plan.services["fastapi"].environment["HTTPS_PROXY"] == "http://egress.internal:3128"
    assert plan.services["fastapi"].environment["NO_PROXY"] == "localhost,127.0.0.1,.svc,10.86.0.1"
    assert plan.services["caddy"].command == "/bin/python3 -m demo_controller.routing"
    assert plan.services["caddy"].environment == {
        "API_HOSTNAME": "demos-controller.canonical.com",
        "KUBERNETES_NAMESPACE": harness.model.name,
        "HOSTNAME_SUFFIX": "demos.canonical.com",
        "XDG_CONFIG_HOME": "/data",
        "XDG_DATA_HOME": "/data",
    }
    assert plan.checks["ready"].http["url"] == "http://localhost:8080/readyz"
    data = harness.get_relation_data(relation_id, harness.model.app.name)
    assert {key: json.loads(value) for key, value in data.items()} == {
        "service": "demos-controller-nodeport",
        "ports": [32080],
        "protocol": "http",
        "hosts": ["10.10.0.2"],
        "hostname": "demos-controller.canonical.com",
        "additional_hostnames": ["*.demos.canonical.com"],
        "paths": ["/"],
        "check": {
            "interval": 10,
            "rise": 2,
            "fall": 3,
            "path": "/readyz",
            "port": 32080,
        },
    }
    unit_data = harness.get_relation_data(relation_id, harness.model.unit.name)
    assert json.loads(unit_data["address"]) == "10.0.0.10"
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


@pytest.mark.parametrize(
    "failure, status_type, message",
    [
        ("vault", ops.WaitingStatus, "Vault reconciliation degraded"),
        ("kubernetes", ops.WaitingStatus, "Kubernetes reconciliation degraded"),
        ("caddy", ops.WaitingStatus, "Caddy route synchronization degraded"),
        ("database", ops.WaitingStatus, "state database unavailable"),
        ("reconciliation", ops.MaintenanceStatus, "reconciliation loop stopped"),
        ("demo", ops.WaitingStatus, "demo reconciliation degraded"),
    ],
)
def test_degraded_diagnostics_are_visible_without_changing_ingress(failure, status_type, message):
    harness = make_harness(nodeport_client())
    harness.set_leader(True)
    relation_id = harness.add_relation("haproxy-route", "haproxy")
    with patch.object(
        DemosControllerCharm,
        "_environment",
        return_value={
            "API_HOSTNAME": "api.demos.example",
            "KUBERNETES_NAMESPACE": "testing",
            "HOSTNAME_SUFFIX": "demos.example",
        },
    ):
        harness.container_pebble_ready("controller")
        previous_route = dict(harness.get_relation_data(relation_id, harness.model.app.name))
        health = healthy_status()
        health["status"] = "degraded"
        if failure in {"vault", "kubernetes", "caddy"}:
            health["dependencies"][failure]["status"] = "error"
        elif failure == "database":
            health["database"] = "error"
        elif failure == "reconciliation":
            health["reconciliation"]["state"] = "stopped"
        harness.handle_exec(
            "controller", ["/bin/python3"], result=ExecResult(stdout=json.dumps(health))
        )
        harness.charm.on.update_status.emit()
        assert isinstance(harness.model.unit.status, status_type)
        assert message in harness.model.unit.status.message
        assert (
            dict(harness.get_relation_data(relation_id, harness.model.app.name)) == previous_route
        )
        assert json.loads(previous_route["check"])["path"] == "/readyz"
        harness.handle_exec(
            "controller", ["/bin/python3"], result=ExecResult(stdout=json.dumps(healthy_status()))
        )
        harness.charm.on.update_status.emit()
        assert isinstance(harness.model.unit.status, ops.ActiveStatus)
    harness.cleanup()


@pytest.mark.parametrize("output", ["", "not JSON", "{}", "[]", '{"status":"ok"}'])
def test_invalid_diagnostics_do_not_report_active(output):
    harness = make_harness()
    harness.set_can_connect("controller", True)
    harness.handle_exec("controller", ["/bin/python3"], result=ExecResult(stdout=output))
    harness.charm._set_workload_status()
    assert isinstance(harness.model.unit.status, ops.WaitingStatus)
    assert harness.model.unit.status.message == "waiting for controller diagnostics"
    harness.cleanup()


def test_unreachable_diagnostics_do_not_report_active():
    harness = make_harness()
    harness.set_can_connect("controller", True)
    harness.handle_exec("controller", ["/bin/python3"], result=ExecResult(exit_code=1))
    harness.charm._set_workload_status()
    assert isinstance(harness.model.unit.status, ops.WaitingStatus)
    harness.cleanup()


def test_demo_logs_action_returns_logs() -> None:
    harness = make_harness()
    with patch("charm.fetch_demo_logs", return_value="line1\nline2\n") as fetch:
        output = harness.run_action(
            "demo-logs",
            {"repository": "canonical/snapcraft.io", "pr": 5932, "tail": 50},
        )
    assert fetch.call_args.args[1:] == ("canonical/snapcraft.io", 5932, 50)
    assert output.results == {"logs": "line1\nline2\n", "tail": 50}
    harness.cleanup()


def test_demo_logs_action_clamps_tail_to_maximum() -> None:
    harness = make_harness()
    with patch("charm.fetch_demo_logs", return_value="") as fetch:
        harness.run_action("demo-logs", {"repository": "o/r", "pr": 1, "tail": 99_999})
    assert fetch.call_args.args[3] == MAX_TAIL
    harness.cleanup()


def test_demo_logs_action_fails_without_demo_pod() -> None:
    harness = make_harness()
    with (
        patch(
            "charm.fetch_demo_logs",
            side_effect=DemoLogsError("no running demo pod for o/r#1"),
        ),
        pytest.raises(ActionFailed) as failure,
    ):
        harness.run_action("demo-logs", {"repository": "o/r", "pr": 1})
    assert "no running demo pod" in failure.value.message
    harness.cleanup()


def test_demo_logs_action_rejects_invalid_repository() -> None:
    harness = make_harness()
    with patch("charm.fetch_demo_logs") as fetch, pytest.raises(ActionFailed):
        harness.run_action("demo-logs", {"repository": "not-a-repo", "pr": 1})
    fetch.assert_not_called()
    harness.cleanup()
