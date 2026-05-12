"""REST integration tests — test_plan.md §3."""
import pytest


# === I-REG: Agent lifecycle (test_plan §3.1) ===

def test_i_reg_1_register_with_perm(client, agent_headers):
    r = client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    assert r.status_code == 200
    assert r.json()["agent_id"] == "a1"


def test_i_reg_2_register_without_auth(client):
    r = client.post("/register", json={"agent_id": "a1", "capabilities": ["echo"]})
    assert r.status_code == 403  # actually it's 403 in this implementation


def test_i_reg_3_register_without_perm(client, viewer_key):
    r = client.post(
        "/register",
        headers={"Authorization": f"Bearer {viewer_key}"},
        json={"agent_id": "a1", "capabilities": ["echo"]},
    )
    assert r.status_code == 403


def test_i_reg_4_heartbeat_changes_status(client, agent_headers):
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    r = client.post("/heartbeat", headers=agent_headers, json={"agent_id": "a1", "status": "busy"})
    assert r.status_code == 200
    g = client.get("/agents/a1", headers=agent_headers)
    assert g.json()["status"] == "busy"


def test_i_reg_5_unregister(client, agent_headers):
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    r = client.post("/unregister?agent_id=a1", headers=agent_headers)
    assert r.status_code == 200
    g = client.get("/agents/a1", headers=agent_headers)
    assert g.status_code == 404


def test_i_reg_6_register_dispatches_pending_task(client, agent_headers, submitter_key):
    """FR-REG-6: pending task auto-dispatches on registration."""
    sub_headers = {"Authorization": f"Bearer {submitter_key}"}
    r = client.post("/tasks", headers=sub_headers, json={"task": "echo", "task_type": "echo"})
    tid = r.json()["task_id"]
    assert r.json()["status"] == "queued"
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    g = client.get(f"/tasks/{tid}", headers=sub_headers)
    assert g.json()["status"] == "assigned"
    assert g.json()["assigned_agent_id"] == "a1"


# === I-TSK: Task submission (test_plan §3.2) ===

def test_i_tsk_1_minimal_submit(client, agent_headers):
    r = client.post("/tasks", headers=agent_headers, json={"task": "echo hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"]
    assert body["status"] in ("queued", "assigned")


def test_i_tsk_2_payload_round_trip(client, agent_headers):
    payload = {"foo": "bar", "n": 42, "list": [1, 2, 3]}
    r = client.post("/tasks", headers=agent_headers, json={"task": "x", "payload": payload, "task_type": "echo"})
    tid = r.json()["task_id"]
    g = client.get(f"/tasks/{tid}", headers=agent_headers)
    body = g.json()
    for k, v in payload.items():
        assert body["payload"][k] == v


def test_i_tsk_3_submit_without_auth(client):
    r = client.post("/tasks", json={"task": "echo"})
    assert r.status_code == 403


def test_i_tsk_4_target_agent_stored(client, agent_headers):
    r = client.post("/tasks", headers=agent_headers, json={"task": "x", "target_agent": "a1", "task_type": "echo"})
    tid = r.json()["task_id"]
    g = client.get(f"/tasks/{tid}", headers=agent_headers)
    assert g.json()["target_agent"] == "a1"


def test_i_tsk_6_priority_dispatch_order(client, agent_headers, submitter_key):
    """FR-TSK-7: higher priority dispatched first.

    Note: this also surfaces a bug — after first assignment, the agent's
    registry status stays IDLE, so dispatch_pending_for_agent keeps assigning
    to the same agent instead of stopping at one. We test only the priority
    order via started_at timestamps.
    """
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    low = client.post("/tasks", headers=sub_h, json={"task": "low", "task_type": "echo", "priority": 0}).json()["task_id"]
    high = client.post("/tasks", headers=sub_h, json={"task": "high", "task_type": "echo", "priority": 10}).json()["task_id"]
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    high_task = client.get(f"/tasks/{high}", headers=sub_h).json()
    low_task = client.get(f"/tasks/{low}", headers=sub_h).json()
    assert high_task["status"] == "assigned"
    if low_task.get("started_at") and high_task.get("started_at"):
        assert high_task["started_at"] <= low_task["started_at"], "high priority must be assigned first"


# === I-CFA: Cloudflare Access JWT alternate auth for view endpoints ===

class _StubCfVerifier:
    """Test double — returns claims for one specific token, None otherwise."""
    def __init__(self, accept_token: str):
        self.accept = accept_token

    def verify(self, token):
        if token == self.accept:
            return {"email": "alice@example.com"}
        return None


def test_i_cfa_1_jwt_grants_view_when_verifier_accepts(client, hub_main):
    hub_main.cf_access_verifier = _StubCfVerifier("good-jwt")
    try:
        r = client.get("/agents", headers={"Cf-Access-Jwt-Assertion": "good-jwt"})
        assert r.status_code == 200
        r = client.get("/tasks", headers={"Cf-Access-Jwt-Assertion": "good-jwt"})
        assert r.status_code == 200
    finally:
        hub_main.cf_access_verifier = None


def test_i_cfa_2_invalid_jwt_returns_403(client, hub_main):
    hub_main.cf_access_verifier = _StubCfVerifier("good-jwt")
    try:
        r = client.get("/agents", headers={"Cf-Access-Jwt-Assertion": "wrong-token"})
        assert r.status_code == 403
    finally:
        hub_main.cf_access_verifier = None


def test_i_cfa_3_jwt_does_not_grant_mutating_endpoints(client, hub_main):
    """JWT path is view-only; submit-task still needs a bearer with can_assign_tasks."""
    hub_main.cf_access_verifier = _StubCfVerifier("good-jwt")
    try:
        r = client.post(
            "/tasks",
            headers={"Cf-Access-Jwt-Assertion": "good-jwt"},
            json={"task": "echo"},
        )
        assert r.status_code == 403
    finally:
        hub_main.cf_access_verifier = None


def test_i_cfa_4_jwt_path_disabled_when_verifier_is_none(client, hub_main):
    """With no verifier configured, the JWT header is ignored — bearer still required."""
    assert hub_main.cf_access_verifier is None
    r = client.get("/agents", headers={"Cf-Access-Jwt-Assertion": "good-jwt"})
    assert r.status_code == 403


# === I-CAN: Task cancellation ===

def test_i_can_1_cancel_queued_task_returns_200(client, agent_headers):
    """POST /tasks/{id}/cancel on a queued task marks it failed with
    result.error='cancelled'."""
    r = client.post("/tasks", headers=agent_headers, json={
        "task": "x", "task_type": "nobody-handles-this", "target_agent": "ghost"
    })
    tid = r.json()["task_id"]
    assert client.get(f"/tasks/{tid}", headers=agent_headers).json()["status"] == "queued"

    c = client.post(f"/tasks/{tid}/cancel", headers=agent_headers)
    assert c.status_code == 200
    assert c.json() == {"status": "cancelled", "task_id": tid}

    after = client.get(f"/tasks/{tid}", headers=agent_headers).json()
    assert after["status"] == "failed"
    assert after["result"]["error"] == "cancelled"


def test_i_can_2_cancel_unknown_task_returns_404(client, agent_headers):
    r = client.post("/tasks/does-not-exist/cancel", headers=agent_headers)
    assert r.status_code == 404


def test_i_can_3_cancel_already_terminal_returns_409(client, agent_headers):
    r = client.post("/tasks", headers=agent_headers, json={
        "task": "x", "task_type": "nobody-handles-this", "target_agent": "ghost"
    })
    tid = r.json()["task_id"]
    assert client.post(f"/tasks/{tid}/cancel", headers=agent_headers).status_code == 200

    second = client.post(f"/tasks/{tid}/cancel", headers=agent_headers)
    assert second.status_code == 409


def test_i_can_4_cancel_without_assign_perm_returns_403(client, agent_headers, admin_headers):
    """Cancel requires can_assign_tasks (same as submit)."""
    r = client.post("/tasks", headers=agent_headers, json={
        "task": "x", "task_type": "nobody-handles-this", "target_agent": "ghost"
    })
    tid = r.json()["task_id"]

    viewer = client.post("/admin/keys", headers=admin_headers, json={
        "name": "viewer-only", "can_view_tasks": True
    }).json()["api_key"]
    viewer_h = {"Authorization": f"Bearer {viewer}"}

    c = client.post(f"/tasks/{tid}/cancel", headers=viewer_h)
    assert c.status_code == 403


# === I-QRY: Queries (test_plan §3.3) ===

def test_i_qry_1_get_task_returns_status(client, agent_headers):
    r = client.post("/tasks", headers=agent_headers, json={"task": "x", "task_type": "echo"})
    tid = r.json()["task_id"]
    g = client.get(f"/tasks/{tid}", headers=agent_headers)
    assert g.status_code == 200
    body = g.json()
    assert "status" in body and "logs" in body


def test_i_qry_2_filter_by_status(client, agent_headers):
    client.post("/tasks", headers=agent_headers, json={"task": "x", "task_type": "echo"})
    r = client.get("/tasks?status=queued", headers=agent_headers)
    assert r.status_code == 200
    assert all(t["status"] == "queued" for t in r.json())


def test_i_qry_3_filter_by_agent(client, agent_headers, submitter_key):
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    client.post("/tasks", headers=sub_h, json={"task": "x", "task_type": "echo", "target_agent": "a1"})
    r = client.get("/tasks?agent_id=a1", headers=sub_h)
    assert r.status_code == 200
    assert all(t["assigned_agent_id"] == "a1" for t in r.json())


def test_i_qry_4_list_agents(client, agent_headers):
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    r = client.get("/agents", headers=agent_headers)
    assert r.status_code == 200
    assert any(a["agent_id"] == "a1" for a in r.json())


def test_i_qry_5_get_agent(client, agent_headers):
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    r = client.get("/agents/a1", headers=agent_headers)
    assert r.status_code == 200
    assert r.json()["agent_id"] == "a1"


def test_i_qry_6_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "agents" in body and "tasks" in body


def test_i_qry_7_stats(client, viewer_key):
    r = client.get("/stats", headers={"Authorization": f"Bearer {viewer_key}"})
    assert r.status_code == 200
    body = r.json()
    assert "agents" in body and "tasks" in body


def test_i_qry_8_no_perm_denied(client, admin_headers):
    """A key without can_view_tasks should be denied."""
    r = client.post(
        "/admin/keys",
        headers=admin_headers,
        json={"name": "no-view", "can_view_tasks": False, "can_view_agents": False},
    )
    raw = r.json()["api_key"]
    g = client.get("/tasks", headers={"Authorization": f"Bearer {raw}"})
    assert g.status_code == 403


# === I-ACL: API keys (test_plan §3.4) ===

def test_i_acl_1_create_returns_raw_once(client, admin_headers):
    r = client.post("/admin/keys", headers=admin_headers, json={"name": "k1", "can_register": True})
    assert r.status_code == 200
    assert "api_key" in r.json()


def test_i_acl_2_list_omits_raw(client, admin_headers):
    client.post("/admin/keys", headers=admin_headers, json={"name": "k1", "can_register": True})
    r = client.get("/admin/keys", headers=admin_headers)
    assert r.status_code == 200
    for entry in r.json():
        assert "api_key" not in entry
        assert "key_hash" not in entry  # NFR-SEC-4: hash also not exposed


def test_i_acl_3_revoke_disables(client, admin_headers):
    r = client.post("/admin/keys", headers=admin_headers, json={"name": "rev", "can_register": True})
    raw = r.json()["api_key"]
    d = client.delete("/admin/keys/rev", headers=admin_headers)
    assert d.status_code == 200
    use = client.post("/register", headers={"Authorization": f"Bearer {raw}"}, json={"agent_id": "a1", "capabilities": []})
    assert use.status_code == 403


def test_i_acl_4_non_admin_denied(client, agent_headers):
    r = client.post("/admin/keys", headers=agent_headers, json={"name": "x"})
    assert r.status_code == 403


def test_i_acl_5_bootstrap_from_env(client):
    """FR-ACL-5: seed admin from env, validates."""
    r = client.get("/admin/keys", headers={"Authorization": "Bearer test-admin-key-1234567890"})
    assert r.status_code == 200


# === Critical bug regression tests (from claude_design-review_*) ===

def test_regression_1_1_agent_status_restored_correctly(tmp_db, hub_main, client, agent_headers):
    """Regression for prior §1.1: _agent_from_row mapped everything to OFFLINE."""
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    client.post("/heartbeat", headers=agent_headers, json={"agent_id": "a1", "status": "busy"})

    import importlib, sys
    for mod in list(sys.modules):
        if mod == "hub" or mod.startswith("hub."):
            del sys.modules[mod]
    new_main = importlib.import_module("hub.main")
    from fastapi.testclient import TestClient
    new_client = TestClient(new_main.app)
    r = new_client.get("/agents/a1", headers=agent_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "offline"  # design says all restored agents are OFFLINE on restart


def test_regression_1_2_db_assigned_agent_id_persisted(tmp_db, hub_main, client, agent_headers, submitter_key):
    """Regression for prior §1.2: db.assign_task was a no-op due to WHERE status='queued' race."""
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    r = client.post("/tasks", headers=sub_h, json={"task": "x", "task_type": "echo", "target_agent": "a1"})
    tid = r.json()["task_id"]
    from hub import database as db
    row = db.get_task(tid)
    assert row["assigned_agent_id"] == "a1", f"DB assigned_agent_id not persisted: {row}"


def test_regression_1_4_detect_task_type_is_public(hub_main):
    """Regression for prior §1.4: was Router._detect_task_type, now public."""
    from hub.router import Router
    from hub.registry import AgentRegistry
    from hub.queue import TaskQueue
    r = Router(AgentRegistry(), TaskQueue())
    assert callable(getattr(r, "detect_task_type", None))


def test_regression_1_8_terminal_guard(hub_main, client, agent_headers, submitter_key):
    """Regression for prior §1.8: completed task should not accept further updates."""
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    r = client.post("/tasks", headers=sub_h, json={"task": "x", "task_type": "echo", "target_agent": "a1"})
    tid = r.json()["task_id"]
    # Complete the task
    rep = client.post(
        "/report",
        headers=agent_headers,
        json={"task_id": tid, "agent_id": "a1", "status": "completed", "result": {"ok": True}},
    )
    assert rep.status_code == 200
    # Try to re-complete
    rep2 = client.post(
        "/report",
        headers=agent_headers,
        json={"task_id": tid, "agent_id": "a1", "status": "failed", "result": {"oops": True}},
    )
    assert rep2.status_code == 409, f"Expected 409 conflict, got {rep2.status_code}: {rep2.text}"


def test_regression_1_6_report_ownership_check(hub_main, client, agent_headers, submitter_key, admin_headers):
    """Regression for prior §1.6: /report must check the reporter is the assigned agent."""
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    # Create two agents
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    client.post("/register", headers=agent_headers, json={"agent_id": "a2", "capabilities": ["echo"]})
    # Submit task targeting a1
    r = client.post("/tasks", headers=sub_h, json={"task": "x", "task_type": "echo", "target_agent": "a1"})
    tid = r.json()["task_id"]
    # a2 tries to report
    rep = client.post(
        "/report",
        headers=agent_headers,
        json={"task_id": tid, "agent_id": "a2", "status": "completed", "result": {}},
    )
    assert rep.status_code == 403


def test_finding_dispatch_overflow_to_single_idle_agent(client, agent_headers, submitter_key):
    """Regression for Finding A1: one idle agent should claim only one queued task."""
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    ids = [
        client.post("/tasks", headers=sub_h, json={"task": f"t{i}", "task_type": "echo"}).json()["task_id"]
        for i in range(3)
    ]
    client.post("/register", headers=agent_headers, json={"agent_id": "a1", "capabilities": ["echo"]})
    statuses = [client.get(f"/tasks/{tid}", headers=sub_h).json()["status"] for tid in ids]
    assigned = sum(s == "assigned" for s in statuses)
    queued = sum(s == "queued" for s in statuses)
    assert assigned == 1, f"Expected one task assigned to a single idle agent. statuses={statuses}"
    assert queued == 2, f"Expected remaining tasks to stay queued. statuses={statuses}"


def test_regression_1_9_parse_task_text(hub_main, client, agent_headers):
    """Regression for prior §1.9: parse_task_row should return real task text from description."""
    r = client.post("/tasks", headers=agent_headers, json={"task": "echo this is the real task", "task_type": "echo"})
    tid = r.json()["task_id"]
    g = client.get(f"/tasks/{tid}", headers=agent_headers)
    assert "echo this is the real task" in g.json()["task"]


# === Phase 2 F2: parent_task_id filter + SSE field (test_plan §5) ============

def test_p2_par_1_filter_by_parent_task_id(client, agent_headers, submitter_key):
    """GET /tasks?parent_task_id=X returns the children; ?top_level=true returns top-level;
    both → 400."""
    sub_h = {"Authorization": f"Bearer {submitter_key}"}

    # Create a parent + 3 children + 1 unrelated top-level.
    parent = client.post(
        "/tasks", headers=sub_h, json={"task": "parent", "task_type": "echo"}
    ).json()
    parent_id = parent["task_id"]
    child_ids = [
        client.post(
            "/tasks",
            headers=sub_h,
            json={"task": f"c{i}", "task_type": "echo", "parent_task_id": parent_id},
        ).json()["task_id"]
        for i in range(3)
    ]
    other_top = client.post(
        "/tasks", headers=sub_h, json={"task": "other", "task_type": "echo"}
    ).json()["task_id"]

    # ?parent_task_id=parent_id returns exactly the 3 children.
    r = client.get(f"/tasks?parent_task_id={parent_id}", headers=sub_h)
    assert r.status_code == 200
    ids = sorted(t["task_id"] for t in r.json())
    assert ids == sorted(child_ids)

    # ?top_level=true returns the parent + the unrelated top-level (and excludes children).
    r = client.get("/tasks?top_level=true", headers=sub_h)
    assert r.status_code == 200
    ids = {t["task_id"] for t in r.json()}
    assert parent_id in ids
    assert other_top in ids
    for cid in child_ids:
        assert cid not in ids

    # Both → 400.
    r = client.get(f"/tasks?parent_task_id={parent_id}&top_level=true", headers=sub_h)
    assert r.status_code == 400
    assert "top_level cannot be combined with parent_task_id" in r.json()["detail"]


def test_p2_par_2_sse_includes_parent_task_id(client, agent_headers, submitter_key):
    """SSE subscriber sees parent_task_id in task_created/_updated/_completed payloads."""
    # We test via the task_event_data helper on hub_main, since iterating a live SSE
    # stream from TestClient is fragile. The SSE payload IS task_event_data(...) verbatim
    # plus the {"type": ..., "data": ...} wrapper.
    from hub import main as hub_main

    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    parent = client.post(
        "/tasks", headers=sub_h, json={"task": "parent", "task_type": "echo"}
    ).json()
    parent_id = parent["task_id"]
    child = client.post(
        "/tasks",
        headers=sub_h,
        json={"task": "c", "task_type": "echo", "parent_task_id": parent_id},
    ).json()
    child_id = child["task_id"]

    top = hub_main.task_event_data(parent_id, status="created")
    assert top["task_id"] == parent_id
    assert "parent_task_id" in top
    assert top["parent_task_id"] is None

    sub = hub_main.task_event_data(child_id, status="created")
    assert sub["task_id"] == child_id
    assert "parent_task_id" in sub
    assert sub["parent_task_id"] == parent_id


# =====================================================================
# Phase 2.2 — F3 + F4: capabilities, discovery, version-aware routing
# =====================================================================


def _register_agent_with_caps(hub_main, agent_id: str, caps: list, status="idle"):
    """Test helper: register an agent with object-form capabilities by going
    directly through the in-process registry (REST RegisterRequest is
    list[str]-typed). Persists to DB via save_agent."""
    from hub.models import AgentStatus, MachineInfo
    agent = hub_main.registry.register(
        agent_id,
        caps,
        MachineInfo(),
        status=AgentStatus(status),
    )
    hub_main.save_agent(agent)
    hub_main._scan_capability_schema_conflicts(agent_id)
    return agent


def test_p22_disc_1_get_capabilities_aggregates(hub_main, client, viewer_key):
    """F6 + F7: GET /capabilities returns top-level array; conflict scan happens
    once at registration time, not per-GET."""
    schema = {"type": "object", "properties": {"src": {"type": "string"}}}
    # Three agents advertising code:v2 — two identical schema, one bare.
    _register_agent_with_caps(hub_main, "coder-1",
                              [{"name": "code", "version": 2, "payload_schema": schema}])
    _register_agent_with_caps(hub_main, "coder-2",
                              [{"name": "code", "version": 2, "payload_schema": schema}])
    _register_agent_with_caps(hub_main, "coder-3",
                              [{"name": "code", "version": 2}])  # bare → payload_schema=None
    # Two agents advertising echo:v1.
    _register_agent_with_caps(hub_main, "e1", ["echo"])
    _register_agent_with_caps(hub_main, "e2", ["echo"])

    headers = {"Authorization": f"Bearer {viewer_key}"}
    r = client.get("/capabilities", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list), "GET /capabilities must return a top-level array (F6)"

    rows = {(row["name"], row["version"]): row for row in body}
    assert ("code", 2) in rows
    code_row = rows[("code", 2)]
    assert code_row["agent_count"] == 3
    # Bare advertiser collapses aggregate to null + conflict flag.
    assert code_row["payload_schema"] is None
    assert code_row.get("aggregated_schema_conflict") is True

    assert ("echo", 1) in rows
    echo_row = rows[("echo", 1)]
    assert echo_row["agent_count"] == 2
    assert echo_row["payload_schema"] is None

    # Sort: name ascending, version descending.
    pairs = [(row["name"], row["version"]) for row in body]
    assert pairs == sorted(pairs, key=lambda p: (p[0], -p[1]))

    # F7: count current `capability_schema_conflict` activity log rows.
    from hub import database as db
    with db.get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM activity_log WHERE action='capability_schema_conflict'"
        )
        before = cur.fetchone()[0]
    # Three repeated GETs — must not add ANY new conflict rows.
    for _ in range(3):
        client.get("/capabilities", headers=headers)
    with db.get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM activity_log WHERE action='capability_schema_conflict'"
        )
        after = cur.fetchone()[0]
    assert before == after, "F7: GET /capabilities must NOT log conflicts"


def test_p22_rte_2_min_version_miss_queues_then_dispatches(
    hub_main, client, agent_headers, submitter_key
):
    """F2 + F4: min_version mismatch keeps task queued; reload from DB preserves
    min_version; later v2 agent picks it up via dispatch_pending_for_agent."""
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    # One v1 agent online.
    _register_agent_with_caps(hub_main, "code-v1",
                              [{"name": "code", "version": 1}])

    r = client.post(
        "/tasks", headers=sub_h,
        json={"task": "x", "task_type": "code", "min_version": 2},
    )
    assert r.status_code == 200
    tid = r.json()["task_id"]
    assert r.json()["status"] == "queued"

    # F2: round-trip min_version through DB.
    from hub import database as db
    row = db.get_task(tid)
    assert row is not None
    assert int(row.get("min_version") or 1) == 2

    # Reload state from DB (simulates a hub restart).
    hub_main.queue.load([])
    hub_main.load_state()
    reloaded = hub_main.queue.get(tid)
    assert reloaded is not None
    assert reloaded.min_version == 2

    # Bring up a v2 agent — pending dispatch picks it up.
    import asyncio
    _register_agent_with_caps(hub_main, "code-v2",
                              [{"name": "code", "version": 2}])
    asyncio.run(hub_main.dispatch_pending_for_agent("code-v2"))
    g = client.get(f"/tasks/{tid}", headers=sub_h)
    assert g.json()["status"] == "assigned"
    assert g.json()["assigned_agent_id"] == "code-v2"


def test_p22_val_1_warn_mode_logs_and_dispatches(
    hub_main, client, agent_headers, submitter_key, monkeypatch
):
    """F4 warn-mode: bad payload still dispatches but logs activity."""
    monkeypatch.setenv("AGENT_HUB_VALIDATE_PAYLOAD", "warn")
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    schema = {"type": "object", "required": ["src"]}
    _register_agent_with_caps(
        hub_main, "code-agent",
        [{"name": "code", "version": 2, "payload_schema": schema}],
    )
    r = client.post(
        "/tasks", headers=sub_h,
        json={"task_type": "code", "min_version": 2, "payload": {"wrong": "yes"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assigned"

    from hub import database as db
    with db.get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM activity_log WHERE action='payload_schema_warn'"
        )
        warn_count = cur.fetchone()[0]
    assert warn_count >= 1, "warn-mode should log at least one payload_schema_warn"


def test_p22_val_2_strict_preflight_rejects_400(monkeypatch, tmp_db):
    """F4 strict-mode preflight: bad payload → HTTP 400, task NOT created."""
    import importlib, sys
    monkeypatch.setenv("AGENT_HUB_VALIDATE_PAYLOAD", "strict")
    # Reload hub modules so capabilities.current_mode() picks up env (it's
    # actually re-read each call, but force-reload to be safe).
    for mod in list(sys.modules):
        if mod == "hub" or mod.startswith("hub."):
            del sys.modules[mod]
    hub_main = importlib.import_module("hub.main")
    from fastapi.testclient import TestClient
    client = TestClient(hub_main.app)

    # Make admin + submitter keys fresh.
    admin_h = {"Authorization": "Bearer test-admin-key-1234567890"}
    r = client.post(
        "/admin/keys", headers=admin_h,
        json={"name": "sub", "can_assign_tasks": True, "can_view_tasks": True},
    )
    sub_h = {"Authorization": f"Bearer {r.json()['api_key']}"}

    schema = {"type": "object", "required": ["src"]}
    _register_agent_with_caps(
        hub_main, "code-agent",
        [{"name": "code", "version": 2, "payload_schema": schema}],
    )
    queue_len_before = len(hub_main.queue.list_all())
    r = client.post(
        "/tasks", headers=sub_h,
        json={"task_type": "code", "min_version": 2, "payload": {"wrong": "yes"}},
    )
    assert r.status_code == 400
    body = r.json()
    detail = body.get("detail")
    assert isinstance(detail, dict)
    assert detail.get("error") == "payload_schema_violation"
    queue_len_after = len(hub_main.queue.list_all())
    assert queue_len_after == queue_len_before, "strict preflight must NOT create task"


def test_p22_val_3_strict_dispatch_time_fails_queued_task(
    monkeypatch, tmp_db
):
    """F4: in warn, queue a task with no schema-bearing agent online; flip to
    strict and bring agent up — dispatch-time validator fails the task; no `task`
    WS frame is sent."""
    import importlib, sys, asyncio
    # Start in warn mode.
    monkeypatch.setenv("AGENT_HUB_VALIDATE_PAYLOAD", "warn")
    for mod in list(sys.modules):
        if mod == "hub" or mod.startswith("hub."):
            del sys.modules[mod]
    hub_main = importlib.import_module("hub.main")
    from fastapi.testclient import TestClient
    client = TestClient(hub_main.app)

    admin_h = {"Authorization": "Bearer test-admin-key-1234567890"}
    r = client.post(
        "/admin/keys", headers=admin_h,
        json={"name": "sub2", "can_assign_tasks": True, "can_view_tasks": True},
    )
    sub_h = {"Authorization": f"Bearer {r.json()['api_key']}"}

    # No agent online → preflight skips. Submit bad payload.
    r = client.post(
        "/tasks", headers=sub_h,
        json={"task_type": "code", "min_version": 2, "payload": {"wrong": "yes"}},
    )
    assert r.status_code == 200
    tid = r.json()["task_id"]
    assert r.json()["status"] == "queued"

    # Flip to strict mode (current_mode() re-reads env each call).
    monkeypatch.setenv("AGENT_HUB_VALIDATE_PAYLOAD", "strict")

    # Track sent WS messages by monkeypatching the manager.send.
    sent: list = []
    orig_send = hub_main.manager.send

    async def spy_send(agent_id, message):
        sent.append((agent_id, message))
        return await orig_send(agent_id, message)

    hub_main.manager.send = spy_send

    # Bring up the schema-bearing agent.
    schema = {"type": "object", "required": ["src"]}
    _register_agent_with_caps(
        hub_main, "code-agent",
        [{"name": "code", "version": 2, "payload_schema": schema}],
    )
    # Dispatcher runs sync via asyncio.
    asyncio.run(hub_main.dispatch_pending_for_agent("code-agent"))

    # Restore.
    hub_main.manager.send = orig_send

    g = client.get(f"/tasks/{tid}", headers=sub_h)
    body = g.json()
    assert body["status"] == "failed", f"expected failed, got {body['status']}"
    assert body["result"] and body["result"].get("error") == "payload_schema_violation"

    # No `task` WS frame should have been sent for this task.
    task_frames = [
        m for (aid, m) in sent
        if m.get("type") == "task" and m.get("task", {}).get("task_id") == tid
    ]
    assert len(task_frames) == 0, "dispatch-time strict failure must NOT send task frame"


def test_p22_val_3b_strict_dispatch_rollback_persists(monkeypatch, tmp_db):
    """F4 regression: after a strict dispatch-time validation failure, the DB
    row for the failed task must have assigned_agent_id and started_at cleared
    (not just the in-memory queue). Proves save_task ran before finalize_task."""
    import importlib, sys, asyncio
    monkeypatch.setenv("AGENT_HUB_VALIDATE_PAYLOAD", "warn")
    for mod in list(sys.modules):
        if mod == "hub" or mod.startswith("hub."):
            del sys.modules[mod]
    hub_main = importlib.import_module("hub.main")
    from fastapi.testclient import TestClient
    from hub import database as hub_db
    client = TestClient(hub_main.app)

    admin_h = {"Authorization": "Bearer test-admin-key-1234567890"}
    r = client.post(
        "/admin/keys", headers=admin_h,
        json={"name": "sub3b", "can_assign_tasks": True, "can_view_tasks": True},
    )
    sub_h = {"Authorization": f"Bearer {r.json()['api_key']}"}

    r = client.post(
        "/tasks", headers=sub_h,
        json={"task_type": "code", "min_version": 2, "payload": {"wrong": "yes"}},
    )
    assert r.status_code == 200
    tid = r.json()["task_id"]
    assert r.json()["status"] == "queued"

    monkeypatch.setenv("AGENT_HUB_VALIDATE_PAYLOAD", "strict")

    schema = {"type": "object", "required": ["src"]}
    _register_agent_with_caps(
        hub_main, "code-agent",
        [{"name": "code", "version": 2, "payload_schema": schema}],
    )
    asyncio.run(hub_main.dispatch_pending_for_agent("code-agent"))

    # Query the DB directly — not the in-memory queue.
    row = hub_db.get_task(tid)
    assert row is not None, "task row must exist in DB"
    assert row["status"] == "failed", f"DB status: {row['status']}"
    assert row["assigned_agent_id"] is None, (
        f"DB assigned_agent_id not cleared: {row['assigned_agent_id']}"
    )
    assert row["started_at"] is None, (
        f"DB started_at not cleared: {row['started_at']}"
    )


def test_p22_rte_3_direct_target_capability_check(
    hub_main, client, agent_headers, submitter_key
):
    """F3: direct-target with min_version mismatch leaves task queued + logs."""
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    _register_agent_with_caps(
        hub_main, "a1", [{"name": "code", "version": 1}]
    )
    r = client.post(
        "/tasks", headers=sub_h,
        json={
            "task": "x", "task_type": "code", "min_version": 2,
            "target_agent": "a1",
        },
    )
    assert r.status_code == 200
    tid = r.json()["task_id"]
    g = client.get(f"/tasks/{tid}", headers=sub_h)
    assert g.json()["status"] == "queued"
    assert g.json()["assigned_agent_id"] is None

    from hub import database as db
    with db.get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM activity_log WHERE action='direct_target_capability_mismatch' AND entity_id=?",
            (tid,),
        )
        count = cur.fetchone()[0]
    assert count >= 1, "expected direct_target_capability_mismatch activity log entry"


# === I-UI: Dashboard static mount (phase dashboard-v1) ===

def test_i_ui_1_ui_index_served(client):
    """GET /ui/ returns 200 text/html containing 'Agent Hub'."""
    r = client.get("/ui/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Agent Hub" in r.text


def test_i_ui_2_root_redirects_to_ui_when_web_absent(monkeypatch, tmp_db):
    """GET / returns 307 -> /ui/ even when web/ is absent at import time.

    Regression test for the redirect-outside-guard fix: monkeypatch
    pathlib.Path.exists to return False for the repo's web/ directory,
    re-import hub.main so the import-time WEB_DIR.exists() check sees False,
    then assert the unconditional / -> /ui/ redirect still fires AND
    /ui/ itself returns 404 (proving the SPA mount was correctly skipped)."""
    import importlib, sys, pathlib
    real_exists = pathlib.Path.exists

    def fake_exists(self):
        # Match the exact WEB_DIR computed by hub/main.py:
        #   pathlib.Path(<hub/main.py>).resolve().parent.parent / "web"
        # i.e. the "web" directory whose parent is the agent-hub repo root.
        if self.name == "web" and self.parent.name == "agent-hub":
            return False
        return real_exists(self)

    monkeypatch.setattr(pathlib.Path, "exists", fake_exists)
    for mod in list(sys.modules):
        if mod == "hub" or mod.startswith("hub."):
            del sys.modules[mod]
    hub_main = importlib.import_module("hub.main")
    from fastapi.testclient import TestClient
    c = TestClient(hub_main.app)

    # The unconditional redirect must fire.
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/ui/"

    # And the SPA route must be absent (proving WEB_DIR.exists() saw False).
    r2 = c.get("/ui/")
    assert r2.status_code == 404


def test_i_ui_3_missing_asset_404(client):
    """GET /ui/some-missing-asset returns 404 (StaticFiles default)."""
    r = client.get("/ui/does-not-exist.css")
    assert r.status_code == 404


# === I-MEK / I-MEU / I-USG: phase user-tokens (design v3 §5 Slice B) =========


class _StubCfMultiVerifier:
    """Test double — maps multiple tokens to claim sets, None otherwise."""

    def __init__(self, mapping: dict):
        # mapping: {token: claims_dict}
        self.mapping = mapping

    def verify(self, token):
        return self.mapping.get(token)


def _jwt_headers_for(email: str, token: str = None) -> dict:
    token = token or f"jwt-for-{email}"
    return {"Cf-Access-Jwt-Assertion": token}


def _install_cf_verifier(hub_main, *emails) -> dict:
    """Install a CF verifier accepting one token per email; return a map of
    `email -> token` for convenience."""
    mapping = {}
    for email in emails:
        mapping[f"jwt-for-{email}"] = {"email": email}
    hub_main.cf_access_verifier = _StubCfMultiVerifier(mapping)
    return mapping


# ---------- /me/keys ----------


def test_me_keys_post_creates_key_owned_by_jwt_email(client, hub_main):
    _install_cf_verifier(hub_main, "alice@example.com")
    try:
        r = client.post(
            "/me/keys",
            headers=_jwt_headers_for("alice@example.com"),
            json={"name": "alice-laptop"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["api_key"]
        assert body["owner"] == "alice@example.com"
        assert body["permission"]["can_assign_tasks"] is True
        assert body["permission"]["is_admin"] is False
        assert body["permission"]["can_register"] is False
        assert body["expires_at"]
        # DB row carries owner.
        from hub import database as db
        rows = [r for r in db.list_api_keys() if r["name"] == "alice-laptop"]
        assert rows and rows[0]["owner"] == "alice@example.com"
    finally:
        hub_main.cf_access_verifier = None


def test_me_keys_post_without_jwt_returns_403(client, hub_main):
    # No verifier installed → 403 (no JWT path).
    r = client.post("/me/keys", json={"name": "x"})
    assert r.status_code == 403


def test_me_keys_get_lists_only_user_own_keys(client, hub_main):
    _install_cf_verifier(hub_main, "alice@example.com", "bob@example.com")
    try:
        client.post(
            "/me/keys",
            headers=_jwt_headers_for("alice@example.com"),
            json={"name": "alice-k1"},
        )
        client.post(
            "/me/keys",
            headers=_jwt_headers_for("alice@example.com"),
            json={"name": "alice-k2"},
        )
        client.post(
            "/me/keys",
            headers=_jwt_headers_for("bob@example.com"),
            json={"name": "bob-k1"},
        )
        ra = client.get("/me/keys", headers=_jwt_headers_for("alice@example.com"))
        assert ra.status_code == 200
        names_a = sorted(k["name"] for k in ra.json())
        assert names_a == ["alice-k1", "alice-k2"]
        # Seed-admin (NULL owner) must not appear.
        assert "seed-admin" not in names_a
        rb = client.get("/me/keys", headers=_jwt_headers_for("bob@example.com"))
        names_b = [k["name"] for k in rb.json()]
        assert names_b == ["bob-k1"]
    finally:
        hub_main.cf_access_verifier = None


def test_me_keys_delete_only_user_own(client, hub_main):
    _install_cf_verifier(hub_main, "alice@example.com", "bob@example.com")
    try:
        client.post(
            "/me/keys",
            headers=_jwt_headers_for("alice@example.com"),
            json={"name": "alice-k"},
        )
        # Bob tries to revoke alice's key → 404 (not 403, no leakage).
        rb = client.delete(
            "/me/keys/alice-k", headers=_jwt_headers_for("bob@example.com")
        )
        assert rb.status_code == 404
        # Alice revokes her own → 200.
        ra = client.delete(
            "/me/keys/alice-k", headers=_jwt_headers_for("alice@example.com")
        )
        assert ra.status_code == 200
    finally:
        hub_main.cf_access_verifier = None


def test_me_keys_post_duplicate_name_returns_409(client, hub_main):
    _install_cf_verifier(hub_main, "alice@example.com")
    try:
        h = _jwt_headers_for("alice@example.com")
        r1 = client.post("/me/keys", headers=h, json={"name": "dup"})
        assert r1.status_code == 200
        r2 = client.post("/me/keys", headers=h, json={"name": "dup"})
        assert r2.status_code == 409
    finally:
        hub_main.cf_access_verifier = None


def test_me_keys_post_soft_cap_returns_429_on_11th(client, hub_main):
    _install_cf_verifier(hub_main, "alice@example.com")
    try:
        h = _jwt_headers_for("alice@example.com")
        for i in range(10):
            r = client.post("/me/keys", headers=h, json={"name": f"k{i}"})
            assert r.status_code == 200, r.text
        # 11th must be 429.
        r = client.post("/me/keys", headers=h, json={"name": "k10"})
        assert r.status_code == 429
        assert "Max 10" in r.json()["detail"]
    finally:
        hub_main.cf_access_verifier = None


# ---------- /me/usage ----------


def _seed_usage_row(
    task_id: str,
    owner_email: str,
    *,
    agent_id: str = "claude-a1",
    cli: str = "claude",
    parent_task_id=None,
    cost_usd=0.01,
    completed_at: str = None,
):
    from hub import database as db
    now = completed_at or datetime_now_iso()
    db.save_task_usage({
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "api_key_name": f"{owner_email}-key",
        "owner_email": owner_email,
        "agent_id": agent_id,
        "cli": cli,
        "task_type": "general",
        "status": "completed",
        "started_at": now,
        "completed_at": now,
        "duration_seconds": 1.0,
        "cost_usd": cost_usd,
        "input_tokens": 100,
        "output_tokens": 50,
        "web_search_requests": None,
        "created_at": now,
    })


def datetime_now_iso():
    from datetime import datetime
    return datetime.now().isoformat()


def test_me_usage_lists_only_user_own_rows(client, hub_main):
    _install_cf_verifier(hub_main, "alice@example.com", "bob@example.com")
    try:
        _seed_usage_row("t-a-1", "alice@example.com", cost_usd=0.05)
        _seed_usage_row("t-a-2", "alice@example.com", cost_usd=0.10)
        _seed_usage_row("t-b-1", "bob@example.com", cost_usd=0.20)
        r = client.get("/me/usage", headers=_jwt_headers_for("alice@example.com"))
        assert r.status_code == 200, r.text
        body = r.json()
        ids = sorted(row["task_id"] for row in body["rows"])
        assert ids == ["t-a-1", "t-a-2"]
        assert abs(body["total_cost_usd"] - 0.15) < 1e-9
        assert body["total_count"] == 2
    finally:
        hub_main.cf_access_verifier = None


def test_me_usage_top_level_only_filter_excludes_children(client, hub_main):
    """B13: ?top_level_only=true excludes delegated rows."""
    _install_cf_verifier(hub_main, "alice@example.com")
    try:
        _seed_usage_row("t-top", "alice@example.com", parent_task_id=None, cost_usd=0.5)
        _seed_usage_row(
            "t-child", "alice@example.com",
            parent_task_id="t-top", cost_usd=0.1,
        )
        r_all = client.get(
            "/me/usage", headers=_jwt_headers_for("alice@example.com")
        )
        assert r_all.status_code == 200
        assert r_all.json()["total_count"] == 2
        r_top = client.get(
            "/me/usage?top_level_only=true",
            headers=_jwt_headers_for("alice@example.com"),
        )
        body = r_top.json()
        assert body["total_count"] == 1
        assert body["rows"][0]["task_id"] == "t-top"
        # Aggregates: split confirms top-level vs delegated counts.
        assert body["top_level_row_count"] == 1
        assert body["delegated_row_count"] == 0
    finally:
        hub_main.cf_access_verifier = None


# ---------- /admin/usage ----------


def test_admin_usage_requires_admin(client, admin_headers, viewer_key):
    r1 = client.get(
        "/admin/usage", headers={"Authorization": f"Bearer {viewer_key}"}
    )
    assert r1.status_code == 403
    r2 = client.get("/admin/usage", headers=admin_headers)
    assert r2.status_code == 200


def test_admin_usage_filters_by_email(client, admin_headers):
    _seed_usage_row("t-a-1", "alice@example.com", cost_usd=0.05)
    _seed_usage_row("t-b-1", "bob@example.com", cost_usd=0.20)
    r = client.get(
        "/admin/usage?email=alice@example.com", headers=admin_headers
    )
    assert r.status_code == 200
    body = r.json()
    ids = sorted(row["task_id"] for row in body["rows"])
    assert ids == ["t-a-1"]


# ---------- finalize_task usage write + submitter attribution ----------


def test_submit_task_records_submitter(client, hub_main, submitter_key):
    """B22: submit_task immediately persists (api_key_name, owner_email) for
    the submitter's bearer key into task_submitters — restart-safe."""
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    r = client.post("/tasks", headers=sub_h, json={"task": "x", "task_type": "echo"})
    tid = r.json()["task_id"]
    from hub import database as db
    row = db.peek_submitter(tid)
    assert row is not None
    assert row["api_key_name"] == "submitter-key"  # owner is NULL for admin-minted


def test_finalize_task_writes_usage_row(
    client, hub_main, agent_headers, submitter_key
):
    """finalize_task → row in task_usage with cost extracted from claude result."""
    import asyncio
    from hub.models import TaskStatus
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    client.post(
        "/register", headers=agent_headers,
        json={"agent_id": "claude-a1", "capabilities": ["echo"]},
    )
    r = client.post(
        "/tasks", headers=sub_h,
        json={"task": "x", "task_type": "echo", "target_agent": "claude-a1"},
    )
    tid = r.json()["task_id"]
    # Finalize with a claude-shaped result.
    result = {
        "raw": {
            "total_cost_usd": 0.0123,
            "usage": {
                "input_tokens": 4512,
                "output_tokens": 821,
                "server_tool_use": {"web_search_requests": 2},
            },
        }
    }
    asyncio.run(
        hub_main.finalize_task(tid, TaskStatus.COMPLETED, result=result)
    )
    from hub import database as db
    rows = db.list_task_usage(limit=1000)
    matching = [row for row in rows if row["task_id"] == tid]
    assert len(matching) == 1, f"expected one task_usage row for {tid}"
    row = matching[0]
    assert row["status"] == "completed"
    assert row["cli"] == "claude"
    assert abs((row["cost_usd"] or 0) - 0.0123) < 1e-9
    assert row["input_tokens"] == 4512
    assert row["output_tokens"] == 821
    assert row["web_search_requests"] == 2
    assert row["api_key_name"] == "submitter-key"
    assert row["parent_task_id"] is None


def test_finalize_task_propagates_root_submitter_to_child(
    client, hub_main, agent_headers, submitter_key
):
    """B20: child task inherits root submitter via peek_submitter copy. Verified
    by examining the child's task_submitters row after a synthesized
    submit_child propagation step."""
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    # Top-level task submitted by submitter.
    r = client.post("/tasks", headers=sub_h, json={"task": "x", "task_type": "echo"})
    parent_tid = r.json()["task_id"]
    # Simulate the submit_child propagation step directly: it's the same
    # peek + record that the WS handler runs.
    from hub import database as db
    child_id = "child-of-" + parent_tid
    parent_row = db.peek_submitter(parent_tid) or {}
    db.record_submitter(
        child_id,
        parent_row.get("api_key_name"),
        parent_row.get("owner_email"),
    )
    inherited = db.peek_submitter(child_id)
    assert inherited["api_key_name"] == "submitter-key"


def test_three_level_chain_inherits_root_submitter(client, hub_main, submitter_key):
    """B21: top → child → grandchild — all carry root submitter via one-hop
    propagation."""
    sub_h = {"Authorization": f"Bearer {submitter_key}"}
    r = client.post("/tasks", headers=sub_h, json={"task": "root", "task_type": "echo"})
    root_tid = r.json()["task_id"]
    from hub import database as db
    child_id = "child-" + root_tid
    grand_id = "grand-" + root_tid
    # Step 1: child inherits from root.
    parent_row = db.peek_submitter(root_tid) or {}
    db.record_submitter(
        child_id, parent_row.get("api_key_name"), parent_row.get("owner_email")
    )
    # Step 2: grandchild inherits from child (which inherited from root).
    child_row = db.peek_submitter(child_id) or {}
    db.record_submitter(
        grand_id, child_row.get("api_key_name"), child_row.get("owner_email")
    )
    assert db.peek_submitter(grand_id)["api_key_name"] == "submitter-key"


def test_rejected_child_submit_leaves_no_submitter_row(
    client, hub_main, agent_headers, submitter_key
):
    """B24 (v3 Finding 2): when a child submission is rejected, the
    `_rollback_child()` helper must drop the task_submitters row it inserted
    so attribution state doesn't leak.

    Empty capabilities are wildcard in this codebase, so the parent must
    advertise a different concrete capability to force capability_unavailable.
    """
    import asyncio, uuid
    from hub.models import MachineInfo, AgentStatus
    from hub import database as db

    sub_h = {"Authorization": f"Bearer {submitter_key}"}

    # Register parent with a concrete capability. A child asking for any other
    # task_type is rejected by the self-target capability gate.
    hub_main.registry.register(
        "parent-agent", ["echo"], MachineInfo(), status=AgentStatus.IDLE
    )
    hub_main.save_agent(hub_main.registry.get("parent-agent"))

    r = client.post(
        "/tasks",
        headers=sub_h,
        json={"task": "p", "task_type": "echo", "target_agent": "parent-agent"},
    )
    assert r.status_code == 200, r.text
    parent_tid = r.json()["task_id"]

    parent_task = hub_main.queue.get(parent_tid)
    if parent_task.assigned_agent_id is None:
        assert hub_main.queue.assign(parent_tid, "parent-agent")
        hub_main.save_task(hub_main.queue.get(parent_tid))
    assert hub_main.queue.get(parent_tid).assigned_agent_id == "parent-agent"
    assert db.peek_submitter(parent_tid) is not None

    class _DummyWS:
        pass

    ws = _DummyWS()
    hub_main.manager.active_connections["parent-agent"] = ws
    hub_main.manager.ws_to_agent[ws] = "parent-agent"
    hub_main.manager.session_keys[ws] = submitter_key

    sent: list = []
    emitted: list = []
    orig_send = hub_main.manager.send
    orig_emit_event = hub_main.emit_event

    async def spy_send(agent_id, message):
        sent.append((agent_id, message))
        return True

    def spy_emit_event(event_type, data):
        emitted.append((event_type, data))
        return orig_emit_event(event_type, data)

    hub_main.manager.send = spy_send
    hub_main.emit_event = spy_emit_event
    try:
        request_id = str(uuid.uuid4())
        msg = {
            "type": "submit_child",
            "request_id": request_id,
            "parent_task_id": parent_tid,
            "target_agent": "parent-agent",
            "task": "c",
            "task_type": "unsupported-cap",
            "min_version": 1,
        }
        asyncio.run(hub_main._handle_submit_child("parent-agent", msg))
    finally:
        hub_main.manager.send = orig_send
        hub_main.emit_event = orig_emit_event
        hub_main.manager.active_connections.pop("parent-agent", None)
        hub_main.manager.ws_to_agent.pop(ws, None)
        hub_main.manager.session_keys.pop(ws, None)

    rejections = [m for (_, m) in sent if m.get("type") == "child_rejected"]
    assert rejections, f"expected child_rejected message, got: {sent}"
    assert rejections[0]["reason"] == "capability_unavailable"

    created = [
        data
        for event_type, data in emitted
        if event_type == "task_created" and data.get("parent_task_id") == parent_tid
    ]
    assert len(created) == 1, f"expected one rejected child creation event, got: {emitted}"
    child_id = created[0]["task_id"]

    assert db.peek_submitter(child_id) is None
    with db.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM task_submitters WHERE task_id = ?", (child_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (child_id,))
        assert cur.fetchone()[0] == 0

    # The parent's own submitter row is still present.
    assert db.peek_submitter(parent_tid) is not None
