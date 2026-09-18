from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from demo_controller.models import DemoStatus, DeployRequest
from demo_controller.naming import hostname


@dataclass(frozen=True)
class DemoRecord:
    repository: str
    pr: int
    commit_sha: str | None
    image: str | None
    delivery_id: str
    request_hash: str
    desired_present: bool
    state: str
    vault_version: int | None
    port: int | None
    message: str
    created_at: int
    updated_at: int
    expires_at: int


class Store:
    def __init__(self, path: Path, hostname_suffix: str):
        self.path = path
        self.hostname_suffix = hostname_suffix
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS demos (
                    repository TEXT NOT NULL,
                    pr INTEGER NOT NULL,
                    commit_sha TEXT,
                    image TEXT,
                    delivery_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    desired_present INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    vault_version INTEGER,
                    port INTEGER,
                    message TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY (repository, pr)
                );
                CREATE TABLE IF NOT EXISTS nonces (
                    repository TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    seen_at INTEGER NOT NULL,
                    PRIMARY KEY (repository, nonce)
                );
                """
            )
            nonce_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(nonces)").fetchall()
            }
            if "request_fingerprint" not in nonce_columns:
                connection.execute(
                    "ALTER TABLE nonces ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''"
                )

    def use_nonce(
        self,
        repository: str,
        nonce: str,
        request_fingerprint: str,
        now: int,
        max_age: int,
    ) -> bool:
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM nonces WHERE seen_at < ?", (now - max_age,))
            existing = connection.execute(
                "SELECT request_fingerprint FROM nonces WHERE repository=? AND nonce=?",
                (repository, nonce),
            ).fetchone()
            if existing:
                return existing["request_fingerprint"] == request_fingerprint
            try:
                connection.execute(
                    """
                    INSERT INTO nonces(repository, nonce, request_fingerprint, seen_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (repository, nonce, request_fingerprint, now),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def upsert_deploy(
        self,
        request: DeployRequest,
        request_hash: str,
        max_lifetime: int,
    ) -> tuple[DemoRecord, bool]:
        now = int(time.time())
        with self._lock, self._connection() as connection:
            current = connection.execute(
                "SELECT * FROM demos WHERE repository=? AND pr=?",
                (request.repository, request.pr),
            ).fetchone()
            if current and current["delivery_id"] == request.delivery_id:
                if current["request_hash"] != request_hash:
                    raise ValueError("delivery ID reused with different request")
                return self._record(current), False
            created_at = current["created_at"] if current else now
            connection.execute(
                """
                INSERT INTO demos(
                    repository, pr, commit_sha, image, delivery_id, request_hash,
                    desired_present, state, vault_version, port, message,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 'pending', NULL, NULL, ?, ?, ?, ?)
                ON CONFLICT(repository, pr) DO UPDATE SET
                    commit_sha=excluded.commit_sha, image=excluded.image,
                    delivery_id=excluded.delivery_id, request_hash=excluded.request_hash,
                    desired_present=1, state='pending',
                    message=excluded.message, updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at
                """,
                (
                    request.repository,
                    request.pr,
                    request.commit_sha,
                    request.image,
                    request.delivery_id,
                    request_hash,
                    "deployment accepted",
                    created_at,
                    now,
                    now + max_lifetime,
                ),
            )
            row = connection.execute(
                "SELECT * FROM demos WHERE repository=? AND pr=?",
                (request.repository, request.pr),
            ).fetchone()
            return self._record(row), True

    def request_destroy(
        self, repository: str, pr: int, delivery_id: str, request_hash: str
    ) -> DemoRecord:
        now = int(time.time())
        with self._lock, self._connection() as connection:
            current = connection.execute(
                "SELECT * FROM demos WHERE repository=? AND pr=?", (repository, pr)
            ).fetchone()
            if current and current["delivery_id"] == delivery_id:
                if current["request_hash"] != request_hash:
                    raise ValueError("delivery ID reused with different request")
                return self._record(current)
            if current:
                connection.execute(
                    """
                    UPDATE demos SET delivery_id=?, request_hash=?, desired_present=0,
                        state='deleting', message='deletion accepted', updated_at=?
                    WHERE repository=? AND pr=?
                    """,
                    (delivery_id, request_hash, now, repository, pr),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO demos VALUES(
                        ?, ?, NULL, NULL, ?, ?, 0, 'deleting', NULL, NULL,
                        'deletion accepted', ?, ?, ?
                    )
                    """,
                    (repository, pr, delivery_id, request_hash, now, now, now),
                )
            row = connection.execute(
                "SELECT * FROM demos WHERE repository=? AND pr=?", (repository, pr)
            ).fetchone()
            return self._record(row)

    def list_reconcilable(self) -> list[DemoRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM demos WHERE state IN ('pending','reconciling','deleting','ready','failed')"
            ).fetchall()
        return [self._record(row) for row in rows]

    def get(self, repository: str, pr: int) -> DemoRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM demos WHERE repository=? AND pr=?", (repository, pr)
            ).fetchone()
        return self._record(row) if row else None

    def get_ready_by_hostname(self, requested_hostname: str) -> DemoRecord | None:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM demos
                WHERE desired_present=1
                  AND port IS NOT NULL
                  AND state IN ('pending','reconciling','ready','failed')
                """
            ).fetchall()
        for row in rows:
            record = self._record(row)
            if hostname(record.repository, record.pr, self.hostname_suffix) == requested_hostname:
                return record
        return None

    def update_state(
        self,
        repository: str,
        pr: int,
        state: str,
        message: str,
        *,
        vault_version: int | None = None,
        port: int | None = None,
        lifetime_seconds: int | None = None,
    ) -> None:
        now = int(time.time())
        fields = ["state=?", "message=?", "updated_at=?"]
        values: list[object] = [state, message[:1000], now]
        if vault_version is not None:
            fields.append("vault_version=?")
            values.append(vault_version)
        if port is not None:
            fields.append("port=?")
            values.append(port)
        if lifetime_seconds is not None:
            fields.append("expires_at=?")
            values.append(now + lifetime_seconds)
        values.extend([repository, pr])
        with self._lock, self._connection() as connection:
            connection.execute(
                f"UPDATE demos SET {', '.join(fields)} WHERE repository=? AND pr=?", values
            )

    def delete_record(self, repository: str, pr: int) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM demos WHERE repository=? AND pr=?", (repository, pr))

    def status(self, record: DemoRecord) -> DemoStatus:
        return DemoStatus(
            repository=record.repository,
            pr=record.pr,
            commit_sha=record.commit_sha,
            image=record.image,
            hostname=hostname(record.repository, record.pr, self.hostname_suffix),
            state=record.state,  # type: ignore[arg-type]
            vault_version=record.vault_version,
            message=record.message,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> DemoRecord:
        values = dict(row)
        values["desired_present"] = bool(values["desired_present"])
        return DemoRecord(**values)
