from __future__ import annotations

from dataclasses import dataclass

from lightkube import ApiError, Client
from lightkube.models.core_v1 import ServicePort, ServiceSpec
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.core_v1 import Node, Service

OWNER_ANNOTATION = "demos.canonical.com/owned-by"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"


class KubernetesPermissionError(RuntimeError):
    """Raised when the charm needs trust to manage cluster resources."""


@dataclass(frozen=True)
class NodePortEndpoint:
    service_name: str
    hosts: list[str]
    port: int


class KubernetesAdapter:
    def __init__(self, client: Client, namespace: str, app_name: str):
        self.client = client
        self.namespace = namespace
        self.app_name = app_name
        self.service_name = f"{app_name}-nodeport"

    def reconcile(self) -> NodePortEndpoint:
        service = Service(
            metadata=ObjectMeta(
                name=self.service_name,
                namespace=self.namespace,
                labels={
                    MANAGED_BY_LABEL: self.app_name,
                    "app.kubernetes.io/name": self.service_name,
                },
                annotations={OWNER_ANNOTATION: self.app_name},
            ),
            spec=ServiceSpec(
                type="NodePort",
                selector={"app.kubernetes.io/name": self.app_name},
                ports=[
                    ServicePort(
                        name="http",
                        port=8080,
                        targetPort=8080,
                        protocol="TCP",
                    )
                ],
            ),
        )
        try:
            self.client.apply(service, field_manager=self.app_name, force=True)
            current = self.client.get(Service, name=self.service_name, namespace=self.namespace)
            nodes = self.client.list(Node)
        except ApiError as error:
            self._raise_for_api_error(error)
            raise
        ports = current.spec.ports if current.spec else None
        node_port = ports[0].nodePort if ports else None
        if not node_port:
            raise RuntimeError("controller NodePort has not been allocated")
        hosts = sorted(
            {
                address.address
                for node in nodes
                if node.status and node.status.addresses
                for address in node.status.addresses
                if address and address.type == "InternalIP" and address.address
            }
        )
        if not hosts:
            raise RuntimeError("no Kubernetes node InternalIP addresses are available")
        return NodePortEndpoint(self.service_name, hosts, node_port)

    def cleanup(self) -> None:
        try:
            service = self.client.get(Service, name=self.service_name, namespace=self.namespace)
            annotations = service.metadata.annotations if service.metadata else None
            if annotations and annotations.get(OWNER_ANNOTATION) == self.app_name:
                self.client.delete(Service, name=self.service_name, namespace=self.namespace)
        except ApiError as error:
            if error.status.code == 404:
                return
            self._raise_for_api_error(error)
            raise

    @staticmethod
    def _raise_for_api_error(error: ApiError) -> None:
        if error.status.code == 403:
            raise KubernetesPermissionError(
                "Kubernetes API access denied; deploy this charm with --trust"
            ) from error
