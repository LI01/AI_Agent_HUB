# Agent Hub — User's Guide

A task-oriented walkthrough. For an overview and reference, see [`README.md`](./README.md). For the wire-level spec, see [`requirements.md`](./requirements.md).

This guide assumes you are one of:

- **A submitter** — a human (or MCP-aware tool like Claude Code / OpenCode) who wants agents to do work. See §1 for REST/curl, §1.8 for MCP.
- **An agent author** — building a new agent that listens for tasks. See §2.
- **An admin** — running the hub and managing keys. See §3.

Pick your role below.

---

## 0. First-time setup (admin)

```bash
# Clone, set up Python venv, install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Pick a strong bootstrap admin key. Store it somewhere safe.
export AGENT_HUB_ADMIN_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
echo "$AGENT_HUB_ADMIN_KEY"  # save this — you'll need it to make more keys

# Start the hub
.venv/bin/python -m uvicorn hub.main:app --host 0.0.0.0 --port 8080
```

If you skip `AGENT_HUB_ADMIN_KEY`, the hub generates one on first start and prints it once. Capture it from stdout — there is no second chance.

The SQLite file lives at `./agent_hub.db` by default. Override with `AGENT_HUB_DB_PATH=/var/lib/agent-hub/db.sqlite`.

Verify:

```bash
curl http://localhost:8080/health
# {"status":"ok","agents":0,"tasks":0,"db":{...}}
```

---

## 1. Submitting tasks (humans / tools)

### 1.1 Get an API key

Ask the admin for one. Or, if you are the admin:

```bash
curl -X POST http://localhost:8080/admin/keys \
  -H "Authorization: Bearer $AGENT_HUB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-laptop","can_assign_tasks":true,"can_view_tasks":true,"can_view_agents":true}'
```

Response includes `"api_key": "...long string..."`. **Store it now** — calling `GET /admin/keys` later will not return the raw key.

### 1.2 List available agents

```bash
curl http://localhost:8080/agents \
  -H "Authorization: Bearer $YOUR_KEY"
```

Each agent shows `agent_id`, `capabilities`, `status` (`idle` / `busy` / `offline`), and `machine_info`. Only `idle` agents can accept new work; `busy` ones are mid-task; `offline` ones are disconnected.

### 1.3 Submit a task — natural language

The simplest form. The hub auto-detects task type from keywords (`echo`, `camera`, `build`, etc.).

```bash
curl -X POST http://localhost:8080/tasks \
  -H "Authorization: Bearer $YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task":"echo hello world"}'
# {"task_id":"6a7b...","status":"assigned"}
```

Response status:
- `assigned` — an agent picked it up immediately.
- `queued` — no matching agent online yet; will dispatch when one appears.

### 1.4 Submit a task — structured

When you want a specific task type, payload, target, priority, or timeout:

```bash
curl -X POST http://localhost:8080/tasks \
  -H "Authorization: Bearer $YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task": "process the upload",
    "task_type": "upload-process",
    "payload": {"file_id": "abc123", "options": {"format": "json"}},
    "target_agent": "worker-1",
    "priority": 5,
    "timeout": 600
  }'
```

- **`target_agent`** — route to a specific agent (must be online).
- **`task_type`** — must match one of the agent's `capabilities`.
- **`priority`** — higher dispatches first. Default `0`.
- **`timeout`** — seconds before the hub marks it `timeout`. Default `300`.

### 1.5 Check status

```bash
curl http://localhost:8080/tasks/$TASK_ID \
  -H "Authorization: Bearer $YOUR_KEY"
```

Status progression: `queued` → `assigned` → `running` → `completed` (or `failed` / `timeout`). The response includes `result` (when complete), `logs` (incremental), `assigned_agent_id`, and timestamps.

Filter the list:

```bash
curl "http://localhost:8080/tasks?status=running" -H "Authorization: Bearer $YOUR_KEY"
curl "http://localhost:8080/tasks?agent_id=worker-1" -H "Authorization: Bearer $YOUR_KEY"
```

### 1.6 Watch live events

Server-Sent Events for real-time task and agent updates:

```bash
curl -N http://localhost:8080/events \
  -H "Authorization: Bearer $YOUR_KEY"
```

Event types: `agent_registered`, `agent_status`, `agent_unregistered`, `task_created`, `task_updated`, `task_completed`.

### 1.7 Common gotchas

- **403 Forbidden** — your key is missing the right permission, or you're trying to target an agent not in your `allowed_agents` list.
- **Task stays `queued`** — no online agent has the matching `task_type` in its capabilities. Check `/agents`.
- **404 on `GET /tasks/{id}`** — the task ID doesn't exist (typo or wrong hub).

### 1.8 Connecting via MCP (recommended for Claude Code / OpenCode)

Phase 1.5 ships a real MCP server. If your client speaks MCP (Claude Code, OpenCode, any MCP-aware tool), use that instead of curl — you get tool discovery, schemas, and the same auth/permission model without writing HTTP plumbing.

**One-shot onboarding (Claude Code).** Add the hub as a remote MCP server:

```bash
claude mcp add agent-hub http://localhost:8080/mcp \
  --scope local --transport http \
  --header "Authorization: Bearer $YOUR_KEY"
```

Or use the bundled skill, which writes the same config for you:

```bash
# From inside Claude Code or OpenCode
/agent-hub <hub-url> <your-key>
```

The skill lives at `skills/agent-hub/`; it's a thin convenience layer — anything it does is reachable directly via the MCP tools below.

**Local stdio mode** (no HTTP, for desktop clients):

```bash
AGENT_HUB_URL=http://localhost:8080 \
AGENT_HUB_API_KEY=$YOUR_KEY \
.venv/bin/python -m hub.mcp
```

**Tools you get.** After connecting, your MCP client discovers these via `tools/list`:

| Tool | Purpose |
|------|---------|
| `list-agents` | List registered agents and their status. |
| `get-agent` | Get one agent's details. |
| `register-agent` | Register a new agent (rare from MCP — usually agents register themselves via WS). |
| `unregister-agent` | Remove an agent. |
| `submit-task` | Submit a task. Same fields as `POST /tasks`: `task`, `task_type`, `payload`, `target_agent`, `priority`, `timeout`, `parent_task_id`. |
| `get-task` | Get task status, result, logs. |
| `list-tasks` | Filter by `status` and/or `agent_id`. |
| `stats` | Aggregate counts. |
| `health` | Hub health check. |
| `create-api-key` / `list-api-keys` / `revoke-api-key` | Admin only. Gated by `is_admin` per call. |

`report-task` is intentionally **not** an MCP tool — agents report via WebSocket (or the REST `/report` fallback), not MCP.

**Auth model.** The API key is supplied once at session-start (in the `Authorization` header for HTTP, or `AGENT_HUB_API_KEY` for stdio) — never as a tool argument (would leak into transcripts). The key's permissions are re-validated on every tool call, so revocation or expiration mid-session take effect immediately.

**Legacy CLI.** The old `python mcp/server.py …` CLI still works as a thin wrapper over the MCP tools — kept for backwards compatibility with existing scripts. New integrations should use MCP directly.

---

## 2. Building an agent

The Python SDK at `agent_sdk/` does the WebSocket plumbing for you. Two flavors: synchronous (`AgentHub`, runs forever) and async (`AsyncAgentHub`, integrates with your event loop).

### 2.1 The minimum viable agent

```python
from agent_sdk import AgentHub

def handle_task(task: dict) -> dict:
    # task has: task_id, task, task_type, payload, timeout, priority, parent_task_id
    payload = task.get("payload", {})
    return {"result": f"processed {payload.get('file_id')}"}

agent = AgentHub(
    hub_url="http://localhost:8080",
    agent_id="my-worker-1",       # must be unique within the hub
    capabilities=["upload-process", "thumbnail"],
    auth_token="<your-agent-key>",  # needs can_register
    task_handler=handle_task,
)
agent.start()  # blocks; auto-reconnects with exponential backoff
```

### 2.2 What the SDK does for you

- Opens the WebSocket and sends the `register` frame with your auth token.
- Receives `task` messages, calls your handler, sends `result` back.
- Sends `status: running` automatically when a task starts.
- Wraps handler exceptions and reports `failed` with the error message.
- Replies to hub `ping` with `pong`.
- Reconnects on disconnect with exponential backoff (sync client, when `reconnect=True`).

### 2.3 Send incremental logs during long tasks

```python
def handle_task(task):
    task_id = task["task_id"]
    for step in steps:
        agent.send_log(task_id, f"finished step {step}")
        do_work(step)
    return {"ok": True}
```

The hub appends each log to the task's `logs` array, visible via `GET /tasks/{id}`.

### 2.4 Async variant

For agents that already use asyncio:

```python
from agent_sdk import AsyncAgentHub

async def handle_task(task):
    return await some_async_work(task["payload"])

sdk = AsyncAgentHub(
    hub_url="http://localhost:8080",
    agent_id="async-worker",
    capabilities=["heavy-compute"],
    auth_token="<key>",
    task_handler=handle_task,
)
await sdk.run()  # blocks the current task
```

### 2.5 Capabilities and routing

Your agent declares `capabilities` — a flat list of task type strings. The hub matches submitted `task_type` against this list. Pick names that are stable and meaningful (`thumbnail-generate`, not `tg`). Avoid empty capabilities — the hub treats those as "match anything" which is rarely what you want.

### 2.6 What the hub does on the wire

```
agent → hub: {"type":"register","agent_id":"...","capabilities":[...],"auth_token":"..."}
hub → agent: {"type":"registered","agent_id":"..."}

(later, when work arrives:)
hub → agent: {"type":"task","task":{"task_id":"...","task":"...","payload":{...}, ...}}
agent → hub: {"type":"status","task_id":"...","status":"running"}
agent → hub: {"type":"log","task_id":"...","log":"step 1 done"}
agent → hub: {"type":"result","task_id":"...","status":"completed","result":{...}}
```

The hub verifies that an agent reporting a result is the agent the task was assigned to. Trying to report someone else's task → `403`.

### 2.7 Reconnect behavior

If your agent reconnects with the same `agent_id` while a previous WebSocket is still tracked, the hub closes the old socket with code `4000` ("agent reconnected") and accepts the new one. Any in-flight task on the dropped socket is re-queued and will be dispatched again.

### 2.7.4 Versioned capabilities and payload schemas (Phase 2.2)

Capabilities can be plain strings (treated as `version=1`, no schema) or full objects:

```python
agent = AgentHub(
    hub_url="http://localhost:8080",
    agent_id="coder-v2",
    capabilities=[
        {"name": "code", "version": 2,
         "payload_schema": {"type": "object", "required": ["src"]},
         "result_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ],
    auth_token="<key>",
    task_handler=...,
)
```

Submitters can target a minimum version:

```bash
curl -X POST http://localhost:8080/tasks \
  -H "Authorization: Bearer $YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task":"build it","task_type":"code","min_version":2,"payload":{"src":"x.py"}}'
```

The hub validates `payload` against the chosen agent's `payload_schema`:
- Default `AGENT_HUB_VALIDATE_PAYLOAD=warn` — logs validation failures to `activity_log`, dispatches anyway.
- `strict` — rejects on submit (HTTP 400) or marks dispatch failed with `payload_schema_violation`.

Discover what's available:

```bash
curl http://localhost:8080/capabilities -H "Authorization: Bearer $YOUR_KEY"
# [{"name":"code","version":2,"payload_schema":{...},"agent_count":3,"aggregated_schema_conflict":false}, ...]
```

The supported JSON-Schema subset is intentionally small: `type`, `required`, `properties`, `items`, `enum`, `additionalProperties`. Any other keyword (`oneOf`, `pattern`, `format`, ...) returns `unsupported_keyword: <name>` deterministically.

### 2.7.5 Delegating to other agents (Phase 2)

Inside a task handler, you can spawn a child task on another agent (or yourself) and await its result:

```python
def my_handler(task):
    plan = ...  # decide what to do
    # Need can_assign_tasks on this key.
    code_result = agent.submit_child(target_agent="coder", task_type="code",
                                     payload={"file": "out.py"})
    review_result = agent.submit_child(target_agent="reviewer", task_type="review",
                                       payload={"path": "out.py"})
    return {"plan": plan, "code": code_result, "review": review_result}
```

Rules:
- `target_agent` is required. Capability-routed (no target) delegation is not in scope for Phase 2.
- The hub rejects cycles: A→A is allowed (immediate self-recursion); A→B→A is not.
- Depth cap defaults to 8 (`AGENT_HUB_MAX_TASK_DEPTH` env var to override).
- Your key needs `can_assign_tasks` permission; permissions are re-checked per call so revocation takes effect mid-session.
- Exceptions: `ChildRejectedError` (cycle, depth, target_unavailable, not_owner, parent_terminal, forbidden, bad_request), `ChildWaitTimeout`, `ChildNotInTaskContext` (called outside a handler).
- Both `AgentHub` (sync) and `AsyncAgentHub` (async) expose `submit_child(...)`. The async variant integrates with `asyncio`, and both keep the WS reader independent of handler execution so a parent waiting on a child does not deadlock.

### 2.8 Try it end-to-end

```bash
# Terminal 1 — hub already running per §0

# Terminal 2 — start the example echo agent
.venv/bin/python examples/echo-agent/agent.py \
  --hub http://localhost:8080 --key <agent-key>

# Terminal 3 — submit a task
curl -X POST http://localhost:8080/tasks \
  -H "Authorization: Bearer <submitter-key>" \
  -H "Content-Type: application/json" \
  -d '{"task":"echo hi","task_type":"echo"}'

# Terminal 3 — fetch the result
curl http://localhost:8080/tasks/<task_id> -H "Authorization: Bearer <submitter-key>"
# → {"status":"completed","result":{"echoed":"hi","agent":"echo-agent",...}}
```

---

## 3. Admin operations

### 3.1 Make a scoped key for a single agent

A key that can register only one specific agent ID and submit tasks only to that same agent:

```bash
curl -X POST http://localhost:8080/admin/keys \
  -H "Authorization: Bearer $AGENT_HUB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name":"worker-1-key",
    "can_register":true,
    "can_assign_tasks":true,
    "allowed_agents":["worker-1"]
  }'
```

`allowed_agents=[]` (default) means "any agent". Set it to lock down a key.

### 3.2 Make a read-only key (for dashboards or monitoring)

```bash
curl -X POST http://localhost:8080/admin/keys \
  -H "Authorization: Bearer $AGENT_HUB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"dashboard","can_view_agents":true,"can_view_tasks":true}'
```

### 3.3 Make an expiring key (for a time-boxed integration)

```bash
curl -X POST http://localhost:8080/admin/keys \
  -H "Authorization: Bearer $AGENT_HUB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"ci-runner","can_assign_tasks":true,"expires_days":30}'
```

### 3.4 List and revoke keys

```bash
# List (no raw values, no hashes)
curl http://localhost:8080/admin/keys -H "Authorization: Bearer $AGENT_HUB_ADMIN_KEY"

# Revoke
curl -X DELETE http://localhost:8080/admin/keys/worker-1-key \
  -H "Authorization: Bearer $AGENT_HUB_ADMIN_KEY"
```

A revoked key is immediately rejected on every endpoint. Restart-safe.

### 3.5 Aggregate stats

```bash
curl http://localhost:8080/stats -H "Authorization: Bearer $AGENT_HUB_ADMIN_KEY"
# {"agents":{"online":3,"total":5},"tasks":{"queued":1,"assigned":2,"running":0,"completed":42,"failed":1,"timeout":0}}
```

### 3.6 Restart the hub

The hub recovers state from SQLite on startup:
- All agents marked `offline` (they need to reconnect).
- Tasks in `assigned` / `running` re-queued (their previous agent is gone).
- Tasks in `queued` stay queued.
- API keys remain valid.

You can kill `uvicorn` and restart it without losing pending work.

### 3.7 Configure timeout policy

Default: when a task exceeds its `timeout` seconds, it's marked `timeout` (terminal). To re-queue timed-out tasks instead, run the hub with:

```bash
AGENT_HUB_TIMEOUT_POLICY=requeue .venv/bin/python -m uvicorn hub.main:app --port 8080
```

Re-queue is dangerous if your agents aren't idempotent. Prefer `terminal` (the default) and let humans decide what to do with `timeout` tasks.

### 3.8 Monitoring

- `GET /health` — public, returns a quick OK with counts. Suitable for liveness checks.
- `GET /stats` — gated by `can_view_tasks`. Suitable for dashboards.
- `GET /events` — SSE stream of every state change. Suitable for live monitoring UIs.

---

## 4. Patterns

### 4.1 Direct routing vs. capability matching

- **Direct routing** (`target_agent: "worker-1"`) — when you know exactly who should do the work. Fails fast if the agent is offline.
- **Capability matching** (omit `target_agent`, set `task_type`) — when any matching agent will do. Hub picks the first idle agent in registry order.

### 4.2 Priority queues

Use `priority` (integer, higher dispatches first) to push urgent work ahead of background work. Within a priority, FIFO by submission time.

### 4.3 Long-running tasks

Set `timeout` generously, and use `agent.send_log()` to stream progress so a watcher knows the task is alive.

### 4.4 Idempotent agents

Disconnect mid-task → re-queue. So your handler may see the same task twice. Keep handlers idempotent or write to the result payload in a way that downstream consumers can dedupe.

### 4.5 Multiple agents with the same capability

Register N agents with overlapping capabilities for parallelism. The hub assigns one task per agent at a time (an idle agent that just got a task is marked busy until it reports `completed` / `failed`).

---

## 5. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `403 Registration not permitted` | Key lacks `can_register`. | Create or use a key with `can_register: true`. |
| `403 Not permitted to assign tasks` | Key lacks `can_assign_tasks`, or `allowed_agents` excludes the target. | Use the right key, or remove the `target_agent` field. |
| `403 Reporter is not assigned to this task` | Your agent is reporting a task it doesn't own. | Check that the WS connection's `agent_id` matches `task.assigned_agent_id`. |
| `409 Task is already terminal` | You're trying to update a `completed` / `failed` / `timeout` task. | Don't. The hub refuses by design (FR-EXE-6). |
| WS connection closes immediately | First frame wasn't `register`, or auth_token invalid. | Send `{"type":"register",...}` as the first frame with a valid `auth_token`. |
| Task stays `queued` forever | No online agent has the matching `task_type` in capabilities. | Register an appropriate agent, or correct the `task_type`. |
| Task delivered to wrong agent | You used capability matching with overlapping agents. | Use `target_agent` for direct routing. |
| Agent dropped after restart | Expected — agents must reconnect. | Run with auto-reconnect (`reconnect=True`, the SDK default). |
| `database is locked` | Concurrent writes saturating SQLite. | Already on WAL mode; if persistent, reduce write rate or move to PostgreSQL. |

---

## 6. Security notes

- API keys are SHA-256 hashed before storage. The raw key is shown exactly once on creation.
- The bootstrap admin key in `AGENT_HUB_ADMIN_KEY` is the most powerful credential. Treat it like a root password. Rotate by creating a new admin key and revoking the old one.
- All mutating endpoints require authentication. `GET /health` is the only public endpoint.
- CORS defaults to localhost origins. Override with `AGENT_HUB_CORS_ORIGINS=https://your.dashboard,https://other.app` (comma-separated).
- No rate limiting in Phase 1 — put a reverse proxy in front if you expose the hub publicly.

---

## 7. What's not in Phase 1 / 1.5

Phase 1 (task dispatch + persistence) and Phase 1.5 (real MCP server + skill) are shipped. Still out of scope:

- Agent-to-agent task delegation.
- Multi-hub federation.
- DAG / task dependency graphs.
- Web UI dashboard.
- Prometheus metrics.
- Windows support is not exercised in CI (Linux + macOS only).

The current MCP `/mcp` endpoint is a simplified Streamable HTTP — `POST` request/response, no SSE event channel, no `Mcp-Session-Id` negotiation. Sufficient for the tested clients; flag a Phase 1.6 if a future MCP client needs full Streamable HTTP semantics.

See [`requirements.md`](./requirements.md) §4.3 for the full Phase 2 deferred list.

---

## 8. Where to next

- Wire-level reference: [`requirements.md`](./requirements.md)
- Test coverage and known limitations: [`test_report.md`](./test_report.md)
- Multi-agent collaboration log (PM, designer, tester hand-offs): `Agent-comm/`
- Project layout overview: [`README.md`](./README.md)
