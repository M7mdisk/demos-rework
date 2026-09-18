from __future__ import annotations

import logging

import uvicorn
from lightkube import Client

from demo_controller.api import create_app
from demo_controller.auth import Authenticator
from demo_controller.kubernetes import KubernetesReconciler
from demo_controller.reconciler import ReconciliationLoop
from demo_controller.settings import Settings
from demo_controller.store import Store
from demo_controller.vault import VaultClient


def build_app():
    settings = Settings.from_env()
    store = Store(settings.database_path, settings.hostname_suffix)
    authenticator = Authenticator(
        settings.hmac_credentials,
        store,
        settings.signature_max_age_seconds,
        settings.rate_limit_per_minute,
    )
    vault = VaultClient(
        settings.vault_address,
        settings.vault_role_id,
        settings.vault_secret_id,
        settings.vault_kv_mount,
        settings.vault_base_path,
        settings.vault_namespace,
    )
    kubernetes = KubernetesReconciler(
        Client(namespace=settings.namespace), settings.namespace, settings.controller_owner
    )
    reconciliation = ReconciliationLoop(
        store,
        vault,
        kubernetes,
        settings.namespace,
        settings.controller_owner,
        settings.image_registry,
        settings.max_demo_lifetime_seconds,
        settings.reconcile_interval_seconds,
    )
    return create_app(settings, store, authenticator, reconciliation)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(build_app(), host="0.0.0.0", port=8080, proxy_headers=True)
