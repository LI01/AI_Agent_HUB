# Phase 2.5 Design v3 — MCP Spec Compliance + Cross-Client Verification

**STATUS: FROZEN — phase 2.5 implementation reference. APPROVED by codex reviewer at 2026-05-10 ~18:25 local. Subsequent code is implemented against this version.**

**Status:** designer-v3, incorporates reviewer-v2 Finding 1 verbatim.
**Scope ref:** `design/phase2.5/scope.md` (locked 2026-05-10 16:52).
**Surface:** `hub/mcp_protocol.py`, `hub/mcp.py`, `hub/main.py` (only the `/mcp` endpoint wrapper), `mcp/server.py` (compatibility CLI — added per reviewer Finding 5), `tests/test_mcp_protocol.py`.

---

## 1. Goal

Make the hub's MCP surface fully compliant with the MCP `2025-03-26` spec so Claude Code, Codex, and OpenCode all load `tools/list`, discover all 13 tools, and successfully invoke `tools/call`. Concretely: every declared `outputSchema` must have `type == "object"`; every `tools/call` `structuredContent` must be a JSON object (not an array); the `/mcp` HTTP endpoint must return `401` with `WWW-Authenticate: Bearer realm="agent-hub"` whenever the bearer is missing OR invalid; and the legacy `mcp/server.py` compatibility CLI must continue to return the same list types it returned before this phase.

Note on Claude Code HTTP: returning the correct `401`/`WWW-Authenticate` is a spec-compliance fix to the hub. It does **not** by itself enable Claude Code to authenticate via HTTP — Claude Code over HTTP without OAuth is explicitly out of scope per the locked scope doc. Claude Code verification in this phase remains stdio unless header auth is independently confirmed.

---

## 2. Audit table — every tool in `TOOLS`

`tools/list` synthesis logic (`list_tool_definitions`) injects a permissive default `outputSchema = {"type": "object", "additionalProperties": True}` for any tool that does not declare one. So **today every tool advertises `outputSchema.type == "object"`** — including tools whose runtime `structuredContent` is actually a JSON array (`list-agents`, `list-tasks`, `list-api-keys`). That mismatch is the root cause of the "server returned an array, client expected a record" failure: the advertised schema says object, the wire payload is an array, and strict clients reject the call.

Spec lookups:
- `inputSchema.type` MUST be `"object"`. ✅ enforced by `_object_schema(...)` for every tool.
- `outputSchema.type` (when present) MUST be `"object"`.
- `structuredContent` (when an `outputSchema` is advertised) MUST be a JSON object matching that schema.
- `content[0].text` MUST be the JSON-encoded mirror of `structuredContent`. ✅ already done in `_handle_one`.

Legend: ✓ = compliant. ✗ = NEEDS-FIX. Handler-return shape is what `call_tool` currently returns (before MCP wrapping).

| # | Tool | `inputSchema` | Declared `outputSchema`? | Effective `outputSchema` on the wire | Handler return shape | `structuredContent` is object? | Finding |
|---|---|---|---|---|---|---|---|
| 1 | `list-agents` | object ✓ | none | injected default object ✓ | **list[Agent]** | ✗ array | **NEEDS-FIX**: wrap as `{"agents": [...]}` |
| 2 | `get-agent` | object ✓ | none | injected default object ✓ | `Agent` (object) | ✓ | compliant |
| 3 | `register-agent` | object ✓ | none | injected default object ✓ | `{"status","agent_id","dispatched"}` | ✓ | compliant |
| 4 | `unregister-agent` | object ✓ | none | injected default object ✓ | `{"status": "unregistered"}` | ✓ | compliant |
| 5 | `submit-task` | object ✓ | none | injected default object ✓ | `TaskSubmitResponse` object | ✓ | compliant |
| 6 | `list-capabilities` | object ✓ | **declared** object ✓ (PM in-flight patch) | object ✓ | `{"capabilities": [...]}` (PM wrapped) | ✓ | compliant — keep PM patch (see §4.1) |
| 7 | `get-task` | object ✓ | none | injected default object ✓ | `Task` (object) | ✓ | compliant |
| 8 | `list-tasks` | object ✓ | none | injected default object ✓ | **list[Task]** | ✗ array | **NEEDS-FIX**: wrap as `{"tasks": [...]}` |
| 9 | `stats` | object ✓ | none | injected default object ✓ | dict | ✓ | compliant |
| 10 | `health` | object ✓ | none | injected default object ✓ | dict | ✓ | compliant |
| 11 | `create-api-key` | object ✓ | none | injected default object ✓ | `CreateKeyResponse` object | ✓ | compliant |
| 12 | `list-api-keys` | object ✓ | none | injected default object ✓ | **list[dict]** | ✗ array | **NEEDS-FIX**: wrap as `{"keys": [...]}` |
| 13 | `revoke-api-key` | object ✓ | none | injected default object ✓ | `{"status","name"}` | ✓ | compliant |

**Summary:** 3 NEEDS-FIX tools (`list-agents`, `list-tasks`, `list-api-keys`). One PM-in-flight patch (`list-capabilities`) is kept and generalized into a consistent wrapping convention.

### Error-code mapping audit

`McpToolError` flows through `_handle_one` and is mapped to JSON-RPC error codes. Current mapping in `hub/mcp_protocol.py`:

| Situation | Today's code | JSON-RPC standard | Verdict |
|---|---|---|---|
| Malformed JSON-RPC envelope (no `jsonrpc:"2.0"`) | `-32600` | `-32600` Invalid Request | ✓ |
| Unknown method | `-32601` | `-32601` Method not found | ✓ |
| `tools/call` missing `name` or non-object `params`; unknown tool | `-32602` | `-32602` Invalid params | ✓ |
| Backend `KeyError` (missing required field) | `-32602` | `-32602` Invalid params | ✓ |
| `payload_schema_violation` from REST 400 | `-32602` (with `data` carrying detail) | `-32602` Invalid params | ✓ |
| Other REST HTTPException (e.g., 403/404) | `-32001` application error | non-standard app range OK; spec reserves `-32099..-32000` for impl-defined server errors | ✓ |
| Unsupported backend path | `-32603` Internal error | `-32603` | ✓ |
| Generic unhandled exception | `-32603` | `-32603` | ✓ |
| Stdio JSON parse error | `-32700` | `-32700` Parse error | ✓ |

**Finding:** error mapping is **compliant**. No fix needed.

---

## 3. HTTP endpoint audit — POST /mcp

Current behavior (`hub/main.py:1229-1244`):

```python
@app.post("/mcp")
async def mcp_endpoint(request: Request, authorization: Optional[str] = Header(None)):
    auth_key = get_auth_key(authorization)
    if not auth_key:
        raise HTTPException(status_code=403, detail="MCP authentication required")
    ...
```

Two problems:

1. Unauthenticated requests return `403 Forbidden` without a `WWW-Authenticate` header. The MCP spec § Authentication / Authorization (Streamable HTTP) requires `401 Unauthorized` + `WWW-Authenticate: Bearer realm="…"` when the server requires bearer credentials. `403` signals "you authenticated but lack permission" — distinct semantics. A bare `403` causes some spec-compliant HTTP clients to interpret it as "OAuth discovery required" and fetch `/.well-known/oauth-authorization-server`, which 404s.
2. Authenticated requests with an invalid or revoked bearer never trigger any HTTP-level rejection. The current code only checks `not auth_key`; any non-empty token reaches `handle_jsonrpc` and surfaces inside a `200 OK` JSON-RPC error. MCP 2025-03-26 requires `401` for both "missing" and "invalid token" cases.

**Finding:** **NEEDS-FIX** on both counts. See §4.3 for the verbatim auth-plan replacement.

---

## 4. Fix plan

### 4.1 Tool return-shape wrapping (3 list-returning tools)

Wrap the three list-returning handler outputs at the MCP boundary so `structuredContent` is always an object. REST callers are unaffected — the wrapping happens in `hub/mcp_protocol.py::call_tool`, not in the REST handler. Declare matching `outputSchema` on each tool so the advertised contract matches the wire.

Convention (matches the PM in-flight patch on `list-capabilities`): **wrap a top-level array under a single key whose name is the tool's natural plural** (`agents`, `tasks`, `keys`, `capabilities`). One in-code comment per wrap points at MCP spec § "Tool result schemas".

Concrete edits in `hub/mcp_protocol.py`:

1. Add three new output-schema constants near the existing `CAPABILITY_OUTPUT`:

   - `AGENTS_OUTPUT = {"type": "object", "properties": {"agents": {"type": "array", "items": {"type": "object", "additionalProperties": True}}}, "required": ["agents"], "additionalProperties": True}`
   - `TASKS_OUTPUT = {"type": "object", "properties": {"tasks": {"type": "array", "items": {"type": "object", "additionalProperties": True}}}, "required": ["tasks"], "additionalProperties": True}`
   - `KEYS_OUTPUT = {"type": "object", "properties": {"keys": {"type": "array", "items": {"type": "object", "additionalProperties": True}}}, "required": ["keys"], "additionalProperties": True}`

   Inner items use `additionalProperties: True` rather than detailed Agent/Task schemas. Rationale: we already keep `outputSchema.type == "object"` strictness at the top level (which is what MCP enforces). Detailed item schemas would lock us into structural commitments orthogonal to this phase's scope. **Open question Q3** in §8 flags this.

2. Attach `outputSchema` to the three NEEDS-FIX tool entries: `list-agents → AGENTS_OUTPUT`, `list-tasks → TASKS_OUTPUT`, `list-api-keys → KEYS_OUTPUT`. `list-capabilities` already declares `CAPABILITY_OUTPUT` — leave it.

3. In `call_tool(...)`:
   - `list-agents` branch: `return {"agents": await backend.request("GET", "/agents")}`
   - `list-tasks` branch: `return {"tasks": await backend.request("GET", "/tasks", params=...)}`
   - `list-api-keys` branch: `return {"keys": await backend.request("GET", "/admin/keys")}`

   The `list-capabilities` branch already wraps as `{"capabilities": [...]}` per the PM patch — kept.

4. Drop the permissive default-injection in `list_tool_definitions`. After step 2, tools either (a) declare a precise `outputSchema` (the 4 list-returning ones), or (b) declare none. The MCP spec says `outputSchema` is OPTIONAL — omitting it on tools that have no structural commitment is preferable to advertising a vacuous `{type:"object", additionalProperties:true}` that clients can interpret as a hard promise. Concretely: `list_tool_definitions` becomes a simple `{"name", "description", "inputSchema", **({"outputSchema": spec["outputSchema"]} if "outputSchema" in spec else {})}` build.

   **This is a deliberate change vs the PM patch's approach.** The PM kept the default-injection. Dropping it means `tools/list` no longer fabricates a contract the server doesn't actually own. This is the only place the design supersedes the PM in-flight code.

   **Test impact:** `test_mcp_tools_list_exposes_required_schemas` asserts every tool's `outputSchema.type == "object"`. That assertion must be relaxed to `"outputSchema not in tool OR tool["outputSchema"]["type"] == "object"`. Covered by the test list in §6. A separate **positive** test (p25_mcp_12) asserts the default injection is actually gone, so a coder who forgets step 4 still fails CI.

### 4.2 Verdict on the PM in-flight patch (`list-capabilities`)

**Keep.** The wrap-as-`{"capabilities": [...]}` shape is the right pattern. Generalize it to the three other list-returning tools (§4.1). The PM's test update (`test_p22_mcp_1_list_capabilities_tool`) already validates the wrapped shape; no further test churn needed for that tool.

### 4.3 HTTP `/mcp` auth response (Fix from §3) — VERBATIM per reviewer Finding 1

Before parsing the JSON-RPC body, compute `auth_key = get_auth_key(authorization)`; if `auth_key` is missing or `access_control.validate_key(auth_key)` returns `None`, return HTTP 401 with headers `{'WWW-Authenticate': 'Bearer realm="agent-hub"'}` and a JSON-RPC error body. Do not claim this enables Claude Code HTTP; Claude Code verification remains stdio unless header auth is confirmed.

Resulting `hub/main.py::mcp_endpoint` shape (illustrative — Slice B owns the exact edit):

```python
WWW_AUTH = 'Bearer realm="agent-hub"'

@app.post("/mcp")
async def mcp_endpoint(request: Request, authorization: Optional[str] = Header(None)):
    auth_key = get_auth_key(authorization)
    if not auth_key or access_control.validate_key(auth_key) is None:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32001, "message": "authentication required"}},
            status_code=401,
            headers={"WWW-Authenticate": WWW_AUTH},
        )
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
            status_code=400,
        )
    response = await handle_jsonrpc(payload, InProcessHubBackend(auth_key))
    if response is None:
        return JSONResponse({}, status_code=202)
    return response
```

Notes:
- This pre-validates the bearer once at the HTTP boundary so both "missing" and "invalid/revoked" cases produce `401` + `WWW-Authenticate`, matching MCP 2025-03-26.
- Per-method permission failures inside `handle_jsonrpc` (e.g. non-admin calling `list-api-keys`) still surface as JSON-RPC errors inside `200 OK`. That contract is unchanged — only "is the key valid at all" moves up to HTTP.
- REST endpoints other than `/mcp` keep their existing 401/403 behavior (out of scope per the constraint).

### 4.4 `mcp/server.py` compatibility wrapper — VERBATIM per reviewer Finding 5

Add `mcp/server.py` to the implementation surface; update `list_agents` to return `_run_tool("list-agents")["agents"]`, `list_tasks` to return `_run_tool("list-tasks", args)["tasks"]`, and `list_api_keys` to return `_run_tool("list-api-keys")["keys"]`; add regression coverage for those three compatibility functions.

Rationale: those three functions are annotated and exposed as returning lists. After §4.1 wraps the underlying tool results, calling `_run_tool` (which goes through `invoke_tool` → `call_tool`) returns `{"agents": [...]}` / `{"tasks": [...]}` / `{"keys": [...]}`. Unwrapping inside the compatibility CLI keeps its public API (and the CLI's `print(json.dumps(out, …))` output for `agents` / `tasks` / `list-keys` commands) unchanged.

### 4.5 No REST changes

`/capabilities`, `/agents`, `/tasks`, `/admin/keys` REST responses are **unchanged**. The wrapping is wholly inside `call_tool` and only affects the MCP boundary, per scope guardrail.

---

## 5. Module slice boundaries for parallel implementation

Three disjoint slices. Test file ownership is exclusive to Slice C, per reviewer Finding 3.

### Slice A — Tool wrapping (the bulk of the fix)

- **Scope:** add `AGENTS_OUTPUT`, `TASKS_OUTPUT`, `KEYS_OUTPUT` constants; attach `outputSchema` to `list-agents`, `list-tasks`, `list-api-keys` in `TOOLS`; wrap the three list returns in `call_tool`; drop the default-injection in `list_tool_definitions` per §4.1 step 4. Also update `mcp/server.py::list_agents`, `list_tasks`, `list_api_keys` per §4.4 so the compatibility CLI keeps returning lists.
- **MAY EDIT:** `hub/mcp_protocol.py`, `mcp/server.py`.
- **MAY READ NOT EDIT:** `hub/main.py`, `hub/registry.py`, `hub/queue.py`, `hub/auth.py`, `hub/models.py`, `tests/test_mcp_protocol.py`.
- **Verification:** p25_mcp_1 through p25_mcp_6, implemented by Slice C, must pass after Slice A lands. Plus p25_mcp_12 (default-injection removed) and the new `mcp/server.py` regression cases owned by Slice C.
- **Depends on:** none.

### Slice B — HTTP `/mcp` 401 + WWW-Authenticate

- **Scope:** change unauthenticated OR invalid-bearer `/mcp` from `403` / 200-with-JSON-RPC-error to `401` + `WWW-Authenticate: Bearer realm="agent-hub"` per §4.3. Add the realm constant. Pre-validate the bearer via `access_control.validate_key` before parsing the JSON-RPC body.
- **MAY EDIT:** `hub/main.py` (only the `mcp_endpoint` function body and any nearby module-level constant the slice introduces, ~15 lines).
- **MAY READ NOT EDIT:** `hub/mcp_protocol.py`, `hub/auth.py`, `hub/mcp.py`, `tests/test_mcp_protocol.py`.
- **Verification:** p25_mcp_7, p25_mcp_8, p25_mcp_9 (new — invalid bearer), and the updated `test_mcp_revoked_key_fails_on_next_tool_call` — all implemented by Slice C — must pass after Slice B lands.
- **Depends on:** none.

### Slice C — Test-suite update + cross-tool end-to-end coverage

- **Scope:** update `test_mcp_tools_list_exposes_required_schemas` to handle the new "outputSchema may be absent" reality (§4.1 step 4); update `test_mcp_revoked_key_fails_on_next_tool_call` per Finding 1 to expect HTTP 401 after revoke; add the new tests p25_mcp_1 … p25_mcp_12 per §6.
- **MAY EDIT:** `tests/test_mcp_protocol.py`.
- **MAY READ NOT EDIT:** all of `hub/` and `mcp/`.
- **Tests owned:** p25_mcp_1 through p25_mcp_12 plus the patch to existing schema-listing and revoked-key tests.
- **Depends on:** Slice A merged (test assertions reference the new wrapped shapes and the dropped default injection) and Slice B merged (auth tests expect 401).

Slices A and B can land in any order; both must land before Slice C's new tests pass.

---

## 6. Numbered test list

All new tests live in `tests/test_mcp_protocol.py`. Prefix: `test_p25_mcp_*`. Each test runs via the existing `client` + `admin_headers` fixtures unless noted.

- **test_p25_mcp_1_list_agents_outputschema_declared:** `tools/list` → `tools["list-agents"]["outputSchema"]["type"] == "object"` and `outputSchema["required"] == ["agents"]` and `outputSchema["properties"]["agents"]["type"] == "array"`. Proves §4.1 step 2 for `list-agents`.
- **test_p25_mcp_2_list_agents_structuredcontent_is_object:** register one agent via REST; `tools/call` `list-agents` → `result.structuredContent` is a dict with key `"agents"` whose value is a list of length 1. `content[0].text` decoded equals `structuredContent`. Proves §4.1 step 3 for `list-agents`.
- **test_p25_mcp_3_list_tasks_outputschema_declared:** parallel to test 1 for `list-tasks` (key `"tasks"`).
- **test_p25_mcp_4_list_tasks_structuredcontent_is_object:** submit one task via REST; `tools/call` `list-tasks` → `structuredContent == {"tasks": [<task>]}`. Decoded `content[0].text` matches.
- **test_p25_mcp_5_list_api_keys_outputschema_declared:** parallel to test 1 for `list-api-keys` (key `"keys"`); uses `admin_headers`.
- **test_p25_mcp_6_list_api_keys_structuredcontent_is_object:** `tools/call` `list-api-keys` with admin → `structuredContent == {"keys": [...]}` containing at least the seeded admin key entry.
- **test_p25_mcp_7_unauthed_mcp_returns_401_with_www_authenticate:** `client.post("/mcp", json=_rpc("tools/list"))` (no Authorization header) → `r.status_code == 401`, `r.headers["WWW-Authenticate"].lower().startswith("bearer")`, `r.headers["WWW-Authenticate"]` contains `realm="agent-hub"`. Proves §4.3 missing-bearer path.
- **test_p25_mcp_8_authed_mcp_stays_200:** same call with `admin_headers` → `r.status_code == 200`, JSON-RPC `result` present. Negative-regression guard for slice B.
- **test_p25_mcp_9_invalid_bearer_returns_401_with_www_authenticate:** `client.post("/mcp", headers={"Authorization": "Bearer not-a-real-key"}, json=_rpc("tools/list"))` → `r.status_code == 401`, `r.headers["WWW-Authenticate"]` matches the same `Bearer realm="agent-hub"` shape as test 7. Proves §4.3 invalid-bearer path per reviewer Finding 1.
- **test_p25_mcp_10_e2e_submit_task_via_mcp:** uses the existing `submitter_key` fixture and constructs `headers = {"Authorization": f"Bearer {submitter_key}"}` inside the test body; calls `_call_tool(client, headers, "submit-task", {"task": "echo", "task_type": "echo", "payload": {"x":1}})` → `body["result"]["structuredContent"]` is an object containing `task_id` and `status`. Mirrors today's `test_mcp_submit_task_and_get_task_round_trip` but explicitly asserts object-shape and content-text mirror equality, as a phase-2.5 anchor.
- **test_p25_mcp_11_e2e_list_capabilities_via_mcp:** smoke-level re-assertion that `_call_tool(list-capabilities)` returns `structuredContent == {"capabilities": [...]}`. Cheap; protects against future regression on the PM in-flight patch.
- **test_p25_mcp_12_tools_without_declared_outputschema_omit_outputschema:** call `tools/list` and assert `"outputSchema" not in tools["get-agent"]`, `"outputSchema" not in tools["submit-task"]`, and `"outputSchema" not in tools["health"]`. Catches a coder who forgets §4.1 step 4 (drop the default injection). Required per reviewer Finding 2.

Plus two **patches** to existing tests:

- `test_mcp_tools_list_exposes_required_schemas`: relax `assert tools[name]["outputSchema"]["type"] == "object"` to `assert "outputSchema" not in tools[name] or tools[name]["outputSchema"]["type"] == "object"`. The list of tool names enumerated in that loop is unchanged.
- `test_mcp_revoked_key_fails_on_next_tool_call`: after `client.delete("/admin/keys/temp-view", ...)`, the next `_call_tool(client, headers, "list-agents", request_id=2)` is expected to return `r.status_code == 401` with `WWW-Authenticate: Bearer realm="agent-hub"` (the previous assertions on JSON-RPC `error.code == -32001` are removed/replaced — the rejection now happens at the HTTP boundary). Required per reviewer Finding 1.

Plus **compatibility-CLI regression tests** for the three updated `mcp/server.py` functions, also owned by Slice C, that monkeypatch `mcp.server._run_tool` to return `{"agents": [...]}`, `{"tasks": [...]}`, and `{"keys": [...]}`; call `mcp.server.list_agents`, `mcp.server.list_tasks`, and `mcp.server.list_api_keys` and assert each unwraps to a list. These tests do not run through TestClient or HttpHubBackend.

- `test_p25_mcp_compat_list_agents_returns_list`: monkeypatch `mcp.server._run_tool` to return `{"agents": [{"agent_id": "a1"}]}`; call `mcp.server.list_agents()` → assert the result is a `list` of length 1 with element `{"agent_id": "a1"}`.
- `test_p25_mcp_compat_list_tasks_returns_list`: monkeypatch `mcp.server._run_tool` to return `{"tasks": [{"task_id": "t1"}]}`; call `mcp.server.list_tasks()` → assert the result is a `list` of length 1 with element `{"task_id": "t1"}`.
- `test_p25_mcp_compat_list_api_keys_returns_list`: monkeypatch `mcp.server._run_tool` to return `{"keys": [{"name": "k1"}]}`; call `mcp.server.list_api_keys()` → assert the result is a `list` of length 1 with element `{"name": "k1"}`.

**Coverage map (NEEDS-FIX → test):**
- list-agents wrap → 1, 2, compat-list-agents
- list-tasks wrap → 3, 4, compat-list-tasks
- list-api-keys wrap → 5, 6, compat-list-api-keys
- /mcp 401+WWW-Authenticate (missing) → 7 (positive), 8 (regression guard)
- /mcp 401+WWW-Authenticate (invalid/revoked) → 9 + patched `test_mcp_revoked_key_fails_on_next_tool_call`
- default-injection actually dropped → 12
- representative end-to-end → 2/4/6 (list-shape proofs), 10 (submit-task), 11 (list-capabilities)

---

## 7. Cross-client verification plan

PM runs these manually after coder slices merge. The hub under test is the **local** instance launched in §7.1; the remote `10.9.0.10:8300` hub stays untouched until redeploy.

### 7.1 Setup (once)

```bash
# From repo root
export AGENT_HUB_DB_PATH=$(mktemp -d)/p25.db
export AGENT_HUB_ADMIN_KEY=p25-admin-key-xxxxxxxxxxxx
uvicorn hub.main:app --host 127.0.0.1 --port 8390 &
HUB_PID=$!
sleep 1
curl -s http://127.0.0.1:8390/health
```

All three clients launch the **stdio** transport `python -m hub.mcp` (with `AGENT_HUB_URL=http://127.0.0.1:8390` and `AGENT_HUB_API_KEY=$AGENT_HUB_ADMIN_KEY`). Stdio is the only transport we verify end-to-end — Claude Code HTTP remains out of scope per §1.

### 7.2 Claude Code (stdio)

**Installed version:** `claude` 2.1.x.

**Config path:** `~/.claude.json` → `mcpServers["agent-hub"]` stanza, already present and pointing at `python -m hub.mcp`. Confirmed during research:

```json
"agent-hub": {
  "type": "stdio",
  "command": "/Users/leon/Documents/work/Virtual_Env/venv_3.13_general/bin/python",
  "args": ["-m", "hub.mcp"],
  "env": {
    "AGENT_HUB_URL": "http://127.0.0.1:8390",
    "AGENT_HUB_API_KEY": "p25-admin-key-xxxxxxxxxxxx"
  }
}
```

PM edits the `env` block in `~/.claude.json` to point at the §7.1 hub URL/key before running the commands below.

**Verify tools/list:**

```bash
claude --debug-mcp -p "List my MCP tools from agent-hub."
```

Pass: output enumerates all 13 tool names with no error.

**Verify the 3 representative tools:**

```bash
claude -p "Use agent-hub's list-agents tool and show me the agents."
claude -p "Use agent-hub's submit-task tool to submit task 'echo hi' with task_type 'echo'."
claude -p "Use agent-hub's list-capabilities tool and show me capabilities."
```

Pass: Claude renders the tool result for each (an agent list, a `task_id`, and a capabilities list). No "server returned an array, client expected a record" error.

### 7.3 Codex (stdio)

**Installed version:** `codex-cli 0.130.0`. Confirmed `codex mcp` subcommand exists with `add` / `list` / `get` / `remove` actions; `codex mcp add <NAME> -- <COMMAND>...` registers a stdio MCP server.

**Config path:** `~/.codex/config.toml`. `codex mcp add` mutates this file.

**Register the hub (one-time, idempotent — re-running after a prior install requires `codex mcp remove agent-hub` first):**

```bash
codex mcp add agent-hub \
  --env AGENT_HUB_URL=http://127.0.0.1:8390 \
  --env AGENT_HUB_API_KEY=p25-admin-key-xxxxxxxxxxxx \
  -- /Users/leon/Documents/work/Virtual_Env/venv_3.13_general/bin/python -m hub.mcp
codex mcp list   # confirms agent-hub is enabled with Status=enabled
```

**Verify tools/list:**

```bash
codex exec "List the MCP tools available from the agent-hub server, then stop."
```

Pass: output enumerates all 13 tool names with no error.

**Verify the 3 representative tools:**

```bash
codex exec "Call agent-hub's list-agents tool and show the result."
codex exec "Call agent-hub's submit-task tool with task='echo hi' and task_type='echo', then show the task_id."
codex exec "Call agent-hub's list-capabilities tool and show the result."
```

Pass: Codex renders the tool result for each (agent list, `task_id`, capabilities list).

### 7.4 OpenCode (stdio)

**Installed version:** `opencode 1.14.41`. Confirmed `opencode mcp` subcommand exists with `add` / `list` / `auth` / `logout` / `debug`. `opencode mcp add` is interactive (no documented non-interactive flag set) and writes to the user's opencode config (`~/.config/opencode/opencode.json` at the user level, or `./opencode.json` at the project level). The project-level file at `/Users/leon/Documents/work/openclaw_docker/agent-hub/opencode.json` is the preferred location for this phase since it co-locates the MCP entry with the repo it talks to.

**Register the hub (one-time):** PM edits `/Users/leon/Documents/work/openclaw_docker/agent-hub/opencode.json` to add an `mcp` stanza (opencode 1.x supports a top-level `mcp` object whose entries take the form below; this matches the schema used by other opencode users who run `opencode mcp add` and inspect the resulting JSON):

```json
"mcp": {
  "agent-hub": {
    "type": "local",
    "command": ["/Users/leon/Documents/work/Virtual_Env/venv_3.13_general/bin/python", "-m", "hub.mcp"],
    "environment": {
      "AGENT_HUB_URL": "http://127.0.0.1:8390",
      "AGENT_HUB_API_KEY": "p25-admin-key-xxxxxxxxxxxx"
    },
    "enabled": true
  }
}
```

Then confirm registration:

```bash
opencode mcp list   # expect agent-hub present and enabled
```

**Verify tools/list:**

```bash
opencode run "List the MCP tools available from the agent-hub server, then stop."
```

Pass: output enumerates all 13 tool names.

**Verify the 3 representative tools (kept single-step per the MEMORY note that opencode `run` can hang on multi-step planning prompts):**

```bash
opencode run "Call agent-hub's list-agents tool and show the result."
opencode run "Call agent-hub's submit-task tool with task='echo hi' and task_type='echo'."
opencode run "Call agent-hub's list-capabilities tool and show the result."
```

Pass: opencode renders the tool result for each.

### 7.5 HTTP-transport smoke (curl only — validates §4.3 fix)

End-to-end validation of the HTTP boundary, independent of any client. Not part of the 12-cell matrix; this is a direct check that the §4.3 edit is live.

```bash
# Missing bearer → expect 401 + WWW-Authenticate
curl -i -X POST http://127.0.0.1:8390/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Invalid bearer → expect 401 + WWW-Authenticate
curl -i -X POST http://127.0.0.1:8390/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer not-a-real-key" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Valid bearer → expect 200 + JSON-RPC result with 13 tools
curl -i -X POST http://127.0.0.1:8390/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AGENT_HUB_ADMIN_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### 7.6 Matrix pass criteria

| Client | tools/list | list-agents | submit-task | list-capabilities |
|---|---|---|---|---|
| Claude Code (stdio) | enumerates 13 | success | task_id returned | rows returned |
| Codex (stdio) | enumerates 13 | success | task_id returned | rows returned |
| OpenCode (stdio) | enumerates 13 | success | task_id returned | rows returned |

All 12 cells must pass for phase 2.5 to be DONE. All three clients confirmed installed and confirmed to support MCP stdio registration, so no scope reduction is proposed.

---

## 8. Open questions / known unknowns

- **Q1 — Detailed item schemas inside `agents` / `tasks` / `keys` wrappers.** Design v3 uses `additionalProperties: True` for items inside the array. We could declare full Agent/Task/ApiKey shapes by reusing the Pydantic schemas. **Recommendation: defer.** It expands the change surface (every model edit becomes an MCP-schema edit) and there is no client demand. Revisit if a client surfaces a need.
- **Q2 — Should we also surface 401 + WWW-Authenticate on `/agents`, `/tasks`, etc.?** Out of scope: the locked scope constrains REST shape to remain unchanged. The OAuth-probe symptom comes from MCP discovery, not REST.
- **Q3 — Protocol-version negotiation.** We hard-code `2025-03-26` in `initialize`. If a client sends `initialize` with a newer `protocolVersion`, we still respond with `2025-03-26` and let the client decide whether to proceed. This matches today's behavior and is explicitly out of scope per the scope doc, but worth flagging if cross-client testing surfaces a negotiation mismatch.
- **Q4 — `WWW-Authenticate` realm value.** Chose `agent-hub`. No spec constraint on the string; any non-empty token works. If the PM has a preferred form for the deployed hub (e.g., domain-scoped), trivial edit.
- **Q5 — Project-level vs user-level `opencode.json`.** §7.4 writes the MCP stanza to the project-level `opencode.json` to keep it co-located with this repo. If `opencode run` is invoked from outside the repo it will not see the stanza; PM runs `opencode run` from the repo root. If a future phase needs cross-repo invocation, move the stanza to `~/.config/opencode/opencode.json`.

---

**End of design v3.**
