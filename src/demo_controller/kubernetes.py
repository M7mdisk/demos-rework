from __future__ import annotations

import base64
from dataclasses import dataclass

from lightkube import ApiError, Client
from lightkube.models.apps_v1 import DeploymentSpec
from lightkube.models.core_v1 import (
    Capabilities,
    ConfigMapEnvSource,
    Container,
    ContainerPort,
    EnvFromSource,
    HTTPGetAction,
    PodSecurityContext,
    PodSpec,
    PodTemplateSpec,
    Probe,
    ResourceRequirements,
    SeccompProfile,
    SecretEnvSource,
    SecurityContext,
    ServicePort,
    ServiceSpec,
)
from lightkube.models.meta_v1 import LabelSelector, ObjectMeta
from lightkube.resources.apps_v1 import Deployment
from lightkube.resources.core_v1 import ConfigMap, Secret, Service

from demo_controller.models import ProjectConfig
from demo_controller.naming import repository_labels, resource_name
from demo_controller.store import DemoRecord


@dataclass(frozen=True)
class RenderedResources:
    deployment: Deployment
    service: Service
    config_map: ConfigMap | None
    secret: Secret | None

    def all(self) -> list[object]:
        return [
            resource
            for resource in (self.config_map, self.secret, self.service, self.deployment)
            if resource is not None
        ]


def render_resources(
    record: DemoRecord,
    project: ProjectConfig,
    vault_version: int,
    namespace: str,
    owner: str,
) -> RenderedResources:
    name = resource_name(record.repository, record.pr)
    labels = repository_labels(record.repository, record.pr, owner) | {"app": name}
    metadata = ObjectMeta(name=name, namespace=namespace, labels=labels)
    config_map = (
        ConfigMap(metadata=metadata, data=project.environment) if project.environment else None
    )
    secret = (
        Secret(
            metadata=metadata,
            type="Opaque",
            data={
                key: base64.b64encode(value.encode()).decode()
                for key, value in project.secrets.items()
            },
        )
        if project.secrets
        else None
    )
    env_from: list[EnvFromSource] = []
    if config_map:
        env_from.append(EnvFromSource(configMapRef=ConfigMapEnvSource(name=name)))
    if secret:
        env_from.append(EnvFromSource(secretRef=SecretEnvSource(name=name)))
    resources = project.resources
    deployment = Deployment(
        metadata=metadata,
        spec=DeploymentSpec(
            replicas=1,
            selector=LabelSelector(matchLabels={"app": name}),
            template=PodTemplateSpec(
                metadata=ObjectMeta(
                    labels=labels,
                    annotations={"demos.canonical.com/vault-version": str(vault_version)},
                ),
                spec=PodSpec(
                    containers=[
                        Container(
                            name="workload",
                            image=record.image,
                            ports=[ContainerPort(containerPort=project.port, name="http")],
                            envFrom=env_from or None,
                            readinessProbe=Probe(
                                httpGet=HTTPGetAction(path=project.health_path, port="http"),
                                initialDelaySeconds=3,
                                periodSeconds=5,
                                timeoutSeconds=2,
                                failureThreshold=6,
                            ),
                            resources=ResourceRequirements(
                                requests={
                                    "cpu": resources.cpu_request,
                                    "memory": resources.memory_request,
                                },
                                limits={
                                    "cpu": resources.cpu_limit,
                                    "memory": resources.memory_limit,
                                },
                            ),
                            securityContext=SecurityContext(
                                allowPrivilegeEscalation=False,
                                capabilities=Capabilities(drop=["ALL"]),
                            ),
                        )
                    ],
                    securityContext=PodSecurityContext(
                        runAsNonRoot=True,
                        seccompProfile=SeccompProfile(type="RuntimeDefault"),
                    ),
                ),
            ),
        ),
    )
    service = Service(
        metadata=metadata,
        spec=ServiceSpec(
            type="ClusterIP",
            selector={"app": name},
            ports=[ServicePort(name="http", port=project.port, targetPort="http")],
        ),
    )
    return RenderedResources(deployment, service, config_map, secret)


class KubernetesReconciler:
    def __init__(self, client: Client, namespace: str, owner: str):
        self.client = client
        self.namespace = namespace
        self.owner = owner

    def apply(self, resources: RenderedResources) -> bool:
        for resource in resources.all():
            self.client.apply(resource, field_manager=self.owner, force=True)
        name = resources.deployment.metadata.name
        if resources.config_map is None:
            self._delete_if_exists(ConfigMap, name)
        if resources.secret is None:
            self._delete_if_exists(Secret, name)
        deployment = self.client.get(Deployment, name=name, namespace=self.namespace)
        status = deployment.status
        desired = deployment.spec.replicas or 1
        return bool(
            status
            and status.observedGeneration == deployment.metadata.generation
            and (status.availableReplicas or 0) >= desired
        )

    def delete(self, repository: str, pr: int) -> None:
        name = resource_name(repository, pr)
        for resource_type in (Deployment, Service, ConfigMap, Secret):
            self._delete_if_exists(resource_type, name)

    def _delete_if_exists(self, resource_type: type, name: str) -> None:
        try:
            self.client.delete(
                resource_type,
                name=name,
                namespace=self.namespace,
            )
        except ApiError as error:
            if error.status.code != 404:
                raise
