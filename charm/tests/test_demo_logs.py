from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, call, patch

import pytest
from demo_logs import (
    MAX_TAIL,
    DemoLogsError,
    decode_log_frame,
    demo_pod_selector,
    fetch_demo_logs,
    repository_hash,
    select_demo_pod,
)
from lightkube.resources.core_v1 import Pod


def make_pod(
    name: str,
    *,
    phase: str = "Running",
    start: str | None = "2026-10-20T10:00:00",
    deleting: bool = False,
) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = "demos"
    pod.metadata.deletionTimestamp = "2026-10-20T10:00:00" if deleting else None
    pod.status.phase = phase
    pod.status.startTime = start
    return pod


def test_repository_hash_matches_controller_labels() -> None:
    expected = hashlib.sha256(b"canonical/snapcraft.io").hexdigest()[:16]
    assert repository_hash("canonical/snapcraft.io") == expected


def test_demo_pod_selector_labels() -> None:
    selector = demo_pod_selector("canonical/snapcraft.io", 5932)
    assert selector == {
        "demos.canonical.com/repository-hash": repository_hash("canonical/snapcraft.io"),
        "demos.canonical.com/pr": "5932",
    }


def test_select_demo_pod_prefers_newest_running_pod() -> None:
    old = make_pod("old", start="2026-10-20T10:00:00")
    newest = make_pod("newest", start="2026-10-20T12:00:00")
    pending = make_pod("pending", phase="Pending", start="2026-10-20T13:00:00")
    deleting = make_pod("deleting", start="2026-10-20T13:00:00", deleting=True)
    assert select_demo_pod([old, newest, pending, deleting]) is newest


def test_select_demo_pod_returns_none_without_running_candidates() -> None:
    assert select_demo_pod([]) is None
    assert select_demo_pod([make_pod("pending", phase="Pending")]) is None
    assert select_demo_pod([make_pod("deleting", deleting=True)]) is None


def test_decode_log_frame_keeps_data_channels() -> None:
    assert decode_log_frame(b"\x01hello\n") == "hello\n"


def test_decode_log_frame_drops_exec_status_frames() -> None:
    status = b'{"metadata":{},"status":"Success"}'
    assert decode_log_frame(b"\x03" + status) is None
    assert decode_log_frame(b"\x01" + status) is None


def test_fetch_demo_logs_execs_into_the_selected_pod() -> None:
    client = MagicMock()
    client.list.return_value = [make_pod("pod-a", start="2026-10-20T10:00:00")]
    with patch("demo_logs._exec_pebble_logs", return_value="logs") as execute:
        assert fetch_demo_logs(client, "canonical/snapcraft.io", 5932, tail=99_999) == "logs"
    assert client.list.call_args == call(
        Pod, labels=demo_pod_selector("canonical/snapcraft.io", 5932)
    )
    execute.assert_called_once_with("demos", "pod-a", MAX_TAIL)


def test_fetch_demo_logs_fails_without_a_running_pod() -> None:
    client = MagicMock()
    client.list.return_value = []
    with pytest.raises(DemoLogsError, match="no running demo pod"):
        fetch_demo_logs(client, "canonical/snapcraft.io", 5932)
