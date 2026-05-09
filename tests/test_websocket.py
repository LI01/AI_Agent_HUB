"""WebSocket protocol + SDK tests — test_plan.md §4 + §6."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_ADMIN_KEY = "test-admin-key-1234567890"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def hub_server():
    """Spawn a real uvicorn process so WebSocket upgrades work."""
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
    yield base_url, ws_url
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


# === I-WS: Connection lifecycle (test_plan §4.1) ===

@pytest.mark.asyncio
async def test_i_ws_1_register_returns_ack(hub_server):
    base_url, ws_url = hub_server
    key = _make_key(base_url, "wsk", can_register=True, can_assign_tasks=True)
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "type": "register", "agent_id": "a-ws-1", "capabilities": ["echo"], "auth_token": key
        }))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert msg["type"] == "registered"
        assert msg["agent_id"] == "a-ws-1"


@pytest.mark.asyncio
async def test_i_ws_2_invalid_token_closed(hub_server):
    base_url, ws_url = hub_server
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "type": "register", "agent_id": "a", "capabilities": [], "auth_token": "bogus"
        }))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert msg["type"] == "error"


@pytest.mark.asyncio
async def test_i_ws_3_first_msg_must_be_register(hub_server):
    base_url, ws_url = hub_server
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"type": "heartbeat", "status": "idle"}))
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert msg["type"] == "error"


@pytest.mark.asyncio
async def test_i_ws_8_reconnect_closes_old(hub_server):
    """Regression for §1.7: reconnect with same agent_id must close old socket."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "wsk", can_register=True, can_assign_tasks=True)

    ws_old = await websockets.connect(ws_url)
    await ws_old.send(json.dumps({
        "type": "register", "agent_id": "dup", "capabilities": ["echo"], "auth_token": key
    }))
    assert json.loads(await asyncio.wait_for(ws_old.recv(), timeout=3))["type"] == "registered"

    ws_new = await websockets.connect(ws_url)
    await ws_new.send(json.dumps({
        "type": "register", "agent_id": "dup", "capabilities": ["echo"], "auth_token": key
    }))
    assert json.loads(await asyncio.wait_for(ws_new.recv(), timeout=3))["type"] == "registered"

    # Old socket should be closed by the hub. We need to drain it.
    try:
        await asyncio.wait_for(ws_old.recv(), timeout=3)
        # If we got a frame instead of a close, fail
        assert False, "old socket still open after reconnect"
    except websockets.exceptions.ConnectionClosed:
        pass  # expected
    finally:
        await ws_old.close()
        await ws_new.close()


# === I-WS: Task delivery (test_plan §4.2) ===

@pytest.mark.asyncio
async def test_i_ws_9_task_delivered(hub_server):
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "type": "register", "agent_id": "a-task", "capabilities": ["echo"], "auth_token": key
        }))
        await ws.recv()  # registered ack

        async with httpx.AsyncClient() as http:
            r = await http.post(f"{base_url}/tasks", headers=headers,
                                json={"task": "echo hi", "task_type": "echo", "target_agent": "a-task"})
            assert r.status_code == 200
            tid = r.json()["task_id"]

        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert msg["type"] == "task"
        assert msg["task"]["task_id"] == tid


@pytest.mark.asyncio
async def test_i_ws_10_result_persisted(hub_server):
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "type": "register", "agent_id": "a-rep", "capabilities": ["echo"], "auth_token": key
        }))
        await ws.recv()

        async with httpx.AsyncClient() as http:
            r = await http.post(f"{base_url}/tasks", headers=headers,
                                json={"task": "x", "task_type": "echo", "target_agent": "a-rep"})
            tid = r.json()["task_id"]

            task_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            assert task_msg["type"] == "task"

            await ws.send(json.dumps({
                "type": "result", "task_id": tid, "status": "completed",
                "result": {"echo": "x"}, "logs": ["did the work"]
            }))
            await asyncio.sleep(0.3)

            g = await http.get(f"{base_url}/tasks/{tid}", headers=headers)
            body = g.json()
            assert body["status"] == "completed"
            assert body["result"] == {"echo": "x"}
            assert "did the work" in body["logs"]


@pytest.mark.asyncio
async def test_i_ws_11_log_appended(hub_server):
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({
            "type": "register", "agent_id": "a-log", "capabilities": ["echo"], "auth_token": key
        }))
        await ws.recv()
        async with httpx.AsyncClient() as http:
            r = await http.post(f"{base_url}/tasks", headers=headers,
                                json={"task": "x", "task_type": "echo", "target_agent": "a-log"})
            tid = r.json()["task_id"]
            await asyncio.wait_for(ws.recv(), timeout=3)
            await ws.send(json.dumps({"type": "log", "task_id": tid, "log": "step 1"}))
            await ws.send(json.dumps({"type": "log", "task_id": tid, "log": "step 2"}))
            await asyncio.sleep(0.3)
            g = await http.get(f"{base_url}/tasks/{tid}", headers=headers)
            assert "step 1" in g.json()["logs"]
            assert "step 2" in g.json()["logs"]


@pytest.mark.asyncio
async def test_i_ws_disconnect_requeues_task(hub_server):
    """FR-CON-6: assigned task requeued on agent disconnect."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws = await websockets.connect(ws_url)
    await ws.send(json.dumps({
        "type": "register", "agent_id": "a-dc", "capabilities": ["echo"], "auth_token": key
    }))
    await ws.recv()

    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "x", "task_type": "echo", "target_agent": "a-dc"})
        tid = r.json()["task_id"]
        await asyncio.wait_for(ws.recv(), timeout=3)

        await ws.close()
        await asyncio.sleep(0.5)

        g = await http.get(f"{base_url}/tasks/{tid}", headers=headers)
        assert g.json()["status"] == "queued", f"expected requeued, got {g.json()['status']}"
        assert g.json()["assigned_agent_id"] is None


@pytest.mark.asyncio
async def test_i_ws_report_ownership_via_ws(hub_server):
    """Regression for §1.6 on the WS path: agent A cannot report task assigned to B."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await websockets.connect(ws_url)
    await ws_a.send(json.dumps({
        "type": "register", "agent_id": "a-A", "capabilities": ["echo"], "auth_token": key
    }))
    await ws_a.recv()

    ws_b = await websockets.connect(ws_url)
    await ws_b.send(json.dumps({
        "type": "register", "agent_id": "a-B", "capabilities": ["echo"], "auth_token": key
    }))
    await ws_b.recv()

    async with httpx.AsyncClient() as http:
        # Assign to A
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "x", "task_type": "echo", "target_agent": "a-A"})
        tid = r.json()["task_id"]
        # A receives task
        msg = json.loads(await asyncio.wait_for(ws_a.recv(), timeout=3))
        assert msg["type"] == "task"
        # B fraudulently sends a result
        await ws_b.send(json.dumps({
            "type": "result", "task_id": tid, "status": "completed", "result": {"hijacked": True}
        }))
        await asyncio.sleep(0.3)
        # Task must NOT be completed
        g = await http.get(f"{base_url}/tasks/{tid}", headers=headers)
        assert g.json()["status"] != "completed", "WS reporter ownership not enforced"

    await ws_a.close()
    await ws_b.close()


# === S-SDK: SDK behavior (test_plan §6) ===

@pytest.mark.asyncio
async def test_s_sdk_2_async_handler_invoked(hub_server):
    """S-SDK-2/3: AsyncAgentHub task_handler runs and returns a result."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    from agent_sdk.client import AsyncAgentHub

    received = asyncio.Event()
    handler_calls = []

    async def handler(task):
        handler_calls.append(task)
        received.set()
        return {"echo": task.get("payload", {}).get("task", "?")}

    sdk = AsyncAgentHub(base_url, "a-sdk", ["echo"], auth_token=key, task_handler=handler)
    run_task = asyncio.create_task(sdk.run())

    await asyncio.sleep(0.5)  # let SDK connect/register

    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "hello", "task_type": "echo", "target_agent": "a-sdk"})
        tid = r.json()["task_id"]
        await asyncio.wait_for(received.wait(), timeout=5)
        await asyncio.sleep(0.5)
        g = await http.get(f"{base_url}/tasks/{tid}", headers=headers)
        assert g.json()["status"] == "completed"
        assert g.json()["result"] == {"echo": "hello"}

    run_task.cancel()
    try:
        await run_task
    except (asyncio.CancelledError, Exception):
        pass


# === Phase 2 F1: agent-to-agent delegation (test_plan §5) ====================
# Tests use raw WS frames so they can exercise the protocol precisely without
# being constrained by SDK handler semantics.


async def _ws_register(ws_url: str, agent_id: str, key: str, capabilities=None):
    ws = await websockets.connect(ws_url)
    await ws.send(json.dumps({
        "type": "register",
        "agent_id": agent_id,
        "capabilities": capabilities or ["x"],
        "auth_token": key,
    }))
    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
    assert ack["type"] == "registered", ack
    return ws


async def _ws_drain_until(ws, predicate, timeout=5.0):
    """Receive frames from `ws` until `predicate(msg)` returns True; return that msg."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError("predicate not satisfied")
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if predicate(msg):
            return msg


@pytest.mark.asyncio
async def test_p2_del_1_submit_child_happy_path(hub_server):
    """Agent A submits child to B; B receives task; B reports; A receives child_completed."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A1", key)
    ws_b = await _ws_register(ws_url, "B1", key)

    # Driver submits parent task to A.
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A1"})
        parent_id = r.json()["task_id"]
        parent_msg = json.loads(await asyncio.wait_for(ws_a.recv(), timeout=3))
        assert parent_msg["type"] == "task" and parent_msg["task"]["task_id"] == parent_id

        # A delegates to B.
        await ws_a.send(json.dumps({
            "type": "submit_child",
            "request_id": "req-1",
            "parent_task_id": parent_id,
            "target_agent": "B1",
            "task": "child-work",
            "task_type": "x",
        }))

        accepted = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_accepted")
        child_id = accepted["child_task_id"]

        # B receives the child task.
        b_msg = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=3))
        assert b_msg["type"] == "task"
        assert b_msg["task"]["task_id"] == child_id
        assert b_msg["task"]["parent_task_id"] == parent_id

        # B reports completed.
        await ws_b.send(json.dumps({
            "type": "result", "task_id": child_id, "status": "completed", "result": {"ok": True}
        }))

        # A receives child_completed.
        completed = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_completed", timeout=5)
        assert completed["child_task_id"] == child_id
        assert completed["parent_task_id"] == parent_id
        assert completed["status"] == "completed"
        assert completed["result"] == {"ok": True}

    await ws_a.close()
    await ws_b.close()


@pytest.mark.asyncio
async def test_p2_del_1b_missing_target_rejected(hub_server):
    """submit_child without target_agent → child_rejected: bad_request."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A2", key)
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A2"})
        parent_id = r.json()["task_id"]
        await asyncio.wait_for(ws_a.recv(), timeout=3)  # task push

        # Missing target_agent.
        await ws_a.send(json.dumps({
            "type": "submit_child",
            "request_id": "req-bad",
            "parent_task_id": parent_id,
            "task": "x",
        }))
        rejected = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_rejected")
        assert rejected["reason"] == "bad_request"
    await ws_a.close()


@pytest.mark.asyncio
async def test_p2_del_2_cycle_rejected_indirect(hub_server):
    """A → B → A (indirect cycle) is rejected with reason 'cycle'."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A3", key)
    ws_b = await _ws_register(ws_url, "B3", key)

    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A3"})
        parent_id = r.json()["task_id"]
        await asyncio.wait_for(ws_a.recv(), timeout=3)

        # A → B
        await ws_a.send(json.dumps({
            "type": "submit_child", "request_id": "r1",
            "parent_task_id": parent_id, "target_agent": "B3", "task": "to-b",
            "task_type": "x",
        }))
        accepted = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_accepted")
        child_id = accepted["child_task_id"]
        b_task = json.loads(await asyncio.wait_for(ws_b.recv(), timeout=3))
        assert b_task["task"]["task_id"] == child_id

        # B → A → expect cycle
        await ws_b.send(json.dumps({
            "type": "submit_child", "request_id": "r2",
            "parent_task_id": child_id, "target_agent": "A3", "task": "to-a",
            "task_type": "x",
        }))
        rejected = await _ws_drain_until(ws_b, lambda m: m.get("type") == "child_rejected", timeout=5)
        assert rejected["reason"] == "cycle"

    await ws_a.close()
    await ws_b.close()


@pytest.mark.asyncio
async def test_p2_del_2b_immediate_self_allowed(hub_server):
    """Immediate A → A passes the cycle check."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A4", key)
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A4"})
        parent_id = r.json()["task_id"]
        await asyncio.wait_for(ws_a.recv(), timeout=3)

        await ws_a.send(json.dumps({
            "type": "submit_child", "request_id": "rself",
            "parent_task_id": parent_id, "target_agent": "A4", "task": "self",
            "task_type": "x",
        }))

        # Both task push (for child) and child_accepted should arrive.
        accepted = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_accepted")
        assert "child_task_id" in accepted
    await ws_a.close()


@pytest.mark.asyncio
async def test_p2_del_2c_self_delegation_completes(hub_server):
    """Full A → A self-delegation: child completes; parent then completes."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A5", key)
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A5"})
        parent_id = r.json()["task_id"]
        parent_push = json.loads(await asyncio.wait_for(ws_a.recv(), timeout=3))
        assert parent_push["task"]["task_id"] == parent_id

        # A delegates to A.
        await ws_a.send(json.dumps({
            "type": "submit_child", "request_id": "r1",
            "parent_task_id": parent_id, "target_agent": "A5", "task": "child",
            "task_type": "x",
        }))

        # The hub sends BOTH a `task` push (for the child) and a `child_accepted` ack.
        # Order is implementation-defined; drain both.
        seen_task_id = None
        seen_accepted = None
        for _ in range(4):
            msg = json.loads(await asyncio.wait_for(ws_a.recv(), timeout=3))
            if msg.get("type") == "task":
                seen_task_id = msg["task"]["task_id"]
            elif msg.get("type") == "child_accepted":
                seen_accepted = msg
            if seen_task_id and seen_accepted:
                break
        assert seen_accepted is not None and seen_task_id is not None
        child_id = seen_accepted["child_task_id"]
        assert seen_task_id == child_id

        # Agent stays BUSY while child runs (parent still RUNNING).
        agent_view = await http.get(f"{base_url}/agents/A5", headers=headers)
        assert agent_view.json()["status"] == "busy"

        # Now A reports the child completed.
        await ws_a.send(json.dumps({
            "type": "result", "task_id": child_id, "status": "completed", "result": {"r": 1}
        }))

        # A receives child_completed.
        child_completed = await _ws_drain_until(
            ws_a, lambda m: m.get("type") == "child_completed", timeout=5
        )
        assert child_completed["child_task_id"] == child_id

        # After child finalize, agent still BUSY (parent still RUNNING).
        agent_view = await http.get(f"{base_url}/agents/A5", headers=headers)
        assert agent_view.json()["status"] == "busy"

        # Now A reports parent completed.
        await ws_a.send(json.dumps({
            "type": "result", "task_id": parent_id, "status": "completed", "result": {"final": True}
        }))
        await asyncio.sleep(0.4)

        # Verify both terminal.
        p = await http.get(f"{base_url}/tasks/{parent_id}", headers=headers)
        c = await http.get(f"{base_url}/tasks/{child_id}", headers=headers)
        assert p.json()["status"] == "completed"
        assert c.json()["status"] == "completed"
        assert c.json()["parent_task_id"] == parent_id

        # Agent now IDLE.
        agent_view = await http.get(f"{base_url}/agents/A5", headers=headers)
        assert agent_view.json()["status"] == "idle"

    await ws_a.close()


@pytest.mark.asyncio
async def test_p2_del_2d_self_child_does_not_release_parent_for_other_dispatch(hub_server):
    """While A is RUNNING parent + self-child, queued top-level task waits for parent."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A6", key)
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A6"})
        parent_id = r.json()["task_id"]
        await asyncio.wait_for(ws_a.recv(), timeout=3)

        # A self-delegates.
        await ws_a.send(json.dumps({
            "type": "submit_child", "request_id": "r1",
            "parent_task_id": parent_id, "target_agent": "A6", "task": "child",
            "task_type": "x",
        }))
        seen = {}
        for _ in range(4):
            msg = json.loads(await asyncio.wait_for(ws_a.recv(), timeout=3))
            if msg.get("type") == "task":
                seen["child_task"] = msg["task"]["task_id"]
            elif msg.get("type") == "child_accepted":
                seen["accepted"] = msg
            if "child_task" in seen and "accepted" in seen:
                break
        child_id = seen["accepted"]["child_task_id"]

        # Submit a queued top-level task with same task_type — no target.
        r2 = await http.post(f"{base_url}/tasks", headers=headers,
                             json={"task": "queued", "task_type": "x"})
        queued_id = r2.json()["task_id"]
        # Agent is BUSY → not assigned.
        q = await http.get(f"{base_url}/tasks/{queued_id}", headers=headers)
        assert q.json()["status"] == "queued"

        # Child completes — parent still RUNNING, so A stays BUSY.
        await ws_a.send(json.dumps({
            "type": "result", "task_id": child_id, "status": "completed", "result": {"r": 1}
        }))
        # Drain child_completed.
        await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_completed", timeout=5)

        await asyncio.sleep(0.3)
        # Queued top-level task should STILL be queued — A is still BUSY (parent running).
        q = await http.get(f"{base_url}/tasks/{queued_id}", headers=headers)
        assert q.json()["status"] == "queued", \
            f"queued task dispatched too early; got status={q.json()['status']}"
        agent_view = await http.get(f"{base_url}/agents/A6", headers=headers)
        assert agent_view.json()["status"] == "busy"

        # Parent completes.
        await ws_a.send(json.dumps({
            "type": "result", "task_id": parent_id, "status": "completed", "result": {}
        }))
        # The queued task should now get dispatched.
        new_task = await _ws_drain_until(ws_a, lambda m: m.get("type") == "task", timeout=5)
        assert new_task["task"]["task_id"] == queued_id

    await ws_a.close()


@pytest.mark.asyncio
async def test_p2_del_4_child_failure_bubbles_to_parent(hub_server):
    """Child returns failed → parent receives child_completed{status=failed}."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A7", key)
    ws_b = await _ws_register(ws_url, "B7", key)
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A7"})
        parent_id = r.json()["task_id"]
        await asyncio.wait_for(ws_a.recv(), timeout=3)

        await ws_a.send(json.dumps({
            "type": "submit_child", "request_id": "r1",
            "parent_task_id": parent_id, "target_agent": "B7", "task": "child",
            "task_type": "x",
        }))
        accepted = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_accepted")
        child_id = accepted["child_task_id"]
        await asyncio.wait_for(ws_b.recv(), timeout=3)

        # B reports failed.
        await ws_b.send(json.dumps({
            "type": "result", "task_id": child_id, "status": "failed",
            "result": {"error": "boom"},
        }))

        completed = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_completed", timeout=5)
        assert completed["status"] == "failed"
        assert completed["result"] == {"error": "boom"}

        # Parent task itself is unchanged (still assigned/running).
        p = await http.get(f"{base_url}/tasks/{parent_id}", headers=headers)
        assert p.json()["status"] in ("assigned", "running")

    await ws_a.close()
    await ws_b.close()


@pytest.mark.asyncio
async def test_p2_del_4b_child_timeout_bubbles_to_parent(hub_server):
    """Child timeout via scan_timeouts_once → parent receives child_completed{status=timeout}."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A8", key)
    ws_b = await _ws_register(ws_url, "B8", key)
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A8"})
        parent_id = r.json()["task_id"]
        await asyncio.wait_for(ws_a.recv(), timeout=3)

        # Child with very small timeout so the watcher fires.
        await ws_a.send(json.dumps({
            "type": "submit_child", "request_id": "r1",
            "parent_task_id": parent_id, "target_agent": "B8", "task": "slow",
            "task_type": "x",
            "timeout": 1,
        }))
        accepted = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_accepted")
        child_id = accepted["child_task_id"]
        await asyncio.wait_for(ws_b.recv(), timeout=3)
        # B never reports — wait for the 1s timeout + watcher tick (1s).
        completed = await _ws_drain_until(
            ws_a, lambda m: m.get("type") == "child_completed", timeout=6
        )
        assert completed["status"] == "timeout"
        assert completed["child_task_id"] == child_id

    await ws_a.close()
    await ws_b.close()


@pytest.mark.asyncio
async def test_p2_del_5_permission_inheritance(hub_server):
    """Without can_assign_tasks → forbidden; with it → ok; revoking blocks next call."""
    base_url, ws_url = hub_server
    # Key WITHOUT can_assign_tasks but WITH can_register.
    no_assign = _make_key(base_url, "noassign", can_register=True)
    full = _make_key(base_url, "full", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {full}"}

    # Connect agent A with no_assign key; submit_child should be forbidden.
    ws_a = await _ws_register(ws_url, "A9", no_assign)
    ws_b = await _ws_register(ws_url, "B9", full)

    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A9"})
        parent_id = r.json()["task_id"]
        await asyncio.wait_for(ws_a.recv(), timeout=3)

        await ws_a.send(json.dumps({
            "type": "submit_child", "request_id": "r1",
            "parent_task_id": parent_id, "target_agent": "B9", "task": "x",
            "task_type": "x",
        }))
        rejected = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_rejected")
        assert rejected["reason"] == "forbidden"

    await ws_a.close()

    # Reconnect with full key; submit_child should succeed.
    ws_a2 = await _ws_register(ws_url, "A9b", full)
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A9b"})
        parent_id = r.json()["task_id"]
        await asyncio.wait_for(ws_a2.recv(), timeout=3)
        await ws_a2.send(json.dumps({
            "type": "submit_child", "request_id": "r2",
            "parent_task_id": parent_id, "target_agent": "B9", "task": "x",
            "task_type": "x",
        }))
        accepted = await _ws_drain_until(ws_a2, lambda m: m.get("type") == "child_accepted")
        assert "child_task_id" in accepted

        # Revoke 'full' key while still connected.
        admin_headers = {"Authorization": f"Bearer {SEED_ADMIN_KEY}"}
        d = await http.delete(f"{base_url}/admin/keys/full", headers=admin_headers)
        assert d.status_code == 200

        # Next submit_child should be forbidden (permission revalidated each call).
        await ws_a2.send(json.dumps({
            "type": "submit_child", "request_id": "r3",
            "parent_task_id": parent_id, "target_agent": "B9", "task": "x",
            "task_type": "x",
        }))
        rejected2 = await _ws_drain_until(ws_a2, lambda m: m.get("type") == "child_rejected")
        assert rejected2["reason"] == "forbidden"

    await ws_a2.close()
    await ws_b.close()


@pytest.mark.asyncio
async def test_p2_del_6_parent_disconnect_orphans_child_safely(hub_server):
    """Parent WS closes mid-child; child finishes; activity log records the drop."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A10", key)
    ws_b = await _ws_register(ws_url, "B10", key)
    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A10"})
        parent_id = r.json()["task_id"]
        await asyncio.wait_for(ws_a.recv(), timeout=3)

        await ws_a.send(json.dumps({
            "type": "submit_child", "request_id": "r1",
            "parent_task_id": parent_id, "target_agent": "B10", "task": "child",
            "task_type": "x",
        }))
        accepted = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_accepted")
        child_id = accepted["child_task_id"]
        await asyncio.wait_for(ws_b.recv(), timeout=3)

        # Parent disconnects (and parent task gets requeued by FR-CON-6).
        await ws_a.close()
        await asyncio.sleep(0.3)

        # B finishes the child.
        await ws_b.send(json.dumps({
            "type": "result", "task_id": child_id, "status": "completed", "result": {"ok": True}
        }))
        await asyncio.sleep(0.5)

        # Child terminal in DB; queryable via REST.
        c = await http.get(f"{base_url}/tasks/{child_id}", headers=headers)
        assert c.json()["status"] == "completed"
        assert c.json()["parent_task_id"] == parent_id

    await ws_b.close()

    # Activity log entry recorded the dropped notification.
    # Open the hub's DB directly (its path is in the subprocess env).
    import sqlite3
    # Find the tempfile by inspecting the running process — fall back to
    # iterating temp .db files matching the pattern. The fixture sets
    # AGENT_HUB_DB_PATH in env; we can extract it from the proc cmdline by
    # scanning open files. Easiest: the fixture stores tmp.name on the
    # base_url string via a side-channel? No — use the AGENT_HUB_DB_PATH
    # env var if pytest set it; otherwise scan recently-modified tmp files.
    # For deterministic behavior, the simplest thing is to also check that
    # the child's parent_task_id matches and rely on the DB activity log
    # being recorded. We re-open by scanning tmp dir for the most recent .db.
    import glob
    import os as _os
    import tempfile as _tempfile
    candidates = sorted(
        glob.glob(_os.path.join(_tempfile.gettempdir(), "*.db")),
        key=lambda p: _os.path.getmtime(p),
        reverse=True,
    )
    found = False
    for path in candidates[:5]:
        try:
            conn = sqlite3.connect(path)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM activity_log WHERE entity_id = ? AND action = ?",
                    (child_id, "child_completed_dropped"),
                )
                rows = cur.fetchall()
                if rows:
                    found = True
                    break
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            continue
    assert found, "expected child_completed_dropped activity log entry"


@pytest.mark.asyncio
async def test_p2_del_7_not_owner_rejected(hub_server):
    """B forges A's parent_task_id → child_rejected:not_owner; A's task unaffected."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A11", key)
    ws_b = await _ws_register(ws_url, "B11", key)

    async with httpx.AsyncClient() as http:
        # A is assigned a parent task.
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A11"})
        parent_id = r.json()["task_id"]
        a_msg = json.loads(await asyncio.wait_for(ws_a.recv(), timeout=3))
        assert a_msg["task"]["task_id"] == parent_id

        # B forges a submit_child for A's parent_id.
        await ws_b.send(json.dumps({
            "type": "submit_child",
            "request_id": "forge-1",
            "parent_task_id": parent_id,
            "target_agent": "A11",
            "task": "forged",
        }))
        rejected = await _ws_drain_until(ws_b, lambda m: m.get("type") == "child_rejected")
        assert rejected["reason"] == "not_owner"
        assert rejected["request_id"] == "forge-1"

        # A's parent task is unaffected (still assigned/running).
        p = await http.get(f"{base_url}/tasks/{parent_id}", headers=headers)
        assert p.json()["status"] in ("assigned", "running")
        assert p.json()["assigned_agent_id"] == "A11"

    await ws_a.close()
    await ws_b.close()


@pytest.mark.asyncio
async def test_p2_del_7b_parent_terminal_rejected(hub_server):
    """submit_child for an already-completed parent → child_rejected:parent_terminal."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    ws_a = await _ws_register(ws_url, "A12", key)

    async with httpx.AsyncClient() as http:
        r = await http.post(f"{base_url}/tasks", headers=headers,
                            json={"task": "p", "task_type": "x", "target_agent": "A12"})
        parent_id = r.json()["task_id"]
        await asyncio.wait_for(ws_a.recv(), timeout=3)

        # A completes the parent.
        await ws_a.send(json.dumps({
            "type": "result", "task_id": parent_id, "status": "completed", "result": {"ok": True}
        }))
        await asyncio.sleep(0.3)

        # Verify terminal in DB.
        p = await http.get(f"{base_url}/tasks/{parent_id}", headers=headers)
        assert p.json()["status"] == "completed"

        # Now A tries submit_child for the terminal parent.
        await ws_a.send(json.dumps({
            "type": "submit_child",
            "request_id": "term-1",
            "parent_task_id": parent_id,
            "target_agent": "A12",
            "task": "too-late",
        }))
        rejected = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_rejected")
        assert rejected["reason"] == "parent_terminal"
        assert rejected["request_id"] == "term-1"

    await ws_a.close()


# === Phase 2.2: F1 + F3 interop with delegation ===========================


@pytest.mark.asyncio
async def test_p22_del_1_submit_child_with_min_version(hub_server):
    """F1+F3+F4: submit_child with min_version routes to a v2 agent and rejects
    when the only matching target is bare-v1 (capability_unavailable).
    Rejected child must leave no orphan DB row."""
    base_url, ws_url = hub_server
    key = _make_key(base_url, "k", can_register=True, can_assign_tasks=True, can_view_tasks=True)
    headers = {"Authorization": f"Bearer {key}"}

    # A advertises ["plan"], B1 ["code"] bare = v1, B2 has code:v2 + schema.
    ws_a = await _ws_register(ws_url, "p22A", key, capabilities=["plan"])
    ws_b1 = await _ws_register(ws_url, "p22B1", key, capabilities=["code"])
    ws_b2 = await _ws_register(
        ws_url, "p22B2", key,
        capabilities=[{
            "name": "code",
            "version": 2,
            "payload_schema": {"type": "object", "required": ["src"]},
        }],
    )

    async with httpx.AsyncClient() as http:
        # Driver submits a `plan` task to A.
        r = await http.post(
            f"{base_url}/tasks", headers=headers,
            json={"task": "plan-it", "task_type": "plan", "target_agent": "p22A"},
        )
        assert r.status_code == 200, r.text
        parent_id = r.json()["task_id"]
        parent_msg = json.loads(await asyncio.wait_for(ws_a.recv(), timeout=3))
        assert parent_msg["type"] == "task"

        # (a) submit_child to B2 with min_version=2 + valid payload → success.
        await ws_a.send(json.dumps({
            "type": "submit_child",
            "request_id": "p22-ok",
            "parent_task_id": parent_id,
            "target_agent": "p22B2",
            "task_type": "code",
            "min_version": 2,
            "payload": {"src": "x"},
        }))
        accepted = await _ws_drain_until(ws_a, lambda m: m.get("type") == "child_accepted")
        child_ok_id = accepted["child_task_id"]

        # B2 receives the task.
        b2_msg = json.loads(await asyncio.wait_for(ws_b2.recv(), timeout=3))
        assert b2_msg["type"] == "task"
        assert b2_msg["task"]["task_id"] == child_ok_id

        # B2 reports completed.
        await ws_b2.send(json.dumps({
            "type": "result", "task_id": child_ok_id, "status": "completed",
            "result": {"ok": True},
        }))
        # A receives child_completed.
        completed = await _ws_drain_until(
            ws_a, lambda m: m.get("type") == "child_completed", timeout=5,
        )
        assert completed["child_task_id"] == child_ok_id

        # (b) submit_child to B1 with min_version=2 → capability_unavailable;
        # no orphan DB row.
        await ws_a.send(json.dumps({
            "type": "submit_child",
            "request_id": "p22-bad",
            "parent_task_id": parent_id,
            "target_agent": "p22B1",
            "task_type": "code",
            "min_version": 2,
            "payload": {"src": "y"},
        }))
        rejected = await _ws_drain_until(
            ws_a, lambda m: m.get("type") == "child_rejected", timeout=5,
        )
        assert rejected["reason"] == "capability_unavailable"
        assert rejected["request_id"] == "p22-bad"

        # No `task` frame should have been pushed to B1.
        # (drain ws_b1 for a brief window — should time out)
        try:
            stray = await asyncio.wait_for(ws_b1.recv(), timeout=0.5)
            raise AssertionError(f"unexpected frame to B1: {stray}")
        except asyncio.TimeoutError:
            pass

        # Orphan check: any child whose parent is parent_id but is NOT child_ok_id
        # should not exist (rolled back).
        list_r = await http.get(
            f"{base_url}/tasks?parent_task_id={parent_id}", headers=headers,
        )
        assert list_r.status_code == 200
        children = list_r.json()
        child_ids = {t["task_id"] for t in children}
        assert child_ids == {child_ok_id}, (
            f"expected only the OK child to exist, got {child_ids}"
        )

    await ws_a.close()
    await ws_b1.close()
    await ws_b2.close()
