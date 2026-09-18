from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from lightkube.resources.core_v1 import ConfigMap, Secret
from pydantic import ValidationError

from demo_controller.kubernetes import KubernetesReconciler, render_resources
from demo_controller.models import DeployRequest, ProjectConfig
from demo_controller.naming import hostname, resource_name
from demo_controller.store import DemoRecord


def test_deploy_requires_matching_immutable_image() -> None:
    request = DeployRequest(
        repository="canonical/example",
        pr=42,
        commit_sha="a" * 40,
        image=f"ghcr.io/canonical/example@sha256:{'b' * 64}",
        delivery_id="delivery-42",
    )
    assert request.repository == "canonical/example"
    with pytest.raises(ValidationError):
        DeployRequest(
            repository="canonical/example",
            pr=42,
            commit_sha="a" * 40,
            image=f"ghcr.io/canonical/other@sha256:{'b' * 64}",
            delivery_id="delivery-43",
        )
    local = DeployRequest(
        repository="canonical/example",
        pr=42,
        commit_sha="a" * 40,
        image=f"localhost:32000/canonical/example@sha256:{'b' * 64}",
        delivery_id="delivery-44",
    )
    assert local.image.startswith("localhost:32000/")


def test_project_config_is_strict_and_separates_secrets() -> None:
    project = ProjectConfig(
        schema_version=1,
        enabled=True,
        environment={"PUBLIC_VALUE": "visible"},
        secrets={"PRIVATE_VALUE": "hidden"},
    )
    assert project.port == 8000
    assert project.resources.memory_limit == "1Gi"
    ProjectConfig(
        schema_version=1,
        enabled=True,
        resources={"memory_request": "1Mi", "memory_limit": "1Gi"},
    )
    with pytest.raises(ValidationError):
        ProjectConfig(schema_version=1, enabled=True, unexpected=True)
    with pytest.raises(ValidationError):
        ProjectConfig(
            schema_version=1,
            enabled=True,
            environment={"SAME": "one"},
            secrets={"SAME": "two"},
        )


def test_resource_rendering_projects_plain_and_secret_values() -> None:
    record = DemoRecord(
        repository="canonical/example",
        pr=7,
        commit_sha="a" * 40,
        image=f"ghcr.io/canonical/example@sha256:{'b' * 64}",
        delivery_id="delivery-7",
        request_hash="hash",
        desired_present=True,
        state="pending",
        vault_version=None,
        port=None,
        message="",
        created_at=1,
        updated_at=1,
        expires_at=9999999999,
    )
    project = ProjectConfig(
        schema_version=1,
        enabled=True,
        environment={"PUBLIC": "hello"},
        secrets={"PRIVATE": "world"},
        health_path="/healthz",
    )
    rendered = render_resources(record, project, 3, "demo-model", "demos-controller")
    rendered.deployment.to_dict()
    name = resource_name(record.repository, record.pr)
    assert rendered.config_map.data == {"PUBLIC": "hello"}
    assert base64.b64decode(rendered.secret.data["PRIVATE"]).decode() == "world"
    assert rendered.deployment.metadata.name == name
    assert (
        rendered.deployment.spec.template.metadata.annotations["demos.canonical.com/vault-version"]
        == "3"
    )
    assert rendered.service.spec.selector == {"app": name}
    assert hostname("canonical/example", 7, "demos.canonical.com").endswith(".demos.canonical.com")


def test_reconciliation_removes_obsolete_config_and_secret_resources() -> None:
    record = DemoRecord(
        repository="canonical/example",
        pr=7,
        commit_sha="a" * 40,
        image=f"ghcr.io/canonical/example@sha256:{'b' * 64}",
        delivery_id="delivery-7",
        request_hash="hash",
        desired_present=True,
        state="ready",
        vault_version=2,
        port=8000,
        message="",
        created_at=1,
        updated_at=1,
        expires_at=9999999999,
    )
    rendered = render_resources(
        record,
        ProjectConfig(schema_version=1, enabled=True),
        3,
        "demo-model",
        "demos-controller",
    )
    client = MagicMock()
    client.get.return_value = rendered.deployment
    rendered.deployment.status = MagicMock(
        observedGeneration=rendered.deployment.metadata.generation,
        availableReplicas=1,
    )

    KubernetesReconciler(client, "demo-model", "demos-controller").apply(rendered)

    deleted_types = [call.args[0] for call in client.delete.call_args_list]
    assert deleted_types == [ConfigMap, Secret]
