# Phase 2.5 Design v1 — MCP Spec Compliance + Cross-Client Verification

**Status:** designer-v1, awaiting review.
**Scope ref:** `design/phase2.5/scope.md` (locked 2026-05-10 16:52).
**Surface:** `hub/mcp_protocol.py`, `hub/mcp.py`, `hub/main.py` (only the `/mcp` endpoint wrapper), `tests/test_mcp_protocol.py`.

---

## 1. Goal

Make the hub's MCP surface fully compliant with the MCP `2025-03-26` spec so Claude Code, Codex, and OpenCode all load `tools/list`, discover all 13 tools, and successfully invoke `tools/call`. Concretely: every declared `outputSchema` must have `type == "object"`; every `tools/call` `structuredContent` must be a JSON object (not an array); and unauthenticated `/mcp` HTTP requests must return `401` with a `WWW-Authenticate: Bearer` header (not `403`) so spec-compliant clients use bearer auth instead of falling into an OAuth-discovery probe.

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

Unauthenticated → `403 Forbidden`. There is no `WWW-Authenticate` header.

**MCP spec § Authentication / Authorization (Streamable HTTP):** when the server requires bearer credentials, it MUST return `401 Unauthorized` with `WWW-Authenticate: Bearer realm="…"`. `403` signals "you authenticated but lack permission" — distinct semantics. The `401`/`403` distinction is load-bearing: spec-compliant clients (Claude Code's HTTP transport in particular) interpret a bare `403` as "OAuth probably required" and trigger `/.well-known/oauth-authorization-server` discovery, which 404s on our hub, which surfaces the "Connection error · Reconnecting" loop the user has been seeing.

**Finding:** **NEEDS-FIX.** Change unauthenticated `/mcp` response from `403` to `401` + `WWW-Authenticate: Bearer realm="agent-hub"` header. Authenticated-but-invalid bearer values stay `401` (same path). Permission failures inside the JSON-RPC handler stay as JSON-RPC errors inside `200 OK` — that contract is unchanged.

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

   **Test impact:** `test_mcp_tools_list_exposes_required_schemas` asserts every tool's `outputSchema.type == "object"`. That assertion must be relaxed to `"outputSchema not in tool OR tool["outputSchema"]["type"] == "object"`. Covered by the test list in §6.

### 4.2 Verdict on the PM in-flight patch (`list-capabilities`)

**Keep.** The wrap-as-`{"capabilities": [...]}` shape is the right pattern. Generalize it to the three other list-returning tools (§4.1). The PM's test update (`test_p22_mcp_1_list_capabilities_tool`) already validates the wrapped shape; no further test churn needed for that tool.

### 4.3 HTTP `/mcp` auth response (Fix from §3)

Edit `hub/main.py` `mcp_endpoint`:

```python
WWW_AUTH = 'Bearer realm="agent-hub"'

@app.post("/mcp")
async def mcp_endpoint(request: Request, authorization: Optional[str] = Header(None)):
    auth_key = get_auth_key(authorization)
    if not auth_key:
        # MCP spec: 401 + WWW-Authenticate: Bearer signals header auth; 403 wrongly
        # triggers OAuth discovery in spec-compliant clients (e.g. Claude Code).
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "authentication required"}},
            status_code=401,
            headers={"WWW-Authenticate": WWW_AUTH},
        )
    ...
```

Notes:
- Body remains a valid JSON-RPC error envelope (some clients parse this; others only read status + header).
- Authenticated requests with a bad/revoked key currently fall through to per-method permission checks → JSON-RPC error inside `200`. **Unchanged** — that's the established contract.
- REST endpoints other than `/mcp` keep their existing 401/403 behavior (out of scope per the constraint).

### 4.4 No REST changes

`/capabilities`, `/agents`, `/tasks`, `/admin/keys` REST responses are **unchanged**. The wrapping is wholly inside `call_tool` and only affects the MCP boundary, per scope guardrail.

---

## 5. Module slice boundaries for parallel implementation

Three disjoint slices. Designed to merge in any order; each owns its tests.

### Slice A — Tool wrapping (the bulk of the fix)

- **Scope:** add `AGENTS_OUTPUT`, `TASKS_OUTPUT`, `KEYS_OUTPUT` constants; attach `outputSchema` to `list-agents`, `list-tasks`, `list-api-keys` in `TOOLS`; wrap the three list returns in `call_tool`; drop the default-injection in `list_tool_definitions` per §4.1 step 4.
- **MAY EDIT:** `hub/mcp_protocol.py`.
- **READ-ONLY:** `hub/main.py`, `hub/registry.py`, `hub/queue.py`, `hub/auth.py`, `hub/models.py`.
- **Tests owned (numbered in §6):** p25_mcp_1, p25_mcp_2, p25_mcp_3, p25_mcp_4, p25_mcp_5, p25_mcp_6.
- **Depends on:** none.

### Slice B — HTTP `/mcp` 401 + WWW-Authenticate

- **Scope:** change unauthenticated `/mcp` from `403` to `401` + `WWW-Authenticate: Bearer realm="agent-hub"`. Add the realm constant.
- **MAY EDIT:** `hub/main.py` (only the `mcp_endpoint` function body, ~10 lines), `hub/mcp.py` (no changes expected, but read-listed in case import surface changes).
- **READ-ONLY:** `hub/mcp_protocol.py`, `hub/auth.py`.
- **Tests owned (numbered in §6):** p25_mcp_7, p25_mcp_8.
- **Depends on:** none.

### Slice C — Test-suite update + cross-tool end-to-end coverage

- **Scope:** update `test_mcp_tools_list_exposes_required_schemas` to handle the new "outputSchema may be absent" reality (§4.1 step 4); add the new end-to-end tests p25_mcp_9, p25_mcp_10, p25_mcp_11 against `tools/call` over the in-process HTTP endpoint.
- **MAY EDIT:** `tests/test_mcp_protocol.py`.
- **READ-ONLY:** all of `hub/`.
- **Tests owned (numbered in §6):** p25_mcp_9, p25_mcp_10, p25_mcp_11, plus the patch to the existing schema-listing test.
- **Depends on:** Slice A merged (test assertions reference the new wrapped shapes).

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
- **test_p25_mcp_7_unauthed_mcp_returns_401_with_www_authenticate:** `client.post("/mcp", json=_rpc("tools/list"))` (no Authorization header) → `r.status_code == 401`, `r.headers["WWW-Authenticate"].lower().startswith("bearer")`, `r.headers["WWW-Authenticate"]` contains `realm="agent-hub"`. Proves §4.3.
- **test_p25_mcp_8_authed_mcp_stays_200:** same call with `admin_headers` → `r.status_code == 200`, JSON-RPC `result` present. Negative-regression guard for slice B.
- **test_p25_mcp_9_e2e_list_agents_via_mcp:** register an agent via REST, then `_call_tool(client, admin_headers, "list-agents", {})` → `body["result"]["structuredContent"]["agents"][0]["agent_id"]` matches. End-to-end representative for list-agents.
- **test_p25_mcp_10_e2e_submit_task_via_mcp:** `_call_tool(client, submitter_headers, "submit-task", {"task": "echo", "task_type": "echo", "payload": {"x":1}})` → `body["result"]["structuredContent"]` is an object containing `task_id` and `status`. Mirrors today's `test_mcp_submit_task_and_get_task_round_trip` but explicitly asserts object-shape and content-text mirror equality, as a phase-2.5 anchor.
- **test_p25_mcp_11_e2e_list_capabilities_via_mcp:** smoke-level re-assertion that `_call_tool(list-capabilities)` returns `structuredContent == {"capabilities": [...]}`. Cheap; protects against future regression on the PM in-flight patch.

Plus one **patch** (not a new test) to `test_mcp_tools_list_exposes_required_schemas`: relax `assert tools[name]["outputSchema"]["type"] == "object"` to `assert "outputSchema" not in tools[name] or tools[name]["outputSchema"]["type"] == "object"`. The list of tool names enumerated in that loop is unchanged.

**Coverage map (NEEDS-FIX → test):**
- list-agents wrap → 1, 2, 9
- list-tasks wrap → 3, 4
- list-api-keys wrap → 5, 6
- /mcp 401+WWW-Authenticate → 7 (positive), 8 (regression guard)
- representative end-to-end → 9 (list-agents), 10 (submit-task), 11 (list-capabilities)

---

## 7. Cross-client verification plan

PM runs these manually after coder slices merge. The hub under test is the **local** instance (in-process or `python -m hub.mcp` stdio); the remote 10.9.0.10 hub stays untouched until redeploy.

### 7.1 Setup (once)

```bash
# From repo root
export AGENT_HUB_DB_PATH=$(mktemp -d)/p25.db
export AGENT_HUB_ADMIN_KEY=p25-admin-key-xxxxxxxxxxxx
uvicorn hub.main:app --host 127.0.0.1 --port 8390 &
HUB_PID=$!
# wait ~1s for boot
curl -s http://127.0.0.1:8390/health
```

### 7.2 Claude Code (stdio)

**Config:** `~/.claude.json` → `mcpServers.agent-hub` already points at `python -m hub.mcp` with `AGENT_HUB_URL=http://127.0.0.1:8390` and `AGENT_HUB_API_KEY=$AGENT_HUB_ADMIN_KEY` (PM updates the env in the config block if needed).

**Verify tools/list:**

```bash
claude --debug-mcp -p "List my MCP tools from agent-hub."
# Pass criterion: output enumerates all 13 tool names with no error.
```

**Verify the 3 representative tools:**

```bash
claude -p "Use agent-hub's list-agents tool and show me the agents."        # tools/call list-agents
claude -p "Use agent-hub's submit-task tool to submit task 'echo hi'."       # tools/call submit-task
claude -p "Use agent-hub's list-capabilities tool and show me capabilities." # tools/call list-capabilities
```

**Pass criteria:** Claude renders the tool result for each (an agent list, a `task_id`, and a capabilities list respectively). No "server returned an array, client expected a record" error. No "Connection error · Reconnecting" loop on startup.

### 7.3 Codex (stdio or HTTP — TBD per §8 Q1)

Config likely at `~/.codex/config.toml` or `~/.config/codex/config.toml` (PM to confirm). Expected stanza form (mirrors Claude):

```toml
[mcp_servers.agent-hub]
command = "python"
args = ["-m", "hub.mcp"]
env = { AGENT_HUB_URL = "http://127.0.0.1:8390", AGENT_HUB_API_KEY = "p25-admin-key-xxxxxxxxxxxx" }
```

**Verify:**

```bash
codex --version  # confirm 0.130
codex mcp list-tools agent-hub                               # if codex exposes a list-tools subcommand
codex -p "Use the agent-hub MCP server's list-agents tool."  # invocation form TBD
codex -p "Use agent-hub's submit-task tool with task='echo hi'."
codex -p "Use agent-hub's list-capabilities tool."
```

**Pass criteria:** same as Claude — tools enumerate, calls return parsed results.

### 7.4 OpenCode (stdio)

Config likely at `~/.config/opencode/config.json` (PM to confirm). Same stdio stanza pattern.

**Verify:**

```bash
opencode --version
opencode run "List MCP tools from agent-hub."                                   # tools/list
opencode run "Use agent-hub's list-agents tool."                                # tools/call list-agents
opencode run "Use agent-hub's submit-task tool with task='echo hi'."            # tools/call submit-task
opencode run "Use agent-hub's list-capabilities tool."                          # tools/call list-capabilities
```

**Pass criteria:** same as the others. (Per the user's MEMORY note, opencode `run` can hang on multi-step planning prompts — keep prompts narrow and single-step.)

### 7.5 HTTP-transport smoke (Claude Code only, if applicable)

Optional add-on, validating the §4.3 fix end-to-end:

```bash
# Unauthed → expect 401 + WWW-Authenticate
curl -i -X POST http://127.0.0.1:8390/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# Authed → expect 200 + JSON-RPC result with 13 tools
curl -i -X POST http://127.0.0.1:8390/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $AGENT_HUB_ADMIN_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### 7.6 Matrix pass criteria

| Client | tools/list | list-agents | submit-task | list-capabilities |
|---|---|---|---|---|
| Claude Code | enumerates 13 | success | task_id returned | rows returned |
| Codex | enumerates 13 | success | task_id returned | rows returned |
| OpenCode | enumerates 13 | success | task_id returned | rows returned |

All 12 cells must pass for phase 2.5 to be DONE.

---

## 8. Open questions / known unknowns

- **Q1 — Codex MCP config path & invocation syntax.** I don't know whether codex 0.130 reads `~/.codex/config.toml`, `~/.config/codex/config.toml`, or something else; nor the exact subcommand to enumerate or call a registered MCP tool. Proposed investigation: PM runs `codex --help`, `codex mcp --help`, and inspects `~/.codex/` and `~/.config/codex/` for an existing config; if no MCP support is found, codex falls out of the matrix and the user is notified explicitly.
- **Q2 — OpenCode MCP config path.** Same unknown. Proposed: `opencode --help`, look for an `mcp` subcommand or `mcpServers`-style config; if absent, opencode also falls out and the matrix downgrades to 8 cells.
- **Q3 — Detailed item schemas inside `agents` / `tasks` / `keys` wrappers.** Design v1 uses `additionalProperties: True` for items inside the array. We could declare full Agent/Task/ApiKey shapes by reusing the Pydantic schemas. **Recommendation: defer.** It expands the change surface (every model edit becomes an MCP-schema edit) and there is no client demand. Revisit if a client surfaces a need.
- **Q4 — Should we also surface 401 + WWW-Authenticate on `/agents`, `/tasks`, etc.?** Out of scope: the user explicitly constrained REST shape to remain unchanged. The OAuth-probe symptom comes from MCP discovery, not REST.
- **Q5 — Protocol-version negotiation.** We hard-code `2025-03-26` in `initialize`. If a client sends `initialize` with a newer `protocolVersion`, we still respond with `2025-03-26` and let the client decide whether to proceed. This matches today's behavior and is explicitly out of scope per the scope doc, but worth flagging if cross-client testing surfaces a negotiation mismatch.
- **Q6 — `WWW-Authenticate` realm value.** Chose `agent-hub`. No spec constraint on the string; any non-empty token works. If the PM has a preferred form for the deployed hub (e.g., domain-scoped), trivial edit.

---

**End of design v1.**
