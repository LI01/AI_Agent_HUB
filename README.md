# Agent Hub

A central task dispatch and coordination server for heterogeneous AI agents running across Docker containers, developer machines, and cloud hosts. Humans submit tasks via REST or MCP; the hub routes work to available agents over WebSocket; agents report status, logs, and structured results back.

**Status:** Phase 1 MVP + Phase 1.5 MCP + Phase 2 (F1 delegation, F2 parent_task_id) + Phase 2.2 (F3 versioned capabilities, F4 discovery) — **127/127 tests passing**, ship-ready. See [`test_report.md`](./test_report.md).

## What it does

- **Register** agents with capabilities, identity, and machine info.
- **Submit** tasks via REST with optional target agent, priority, and timeout.
- **Route** to an explicitly targeted agent or auto-route by capability.
- **Deliver** tasks over persistent WebSocket; HTTP fallback supported.
- **Persist** agents, tasks, logs, results, and API keys in SQLite.
- **Recover** queued and assigned work after hub restart.
- **Re-queue** incomplete work when an agent disconnects.
- **Authenticate** via API keys with granular permissions.
- **Delegate** child tasks from one agent to another (or itself) via the SDK's `submit_child(...)` — Phase 2.
- **Query** parent↔child relationships via `GET /tasks?parent_task_id=...` and `?top_level=true`; SSE events carry `parent_task_id` — Phase 2.
- **Version capabilities** with payload schemas: agents advertise `{name, version, payload_schema, result_schema}`; tasks request `min_version`; hub validates payloads (warn or strict via `AGENT_HUB_VALIDATE_PAYLOAD`) — Phase 2.2.
- **Discover** what's available: `GET /capabilities` returns aggregated `(name, version)` pairs across online agents — Phase 2.2.

Out of scope for Phase 1: agent-to-agent delegation, multi-hub federation, DAG workflows, web UI, Prometheus metrics. See [`requirements.md`](./requirements.md) §4.2.

## Architecture

```
                    ┌─────────────────┐
                    │ OpenCode/Claude │
                    │     Code        │
                    │   (Human UI)    │
                    └────────┬────────┘
                             │ HTTP / MCP
                    ┌────────▼────────┐
                    │      HUB        │
                    │   (port 8080)   │
                    │  ┌───────────┐  │
                    │  │  SQLite   │  │
                    │  └───────────┘  │
                    └────────┬────────┘
                             │ WebSocket (persistent)
              ┌──────────────┼──────────────┐
              │              │              │
         ┌────▼────┐    ┌────▼────┐    ┌────▼────┐
         │ Agent 1 │    │ Agent 2 │    │ Agent 3 │
         └─────────┘    └─────────┘    └─────────┘
```

## Quick start

```bash
# 1. Set up venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Pick a bootstrap admin key (or omit and the hub prints a generated one once)
export AGENT_HUB_ADMIN_KEY=your-admin-key-here

# 3. Run the hub
.venv/bin/python -m uvicorn hub.main:app --port 8080

# 4. Create an agent key (in another terminal)
curl -X POST http://localhost:8080/admin/keys \
  -H "Authorization: Bearer $AGENT_HUB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"echo-agent","can_register":true,"can_assign_tasks":true}'

# 5. Run the example echo agent (use the key from step 4)
.venv/bin/python examples/echo-agent/agent.py \
  --hub http://localhost:8080 --key <agent-key>

# 6. Submit a task
curl -X POST http://localhost:8080/tasks \
  -H "Authorization: Bearer <agent-key>" \
  -H "Content-Type: application/json" \
  -d '{"task":"echo hello world"}'
```

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `AGENT_HUB_ADMIN_KEY` | (generates one) | Bootstrap admin key. If unset, the hub generates one on first start and prints it to stdout — store it. |
| `AGENT_HUB_DB_PATH` | `./agent_hub.db` | SQLite database path. |
| `AGENT_HUB_TIMEOUT_POLICY` | `terminal` | `terminal` marks expired tasks `timeout`; `requeue` puts them back in the queue. |
| `AGENT_HUB_TIMEOUT_SCAN_INTERVAL` | `5` | Seconds between timeout-watcher scans. |
| `AGENT_HUB_CORS_ORIGINS` | `localhost,127.0.0.1` | Comma-separated allowed CORS origins. |

## REST API

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/register` | `can_register` | Register an agent. |
| `POST` | `/heartbeat` | `can_register` | Update agent status. |
| `POST` | `/unregister?agent_id=…` | `can_register` | Remove an agent. |
| `POST` | `/report` | `can_register` + ownership | Report task result. Body must include `agent_id` matching the task's assigned agent. |
| `WS`   | `/ws` | `auth_token` in first frame | Persistent agent connection. |
| `POST` | `/tasks` | `can_assign_tasks` | Submit a task. |
| `GET`  | `/tasks/{id}` | `can_view_tasks` | Get task status, result, logs. |
| `GET`  | `/tasks?status=&agent_id=` | `can_view_tasks` | List tasks with optional filters. |
| `GET`  | `/agents` | `can_view_agents` | List agents. |
| `GET`  | `/agents/{id}` | `can_view_agents` | Get agent details. |
| `GET`  | `/health` | none | Health check. |
| `GET`  | `/stats` | `can_view_tasks` | Aggregate statistics. |
| `GET`  | `/events` | `can_view_tasks` | SSE event stream. |
| `POST` | `/admin/keys` | `is_admin` | Create API key (raw key returned once). |
| `GET`  | `/admin/keys` | `is_admin` | List keys (no raw values). |
| `DELETE` | `/admin/keys/{name}` | `is_admin` | Revoke a key. |

Full API contract in [`requirements.md`](./requirements.md) §12.

## WebSocket protocol

First frame from agent to `/ws`:

```json
{"type":"register","agent_id":"echo-1","capabilities":["echo"],"auth_token":"raw-key","machine_info":{"hostname":"h","ip":"1.2.3.4","platform":"docker"}}
```

Hub responds with `{"type":"registered","agent_id":"echo-1"}`. Subsequent agent → hub messages: `heartbeat`, `status`, `log`, `result`, `pong`. Hub → agent: `task`, `ping`, `error`. Same `agent_id` reconnect closes the old socket with code `4000`. Full protocol in [`requirements.md`](./requirements.md) §13.

## Permissions

API keys carry granular permissions:

- `can_register` — register agents, send heartbeats, report results
- `can_assign_tasks` — submit tasks
- `can_view_agents` — list/get agents
- `can_view_tasks` — list/get tasks, stats, SSE events
- `allowed_agents` — restrict which agents this key may target (empty = any)
- `is_admin` — full access including key management

Keys are SHA-256 hashed before storage. Raw keys are returned only on creation.

## Project layout

```
agent-hub/
├── hub/                # FastAPI + WebSocket server
│   ├── main.py        # endpoints, WS, dispatch, timeout watcher
│   ├── models.py      # Pydantic models, AgentStatus, TaskStatus
│   ├── auth.py        # API key hashing, validation, bootstrap
│   ├── database.py    # SQLite persistence
│   ├── registry.py    # in-memory agent registry (RLock)
│   ├── queue.py       # in-memory task queue (RLock)
│   └── router.py      # capability matching, NL task-type detection
├── agent_sdk/         # Python SDK for agents (sync + async)
├── hub/mcp_protocol.py # Phase 1.5 MCP tool registry, JSON-RPC, transports
├── hub/mcp.py         # stdio MCP entrypoint: `python -m hub.mcp`
├── mcp/               # legacy CLI wrapper over the MCP tools (compatibility only)
├── clients/           # Per-agent clients: codex, hermes, openclaw, etc.
├── examples/
│   └── echo-agent/    # Minimal echo agent for end-to-end testing
├── skills/            # OpenCode skill bindings
├── tests/             # pytest suite (77 tests, all green)
├── requirements.md    # Full Phase 1 requirements (FR-* / NFR-*)
├── test_plan.md       # Test plan keyed to requirement IDs
├── test_report.md     # Latest test results
└── requirements.txt   # Python dependencies
```

## Testing

```bash
.venv/bin/pip install pytest pytest-asyncio websockets httpx
.venv/bin/python -m pytest tests/ -v
```

Current state: **77 passed in ~10s** across unit, REST integration, WebSocket, SDK, persistence, concurrency, and security tests. See [`test_report.md`](./test_report.md) for the coverage matrix.

## Database schema

Phase 1 ships these tables (full schema in [`requirements.md`](./requirements.md) §11):

- `agents` — id, name, skills (JSON), position (JSON), status, timestamps
- `tasks` — id, status, task_type, payload (JSON), result (JSON), logs (JSON), assigned_agent_id, priority, timeout, parent_task_id, timestamps
- `api_keys` — key_hash, name, permission columns, active, expiry
- `activity_log` — append-only audit trail
- Plus `agent_groups`, `agent_group_members`, `task_templates` (for future use)

WAL mode is enabled. Indexes cover `tasks.status`, `tasks.assigned_agent_id`, `tasks.created_at`, `agents.status`, and the activity log.

## Known limitations

- `/unregister` takes `agent_id` as a query parameter (REST polish).
- Pydantic v1 `class Config` and FastAPI `@app.on_event` deprecation warnings.
- Windows is not exercised in CI.
- No rate limiting (P2 in requirements).
- The MCP `/mcp` endpoint is a simplified Streamable HTTP — `POST` request/response, no SSE event channel, no `Mcp-Session-Id` negotiation. Sufficient for tested clients; full Streamable HTTP semantics would be Phase 1.6 if a future MCP client requires them.

## Documents

- [`requirements.md`](./requirements.md) — full Phase 1 requirements
- [`test_plan.md`](./test_plan.md) — test cases keyed to FR-* / NFR-* IDs
- [`test_report.md`](./test_report.md) — latest test results
- `Agent-comm/` — multi-agent collaboration log (PM, designer, tester hand-offs)
