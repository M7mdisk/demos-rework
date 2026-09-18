from __future__ import annotations

import hashlib
import re


def resource_name(repository: str, pr: int) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", repository.lower().replace("/", "-")).strip("-")
    digest = hashlib.sha256(repository.encode()).hexdigest()[:8]
    return f"demo-{slug[:39]}-{digest}-pr-{pr}"[:63].rstrip("-")


def hostname(repository: str, pr: int, suffix: str) -> str:
    project = repository.rsplit("/", 1)[1]
    slug = re.sub(r"[^a-z0-9-]+", "-", project.lower()).strip("-")[:40]
    return f"{slug}-pr-{pr}.{suffix}"


def repository_labels(repository: str, pr: int, owner: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": owner,
        "demos.canonical.com/repository-hash": hashlib.sha256(repository.encode()).hexdigest()[:16],
        "demos.canonical.com/pr": str(pr),
    }
