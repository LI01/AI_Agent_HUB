# Agent Hub

A central task dispatch and coordination server for heterogeneous AI agents running across Docker containers, developer machines, and cloud hosts. Humans submit tasks via REST or MCP; the hub routes work to available agents over WebSocket; agents report status, logs, and structured results back.

**Status:** Phase 1 MVP + Phase 1.5 MCP + Phase 2 (F1 delegation, F2 parent_task_id) + Phase 2.2 (F3 versioned capabilities, F4 discovery) + Phase 2.3 (LLM-CLI worker skills + adapters) + Phase 2.4 (file sharing via payload.files) + Phase 2.5 (MCP spec compliance — Claude Code / Codex / OpenCode all green) — ship-ready. See [`test_report.md`](./test_report.md).

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
- **Spawn agents** as wrappers around codex / claude / opencode CLIs via the `/agent-spawn`, `/agent-tasks`, `/agent-close` skills. One CLI per agent, per role; multiple per machine — Phase 2.3.
- **Share files with agents** via `payload.files = [{path, content}]` (materialized to workdir before CLI runs) and `payload.expect_files_back = [paths]` (read back into `result.files`). 1 MiB per file, 5 MiB per task; path-traversal-safe — Phase 2.4.
- **MCP spec compliance** — `tools/list` returns spec-compliant `outputSchema` (top-level `object` for the 4 list-returning tools; omitted entirely on tools without a structural commitment). Unauthenticated/invalid `POST /mcp` returns `401` + `WWW-Authenticate: Bearer realm="agent-hub"` instead of `403`, so spec-compliant clients use bearer auth instead of probing OAuth. Verified end-to-end with Claude Code, Codex, and OpenCode — Phase 2.5.
- **Boot-time install for adapter agents** — `scripts/install_agent_services.sh` drops three `systemd --user` units (claude / codex / opencode) on a Linux host, wires them to register with the hub, enables linger so they survive reboot. See [Run agents as boot services](#run-agents-as-boot-services).

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

## Run agents as boot services

On a Linux host where you want claude / codex / opencode adapters to come up automatically at boot and restart on failure, use `scripts/install_agent_services.sh`. It installs three `systemd --user` units, enables linger, and starts them.

```bash
# On the agent host, as the user that should own the services:
git clone https://github.com/LI01/AI_Agent_HUB.git ~/work/AI_Agent_HUB
cd ~/work/AI_Agent_HUB
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Mint a registration key from the hub (admin only)
curl -X POST http://<hub-host>:8300/admin/keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"<host>-agents","can_register":true,"can_view_agents":true,"can_view_tasks":true}'

# Install and start the three services (key from env — never on argv)
AGENT_HUB_API_KEY=<key-from-above> \
AGENT_HUB_URL=http://<hub-host>:8300 \
  bash scripts/install_agent_services.sh
```

Defaults assume the repo is at `$HOME/work/AI_Agent_HUB` and the venv at `$AGENT_HUB_REPO/.venv`. The script auto-detects `claude`, `codex`, and `opencode` CLIs from `$HOME/.local/bin` and `$HOME/.opencode/bin`. Roles default to `reviewer` / `designer` / `tester`; agent IDs default to `<cli>-<role>-$(hostname -s)`. Every default is overridable via env (`AGENT_HUB_REPO`, `AGENT_HUB_VENV`, `AGENT_HUB_WORKDIRS`, `AGENT_ID_SUFFIX`, `ROLE_CLAUDE`, `ROLE_CODEX`, `ROLE_OPENCODE`, `EXTRA_PATH`). The API key is read from `$AGENT_HUB_API_KEY` and written to `~/.config/systemd/user/agent-hub.env` (mode 0600); it never appears in argv.

Day-2 ops:

```bash
systemctl --user status claude-agent codex-agent opencode-agent
journalctl --user -u claude-agent -f
systemctl --user restart codex-agent
# Rotate the key: edit ~/.config/systemd/user/agent-hub.env, then restart the three services.
```

### Adapter env vars

The shared `~/.config/systemd/user/agent-hub.env` accepts these:

| Env var | Default | Purpose |
|---------|---------|---------|
| `AGENT_HUB_API_KEY` | (required) | Registration key. Must have `can_register`. |
| `AGENT_HUB_URL` | `http://10.9.0.10:8300` | Hub URL. |
| `AGENT_HUB_CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS` | unset | If `1`, claude adapter passes `--dangerously-skip-permissions` to `claude -p`, letting it use Bash/Edit/WebSearch/WebFetch without interactive confirmation. **Without this, the claude adapter has effectively no tools** in headless mode (codex/opencode are unaffected). Recommended only for trusted hub deployments. |

### Run on macOS at login (launchd)

For a Mac that hosts the hub itself (or adapters), use a `~/Library/LaunchAgents/<label>.plist` with `RunAtLoad=true` + `KeepAlive=true`. Minimal hub plist:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.openclaw.agent-hub</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>WorkingDirectory</key><string>/Users/you/agent-hub</string>
  <key>ProgramArguments</key><array>
    <string>/Users/you/agent-hub/venv/bin/python</string>
    <string>-m</string><string>uvicorn</string>
    <string>hub.main:app</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8300</string>
  </array>
  <key>StandardOutPath</key><string>/Users/you/.openclaw/logs/agent-hub.log</string>
  <key>StandardErrorPath</key><string>/Users/you/.openclaw/logs/agent-hub.err.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>/Users/you</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
</dict></plist>
```

Load / verify / unload:

```bash
launchctl bootstrap   gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.agent-hub.plist
launchctl print       gui/$(id -u)/ai.openclaw.agent-hub | grep -E 'state|pid|exit'
launchctl kickstart -k gui/$(id -u)/ai.openclaw.agent-hub   # restart
launchctl bootout     gui/$(id -u)/ai.openclaw.agent-hub
```

Two macOS-specific gotchas:

- **TCC blocks `/Volumes`.** Launchd-spawned processes can't read external volumes (`/Volumes/<anything>`) by default — Python startup hangs in `open()` and `ls` returns `Operation not permitted`. Keep the project, venv, and DB on the boot volume (e.g. under `/Users/<you>/`). To override, grant the binary Full Disk Access in System Settings → Privacy & Security.
- **LaunchAgent vs LaunchDaemon.** `~/Library/LaunchAgents/` runs only when the user is logged in. For headless boot (no login), put the plist under `/Library/LaunchDaemons/` instead and adjust paths.

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
