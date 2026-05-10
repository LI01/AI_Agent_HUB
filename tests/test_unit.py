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


# === Phase 2.3 unit B — role_presets + _state (FR-LLM-1, FR-LLM-2, FR-LLM-2b) ===


@pytest.mark.parametrize(
    "role,expected_caps,expected_prompt,expected_budget",
    [
        (
            "pm",
            ["pm", "plan", "coordinate"],
            "You are the PM. Decompose, sequence, escalate.",
            30000,
        ),
        (
            "architect",
            ["architect", "design"],
            "You are the architect. Module boundaries, data flow, tradeoffs.",
            50000,
        ),
        (
            "designer",
            ["design", "spec"],
            "You are the designer. Concrete schemas, message shapes.",
            40000,
        ),
        (
            "coder",
            ["code"],
            "You implement features. Match style. Test-first.",
            30000,
        ),
        (
            "reviewer",
            ["review", "code-review"],
            "You critique. Find real bugs and design issues, ignore style nits.",
            20000,
        ),
        (
            "tester",
            ["test"],
            "You write tests that fail without the change and pass with it.",
            20000,
        ),
        (
            "generic",
            ["chat"],
            None,
            20000,
        ),
    ],
)
def test_p23_unit_role_preset_builtin_lookup(
    role, expected_caps, expected_prompt, expected_budget
):
    """get_preset(<role>) returns the spec capabilities, prompt, and budget
    for every built-in role (design v3 §4)."""
    from clients.role_presets import get_preset

    p = get_preset(role)
    assert p["capabilities"] == expected_caps
    if expected_prompt is None:
        assert p["prompt"] is None
    else:
        assert p["prompt"] == expected_prompt
    assert p["budget"] == expected_budget


def test_p23_unit_role_preset_unknown_raises():
    """get_preset('nonexistent') raises ValueError."""
    from clients.role_presets import get_preset

    with pytest.raises(ValueError):
        get_preset("nonexistent")


def test_p23_unit_role_preset_user_overrides(tmp_path, monkeypatch):
    """Global + local config deep-merge with local-wins precedence (per spec
    finding #8). HOME is redirected to tmp_path and cwd is changed so the real
    user config is untouched."""
    import json as _json

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    cfg_dir = tmp_path / ".agent-hub"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        _json.dumps({
            "role_budgets": {"coder": 99000, "tester": 88000},
            "role_prompts": {"coder": "global coder prompt"},
        })
    )
    (tmp_path / ".agent-hub.local.json").write_text(
        _json.dumps({
            "role_budgets": {"coder": 11111},
            "role_prompts": {"reviewer": "local reviewer prompt"},
        })
    )

    from clients.role_presets import get_preset, load_user_overrides

    role_prompts, role_budgets = load_user_overrides()

    # Local wins for coder budget; global tester budget survives the merge.
    assert role_budgets.get("coder") == 11111
    assert role_budgets.get("tester") == 88000
    # Prompts: global has coder, local has reviewer; both survive.
    assert role_prompts.get("coder") == "global coder prompt"
    assert role_prompts.get("reviewer") == "local reviewer prompt"

    coder = get_preset("coder", role_budgets=role_budgets, role_prompts=role_prompts)
    assert coder["budget"] == 11111
    assert coder["prompt"] == "global coder prompt"

    tester = get_preset("tester", role_budgets=role_budgets, role_prompts=role_prompts)
    assert tester["budget"] == 88000
    # Tester prompt has no override -> built-in value.
    assert tester["prompt"] == (
        "You write tests that fail without the change and pass with it."
    )

    reviewer = get_preset(
        "reviewer", role_budgets=role_budgets, role_prompts=role_prompts
    )
    assert reviewer["prompt"] == "local reviewer prompt"
    # Reviewer budget has no override -> built-in value.
    assert reviewer["budget"] == 20000

    # An unrelated role is entirely untouched.
    pm = get_preset("pm", role_budgets=role_budgets, role_prompts=role_prompts)
    assert pm["budget"] == 30000
    assert pm["prompt"] == "You are the PM. Decompose, sequence, escalate."


def test_p23_unit_role_preset_malformed_config_no_raise(tmp_path, monkeypatch):
    """Malformed JSON in either config file -> load_user_overrides() returns
    ({}, {}) without raising."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    cfg_dir = tmp_path / ".agent-hub"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("not json")

    from clients.role_presets import load_user_overrides

    role_prompts, role_budgets = load_user_overrides()
    assert role_prompts == {}
    assert role_budgets == {}


def test_p23_unit_state_atomic_write(tmp_path, monkeypatch):
    """atomic_write_json writes a list of dicts and leaves no .tmp file behind."""
    from clients import _state

    target = tmp_path / "spawned.json"
    monkeypatch.setattr(_state, "SPAWNED_PATH", target)

    payload = [{"agent_id": "a1", "pid": 1}, {"agent_id": "a2", "pid": 2}]
    _state.atomic_write_json(target, payload)

    import json as _json
    assert _json.loads(target.read_text()) == payload
    # No leftover temp files in the directory.
    leftovers = [
        p.name for p in tmp_path.iterdir()
        if p.name != "spawned.json"
    ]
    assert leftovers == [], f"unexpected files left behind: {leftovers}"


def test_p23_unit_state_corrupt_file_quarantined(tmp_path, monkeypatch):
    """Corrupt spawned.json -> locked_read_spawned() returns [] AND a sibling
    spawned.json.corrupt-* file appears with the original bytes."""
    from clients import _state

    target = tmp_path / "spawned.json"
    monkeypatch.setattr(_state, "SPAWNED_PATH", target)

    target.write_text("not json")

    result = _state.locked_read_spawned()
    assert result == []

    quarantined = list(tmp_path.glob("spawned.json.corrupt-*"))
    assert len(quarantined) == 1, (
        f"expected exactly one quarantine file, got {quarantined}"
    )
    assert quarantined[0].read_text() == "not json"
    # Original is gone (renamed away).
    assert not target.exists()


def test_p23_unit_state_concurrent_writers(tmp_path, monkeypatch):
    """Two threads each append one record via locked_mutate_spawned. Final
    file contains both records (no clobber). Small sleep inside mutator
    forces overlap so only the lock prevents a lost update."""
    import threading
    import time as _time

    from clients import _state

    target = tmp_path / "spawned.json"
    monkeypatch.setattr(_state, "SPAWNED_PATH", target)

    def make_mutator(tag):
        def mutator(records):
            # Force a window where the other thread would clobber if not locked.
            _time.sleep(0.05)
            return list(records) + [{"agent_id": tag}]
        return mutator

    errors = []

    def worker(tag):
        try:
            _state.locked_mutate_spawned(make_mutator(tag))
        except Exception as e:  # pragma: no cover - surfaced via assertion below
            errors.append(e)

    t1 = threading.Thread(target=worker, args=("t0",))
    t2 = threading.Thread(target=worker, args=("t1",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"worker errors: {errors}"
    final = _state.locked_read_spawned()
    assert len(final) == 2, f"expected 2 records, got {final}"
    tags = {r["agent_id"] for r in final}
    assert tags == {"t0", "t1"}


# === P23 Unit Cx — codex CLI adapter (design v3 §3.1, §3.3, §8) ===


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("hi\n[assistant]: final answer\n", "final answer"),
        ("\nassistant: hello world", "hello world"),
        ("random output", "random output"),
        ("", ""),
    ],
)
def test_p23_unit_codex_parse_assistant_marker(stdout, expected):
    """Per design §3.3 + §8: parser handles startswith and embedded markers."""
    from clients.codex.llm_agent import _parse_codex_output
    assert _parse_codex_output(stdout) == expected


def test_p23_unit_codex_compute_deadline():
    """Per design §3 finding #12: tiny timeouts get 80% (min 0.5s); else -5s."""
    from clients.codex.llm_agent import compute_deadline
    assert compute_deadline(3) == max(0.5, 3 * 0.8)
    assert compute_deadline(3) == pytest.approx(2.4)
    assert compute_deadline(60) == 55
    assert compute_deadline(5) == 4.0


# === P23 Unit Eo — opencode CLI adapter (design v3 §3.3, §3.5, §10 d) ===


def test_p23_unit_opencode_session_marker(tmp_path, monkeypatch):
    """First task: argv lacks `-c`; after a successful run the
    `.opencode-session-started` marker exists; second task's argv contains `-c`.
    Both invocations run with cwd=str(workdir). Subprocess is mocked — we never
    actually invoke opencode.
    """
    from clients.opencode import llm_agent as oc

    workdir = tmp_path / "ws"
    workdir.mkdir()

    captured: list[dict] = []

    class _CP:
        def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def fake_run(cmd, capture_output, text, timeout, cwd):
        captured.append({"cmd": list(cmd), "cwd": cwd, "timeout": timeout})
        # Minimal valid JSONL: one assistant event.
        return _CP('{"role": "assistant", "content": "hello"}\n', returncode=0)

    monkeypatch.setattr(oc.subprocess, "run", fake_run)

    class _FakeHub:
        def send_activity_log(self, *a, **kw):
            pass

    task = {"task_id": "t1", "task": "ping", "timeout": 60}

    # First call.
    res1 = oc._run_cli_for_task(task, workdir, role_prompt=None,
                                budget=10_000, role="generic", hub=_FakeHub())
    assert res1["text"] == "hello"
    assert res1["cli"] == "opencode"
    assert (workdir / oc._SESSION_MARKER).exists(), \
        "marker should be written after first successful run"

    # Second call.
    task2 = {"task_id": "t2", "task": "again", "timeout": 60}
    res2 = oc._run_cli_for_task(task2, workdir, role_prompt=None,
                                budget=10_000, role="generic", hub=_FakeHub())
    assert res2["text"] == "hello"

    assert len(captured) == 2, f"expected 2 subprocess calls, got {captured}"

    first_cmd = captured[0]["cmd"]
    second_cmd = captured[1]["cmd"]

    # Both share the leading flags.
    assert first_cmd[:4] == ["opencode", "run", "--format", "json"]
    assert second_cmd[:4] == ["opencode", "run", "--format", "json"]

    # First call: no -c flag.
    assert "-c" not in first_cmd, f"first call should not have -c: {first_cmd}"
    # Second call: -c is present.
    assert "-c" in second_cmd, f"second call should have -c: {second_cmd}"

    # cwd is always the workdir (string).
    assert captured[0]["cwd"] == str(workdir)
    assert captured[1]["cwd"] == str(workdir)


def test_p23_unit_opencode_parse_jsonl_events(tmp_path, monkeypatch):
    """Multi-line JSONL stdout: parser returns the LAST assistant message text.
    Empty / unparseable stdout falls back to the whole stdout (when no
    individual lines parse). Subprocess is mocked.
    """
    from clients.opencode import llm_agent as oc

    workdir = tmp_path / "ws"
    workdir.mkdir()

    class _CP:
        def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    # Three JSONL events; last assistant message is "third reply".
    stdout = (
        '{"type": "session_started", "session_id": "abc"}\n'
        '{"role": "assistant", "content": "first reply"}\n'
        '{"role": "assistant", "content": "third reply"}\n'
    )

    def fake_run_ok(cmd, capture_output, text, timeout, cwd):
        return _CP(stdout, returncode=0)

    monkeypatch.setattr(oc.subprocess, "run", fake_run_ok)

    class _FakeHub:
        def send_activity_log(self, *a, **kw):
            pass

    task = {"task_id": "t", "task": "x", "timeout": 60}
    res = oc._run_cli_for_task(task, workdir, role_prompt=None,
                               budget=10_000, role="generic", hub=_FakeHub())
    assert res["text"] == "third reply", f"got {res['text']!r}"
    # raw_events should retain all three parsed objects.
    assert len(res["raw_events"]) == 3
    assert res["raw_events"][0]["type"] == "session_started"

    # --- Fallback case: stdout has no parseable JSON lines at all. ---
    # Reset marker so second call still treated normally.
    fallback_text = "no events here, just plain text"

    def fake_run_plain(cmd, capture_output, text, timeout, cwd):
        return _CP(fallback_text, returncode=0)

    monkeypatch.setattr(oc.subprocess, "run", fake_run_plain)

    # New workdir to start fresh.
    workdir2 = tmp_path / "ws2"
    workdir2.mkdir()

    # Plain (non-JSON) stdout -> unparseable JSON; per design this raises
    # TaskFailed with error="cli_unparseable_json".
    from agent_sdk import TaskFailed
    with pytest.raises(TaskFailed) as ei:
        oc._run_cli_for_task(task, workdir2, role_prompt=None,
                             budget=10_000, role="generic", hub=_FakeHub())
    assert ei.value.result["error"] == "cli_unparseable_json"
    assert fallback_text in ei.value.result["stdout_tail"]

    # --- Pure-fallback case: empty stdout -> text == "" without raising. ---
    workdir3 = tmp_path / "ws3"
    workdir3.mkdir()

    def fake_run_empty(cmd, capture_output, text, timeout, cwd):
        return _CP("", returncode=0)

    monkeypatch.setattr(oc.subprocess, "run", fake_run_empty)
    res_empty = oc._run_cli_for_task(task, workdir3, role_prompt=None,
                                     budget=10_000, role="generic", hub=_FakeHub())
    assert res_empty["text"] == ""
    assert res_empty["raw_events"] == []


# === P23 Unit Dc — claude_code CLI adapter (design v3 §3.0 / §3.2 / §3.4) ===


def test_p23_unit_claude_command_construction(monkeypatch):
    """_build_claude_cmd produces the spec'd argv with correct ordering, and
    omits --append-system-prompt when role_prompt is None.

    Default: no --bare so the user's `claude login` keychain auth works.
    With AGENT_HUB_CLAUDE_BARE=1: --bare is added (hermetic mode).
    """
    import pathlib
    from clients.claude_code.llm_agent import _build_claude_cmd
    from clients.role_presets import get_preset

    workdir = "/tmp/x"
    user_prompt = "<user_prompt>"
    reviewer_prompt = get_preset("reviewer")["prompt"]

    monkeypatch.delenv("AGENT_HUB_CLAUDE_BARE", raising=False)

    cmd_with_role = _build_claude_cmd(
        pathlib.Path(workdir), reviewer_prompt, user_prompt
    )
    expected_with = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--add-dir",
        workdir,
        "--append-system-prompt",
        reviewer_prompt,
        user_prompt,
    ]
    assert cmd_with_role == expected_with
    assert "--bare" not in cmd_with_role

    # role=generic -> role_prompt is None -> --append-system-prompt omitted.
    generic_prompt = get_preset("generic")["prompt"]
    assert generic_prompt is None

    cmd_generic = _build_claude_cmd(
        pathlib.Path(workdir), generic_prompt, user_prompt
    )
    expected_generic = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--add-dir",
        workdir,
        user_prompt,
    ]
    assert cmd_generic == expected_generic
    assert "--append-system-prompt" not in cmd_generic
    assert "--bare" not in cmd_generic

    # Hermetic mode: --bare is added when env var is set.
    monkeypatch.setenv("AGENT_HUB_CLAUDE_BARE", "1")
    cmd_bare = _build_claude_cmd(pathlib.Path(workdir), reviewer_prompt, user_prompt)
    assert "--bare" in cmd_bare
    assert cmd_bare.index("--bare") == 2  # right after `-p`


def test_p23_unit_claude_parse_json_response():
    """_parse_claude_response extracts the assistant text and sums usage tokens.

    Per design §3.4: text from `result` (or `response`); usage_total from
    `usage.total_tokens` else `usage.input_tokens + usage.output_tokens`.
    """
    from clients.claude_code.llm_agent import _parse_claude_response

    # Spec fixture from the task: {"result": "hello", "usage": {input:100, output:200}}
    parsed = {
        "result": "hello",
        "usage": {"input_tokens": 100, "output_tokens": 200},
    }
    text, total = _parse_claude_response(parsed)
    assert text == "hello"
    assert total == 300

    # `response` takes precedence over `result` (forward-compat per design note);
    # `total_tokens` overrides input+output sum.
    text2, total2 = _parse_claude_response(
        {"response": "newer", "result": "older", "usage": {"total_tokens": 42}}
    )
    assert text2 == "newer"
    assert total2 == 42

    # Missing usage -> 0.
    text3, total3 = _parse_claude_response({"result": "ok"})
    assert text3 == "ok"
    assert total3 == 0


# === P23 Unit G — agent-tasks skill helper (design v3 §2.2 + §10 g) ===


def _load_tasks_module():
    """The skill dir is hyphenated (`agent-tasks`) so it isn't an importable
    Python package. Load `scripts/tasks.py` as a standalone module."""
    import importlib.util
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    script = repo_root / "skills" / "agent-tasks" / "scripts" / "tasks.py"
    spec = importlib.util.spec_from_file_location("agent_tasks_skill_tasks", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_p23_unit_tasks_render_table_metadata_priority():
    """render_agent_table resolves cli/role per design v3 §2.2 finding 5:
    spawned.json > description tag > 'unknown'."""
    tasks = _load_tasks_module()

    agents = [
        # X: in spawned.json AND has a (different) description tag — spawned wins.
        {
            "agent_id": "X",
            "status": "idle",
            "description": "cli=claude;role=coder",
            "capabilities": [{"name": "design", "version": 1}],
        },
        # Y: only in /agents, with a description tag — description wins.
        {
            "agent_id": "Y",
            "status": "idle",
            "description": "cli=opencode;role=pm",
            "capabilities": [{"name": "pm", "version": 1}],
        },
        # Z: no spawned record, no description -> "unknown" / "unknown".
        {
            "agent_id": "Z",
            "status": "idle",
            "capabilities": [{"name": "chat", "version": 1}],
        },
    ]
    spawned = [
        {"agent_id": "X", "cli": "codex", "role": "designer", "pid": 1234},
    ]

    table = tasks.render_agent_table(agents, spawned)

    # Header is present.
    assert "agent_id" in table
    assert "cli" in table
    assert "role" in table

    # Each row's metadata is on a single line containing the agent_id.
    rows_by_id = {}
    for line in table.splitlines():
        for aid in ("X", "Y", "Z"):
            # Match by whole-token to avoid the header line catching X/Y/Z.
            tokens = line.split()
            if aid in tokens:
                rows_by_id[aid] = line

    assert "X" in rows_by_id, f"missing X row in:\n{table}"
    assert "Y" in rows_by_id, f"missing Y row in:\n{table}"
    assert "Z" in rows_by_id, f"missing Z row in:\n{table}"

    # X: spawned.json wins over description.
    x_tokens = rows_by_id["X"].split()
    assert "codex" in x_tokens, f"X row should show cli=codex (spawned wins): {x_tokens}"
    assert "designer" in x_tokens, f"X row should show role=designer: {x_tokens}"
    assert "claude" not in x_tokens, f"X row should NOT show description's cli: {x_tokens}"

    # Y: description tag is used.
    y_tokens = rows_by_id["Y"].split()
    assert "opencode" in y_tokens, f"Y row should show cli=opencode: {y_tokens}"
    assert "pm" in y_tokens, f"Y row should show role=pm: {y_tokens}"

    # Z: fallback "unknown".
    z_tokens = rows_by_id["Z"].split()
    assert z_tokens.count("unknown") >= 2, f"Z row should show unknown/unknown: {z_tokens}"


def test_p23_unit_tasks_prompt_payload_object_schema():
    """For type:object with properties, prompt per field. Coerce 'integer'
    types to int. Per design v3 §10 g (default mode)."""
    tasks = _load_tasks_module()

    schema = {
        "type": "object",
        "properties": {
            "src": {"type": "string"},
            "n":   {"type": "integer"},
        },
        "required": ["src", "n"],
    }

    answers = iter(["hello", "42"])

    def fake_input(prompt):
        return next(answers)

    out = tasks.prompt_payload_from_schema(schema, "code", input_fn=fake_input)
    assert out == {"src": "hello", "n": 42}, out


def test_p23_unit_tasks_prompt_payload_raw_fallback():
    """Unsupported keyword (oneOf) -> raw JSON fallback. Per design v3 §10 g."""
    tasks = _load_tasks_module()

    schema = {
        "oneOf": [
            {"type": "object", "properties": {"x": {"type": "integer"}}},
            {"type": "object", "properties": {"y": {"type": "string"}}},
        ],
    }

    answers = iter(['{"x": 1}'])

    def fake_input(prompt):
        return next(answers)

    out = tasks.prompt_payload_from_schema(schema, "weird", input_fn=fake_input)
    assert out == {"x": 1}, out


def test_p23_unit_tasks_prompt_payload_nested_object():
    """Recurse one level into nested type:object properties. Per design v3 §10 g."""
    tasks = _load_tasks_module()

    schema = {
        "type": "object",
        "properties": {
            "author": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                },
            },
        },
    }

    answers = iter(["alice", "a@b.com"])

    def fake_input(prompt):
        return next(answers)

    out = tasks.prompt_payload_from_schema(schema, "publish", input_fn=fake_input)
    assert out == {"author": {"name": "alice", "email": "a@b.com"}}, out


def test_p23_unit_tasks_submit_task_includes_timeout(monkeypatch):
    """submit_task threads `timeout` into the POST /tasks body. Per design v3 §2.2."""
    tasks = _load_tasks_module()

    captured: dict = {}

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"task_id": "tid-123"}

        text = ""

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return _FakeResp()

    monkeypatch.setattr(tasks.httpx, "post", fake_post)

    task_id = tasks.submit_task(
        "http://hub",
        "key",
        target_agent="x",
        task_type="t",
        payload={},
        timeout=60,
    )
    assert task_id == "tid-123"
    assert captured["body"].get("timeout") == 60, captured["body"]


# === U-P23-SPAWN (Phase 2.3 — agent-spawn skill, Unit F) ==================

def _import_spawn_module():
    """Import skills/agent-spawn/scripts/spawn.py without relying on the
    package layout (the directory name contains a hyphen, so it isn't a valid
    Python package). Returns the loaded module object."""
    import importlib.util
    import pathlib as _pl

    repo_root = _pl.Path(__file__).resolve().parent.parent
    spawn_path = repo_root / "skills" / "agent-spawn" / "scripts" / "spawn.py"
    spec = importlib.util.spec_from_file_location("agent_spawn_script", spawn_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_p23_unit_spawn_config_precedence(tmp_path, monkeypatch):
    """load_config() deep-merges global ~/.agent-hub/config.json with
    <cwd>/.agent-hub.local.json — local wins per-key, global keys preserved
    for non-overlapping keys (design §10 #8)."""
    import json as _json

    spawn = _import_spawn_module()

    # Sandbox HOME and cwd so we never touch the real user config.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    global_dir = tmp_path / ".agent-hub"
    global_dir.mkdir(parents=True, exist_ok=True)
    (global_dir / "config.json").write_text(_json.dumps({
        "hub_url": "http://global:8080",
        "api_key": "global-key",
        "role_budgets": {"coder": 10, "tester": 20},
        "only_global": "G",
    }))

    (tmp_path / ".agent-hub.local.json").write_text(_json.dumps({
        "hub_url": "http://local:8080",
        "role_budgets": {"coder": 999},
        "only_local": "L",
    }))

    merged = spawn.load_config()

    # Local wins for overlapping top-level scalars.
    assert merged["hub_url"] == "http://local:8080"
    # Global keys preserved when not in local.
    assert merged["api_key"] == "global-key"
    assert merged["only_global"] == "G"
    # Local-only keys present.
    assert merged["only_local"] == "L"
    # Nested deep-merge: coder overridden by local, tester preserved from global.
    assert merged["role_budgets"]["coder"] == 999
    assert merged["role_budgets"]["tester"] == 20

    # The pure helper agrees with the same-input deep-merge.
    pure = spawn.merge_config(
        {"a": 1, "nested": {"x": 1, "y": 2}},
        {"b": 2, "nested": {"y": 99, "z": 3}},
    )
    assert pure == {"a": 1, "b": 2, "nested": {"x": 1, "y": 99, "z": 3}}


def test_p23_unit_spawn_workspace_creation(tmp_path, monkeypatch):
    """create_workspace() creates ~/.agent-hub/pipelines/<pipe>/<agent>/
    under a monkeypatched HOME and leaves the workdir empty."""
    import pathlib as _pl

    spawn = _import_spawn_module()

    monkeypatch.setattr(
        _pl.Path, "home", classmethod(lambda cls: tmp_path),
    )

    workdir = spawn.create_workspace("test-pipe", "test-agent")

    assert workdir.is_dir(), f"workdir not created: {workdir}"
    assert workdir == tmp_path / ".agent-hub" / "pipelines" / "test-pipe" / "test-agent"
    assert workdir.parent == tmp_path / ".agent-hub" / "pipelines" / "test-pipe"
    # Newly-created workdir must be empty.
    assert list(workdir.iterdir()) == []

    # Default pipeline_id when omitted.
    workdir2 = spawn.create_workspace(None, "lonely-agent")
    assert workdir2 == (
        tmp_path / ".agent-hub" / "pipelines" / "default-lonely-agent" / "lonely-agent"
    )
    assert workdir2.is_dir()


def test_p23_unit_spawn_record_roundtrip(tmp_path, monkeypatch):
    """append_spawned_record() persists a record that locked_read_spawned()
    can read back. SPAWNED_PATH is monkeypatched to a tmp file so the real
    user state is never touched."""
    spawn = _import_spawn_module()

    from clients import _state

    target = tmp_path / "spawned.json"
    monkeypatch.setattr(_state, "SPAWNED_PATH", target)

    record = {
        "agent_id": "x",
        "pid": 99,
        "cli": "codex",
        "role": "coder",
        "pipeline_id": "p1",
        "hub_url": "http://localhost:8080",
        "workdir": str(tmp_path / "ws"),
        "started_at": "2026-05-09T00:00:00Z",
    }

    returned = spawn.append_spawned_record(record)
    assert returned == [record]

    read_back = _state.locked_read_spawned()
    assert len(read_back) == 1
    assert read_back[0] == record

    # A second append accumulates rather than clobbering.
    record2 = dict(record)
    record2["agent_id"] = "y"
    record2["pid"] = 100
    spawn.append_spawned_record(record2)

    final = _state.locked_read_spawned()
    assert len(final) == 2
    assert {r["agent_id"] for r in final} == {"x", "y"}


def test_p23_unit_spawn_argparse_includes_custom_flags():
    """Argparse must accept --capabilities and --system-prompt-extra flags so
    the spawn helper can forward them to the adapter for --role custom
    (design §2.1, §10(f))."""
    spawn = _import_spawn_module()

    parser = spawn._build_argparser()
    flag_strings = set()
    for action in parser._actions:
        flag_strings.update(action.option_strings)

    assert "--capabilities" in flag_strings, (
        f"--capabilities missing from argparse; got {sorted(flag_strings)}"
    )
    assert "--system-prompt-extra" in flag_strings, (
        f"--system-prompt-extra missing from argparse; got {sorted(flag_strings)}"
    )

    # And they actually parse end-to-end on a minimal command line.
    ns = parser.parse_args([
        "--cli", "codex", "--role", "custom",
        "--capabilities", "alpha,beta",
        "--system-prompt-extra", "be terse",
    ])
    assert ns.capabilities == "alpha,beta"
    assert ns.system_prompt_extra == "be terse"


def test_p23_unit_spawn_codex_no_key_in_argv(tmp_path, monkeypatch):
    """spawn_adapter() must NOT pass --key on argv for codex (or any CLI).
    The API key is provided exclusively via the AGENT_HUB_API_KEY env var
    so it doesn't leak through process listings (design §2.1)."""
    import subprocess as _sp

    spawn = _import_spawn_module()

    captured: dict = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)

    workdir = tmp_path / "ws"
    pid = spawn.spawn_adapter(
        cli="codex",
        agent_id="codex-coder-1",
        hub_url="http://localhost:8080",
        api_key="secret-shhh",
        role="coder",
        workdir=workdir,
        max_budget_tokens=12345,
        description="cli=codex;role=coder;pipeline=p1",
    )
    assert pid == 4242

    cmd = captured["cmd"]
    assert "--key" not in cmd, (
        f"--key must not appear in argv for codex; got {cmd}"
    )
    # And the secret value itself must not be anywhere in argv (defence in
    # depth — it could only be there because of --key).
    assert "secret-shhh" not in cmd, f"api_key leaked into argv: {cmd}"

    # The env passed to Popen MUST carry AGENT_HUB_API_KEY.
    assert captured["env"].get("AGENT_HUB_API_KEY") == "secret-shhh"


def test_p23_unit_spawn_custom_role_forwards_capabilities(tmp_path, monkeypatch):
    """spawn_adapter() must forward --capabilities and --system-prompt-extra
    to the adapter when supplied (design §3.0, §10(f))."""
    import subprocess as _sp

    spawn = _import_spawn_module()

    captured: dict = {}

    class _FakeProc:
        pid = 5151

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeProc()

    monkeypatch.setattr(_sp, "Popen", _fake_popen)

    pid = spawn.spawn_adapter(
        cli="claude",
        agent_id="claude-custom-1",
        hub_url="http://localhost:8080",
        api_key="k",
        role="custom",
        workdir=tmp_path / "ws",
        max_budget_tokens=None,
        description="d",
        capabilities="x,y",
        system_prompt_extra="be helpful",
    )
    assert pid == 5151

    cmd = captured["cmd"]
    # `--capabilities x,y` is present as adjacent argv entries.
    assert "--capabilities" in cmd, f"--capabilities missing from argv: {cmd}"
    cap_idx = cmd.index("--capabilities")
    assert cmd[cap_idx + 1] == "x,y", f"unexpected capabilities value: {cmd}"

    assert "--system-prompt-extra" in cmd, (
        f"--system-prompt-extra missing from argv: {cmd}"
    )
    sp_idx = cmd.index("--system-prompt-extra")
    assert cmd[sp_idx + 1] == "be helpful", f"unexpected sys-prompt: {cmd}"


# === P23 Unit H — agent-close skill (design v3 §2.3, §5, §10 fix #13) ===


def _import_close_module():
    """Import skills/agent-close/scripts/close.py via importlib (the directory
    name has a hyphen, so it can't be a real package). Returns the module."""
    import importlib.util
    import pathlib as _pl

    repo_root = _pl.Path(__file__).resolve().parents[1]
    src = repo_root / "skills" / "agent-close" / "scripts" / "close.py"
    spec = importlib.util.spec_from_file_location("agent_close_close", str(src))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_p23_unit_close_pid_check_inconclusive(tmp_path, monkeypatch):
    """pid_is_adapter -> None must skip os.kill, skip purge, set
    pid_check_inconclusive, and NOT falsely set pid_reused or terminated.
    /unregister IS called. Per design v3 §2.3 + §10 fix #13."""
    from clients import _state
    close_mod = _import_close_module()

    # Sandbox HOME so pipelines paths and spawned.json point at tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        _state,
        "SPAWNED_PATH",
        tmp_path / ".agent-hub" / "spawned.json",
    )
    monkeypatch.setattr(
        close_mod,
        "PIPELINES_ROOT",
        tmp_path / ".agent-hub" / "pipelines",
    )

    # Trusted workdir under PIPELINES_ROOT — purge would otherwise be allowed,
    # so we can prove inconclusive PID-check still skips it.
    workdir = tmp_path / ".agent-hub" / "pipelines" / "p1" / "agent-x"
    workdir.mkdir(parents=True)
    (workdir / "marker").write_text("present")

    _state.locked_mutate_spawned(lambda _: [{
        "agent_id": "agent-x",
        "pid": 99999,
        "cli": "codex",
        "role": "designer",
        "pipeline_id": "p1",
        "hub_url": "http://localhost:8080",
        "workdir": str(workdir),
        "started_at": "2026-05-09T12:00:00Z",
    }])

    # pid alive but adapter check inconclusive (None).
    monkeypatch.setattr(close_mod, "pid_is_alive", lambda pid: True)
    monkeypatch.setattr(close_mod, "pid_is_adapter", lambda pid: None)

    kill_calls: list = []
    monkeypatch.setattr(
        close_mod.os, "kill", lambda *a, **k: kill_calls.append(a)
    )
    rmtree_calls: list = []
    monkeypatch.setattr(
        close_mod.shutil,
        "rmtree",
        lambda p, *a, **k: rmtree_calls.append(p),
    )

    unreg_calls: list = []

    def fake_unregister(hub_url, api_key, agent_id):
        unreg_calls.append((hub_url, agent_id))
        return {"ok": True, "status_code": 200, "error": None}

    monkeypatch.setattr(close_mod, "unregister_agent", fake_unregister)

    rc = close_mod.main([
        "--hub", "http://localhost:8080",
        "--key", "k",
        "--agent", "agent-x",
        "--purge",
    ])
    assert rc == 0

    # No kill, no purge.
    assert kill_calls == [], (
        f"os.kill must not be called when pid_check is None, got {kill_calls}"
    )
    assert rmtree_calls == [], (
        f"shutil.rmtree must not be called when pid_check is None, "
        f"got {rmtree_calls}"
    )
    # /unregister IS called.
    assert unreg_calls == [("http://localhost:8080", "agent-x")]

    # Inspect the per-row report directly (main prints to stdout but does
    # not return it). spawned record was already removed by main(); rebuild
    # the by_id map for direct inspection.
    spawned_by_id = {
        "agent-x": {
            "agent_id": "agent-x",
            "pid": 99999,
            "workdir": str(workdir),
        }
    }
    rep = close_mod.close_one_agent(
        "agent-x",
        spawned_by_id,
        "http://localhost:8080",
        "k",
        purge=True,
    )
    assert rep.get("pid_check_inconclusive") is True
    assert "pid_reused" not in rep, (
        f"pid_reused must NOT be set when pid_check is None, got {rep}"
    )
    assert "terminated" not in rep, (
        f"terminated must NOT be set when pid_check is None, got {rep}"
    )
    assert rep.get("purge_skipped_process_still_alive_or_untrusted_path") is True
    assert rep.get("unregistered") is True

    # Workspace must still exist (not purged).
    assert workdir.exists()
    assert (workdir / "marker").exists()

    # spawned.json record was removed in the main() call.
    final = _state.locked_read_spawned()
    assert final == [], f"spawned record should have been removed, got {final}"


def test_p23_unit_close_kill_then_purge(tmp_path, monkeypatch):
    """pid_is_adapter -> True, pid_is_alive -> True then False after SIGTERM:
    SIGTERM is sent, purge runs (workdir under ~/.agent-hub/pipelines/),
    /unregister called, spawned record removed."""
    from clients import _state
    close_mod = _import_close_module()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        _state, "SPAWNED_PATH", tmp_path / ".agent-hub" / "spawned.json"
    )
    monkeypatch.setattr(
        close_mod,
        "PIPELINES_ROOT",
        tmp_path / ".agent-hub" / "pipelines",
    )

    workdir = tmp_path / ".agent-hub" / "pipelines" / "p1" / "agent-y"
    workdir.mkdir(parents=True)
    (workdir / "stdout.log").write_text("hi")

    _state.locked_mutate_spawned(lambda _: [{
        "agent_id": "agent-y",
        "pid": 12345,
        "cli": "codex",
        "role": "coder",
        "pipeline_id": "p1",
        "hub_url": "http://localhost:8080",
        "workdir": str(workdir),
        "started_at": "2026-05-09T12:00:00Z",
    }])

    # pid_is_alive: True for the first two probes (initial gate inside
    # close_one_agent + the entry guard inside terminate_pid), then False.
    alive_state = {"calls": 0}

    def fake_alive(pid):
        alive_state["calls"] += 1
        return alive_state["calls"] <= 2

    monkeypatch.setattr(close_mod, "pid_is_alive", fake_alive)
    monkeypatch.setattr(close_mod, "pid_is_adapter", lambda pid: True)

    kill_calls: list = []
    monkeypatch.setattr(
        close_mod.os,
        "kill",
        lambda pid, sig: kill_calls.append((pid, sig)),
    )
    # Make time.sleep a no-op so the test is fast.
    monkeypatch.setattr(close_mod.time, "sleep", lambda *_a, **_k: None)

    unreg_calls: list = []

    def fake_unregister(hub_url, api_key, agent_id):
        unreg_calls.append((hub_url, agent_id))
        return {"ok": True, "status_code": 200, "error": None}

    monkeypatch.setattr(close_mod, "unregister_agent", fake_unregister)

    rc = close_mod.main([
        "--hub", "http://localhost:8080",
        "--key", "k",
        "--agent", "agent-y",
        "--purge",
    ])
    assert rc == 0

    assert kill_calls, "expected at least one os.kill call"
    assert kill_calls[0] == (12345, close_mod.signal.SIGTERM)

    assert unreg_calls == [("http://localhost:8080", "agent-y")]

    # Workdir was purged.
    assert not workdir.exists(), (
        f"workdir should have been purged but still exists: {workdir}"
    )

    final = _state.locked_read_spawned()
    assert final == []


def test_p23_unit_close_remote_only_no_purge(tmp_path, monkeypatch):
    """Registry-only agent (not in spawned.json) -> /unregister only,
    no os.kill, no shutil.rmtree (no trusted local workdir)."""
    from clients import _state
    close_mod = _import_close_module()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        _state, "SPAWNED_PATH", tmp_path / ".agent-hub" / "spawned.json"
    )
    monkeypatch.setattr(
        close_mod,
        "PIPELINES_ROOT",
        tmp_path / ".agent-hub" / "pipelines",
    )

    # spawned.json starts empty.
    kill_calls: list = []
    rmtree_calls: list = []
    unreg_calls: list = []

    monkeypatch.setattr(
        close_mod.os, "kill", lambda *a, **k: kill_calls.append(a)
    )
    monkeypatch.setattr(
        close_mod.shutil,
        "rmtree",
        lambda p, *a, **k: rmtree_calls.append(p),
    )

    def fake_unregister(hub_url, api_key, agent_id):
        unreg_calls.append((hub_url, agent_id))
        return {"ok": True, "status_code": 200, "error": None}

    monkeypatch.setattr(close_mod, "unregister_agent", fake_unregister)

    # Defensive: pid checks must not influence outcome for registry-only.
    monkeypatch.setattr(close_mod, "pid_is_alive", lambda pid: False)
    monkeypatch.setattr(close_mod, "pid_is_adapter", lambda pid: True)

    rc = close_mod.main([
        "--hub", "http://localhost:8080",
        "--key", "k",
        "--agent", "remote-only",
        "--purge",
    ])
    assert rc == 0

    assert kill_calls == [], f"no os.kill expected, got {kill_calls}"
    assert rmtree_calls == [], (
        f"no purge expected for remote-only agent, got {rmtree_calls}"
    )
    assert unreg_calls == [("http://localhost:8080", "remote-only")]

    # Inspect the per-row report directly.
    rep = close_mod.close_one_agent(
        "remote-only", {}, "http://localhost:8080", "k", purge=True
    )
    assert rep.get("unregistered") is True
    assert rep.get("purge_skipped_process_still_alive_or_untrusted_path") is True
    # local is None -> terminated branch in design §2.3.
    assert rep.get("terminated") is True
