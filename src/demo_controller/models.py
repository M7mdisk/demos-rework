from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPOSITORY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,38})/[a-z0-9](?:[a-z0-9_.-]{0,99})$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(
    r"^(?P<registry>[a-z0-9.-]+(?::[0-9]{1,5})?)/"
    r"(?P<repo>[a-z0-9](?:[a-z0-9_.-]*/)+[a-z0-9_.-]+)"
    r"@sha256:[0-9a-f]{64}$"
)
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeployRequest(StrictModel):
    repository: str
    pr: Annotated[int, Field(ge=1, le=2_147_483_647)]
    commit_sha: str
    image: str
    delivery_id: Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        value = value.lower()
        if not REPOSITORY_RE.fullmatch(value):
            raise ValueError("invalid repository identity")
        return value

    @field_validator("commit_sha")
    @classmethod
    def validate_sha(cls, value: str) -> str:
        value = value.lower()
        if not SHA_RE.fullmatch(value):
            raise ValueError("commit_sha must be a full lowercase SHA")
        return value

    @model_validator(mode="after")
    def validate_image_namespace(self) -> DeployRequest:
        match = IMAGE_RE.fullmatch(self.image.lower())
        if not match or match.group("repo") != self.repository:
            raise ValueError("image must be an immutable registry digest for repository")
        return self


class DestroyRequest(StrictModel):
    repository: str
    pr: Annotated[int, Field(ge=1, le=2_147_483_647)]
    delivery_id: Annotated[str, Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        value = value.lower()
        if not REPOSITORY_RE.fullmatch(value):
            raise ValueError("invalid repository identity")
        return value


class ResourceValues(StrictModel):
    cpu_request: str = "50m"
    cpu_limit: str = "1"
    memory_request: str = "128Mi"
    memory_limit: str = "1Gi"

    @field_validator("cpu_request", "cpu_limit")
    @classmethod
    def validate_cpu(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[1-9][0-9]{0,3}m|[1-4])", value):
            raise ValueError("CPU value outside controller policy")
        return value

    @field_validator("memory_request", "memory_limit")
    @classmethod
    def validate_memory(cls, value: str) -> str:
        match = re.fullmatch(r"([1-9][0-9]{0,4})(Mi|Gi)", value)
        if not match:
            raise ValueError("invalid memory value")
        mib = int(match.group(1)) * (1024 if match.group(2) == "Gi" else 1)
        if mib > 8192:
            raise ValueError("memory value outside controller policy")
        return value


class ProjectConfig(StrictModel):
    schema_version: Literal[1]
    enabled: bool
    environment: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    port: Annotated[int, Field(ge=1024, le=65535)] = 8000
    health_path: str = "/"
    resources: ResourceValues = Field(default_factory=ResourceValues)
    image_namespace: str | None = None
    lifetime_seconds: Annotated[int | None, Field(ge=300, le=2_592_000)] = None

    @field_validator("environment", "secrets")
    @classmethod
    def validate_environment(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 100:
            raise ValueError("too many environment values")
        for name, item in value.items():
            if not ENV_RE.fullmatch(name) or not isinstance(item, str) or len(item) > 16384:
                raise ValueError("invalid environment value")
        return value

    @field_validator("health_path")
    @classmethod
    def validate_health_path(cls, value: str) -> str:
        if not re.fullmatch(r"/[A-Za-z0-9_./~-]{0,255}", value) or ".." in value:
            raise ValueError("invalid health path")
        return value

    @model_validator(mode="after")
    def distinct_environment_names(self) -> ProjectConfig:
        overlap = self.environment.keys() & self.secrets.keys()
        if overlap:
            raise ValueError(f"plain and secret environment overlap: {sorted(overlap)!r}")
        if not self.enabled:
            raise ValueError("repository is disabled")
        return self


DemoState = Literal["pending", "reconciling", "ready", "failed", "deleting"]


class DemoStatus(StrictModel):
    repository: str
    pr: int
    commit_sha: str | None
    image: str | None
    hostname: str
    state: DemoState
    vault_version: int | None
    message: str
    updated_at: int
