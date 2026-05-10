"""SDK delegation tests — Phase 2 §2.2.6 + §4 row 8.

Covers:
- test_p2_unit_submit_child_outside_handler_raises (pure SDK, MUST pass now)
- test_p2_del_8_sdk_no_deadlock_async (integration; deferred to Unit C)
- test_p2_del_8_sdk_no_deadlock_sync  (integration; deferred to Unit C)

The integration tests intentionally hit a real hub fixture so the same hub
process runs the real `_handle_submit_child` Unit C will land. Until that
exists they are skipped with an explicit reason.
"""
import asyncio
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from agent_sdk.client import (
    AgentHub,
    AsyncAgentHub,
    ChildNotInTaskContext,
    TaskFailed,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_ADMIN_KEY = "test-admin-key-1234567890"


def _hub_supports_submit_child() -> bool:
    """Best-effort introspection: does the hub source mention submit_child?"""
    main_py = REPO_ROOT / "hub" / "main.py"
    if not main_py.is_file():
        return False
    try:
        text = main_py.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "submit_child" in text and "_handle_submit_child" in text


HUB_HAS_SUBMIT_CHILD = _hub_supports_submit_child()
SKIP_REASON = (
    "hub-side handler is Unit C's deliverable; integration covered in test_websocket.py"
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def hub_server():
    """Mirror the hub_server fixture in tests/test_websocket.py."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    port = _free_port()
    env = os.environ.copy()
    env["AGENT_HUB_DB_PATH"] = tmp.name
    env["AGENT_HUB_ADMIN_KEY"] = SEED_ADMIN_KEY
    env["AGENT_HUB_TIMEOUT_SCAN_INTERVAL"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.Popen(
        [str(REPO_ROOT / ".venv/bin/python"), "-m", "uvicorn", "hub.main:app",
         "--port", str(port), "--host", "127.0.0.1", "--log-level", "warning"],
        env=env, cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=0.5)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        os.unlink(tmp.name)
        raise RuntimeError("hub did not start")
    # Expose the db path on the tuple as a 3rd element for tests that need
    # to peek at hub-side DB state (e.g. activity_log persistence). Existing
    # 2-tuple unpacking still works because Python lets you do
    # `a, b = (x, y, z)` only with explicit star-target; we therefore keep
    # the explicit 3-tuple and update existing tests' unpacks below.
    yield base_url, ws_url, tmp.name
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    os.unlink(tmp.name)


def _make_key(base_url: str, name: str, **perms) -> str:
    body = {"name": name, **perms}
    r = httpx.post(f"{base_url}/admin/keys",
                   headers={"Authorization": f"Bearer {SEED_ADMIN_KEY}"}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


# === Pure SDK test (MUST pass now) ===========================================


def test_p2_unit_submit_child_outside_handler_raises():
    """submit_child outside any task handler raises ChildNotInTaskContext."""
    sync_sdk = AgentHub("http://localhost:0", "a-no-ctx", ["x"], auth_token="k")
    with pytest.raises(ChildNotInTaskContext):
        sync_sdk.submit_child(target_agent="b", task="x")

    async def _go():
        async_sdk = AsyncAgentHub("http://localhost:0", "a-no-ctx", ["x"], auth_token="k")
        with pytest.raises(ChildNotInTaskContext):
            await async_sdk.submit_child(target_agent="b", task="x")

    asyncio.run(_go())


# === Integration tests (deferred to Unit C) =================================


@pytest.mark.skipif(
    not HUB_HAS_SUBMIT_CHILD, reason=SKIP_REASON,
)
@pytest.mark.parametrize("target_agent", ["B", "A"])
@pytest.mark.asyncio
async def test_p2_del_8_sdk_no_deadlock_async(hub_server, target_agent):
    """Async: parent A awaits submit_child(target=B or A); reader stays live."""
    base_url, ws_url, db_path = hub_server
    key = _make_key(
        base_url, "k",
        can_register=True, can_assign_tasks=True, can_view_tasks=True,
    )
    headers = {"Authorization": f"Bearer {key}"}

    a_done = asyncio.Event()
    a_result_holder: dict = {}

    async def handler_a(task):
        # Self-child fast-path test: when this same handler runs for the
        # delegated child, short-circuit so we don't recurse forever.
        if task.get("payload", {}).get("who") == "A":
            return {"echo": task.get("payload", {})}
        # Parent: delegate to target_agent and wait for its result.
        child_result = await sdk_a.submit_child(
            target_agent=target_agent,
            task="from-A",
            task_type="x",
            payload={"who": "A"},
            timeout=10,
            wait_timeout=10.0,
        )
        a_result_holder["child"] = child_result
        a_done.set()
        return {"parent": "A", "child_status": child_result.get("status")}

    async def handler_b(task):
        return {"echo": task.get("payload", {})}

    sdk_a = AsyncAgentHub(base_url, "A", ["x"], auth_token=key, task_handler=handler_a)
    sdk_b = AsyncAgentHub(base_url, "B", ["x"], auth_token=key, task_handler=handler_b)

    run_a = asyncio.create_task(sdk_a.run())
    run_b = asyncio.create_task(sdk_b.run())
    await asyncio.sleep(0.5)  # connect/register

    try:
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{base_url}/tasks", headers=headers,
                json={"task": "go", "task_type": "x", "target_agent": "A"},
            )
            assert r.status_code == 200, r.text
            await asyncio.wait_for(a_done.wait(), timeout=10)
            assert a_result_holder["child"].get("status") == "completed"
    finally:
        for t in (run_a, run_b):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.skipif(
    not HUB_HAS_SUBMIT_CHILD, reason=SKIP_REASON,
)
def test_p2_del_8_sdk_no_deadlock_sync(hub_server):
    """Sync: parent on a worker thread awaits submit_child; reader stays live."""
    base_url, ws_url, db_path = hub_server
    key = _make_key(
        base_url, "k",
        can_register=True, can_assign_tasks=True, can_view_tasks=True,
    )
    headers = {"Authorization": f"Bearer {key}"}

    a_done = threading.Event()
    a_holder: dict = {}

    sdk_a = AgentHub(base_url, "A-sync", ["x"], auth_token=key, reconnect=False)
    sdk_b = AgentHub(base_url, "B-sync", ["x"], auth_token=key, reconnect=False)

    @sdk_a.task_handler
    def handler_a(task):
        child = sdk_a.submit_child(
            target_agent="B-sync", task="from-A", task_type="x",
            timeout=10, wait_timeout=10.0,
        )
        a_holder["child"] = child
        a_done.set()
        return {"parent": "A", "child_status": child.get("status")}

    @sdk_b.task_handler
    def handler_b(task):
        return {"echo": task.get("payload", {})}

    t_a = threading.Thread(target=sdk_a.start, daemon=True)
    t_b = threading.Thread(target=sdk_b.start, daemon=True)
    t_a.start()
    t_b.start()
    time.sleep(1.0)  # connect/register

    try:
        r = httpx.post(
            f"{base_url}/tasks", headers=headers,
            json={"task": "go", "task_type": "x", "target_agent": "A-sync"},
        )
        assert r.status_code == 200, r.text
        assert a_done.wait(timeout=10), "parent handler never completed"
        assert a_holder["child"].get("status") == "completed"
    finally:
        sdk_a.stop()
        sdk_b.stop()
        t_a.join(timeout=2)
        t_b.join(timeout=2)


# === P2.3 polish — unregister_on_stop flag ===

@pytest.mark.skipif(
    not _hub_supports_submit_child(),
    reason="hub-side handler is Unit C's deliverable; integration tests deferred",
)
def test_p23_unregister_on_stop_true_removes_agent_from_registry(hub_server):
    """When unregister_on_stop=True, stop() POSTs /unregister; agent row gone."""
    import threading
    import time
    from agent_sdk.client import AgentHub

    base_url, ws_url, db_path = hub_server
    key = _make_key(base_url, "uos-true", can_register=True, can_view_agents=True)
    headers = {"Authorization": f"Bearer {key}"}

    sdk = AgentHub(
        hub_url=base_url, agent_id="uos-true-agent",
        capabilities=["echo"], auth_token=key, task_handler=lambda t: {},
        reconnect=False, unregister_on_stop=True,
    )
    t = threading.Thread(target=sdk.start, daemon=True)
    t.start()
    time.sleep(0.5)

    r = httpx.get(f"{base_url}/agents/uos-true-agent", headers=headers)
    assert r.status_code == 200, "agent should be registered after start"

    sdk.stop()
    t.join(timeout=2)

    r = httpx.get(f"{base_url}/agents/uos-true-agent", headers=headers)
    assert r.status_code == 404, "agent should be unregistered (row deleted) after stop"


@pytest.mark.skipif(
    not _hub_supports_submit_child(),
    reason="hub-side handler is Unit C's deliverable; integration tests deferred",
)
def test_p23_unregister_on_stop_false_leaves_agent_offline(hub_server):
    """Default (unregister_on_stop=False): stop() only closes WS; agent row stays as offline."""
    import threading
    import time
    from agent_sdk.client import AgentHub

    base_url, ws_url, db_path = hub_server
    key = _make_key(base_url, "uos-false", can_register=True, can_view_agents=True)
    headers = {"Authorization": f"Bearer {key}"}

    sdk = AgentHub(
        hub_url=base_url, agent_id="uos-false-agent",
        capabilities=["echo"], auth_token=key, task_handler=lambda t: {},
        reconnect=False,  # default unregister_on_stop=False
    )
    t = threading.Thread(target=sdk.start, daemon=True)
    t.start()
    time.sleep(0.5)

    sdk.stop()
    t.join(timeout=2)
    time.sleep(0.3)  # let hub process disconnect

    r = httpx.get(f"{base_url}/agents/uos-false-agent", headers=headers)
    assert r.status_code == 200, "agent row should still exist (default behavior)"
    assert r.json()["status"] == "offline"


# === Phase 2.3 — coder-A SDK additions ======================================


@pytest.mark.skipif(
    not _hub_supports_submit_child(),
    reason="hub-side handler is Unit C's deliverable; integration tests deferred",
)
def test_p23_sdk_description_in_register_frame(hub_server):
    """description kwarg is sent in the WS register frame and surfaces via GET /agents/<id>."""
    base_url, ws_url, db_path = hub_server
    key = _make_key(base_url, "p23-desc", can_register=True, can_view_agents=True)
    headers = {"Authorization": f"Bearer {key}"}

    sdk = AgentHub(
        hub_url=base_url, agent_id="p23-desc-agent",
        capabilities=["echo"], auth_token=key, task_handler=lambda t: {},
        reconnect=False, description="cli=codex;role=designer",
    )
    t = threading.Thread(target=sdk.start, daemon=True)
    t.start()
    time.sleep(0.6)

    try:
        r = httpx.get(f"{base_url}/agents/p23-desc-agent", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("description") == "cli=codex;role=designer", body
    finally:
        sdk.stop()
        t.join(timeout=2)


@pytest.mark.skipif(
    not _hub_supports_submit_child(),
    reason="hub-side handler is Unit C's deliverable; integration tests deferred",
)
def test_p23_sdk_task_failed_reports_failed_status(hub_server):
    """A handler that raises TaskFailed → GET /tasks/<id> shows status=failed and the carried result dict."""
    base_url, ws_url, db_path = hub_server
    key = _make_key(
        base_url, "p23-tf",
        can_register=True, can_assign_tasks=True, can_view_tasks=True,
    )
    headers = {"Authorization": f"Bearer {key}"}

    fail_payload = {"error": "x", "detail": "y"}

    sdk = AgentHub(
        hub_url=base_url, agent_id="p23-tf-agent",
        capabilities=["echo"], auth_token=key, reconnect=False,
    )

    @sdk.task_handler
    def _h(task):
        raise TaskFailed(fail_payload)

    t = threading.Thread(target=sdk.start, daemon=True)
    t.start()
    time.sleep(0.6)

    try:
        r = httpx.post(
            f"{base_url}/tasks", headers=headers,
            json={"task": "go", "task_type": "echo", "target_agent": "p23-tf-agent"},
        )
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]

        deadline = time.time() + 5.0
        body = None
        while time.time() < deadline:
            g = httpx.get(f"{base_url}/tasks/{task_id}", headers=headers)
            if g.status_code == 200:
                body = g.json()
                if body.get("status") in ("failed", "completed", "timeout"):
                    break
            time.sleep(0.1)
        assert body is not None, "no terminal task state observed"
        assert body.get("status") == "failed", body
        assert body.get("result") == fail_payload, body
    finally:
        sdk.stop()
        t.join(timeout=2)


@pytest.mark.skipif(
    not _hub_supports_submit_child(),
    reason="hub-side handler is Unit C's deliverable; integration tests deferred",
)
def test_p23_sdk_send_activity_log_writes_via_hub(hub_server):
    """send_activity_log → hub persists the row to activity_log via db.log_activity."""
    base_url, ws_url, db_path = hub_server
    key = _make_key(
        base_url, "p23-al",
        can_register=True, can_assign_tasks=True, can_view_tasks=True,
    )
    headers = {"Authorization": f"Bearer {key}"}

    sdk = AgentHub(
        hub_url=base_url, agent_id="p23-al-agent",
        capabilities=["echo"], auth_token=key, reconnect=False,
    )

    @sdk.task_handler
    def _h(task):
        sdk.send_activity_log(task.get("task_id"), "test_action", {"k": "v"})
        return {"ok": True}

    t = threading.Thread(target=sdk.start, daemon=True)
    t.start()
    time.sleep(0.6)

    try:
        r = httpx.post(
            f"{base_url}/tasks", headers=headers,
            json={"task": "go", "task_type": "echo", "target_agent": "p23-al-agent"},
        )
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]

        deadline = time.time() + 5.0
        while time.time() < deadline:
            g = httpx.get(f"{base_url}/tasks/{task_id}", headers=headers)
            if g.status_code == 200 and g.json().get("status") in ("completed", "failed", "timeout"):
                break
            time.sleep(0.1)

        g = httpx.get(f"{base_url}/tasks/{task_id}", headers=headers)
        assert g.status_code == 200, g.text
        body = g.json()
        assert body.get("status") == "completed", body
        assert body.get("result") == {"ok": True}, body

        # Verify the activity_log row was persisted by the hub. The hub
        # exposes no list-activity REST endpoint, so query SQLite directly
        # against the same DB file the hub subprocess is using (yielded by
        # the fixture).
        import json as _json
        import sqlite3

        # Allow a brief window for the WS message to be processed and the
        # row committed before we read it back.
        rows = []
        deadline = time.time() + 3.0
        while time.time() < deadline:
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.execute(
                    "SELECT entity_type, entity_id, action, details "
                    "FROM activity_log WHERE entity_type=? AND entity_id=? "
                    "AND action=?",
                    ("task", task_id, "test_action"),
                )
                rows = cur.fetchall()
            finally:
                conn.close()
            if rows:
                break
            time.sleep(0.1)

        assert rows, "expected an activity_log row for test_action"
        # Exactly one row from the single send_activity_log call.
        assert len(rows) == 1, rows
        entity_type, entity_id, action, details_json = rows[0]
        assert entity_type == "task"
        assert entity_id == task_id
        assert action == "test_action"
        assert _json.loads(details_json) == {"k": "v"}
    finally:
        sdk.stop()
        t.join(timeout=2)
