"""Unit tests — test_plan.md §2."""
import json
import sys
import uuid

import pytest


# === U-AUTH (test_plan §2.1) ===

def test_u_auth_1_hash_sha256(hub_main):
    """U-AUTH-1: SHA-256 hash matches stored value."""
    from hub.auth import AccessControl
    raw = "raw-key-value"
    h = AccessControl.hash_key(raw)
    assert len(h) == 64  # SHA-256 hex
    assert h == AccessControl.hash_key(raw)


def test_u_auth_2_can_register_denies_without_perm(hub_main):
    """U-AUTH-2: key without can_register is denied."""
    from hub.auth import AccessControl, Permission
    ac = AccessControl()
    raw, _ = ac.create_api_key("no-register", Permission(can_register=False))
    assert ac.can_agent_register(raw) is False


def test_u_auth_3_allowed_agents_restricts_target(hub_main):
    """U-AUTH-3: allowed_agents=[a1] cannot target a2."""
    from hub.auth import AccessControl, Permission
    ac = AccessControl()
    raw, _ = ac.create_api_key(
        "scoped", Permission(can_assign_tasks=True, allowed_agents=["a1"])
    )
    assert ac.can_assign_task(raw, "a2") is False
    assert ac.can_assign_task(raw, "a1") is True


def test_u_auth_4_admin_passes_everywhere(hub_main):
    """U-AUTH-4: admin key allowed everywhere."""
    from hub.auth import AccessControl, Permission
    ac = AccessControl()
    raw, _ = ac.create_api_key("admin", Permission(is_admin=True))
    assert ac.can_agent_register(raw)
    assert ac.can_assign_task(raw, "any-agent")
    assert ac.can_view_agents(raw)
    assert ac.can_view_tasks(raw)


def test_u_auth_5_expired_key_denied(hub_main):
    """U-AUTH-5: expired key is denied."""
    from hub.auth import AccessControl, Permission
    ac = AccessControl()
    raw, _ = ac.create_api_key("expiring", Permission(can_register=True), expires_days=-1)
    assert ac.can_agent_register(raw) is False


def test_u_auth_6_inactive_key_denied(hub_main):
    """U-AUTH-6: revoked key denied."""
    from hub.auth import access_control, Permission
    raw, _ = access_control.create_api_key("revokable", Permission(can_register=True))
    access_control.revoke_key("revokable")
    assert access_control.can_agent_register(raw) is False


def test_u_auth_7_bootstrap_admin_from_env(hub_main):
    """U-AUTH-7: seed admin sourced from env, not hardcoded."""
    from hub.auth import access_control
    perm = access_control.validate_key("test-admin-key-1234567890")
    assert perm and perm.is_admin


# === U-REG (test_plan §2.2) ===

def test_u_reg_1_duplicate_agent_id_upserts(hub_main):
    """U-REG-1: duplicate agent_id upserts (per design decision §2.1)."""
    from hub.registry import AgentRegistry
    from hub.models import MachineInfo
    reg = AgentRegistry()
    reg.register("a1", ["echo"], MachineInfo())
    reg.register("a1", ["echo", "extra"], MachineInfo())
    # Phase 2.2: capabilities are normalized to Capability instances.
    cap_names = [c.name for c in reg.get("a1").capabilities]
    assert cap_names == ["echo", "extra"]


def test_u_reg_2_mark_offline_via_heartbeat(hub_main):
    from hub.registry import AgentRegistry
    from hub.models import AgentStatus, MachineInfo
    reg = AgentRegistry()
    reg.register("a1", ["echo"], MachineInfo())
    assert reg.heartbeat("a1", AgentStatus.OFFLINE)
    assert reg.get("a1").status == AgentStatus.OFFLINE


def test_u_reg_3_idle_agents_filter(hub_main):
    from hub.registry import AgentRegistry
    from hub.models import AgentStatus, MachineInfo
    reg = AgentRegistry()
    reg.register("a1", ["echo"], MachineInfo())
    reg.register("a2", ["camera"], MachineInfo())
    reg.heartbeat("a2", AgentStatus.BUSY)
    found = reg.find_available("echo")
    assert found and found.agent_id == "a1"


def test_u_reg_4_heartbeat_updates_timestamp(hub_main):
    from hub.registry import AgentRegistry
    from hub.models import AgentStatus, MachineInfo
    reg = AgentRegistry()
    reg.register("a1", ["echo"], MachineInfo())
    before = reg.get("a1").last_heartbeat
    reg.heartbeat("a1", AgentStatus.BUSY)
    assert reg.get("a1").last_heartbeat >= before
    assert reg.get("a1").status == AgentStatus.BUSY


# === U-QUE (test_plan §2.3) ===

def test_u_que_1_priority_ordering(hub_main):
    """U-QUE-1: priorities [0,5,1] dequeue as [5,1,0]."""
    from hub.queue import TaskQueue
    q = TaskQueue()
    for tid, prio in [("t0", 0), ("t1", 5), ("t2", 1)]:
        q.create(tid, "human", "echo", {}, priority=prio)
    pending = q.list_pending()
    assert [t.task_id for t in pending] == ["t1", "t2", "t0"]


def test_u_que_2_fifo_within_priority(hub_main):
    from hub.queue import TaskQueue
    import time
    q = TaskQueue()
    q.create("t1", "human", "echo", {}, priority=0)
    time.sleep(0.001)
    q.create("t2", "human", "echo", {}, priority=0)
    pending = q.list_pending()
    assert [t.task_id for t in pending] == ["t1", "t2"]


def test_u_que_3_dequeue_when_empty(hub_main):
    from hub.queue import TaskQueue
    q = TaskQueue()
    assert q.list_pending() == []
    assert q.get("nope") is None


def test_u_que_4_requeue_assigned(hub_main):
    """U-QUE-4: requeue_assigned_to puts task back in pending."""
    from hub.queue import TaskQueue
    q = TaskQueue()
    q.create("t1", "human", "echo", {})
    q.assign("t1", "a1")
    assert q.list_pending() == []
    requeued = q.requeue_assigned_to("a1")
    assert len(requeued) == 1
    assert q.list_pending()[0].task_id == "t1"
    assert q.get("t1").assigned_agent_id is None


# === U-RTE (test_plan §2.4) ===

def test_u_rte_1_target_agent_idle(hub_main):
    from hub.router import Router
    from hub.registry import AgentRegistry
    from hub.queue import TaskQueue
    from hub.models import AgentStatus, MachineInfo
    reg = AgentRegistry()
    q = TaskQueue()
    reg.register("a1", ["echo"], MachineInfo())
    q.create("t1", "human", "echo", {}, target_agent="a1")
    r = Router(reg, q)
    assert r.route("t1", "echo hi", "echo", {}, "a1") == "a1"


def test_u_rte_2_target_agent_offline_returns_none(hub_main):
    from hub.router import Router
    from hub.registry import AgentRegistry
    from hub.queue import TaskQueue
    from hub.models import AgentStatus, MachineInfo
    reg = AgentRegistry()
    q = TaskQueue()
    reg.register("a1", ["echo"], MachineInfo())
    reg.heartbeat("a1", AgentStatus.OFFLINE)
    q.create("t1", "human", "echo", {}, target_agent="a1")
    r = Router(reg, q)
    assert r.route("t1", "echo hi", "echo", {}, "a1") is None


def test_u_rte_3_target_agent_busy_returns_none(hub_main):
    from hub.router import Router
    from hub.registry import AgentRegistry
    from hub.queue import TaskQueue
    from hub.models import AgentStatus, MachineInfo
    reg = AgentRegistry()
    q = TaskQueue()
    reg.register("a1", ["echo"], MachineInfo())
    reg.heartbeat("a1", AgentStatus.BUSY)
    q.create("t1", "human", "echo", {}, target_agent="a1")
    r = Router(reg, q)
    assert r.route("t1", "echo hi", "echo", {}, "a1") is None


def test_u_rte_4_no_target_picks_matching_agent(hub_main):
    from hub.router import Router
    from hub.registry import AgentRegistry
    from hub.queue import TaskQueue
    from hub.models import MachineInfo
    reg = AgentRegistry()
    q = TaskQueue()
    reg.register("a1", ["echo"], MachineInfo())
    reg.register("a2", ["echo"], MachineInfo())
    q.create("t1", "human", "echo", {})
    r = Router(reg, q)
    assigned = r.route("t1", "echo hi", "echo", {}, None)
    assert assigned in ("a1", "a2")


def test_u_rte_5_no_match_returns_none(hub_main):
    from hub.router import Router
    from hub.registry import AgentRegistry
    from hub.queue import TaskQueue
    from hub.models import MachineInfo
    reg = AgentRegistry()
    q = TaskQueue()
    reg.register("a1", ["camera"], MachineInfo())
    q.create("t1", "human", "echo", {})
    r = Router(reg, q)
    assert r.route("t1", "echo hi", "echo", {}, None) is None


def test_u_rte_6_detect_task_type_from_nl(hub_main):
    """U-RTE-6: NL detection. Also verifies §1.4 fix (renamed to public method)."""
    from hub.router import Router
    from hub.registry import AgentRegistry
    from hub.queue import TaskQueue
    r = Router(AgentRegistry(), TaskQueue())
    assert hasattr(r, "detect_task_type"), "Router.detect_task_type should be public per design §1.4"
    assert r.detect_task_type("echo foo") == "echo"
    assert r.detect_task_type("camera test plz") == "camera-test"
    assert r.detect_task_type("xyzzy") == "general"


# === U-MOD (test_plan §2.5) ===

def test_u_mod_1_json_columns_round_trip(hub_main):
    """U-MOD-1 / NFR-PER-5: JSON columns are real JSON, not str()."""
    from hub import database as db
    db.save_agent({
        "id": "agent-json",
        "name": "n",
        "skills": ["a", "b"],
        "position": {"hostname": "h", "ip": "1.2.3.4", "platform": "docker"},
    })
    row = db.get_agent("agent-json")
    assert json.loads(row["skills"]) == ["a", "b"]
    assert json.loads(row["position"])["hostname"] == "h"


def test_u_mod_2_task_uuid_v4(hub_main, client, agent_headers):
    """U-MOD-2: submitted task IDs are valid UUIDs."""
    r = client.post("/tasks", headers=agent_headers, json={"task": "echo"})
    assert r.status_code == 200
    tid = r.json()["task_id"]
    parsed = uuid.UUID(tid)
    assert parsed.version == 4


def test_u_mod_3_defaults_applied(hub_main):
    from hub.models import TaskSubmitRequest
    req = TaskSubmitRequest(task="x")
    assert req.timeout == 300
    assert req.priority == 0


# === P2-UNIT delegation helpers (test_plan §2 — phase 2) ===

class _FakeTask:
    """Minimal stand-in for hub.models.Task used by delegation unit tests."""
    def __init__(self, task_id, parent_task_id=None, assigned_agent_id=None):
        self.task_id = task_id
        self.parent_task_id = parent_task_id
        self.assigned_agent_id = assigned_agent_id


def _make_db_get(tasks_by_id):
    return lambda tid: tasks_by_id.get(tid)


def test_p2_unit_cycle_helper_immediate_self_allowed(hub_main):
    """Immediate self A→A: parent assigned to A, target=A → allowed (None)."""
    from hub import delegation
    delegation.reset_state()
    tasks = {
        "p": _FakeTask("p", parent_task_id=None, assigned_agent_id="A"),
    }
    result = delegation.check_delegation(
        parent_task_id="p",
        target_agent="A",
        parent_agent_id="A",
        db_get_task=_make_db_get(tasks),
    )
    assert result is None


def test_p2_unit_cycle_helper_indirect_cycle_rejected(hub_main):
    """A→B→A: child of B (assigned to B) whose parent was assigned to A;
    target=A must be rejected as a cycle (non-immediate ancestor matches)."""
    from hub import delegation
    delegation.reset_state()
    tasks = {
        "root": _FakeTask("root", parent_task_id=None, assigned_agent_id="A"),
        "mid":  _FakeTask("mid",  parent_task_id="root", assigned_agent_id="B"),
    }
    result = delegation.check_delegation(
        parent_task_id="mid",
        target_agent="A",
        parent_agent_id="B",
        db_get_task=_make_db_get(tasks),
    )
    assert result == "cycle"


def test_p2_unit_cycle_helper_depth_boundary(hub_main):
    """Chain already at MAX_TASK_DEPTH → next submit returns depth_limit_exceeded."""
    from hub import delegation
    delegation.reset_state()
    # Build a chain of length exactly MAX_TASK_DEPTH whose deepest task is the
    # immediate parent. Each task assigned to a distinct agent so the cycle
    # branch can't fire first.
    tasks = {}
    prev = None
    chain_ids = []
    for i in range(delegation.MAX_TASK_DEPTH):
        tid = f"t{i}"
        tasks[tid] = _FakeTask(tid, parent_task_id=prev, assigned_agent_id=f"agent{i}")
        chain_ids.append(tid)
        prev = tid
    deepest = chain_ids[-1]
    result = delegation.check_delegation(
        parent_task_id=deepest,
        target_agent="new-agent",
        parent_agent_id=f"agent{delegation.MAX_TASK_DEPTH - 1}",
        db_get_task=_make_db_get(tasks),
    )
    assert result == "depth_limit_exceeded"


def test_p2_unit_register_release_roundtrip(hub_main):
    """register_child(P,C) then release_child(C) leaves both maps empty."""
    from hub import delegation
    delegation.reset_state()
    delegation.register_child("P", "C")
    assert delegation._parent_of_child.get("C") == "P"
    assert "C" in delegation._children_by_parent.get("P", [])
    delegation.release_child("C")
    assert delegation._parent_of_child == {}
    assert delegation._children_by_parent == {}


def test_p2_unit_rebuild_from_db(hub_main):
    """rebuild_from_db populates _parent_of_child from persisted (child, parent) pairs."""
    from hub import delegation
    delegation.reset_state()
    pairs = [
        ("c1", "p1"),
        ("c2", "p1"),
        ("c3", "p2"),
        ("top", None),  # top-level — must be skipped
    ]
    delegation.rebuild_from_db(iter(pairs))
    assert delegation._parent_of_child == {"c1": "p1", "c2": "p1", "c3": "p2"}
    assert sorted(delegation._children_by_parent.get("p1", [])) == ["c1", "c2"]
    assert delegation._children_by_parent.get("p2") == ["c3"]
    assert "top" not in delegation._parent_of_child


# === P2-PAR (Phase 2 F2 — queue.list_all parent filter + get_parent_task_id) ===

def test_p2_par_1_queue_filter_by_parent(hub_main):
    """F2 §3.2: TaskQueue.list_all(parent_task_id=X) and top_level=True filter correctly."""
    from hub.queue import TaskQueue
    q = TaskQueue()
    parent_id = "queue-parent"
    for i in range(3):
        q.create(f"qchild-{i}", "human", "echo", {}, parent_task_id=parent_id)
    for i in range(2):
        q.create(f"qtop-{i}", "human", "echo", {})

    children = q.list_all(parent_task_id=parent_id)
    assert {t.task_id for t in children} == {f"qchild-{i}" for i in range(3)}

    tops = q.list_all(top_level=True)
    assert {t.task_id for t in tops} == {f"qtop-{i}" for i in range(2)}

    with pytest.raises(ValueError):
        q.list_all(parent_task_id=parent_id, top_level=True)


def test_p2_par_1_get_parent_task_id(hub_main):
    """F2 §3.3: get_parent_task_id returns parent for child, None for root, None for missing id."""
    from hub import database as db

    db.save_task({"id": "root-1", "name": "root-1", "task_type": "echo"})
    db.save_task({"id": "child-1", "name": "child-1", "task_type": "echo", "parent_task_id": "root-1"})

    assert db.get_parent_task_id("child-1") == "root-1"
    assert db.get_parent_task_id("root-1") is None
    assert db.get_parent_task_id("does-not-exist") is None


# === Phase 2.2 unit tests (F3 schema layer) ===

def test_p22_unit_normalize_bare_string(hub_main):
    """A bare string capability normalizes to Capability(name=..., version=1, schemas=None)."""
    from hub.capabilities import normalize_capability
    from hub.models import Capability

    cap = normalize_capability("echo")
    assert isinstance(cap, Capability)
    assert cap.name == "echo"
    assert cap.version == 1
    assert cap.payload_schema is None
    assert cap.result_schema is None


def test_p22_unit_normalize_dict(hub_main):
    """A dict capability normalizes by passing fields through Capability(**dict)."""
    from hub.capabilities import normalize_capability, capability_to_dict
    from hub.models import Capability

    src = {"name": "code", "version": 2, "payload_schema": {"type": "object"}}
    cap = normalize_capability(src)
    assert isinstance(cap, Capability)
    assert cap.name == "code"
    assert cap.version == 2
    assert cap.payload_schema == {"type": "object"}
    # round-trip
    d = capability_to_dict(cap)
    assert d["name"] == "code"
    assert d["version"] == 2
    assert d["payload_schema"] == {"type": "object"}


def test_p22_unit_validate_basic_types(hub_main):
    """Pure-python validator handles type+required correctly."""
    from hub.capabilities import validate_payload

    schema = {"type": "object", "required": ["src"]}
    assert validate_payload({"src": "x"}, schema) is None
    err = validate_payload({"wrong": "y"}, schema)
    assert err is not None
    assert "src" in err


def test_p22_unit_validate_unsupported_keyword(hub_main):
    """Unsupported keywords are flagged deterministically."""
    from hub.capabilities import validate_payload

    schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
    err = validate_payload({"anything": "goes"}, schema)
    assert err == "unsupported_keyword: oneOf"


def test_p22_unit_agent_can_handle_min_version(hub_main):
    """_agent_can_handle enforces min_version against advertised capability version."""
    from hub.capabilities import _agent_can_handle
    from hub.models import Agent, AgentStatus, Capability, MachineInfo, Task

    # Bare-string agent — Capability normalizes to v1
    a_bare = Agent(
        agent_id="a-bare",
        capabilities=["code"],
        status=AgentStatus.IDLE,
        machine_info=MachineInfo(),
    )
    t_v1 = Task(task_id="t1", submitted_by="x", task_type="code", min_version=1)
    t_v2 = Task(task_id="t2", submitted_by="x", task_type="code", min_version=2)
    assert _agent_can_handle(a_bare, t_v1) is True
    assert _agent_can_handle(a_bare, t_v2) is False

    # Object capability at v3 — satisfies min_version=2
    a_obj = Agent(
        agent_id="a-obj",
        capabilities=[Capability(name="code", version=3)],
        status=AgentStatus.IDLE,
        machine_info=MachineInfo(),
    )
    assert _agent_can_handle(a_obj, t_v2) is True


def test_p22_unit_aggregate_null_advertiser_sets_conflict(hub_main):
    """Per design §3.1.1: any null/bare advertiser in a bucket sets the conflict
    flag and collapses the aggregate payload_schema to None."""
    from hub.capabilities import aggregate_capabilities
    from hub.models import Agent, AgentStatus, Capability, MachineInfo

    a_schema = Agent(
        agent_id="agent-a",
        capabilities=[Capability(name="code", version=1, payload_schema={"type": "object"})],
        status=AgentStatus.IDLE,
        machine_info=MachineInfo(),
    )
    a_bare = Agent(
        agent_id="agent-b",
        capabilities=["code"],  # bare string => version=1, payload_schema=None
        status=AgentStatus.IDLE,
        machine_info=MachineInfo(),
    )
    rows = aggregate_capabilities([a_schema, a_bare])
    bucket = next(r for r in rows if r["name"] == "code" and r["version"] == 1)
    assert bucket["payload_schema"] is None
    assert bucket.get("aggregated_schema_conflict") is True


def test_p22_unit_resolve_cap_version_picks_highest(hub_main):
    """Per design §2.3: validation uses the largest version >= min_version."""
    from hub.capabilities import _resolve_cap_version
    from hub.models import Agent, AgentStatus, Capability, MachineInfo

    agent = Agent(
        agent_id="multi",
        capabilities=[
            Capability(name="code", version=1),
            Capability(name="code", version=2),
            Capability(name="code", version=3),
        ],
        status=AgentStatus.IDLE,
        machine_info=MachineInfo(),
    )
    assert _resolve_cap_version(agent, "code", min_version=2) == 3
