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
        assert tools[name]["outputSchema"]["type"] == "object"

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
    body = denied.json()
    assert body["error"]["code"] == -32001
    assert "permission" in body["error"]["message"].lower()


def test_p22_mcp_1_list_capabilities_tool(hub_main, client, admin_headers):
    """Phase 2.2 §3.4 / §5 row 8 / FR-CAP-6.

    (a) tools/list exposes `list-capabilities` with the documented schemas.
    (b) tools/call list-capabilities returns the top-level array directly
        (no wrapper) per Fix F6.
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
    assert spec["outputSchema"]["type"] == "array"
    item_schema = spec["outputSchema"]["items"]
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

    # (b) tools/call returns the top-level array
    call = _call_tool(client, admin_headers, "list-capabilities", {}, request_id=2)
    assert call.status_code == 200, call.text
    body = call.json()
    assert "error" not in body, body
    rows = body["result"]["structuredContent"]
    assert isinstance(rows, list), f"expected top-level array, got {type(rows)}: {rows}"
    by_name = {(r["name"], r["version"]): r for r in rows}
    assert ("code", 2) in by_name
    assert ("echo", 1) in by_name
    code_row = by_name[("code", 2)]
    assert code_row["agent_count"] == 1
    assert code_row["payload_schema"] == {"type": "object", "required": ["src"]}
    echo_row = by_name[("echo", 1)]
    assert echo_row["agent_count"] == 1
    assert echo_row["payload_schema"] is None

    # text-content mirror is also a JSON-encoded array
    text = body["result"]["content"][0]["text"]
    decoded = json.loads(text)
    assert isinstance(decoded, list)

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
