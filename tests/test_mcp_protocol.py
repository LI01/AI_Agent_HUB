"""Phase 1.5 MCP protocol tests."""
import json
import os
import subprocess
import sys
from pathlib import Path


def _rpc(method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _call_tool(client, headers, name, arguments=None, request_id=1):
    return client.post(
        "/mcp",
        headers=headers,
        json=_rpc("tools/call", {"name": name, "arguments": arguments or {}}, request_id),
    )


def test_mcp_tools_list_exposes_required_schemas(client, admin_headers):
    r = client.post("/mcp", headers=admin_headers, json=_rpc("tools/list"))
    assert r.status_code == 200
    tools = {tool["name"]: tool for tool in r.json()["result"]["tools"]}

    for name in [
        "list-agents",
        "get-agent",
        "register-agent",
        "unregister-agent",
        "submit-task",
        "get-task",
        "list-tasks",
        "stats",
        "health",
        "create-api-key",
        "list-api-keys",
        "revoke-api-key",
    ]:
        assert name in tools
        assert tools[name]["inputSchema"]["type"] == "object"
        assert "outputSchema" not in tools[name] or tools[name]["outputSchema"]["type"] == "object"

    submit_props = tools["submit-task"]["inputSchema"]["properties"]
    for field in ["task", "task_type", "payload", "target_agent", "priority", "timeout", "parent_task_id"]:
        assert field in submit_props


def test_mcp_submit_task_and_get_task_round_trip(client, submitter_key):
    headers = {"Authorization": f"Bearer {submitter_key}"}
    submit = _call_tool(
        client,
        headers,
        "submit-task",
        {"task": "echo hello", "task_type": "echo", "payload": {"x": 1}},
    )
    assert submit.status_code == 200, submit.text
    submit_body = submit.json()["result"]["structuredContent"]
    task_id = submit_body["task_id"]

    get = _call_tool(client, headers, "get-task", {"task_id": task_id}, request_id=2)
    assert get.status_code == 200, get.text
    task = get.json()["result"]["structuredContent"]
    assert task["task_id"] == task_id
    assert task["payload"]["x"] == 1


def test_mcp_permission_error_does_not_leak_key(client, admin_headers):
    raw = client.post(
        "/admin/keys",
        headers=admin_headers,
        json={"name": "view-only", "can_view_tasks": True, "can_assign_tasks": False},
    ).json()["api_key"]
    headers = {"Authorization": f"Bearer {raw}"}
    r = _call_tool(client, headers, "submit-task", {"task": "echo"})
    assert r.status_code == 200
    err = r.json()["error"]
    assert err["code"] == -32001
    assert raw not in json.dumps(err)


def test_mcp_revoked_key_fails_on_next_tool_call(client, admin_headers):
    raw = client.post(
        "/admin/keys",
        headers=admin_headers,
        json={"name": "temp-view", "can_view_agents": True},
    ).json()["api_key"]
    headers = {"Authorization": f"Bearer {raw}"}

    ok = _call_tool(client, headers, "list-agents")
    assert "result" in ok.json()

    client.delete("/admin/keys/temp-view", headers=admin_headers)
    denied = _call_tool(client, headers, "list-agents", request_id=2)
    # Phase 2.5 §4.3 / Finding 1: invalid/revoked bearer now rejected at the HTTP
    # boundary with 401 + WWW-Authenticate, not as a JSON-RPC error inside 200.
    assert denied.status_code == 401
    assert denied.headers["WWW-Authenticate"].lower().startswith("bearer")
    assert 'realm="agent-hub"' in denied.headers["WWW-Authenticate"]


def test_p22_mcp_1_list_capabilities_tool(hub_main, client, admin_headers):
    """Phase 2.2 §3.4 / §5 row 8 / FR-CAP-6.

    (a) tools/list exposes `list-capabilities` with the documented schemas.
    (b) tools/call list-capabilities returns {"capabilities": [...]} per MCP
        spec (structuredContent must be an object). REST /capabilities still
        returns the top-level array (Fix F6).
    (c) A key lacking can_view_agents surfaces an MCP error mapped from REST 403.
    """
    from hub.models import AgentStatus, MachineInfo

    # Object-form caps register through the in-process registry (the REST
    # RegisterRequest is list[str]-typed; mirrors tests/test_rest.py helper).
    hub_main.save_agent(
        hub_main.registry.register(
            "coder-v2",
            [{"name": "code", "version": 2,
              "payload_schema": {"type": "object", "required": ["src"]}}],
            MachineInfo(),
            status=AgentStatus.IDLE,
        )
    )
    hub_main.save_agent(
        hub_main.registry.register(
            "echoer", ["echo"], MachineInfo(), status=AgentStatus.IDLE,
        )
    )

    # (a) tools/list shape
    listing = client.post("/mcp", headers=admin_headers, json=_rpc("tools/list"))
    assert listing.status_code == 200, listing.text
    tools = {t["name"]: t for t in listing.json()["result"]["tools"]}
    assert "list-capabilities" in tools
    spec = tools["list-capabilities"]
    assert spec["description"] == "List capabilities aggregated across online agents."
    assert spec["inputSchema"]["type"] == "object"
    assert spec["inputSchema"]["properties"] == {}
    assert spec["outputSchema"]["type"] == "object"
    assert spec["outputSchema"]["required"] == ["capabilities"]
    rows_schema = spec["outputSchema"]["properties"]["capabilities"]
    assert rows_schema["type"] == "array"
    item_schema = rows_schema["items"]
    assert item_schema["type"] == "object"
    for required_field in ["name", "version", "agent_count"]:
        assert required_field in item_schema["properties"]
    assert "aggregated_schema_conflict" in item_schema["properties"]

    # Submit-task schema gained min_version (additive, optional, default 1).
    submit_props = tools["submit-task"]["inputSchema"]["properties"]
    assert "min_version" in submit_props
    assert submit_props["min_version"]["type"] == "integer"
    assert submit_props["min_version"]["minimum"] == 1
    assert submit_props["min_version"].get("default") == 1

    # Register-agent schema widened to oneOf string|object.
    reg_props = tools["register-agent"]["inputSchema"]["properties"]
    items = reg_props["capabilities"]["items"]
    assert "oneOf" in items
    variants = items["oneOf"]
    types = {v.get("type") for v in variants}
    assert "string" in types
    assert "object" in types
    obj_variant = next(v for v in variants if v.get("type") == "object")
    assert "name" in obj_variant["required"]

    # (b) tools/call returns {"capabilities": [...]} per MCP spec
    call = _call_tool(client, admin_headers, "list-capabilities", {}, request_id=2)
    assert call.status_code == 200, call.text
    body = call.json()
    assert "error" not in body, body
    structured = body["result"]["structuredContent"]
    assert isinstance(structured, dict), f"expected object, got {type(structured)}: {structured}"
    rows = structured["capabilities"]
    assert isinstance(rows, list), f"expected capabilities array, got {type(rows)}: {rows}"
    by_name = {(r["name"], r["version"]): r for r in rows}
    assert ("code", 2) in by_name
    assert ("echo", 1) in by_name
    code_row = by_name[("code", 2)]
    assert code_row["agent_count"] == 1
    assert code_row["payload_schema"] == {"type": "object", "required": ["src"]}
    echo_row = by_name[("echo", 1)]
    assert echo_row["agent_count"] == 1
    assert echo_row["payload_schema"] is None

    # text-content mirror is the JSON-encoded wrapped object
    text = body["result"]["content"][0]["text"]
    decoded = json.loads(text)
    assert isinstance(decoded, dict)
    assert isinstance(decoded["capabilities"], list)

    # (c) key without can_view_agents → MCP error mapped from REST 403.
    no_view = client.post(
        "/admin/keys",
        headers=admin_headers,
        json={
            "name": "no-view",
            "can_assign_tasks": True,
            "can_view_agents": False,
            "can_view_tasks": False,
        },
    ).json()["api_key"]
    denied = _call_tool(
        client,
        {"Authorization": f"Bearer {no_view}"},
        "list-capabilities",
        {},
        request_id=3,
    )
    assert denied.status_code == 200
    err = denied.json()["error"]
    # REST 403 → mapped to -32001 by the InProcessHubBackend exception bridge.
    assert err["code"] == -32001
    assert "permission" in err["message"].lower() or "view" in err["message"].lower()


def test_mcp_stdio_smoke_does_not_create_default_db(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    env["AGENT_HUB_URL"] = "http://127.0.0.1:9"
    env["AGENT_HUB_API_KEY"] = "dummy-key"
    proc = subprocess.run(
        [sys.executable, "-m", "hub.mcp"],
        input=json.dumps(_rpc("tools/list")) + "\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=env,
        timeout=5,
    )
    assert proc.returncode == 0, proc.stderr
    assert "tools" in json.loads(proc.stdout.strip())["result"]
    assert not (tmp_path / "agent_hub.db").exists()


# ---------------------------------------------------------------------------
# Phase 2.5: MCP spec-compliance tests (§6 of design_v3.md).
# Tests p25_mcp_1-6, 11, 12 depend on Slice A (tool wrapping + outputSchema).
# Tests p25_mcp_7, 8, 9 depend on Slice B (HTTP 401 + WWW-Authenticate).
# compat-CLI tests are pure unit tests that monkeypatch mcp.server._run_tool.
# ---------------------------------------------------------------------------


def test_p25_mcp_1_list_agents_outputschema_declared(client, admin_headers):
    """§6 test 1 — list-agents declares outputSchema with object/agents:array."""
    r = client.post("/mcp", headers=admin_headers, json=_rpc("tools/list"))
    assert r.status_code == 200, r.text
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    spec = tools["list-agents"]
    assert spec["outputSchema"]["type"] == "object"
    assert spec["outputSchema"]["required"] == ["agents"]
    assert spec["outputSchema"]["properties"]["agents"]["type"] == "array"


def test_p25_mcp_2_list_agents_structuredcontent_is_object(hub_main, client, admin_headers):
    """§6 test 2 — tools/call list-agents returns {"agents": [...]} object."""
    from hub.models import AgentStatus, MachineInfo

    # Register one agent via the in-process registry so the list has content.
    hub_main.save_agent(
        hub_main.registry.register(
            "agent-p25-2", ["echo"], MachineInfo(), status=AgentStatus.IDLE,
        )
    )

    call = _call_tool(client, admin_headers, "list-agents", {})
    assert call.status_code == 200, call.text
    body = call.json()
    assert "error" not in body, body
    structured = body["result"]["structuredContent"]
    assert isinstance(structured, dict)
    assert "agents" in structured
    assert isinstance(structured["agents"], list)
    assert len(structured["agents"]) == 1

    # content[0].text decoded matches structuredContent
    decoded = json.loads(body["result"]["content"][0]["text"])
    assert decoded == structured


def test_p25_mcp_3_list_tasks_outputschema_declared(client, admin_headers):
    """§6 test 3 — list-tasks declares outputSchema with object/tasks:array."""
    r = client.post("/mcp", headers=admin_headers, json=_rpc("tools/list"))
    assert r.status_code == 200
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    spec = tools["list-tasks"]
    assert spec["outputSchema"]["type"] == "object"
    assert spec["outputSchema"]["required"] == ["tasks"]
    assert spec["outputSchema"]["properties"]["tasks"]["type"] == "array"


def test_p25_mcp_4_list_tasks_structuredcontent_is_object(client, admin_headers, submitter_key):
    """§6 test 4 — tools/call list-tasks returns {"tasks": [...]} object."""
    sub_headers = {"Authorization": f"Bearer {submitter_key}"}
    submit = client.post(
        "/tasks",
        headers=sub_headers,
        json={"task": "echo hi", "task_type": "echo", "payload": {"x": 1}},
    )
    assert submit.status_code == 200, submit.text

    call = _call_tool(client, admin_headers, "list-tasks", {})
    assert call.status_code == 200, call.text
    body = call.json()
    assert "error" not in body, body
    structured = body["result"]["structuredContent"]
    assert isinstance(structured, dict)
    assert "tasks" in structured
    assert isinstance(structured["tasks"], list)
    assert len(structured["tasks"]) >= 1

    decoded = json.loads(body["result"]["content"][0]["text"])
    assert decoded == structured


def test_p25_mcp_5_list_api_keys_outputschema_declared(client, admin_headers):
    """§6 test 5 — list-api-keys declares outputSchema with object/keys:array."""
    r = client.post("/mcp", headers=admin_headers, json=_rpc("tools/list"))
    assert r.status_code == 200
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    spec = tools["list-api-keys"]
    assert spec["outputSchema"]["type"] == "object"
    assert spec["outputSchema"]["required"] == ["keys"]
    assert spec["outputSchema"]["properties"]["keys"]["type"] == "array"


def test_p25_mcp_6_list_api_keys_structuredcontent_is_object(client, admin_headers):
    """§6 test 6 — tools/call list-api-keys returns {"keys": [...]} object."""
    call = _call_tool(client, admin_headers, "list-api-keys", {})
    assert call.status_code == 200, call.text
    body = call.json()
    assert "error" not in body, body
    structured = body["result"]["structuredContent"]
    assert isinstance(structured, dict)
    assert "keys" in structured
    assert isinstance(structured["keys"], list)
    # The seeded admin key should be present.
    assert len(structured["keys"]) >= 1


def test_p25_mcp_7_unauthed_mcp_returns_401_with_www_authenticate(client):
    """§6 test 7 — missing bearer → 401 + WWW-Authenticate: Bearer realm=…"""
    r = client.post("/mcp", json=_rpc("tools/list"))
    assert r.status_code == 401
    www_auth = r.headers["WWW-Authenticate"]
    assert www_auth.lower().startswith("bearer")
    assert 'realm="agent-hub"' in www_auth


def test_p25_mcp_8_authed_mcp_stays_200(client, admin_headers):
    """§6 test 8 — valid bearer keeps returning 200 with JSON-RPC result."""
    r = client.post("/mcp", headers=admin_headers, json=_rpc("tools/list"))
    assert r.status_code == 200
    assert "result" in r.json()


def test_p25_mcp_9_invalid_bearer_returns_401_with_www_authenticate(client):
    """§6 test 9 — invalid bearer → 401 + WWW-Authenticate per Finding 1."""
    r = client.post(
        "/mcp",
        headers={"Authorization": "Bearer not-a-real-key"},
        json=_rpc("tools/list"),
    )
    assert r.status_code == 401
    www_auth = r.headers["WWW-Authenticate"]
    assert www_auth.lower().startswith("bearer")
    assert 'realm="agent-hub"' in www_auth


def test_p25_mcp_10_e2e_submit_task_via_mcp(client, submitter_key):
    """§6 test 10 — end-to-end submit-task via MCP returns object structuredContent."""
    headers = {"Authorization": f"Bearer {submitter_key}"}
    r = _call_tool(
        client,
        headers,
        "submit-task",
        {"task": "echo", "task_type": "echo", "payload": {"x": 1}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    structured = body["result"]["structuredContent"]
    assert isinstance(structured, dict)
    assert "task_id" in structured
    assert "status" in structured

    # content[0].text mirrors structuredContent
    decoded = json.loads(body["result"]["content"][0]["text"])
    assert decoded == structured


def test_p25_mcp_11_e2e_list_capabilities_via_mcp(client, admin_headers):
    """§6 test 11 — list-capabilities tools/call returns {"capabilities": [...]}."""
    r = _call_tool(client, admin_headers, "list-capabilities", {})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    structured = body["result"]["structuredContent"]
    assert isinstance(structured, dict)
    assert "capabilities" in structured
    assert isinstance(structured["capabilities"], list)


def test_p25_mcp_12_tools_without_declared_outputschema_omit_outputschema(client, admin_headers):
    """§6 test 12 — default outputSchema injection has been dropped (§4.1 step 4)."""
    r = client.post("/mcp", headers=admin_headers, json=_rpc("tools/list"))
    assert r.status_code == 200
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert "outputSchema" not in tools["get-agent"]
    assert "outputSchema" not in tools["submit-task"]
    assert "outputSchema" not in tools["health"]


# ---------------------------------------------------------------------------
# Compatibility-CLI regression tests (§6, §4.4). Pure unit tests that
# monkeypatch mcp.server._run_tool. They do NOT use TestClient or
# HttpHubBackend — they verify the wrapper functions unwrap correctly.
# ---------------------------------------------------------------------------


def test_p25_mcp_compat_list_agents_returns_list(monkeypatch):
    """§6 compat — mcp.server.list_agents() unwraps {"agents": [...]} → list."""
    import mcp.server as mcp_server

    monkeypatch.setattr(
        mcp_server, "_run_tool",
        lambda name, arguments=None: {"agents": [{"agent_id": "a1"}]},
    )
    result = mcp_server.list_agents()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0] == {"agent_id": "a1"}


def test_p25_mcp_compat_list_tasks_returns_list(monkeypatch):
    """§6 compat — mcp.server.list_tasks() unwraps {"tasks": [...]} → list."""
    import mcp.server as mcp_server

    monkeypatch.setattr(
        mcp_server, "_run_tool",
        lambda name, arguments=None: {"tasks": [{"task_id": "t1"}]},
    )
    result = mcp_server.list_tasks()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0] == {"task_id": "t1"}


def test_p25_mcp_compat_list_api_keys_returns_list(monkeypatch):
    """§6 compat — mcp.server.list_api_keys() unwraps {"keys": [...]} → list."""
    import mcp.server as mcp_server

    monkeypatch.setattr(
        mcp_server, "_run_tool",
        lambda name, arguments=None: {"keys": [{"name": "k1"}]},
    )
    result = mcp_server.list_api_keys()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0] == {"name": "k1"}
