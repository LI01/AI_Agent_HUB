# Agent Hub Requirements

## 1. Purpose

Agent Hub is a central task dispatch and coordination server for heterogeneous AI agents running across Docker containers, developer machines, and cloud hosts. It replaces manual script execution with a structured system where humans submit tasks through OpenCode/Claude Code, REST, or MCP; the hub routes work to available agents; and agents report status, logs, and structured results back.

## 2. Problem

Teams may have multiple agents for testing, development, project management, automation, or device control, but current workflows are manual and hard to observe:

- Humans run scripts directly on individual machines.
- Agent availability is unclear.
- Task status is scattered across terminals or local files.
- Results are not centrally tracked.
- Routing work to the right agent is ad hoc.
- Chaining and delegation are future needs, but not part of the MVP.

## 3. Vision

Agent Hub provides one operational control plane for humans and agents:

- Humans submit work and inspect status from OpenCode/Claude Code, MCP tools, or REST endpoints.
- Agents register their capabilities, receive tasks, execute work, and report results.
- The hub tracks agent state, task lifecycle, access control, and persisted history.
- The MVP proves reliable task dispatch and status visibility before adding collaboration or delegation patterns.

## 4. Scope

### 4.1 MVP: Phase 1

Phase 1 is task dispatch plus status:

- Register agents with capabilities.
- Submit tasks through REST and MCP-facing tooling.
- Route tasks to an explicitly targeted agent or a matching available agent.
- Deliver tasks to agents over WebSocket.
- Persist agents, tasks, logs, results, and API keys in SQLite.
- Query agent and task status.
- Recover queued and assigned work after hub restart.
- Re-queue incomplete work when an agent disconnects.
- Provide a single Python SDK for agent integration.

### 4.2 Phase 1.5: MCP-First Integration

Phase 1.5 lands after the Phase 1 MVP is stable and exists to make Claude Code, OpenCode, and any other MCP-aware client first-class clients of the hub:

- **Real MCP protocol server** (replaces the current `mcp/server.py` CLI/REST bridge). Implements the Model Context Protocol so MCP-aware clients discover hub operations as tools without bespoke client wrappers.
- **Claude Code / OpenCode skill** that wraps registration + the most common submit/query flows behind a single slash command, layered on top of the MCP server. Optional convenience layer; not the primary integration path.
- **Retire** `clients/claude-code/` and `clients/opencode/` once the MCP server covers their use cases.

### 4.3 Deferred: Phase 2

The following are explicitly out of scope for Phase 1 and Phase 1.5:

- Agent-to-agent delegation.
- Multi-hub federation.
- Task dependency graphs or DAG workflows.
- Agent capability negotiation or versioned protocols.
- Web UI dashboard.
- Prometheus/Grafana metrics integration.
- Streaming partial task results beyond incremental logs.
- Agent groups with group-level routing.
- Large-scale deployment beyond the target below.

## 5. Constraints

- **Hub implementation:** Python with FastAPI.
- **Database:** SQLite for MVP, with schema choices that do not block later PostgreSQL migration.
- **Protocol:** WebSocket is primary for agents. HTTP polling is a fallback, not the preferred path.
- **Human interface:** REST endpoints and MCP integration for OpenCode/Claude Code.
- **SDK:** One Python SDK package, `agent_sdk`.
- **Auth:** API key based. OAuth/OIDC is out of scope for MVP.
- **Scale target:** 10-50 concurrent agents and hundreds of tasks per day. Not designed for thousands of agents.
- **Deployment:** Docker containers or bare metal on Linux, macOS, and Windows.

## 6. Priorities

| Priority | Meaning |
|----------|---------|
| P0 | Required for Phase 1 MVP correctness and reliability. |
| P1 | Important after MVP is stable, or required for a production pilot. |
| P2 | Nice-to-have or later hardening. |

## 7. Actors

| Actor | Description |
|-------|-------------|
| Human | Submits tasks, monitors status, and reads results through OpenCode/Claude Code, MCP tools, or REST API. |
| Agent | Registers capabilities, receives tasks, executes work, sends logs, and reports results. |
| Admin | Manages API keys, permissions, and hub configuration. |
| Hub | Routes tasks, tracks state, enforces access control, and persists data. |

## 8. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    OpenCode / Claude Code                   │
│              Human interface and task sender                │
└─────────────────────────┬───────────────────────────────────┘
                          │ REST / MCP / optional SSE
┌─────────────────────────▼───────────────────────────────────┐
│                         Hub Server                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │  Registry   │  │   Router    │  │     Task Queue      │ │
│  │ agent state │  │ task choice │  │ pending / assigned  │ │
│  └─────────────┘  └─────────────┘  └─────────────────────┘ │
│                         SQLite                             │
└─────────────────────────┬───────────────────────────────────┘
                    Agent WebSocket connections
           ┌─────────────┼─────────────┐
           │             │             │
      ┌────▼────┐   ┌────▼────┐   ┌────▼────┐
      │ Agent 1 │   │ Agent 2 │   │ Agent 3 │
      │  test   │   │   PM    │   │   dev   │
      └─────────┘   └─────────┘   └─────────┘
```

## 9. Functional Requirements

### 9.1 Agent Registration

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-REG-1 | Agents MUST register with a unique `agent_id` and a list of capabilities. | P0 |
| FR-REG-2 | Registration MUST require a valid API key with `can_register` permission. | P0 |
| FR-REG-3 | Registration MUST accept optional metadata: `name`, `description`, `owner`, and `machine_info` with hostname, IP, and platform. | P1 |
| FR-REG-4 | The hub MUST persist registered agents to SQLite. | P0 |
| FR-REG-5 | The hub MUST support agent unregistration. | P1 |
| FR-REG-6 | On registration, the hub MUST check pending queued tasks matching the agent's capabilities and dispatch them. | P0 |

### 9.2 Agent Connection

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-CON-1 | Agents MUST connect via persistent WebSocket at `/ws` for real-time task delivery. | P0 |
| FR-CON-2 | The hub SHOULD support HTTP polling as a fallback for agents that cannot maintain WebSocket connections. | P1 |
| FR-CON-3 | Agents MUST send periodic heartbeats with current status: `idle` or `busy`. | P0 |
| FR-CON-4 | The hub MUST mark agents `offline` when their WebSocket disconnects or heartbeats stop. | P0 |
| FR-CON-5 | Agents SHOULD automatically reconnect on connection loss with exponential backoff. | P1 |
| FR-CON-6 | On agent disconnect, the hub MUST re-queue tasks assigned to that agent that have not completed. | P0 |

### 9.3 Task Submission

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-TSK-1 | Humans MUST be able to submit tasks via `POST /tasks` with a natural language description, structured payload, or both. | P0 |
| FR-TSK-2 | Task submission MUST require a valid API key with `can_assign_tasks` permission. | P0 |
| FR-TSK-3 | Each task MUST receive a unique UUID on creation. | P0 |
| FR-TSK-4 | Tasks MUST be persisted to SQLite on creation. | P0 |
| FR-TSK-5 | Submitters MAY specify a `target_agent` for direct routing. | P0 |
| FR-TSK-6 | Submitters MAY specify a `timeout` in seconds, defaulting to 300. | P1 |
| FR-TSK-7 | Submitters MAY specify a `priority`; higher values dispatch first. | P2 |
| FR-TSK-8 | Submitters MAY specify a `parent_task_id` for future task chaining. | P2 |

### 9.4 Task Routing

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-RTE-1 | If `target_agent` is specified, the hub MUST route to that agent only if it is registered and online. | P0 |
| FR-RTE-2 | If `target_agent` is not specified, the hub MUST auto-route to an available agent whose capabilities match the task type. | P0 |
| FR-RTE-3 | If no matching agent is available, the task MUST remain `queued` and dispatch when a matching agent becomes available. | P0 |
| FR-RTE-4 | The hub MUST detect task type from natural language input when `task_type` is not explicitly provided. | P1 |
| FR-RTE-5 | When an agent registers or transitions to `idle`, the hub MUST scan pending queued tasks and dispatch matches. | P0 |

### 9.5 Task Execution and Reporting

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-EXE-1 | The hub MUST deliver tasks to agents over the active WebSocket connection when available. | P0 |
| FR-EXE-2 | Agents MUST report task results with status `completed` or `failed` and structured result data. | P0 |
| FR-EXE-3 | Agents MAY send incremental log messages during task execution. | P1 |
| FR-EXE-4 | The hub MUST update task status, logs, and results in both live state and SQLite when results are received. | P0 |
| FR-EXE-5 | The hub SHOULD enforce task timeouts. Timed-out tasks MUST be marked `timeout` and either re-queued or failed according to configuration. | P1 |
| FR-EXE-6 | The hub MUST NOT re-execute completed tasks. | P0 |

### 9.6 Queries

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-QRY-1 | `GET /tasks/{task_id}` MUST return task status, result, and logs. | P0 |
| FR-QRY-2 | `GET /tasks` MUST return tasks with optional filters by `status` and `agent_id`. | P0 |
| FR-QRY-3 | `GET /agents` MUST return registered agents with status and capabilities. | P0 |
| FR-QRY-4 | `GET /agents/{agent_id}` MUST return details for a specific agent. | P0 |
| FR-QRY-5 | `GET /health` MUST return hub health including agent count, task count, and database stats. | P1 |
| FR-QRY-6 | `GET /stats` MUST return aggregate statistics for online agents and queued/running/completed tasks. | P1 |
| FR-QRY-7 | Query endpoints MUST require a valid API key with the appropriate view permission, except `/health` if configured as public. | P1 |

### 9.7 Real-Time Events

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-SSE-1 | `GET /events` MAY stream server-sent events to connected clients. | P2 |
| FR-SSE-2 | Events SHOULD include `agent_registered`, `agent_status`, `agent_unregistered`, `task_created`, `task_updated`, and `task_completed`. | P2 |
| FR-SSE-3 | Each SSE subscriber SHOULD receive events that occur while connected. | P2 |

### 9.8 Access Control

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-ACL-1 | The hub MUST use API key authentication via `Authorization: Bearer <key>` for HTTP and `auth_token` for WebSocket registration. | P0 |
| FR-ACL-2 | API keys MUST support granular permissions: `can_register`, `can_assign_tasks`, `can_view_agents`, `can_view_tasks`, and `is_admin`. | P0 |
| FR-ACL-3 | API keys MAY restrict which agents can be targeted via `allowed_agents`. | P1 |
| FR-ACL-4 | Admin keys MUST be able to create, list, and revoke API keys through `/admin/keys`. | P0 |
| FR-ACL-5 | The hub MUST provide a bootstrap mechanism for the first admin key. | P0 |
| FR-ACL-6 | API keys MUST be persisted to SQLite so they survive hub restarts. | P0 |
| FR-ACL-7 | Mutating endpoints MUST require authentication: `/register`, `/unregister`, `/heartbeat`, `/report`, and `/tasks`. | P0 |
| FR-ACL-8 | API keys SHOULD support optional expiration dates. | P2 |

### 9.9 MCP Integration (Phase 1.5)

The current `mcp/server.py` is a CLI/REST bridge, not a Model Context Protocol server. The Phase 1.5 deliverable is a real MCP server that any MCP-aware client (Claude Code, OpenCode, future MCP clients) can mount with no per-client wrapper code. Per `requirements.md` §4.2, the per-client wrappers under `clients/claude-code/` and `clients/opencode/` are retired once this lands.

**Non-goal.** Phase 1.5 does not add workflow orchestration, task dependencies, or new hub semantics; it only exposes existing REST/hub operations through MCP. Anything beyond that — agent-to-agent delegation, DAG workflows, session multiplexing beyond what the chosen MCP SDK provides — is Phase 2 (see §4.3).

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-MCP-1 | The hub MUST ship an MCP protocol server per the current Model Context Protocol spec. Transports: **JSON-RPC 2.0 over stdio** for local clients, and **MCP Streamable HTTP** for remote clients (legacy HTTP+SSE compatibility is P2 and only added if a target client requires it). The CLI/REST bridge in `mcp/server.py` is replaced, not augmented. | P0 (Phase 1.5) |
| FR-MCP-2 | MCP tools MUST include `list-agents`, `get-agent`, `register-agent`, `unregister-agent`, `submit-task`, `get-task`, `list-tasks`, `stats`, and `health`. Each MUST advertise a JSON-schema for inputs and outputs that the client can introspect. Schemas MUST mirror the REST request fields: e.g. `submit-task` accepts `task`, `task_type`, `payload`, `target_agent`, `priority`, `timeout`, `parent_task_id` (one tool, not separate `submit-task-with-target`). `list-tasks` accepts `status` and `agent_id` filters. `register-agent` accepts the same fields as `POST /register`. `unregister-agent` accepts `agent_id` in the JSON body even though the current REST endpoint uses a query parameter. `report-task` is intentionally excluded — agents use WS/SDK or REST `/report`, not MCP. | P0 (Phase 1.5) |
| FR-MCP-3 | MCP admin tools (`create-api-key`, `list-api-keys`, `revoke-api-key`) SHOULD be exposed and gated by `is_admin` at call time. | P1 (Phase 1.5) |
| FR-MCP-4 | The MCP server MUST authenticate the connecting client via an API key supplied at session-start, never as a tool parameter (passing keys per-call would leak secrets into transcripts and tool-call logs). For Streamable HTTP, use `Authorization: Bearer …` on the session handshake. For stdio, prefer `AGENT_HUB_API_KEY` env var or config-file injection over a custom handshake field, unless the chosen MCP SDK provides a standard auth hook. The session-bound key identity MUST be re-validated through the same `access_control` path used by REST on every tool call, so revocation, expiration, and permission changes during a long-lived session take effect immediately. Admin tools MUST require `is_admin` at call time. | P0 (Phase 1.5) |
| FR-MCP-5a | A standalone stdio MCP server (e.g. `python -m hub.mcp` or equivalent) MUST be provided for local desktop/client integration. | P0 (Phase 1.5) |
| FR-MCP-5b | An embedded Streamable HTTP MCP endpoint (mounted at a path like `/mcp` in the hub process) SHOULD be provided for remote or shared-hub access. Implement only if it shares the same tool registry as FR-MCP-5a — one tool implementation layer, two transports. Do not fork separate stdio and HTTP tool implementations. | P1 (Phase 1.5) |
| FR-MCP-6 | The existing CLI command path (`python mcp/server.py … agents`, `… submit …`) MAY be preserved as a thin wrapper over the MCP tools to keep the docs in `summary.md` and `USERS_GUIDE.md` working, but is no longer the primary integration surface. | P2 (Phase 1.5) |
| FR-MCP-7 | The design pass MUST pick between using an MCP Python SDK (preferred) and hand-rolled JSON-RPC, and document the choice. The implementation MUST come with a test plan covering at minimum: (a) `tools/list` exposes the exact FR-MCP-2/3 schemas; (b) `tools/call submit-task` creates a task that `get-task` then retrieves; (c) permission failures map cleanly to MCP errors without leaking raw keys; (d) a revoked or expired key fails on subsequent tool calls within a long-lived session; (e) stdio smoke runs do not import the hub in a way that writes to the wrong DB path. | P0 (Phase 1.5) |

### 9.10 Agent SDK

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-SDK-1 | The project MUST provide a single Python SDK package named `agent_sdk`. | P0 |
| FR-SDK-2 | The SDK MUST handle WebSocket connection, registration, task reception, and result reporting. | P0 |
| FR-SDK-3 | The SDK MUST support a `task_handler` callback pattern for task execution. | P0 |
| FR-SDK-4 | The SDK SHOULD provide both synchronous and asynchronous client implementations. | P1 |
| FR-SDK-5 | The SDK SHOULD handle reconnection on connection loss. | P1 |
| FR-SDK-6 | The SDK SHOULD support sending log messages during task execution. | P1 |

### 9.11 Claude Code / OpenCode Skill (Phase 1.5)

A discoverable convenience layer for MCP-aware clients, sitting on top of FR-MCP-*. Optional — the MCP server alone is sufficient for an MCP client; the skill exists to make first-time onboarding a single slash command.

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-SKL-1 | The project SHOULD ship a new `skills/agent-hub/` skill (with `SKILL.md` + supporting scripts) that registers the hub as an MCP server in the host client (Claude Code or OpenCode) and runs a smoke `list-agents` check. | P1 (Phase 1.5) |
| FR-SKL-2 | The skill SHOULD accept the hub URL and an API key as arguments and persist them in the host client's MCP config. It MUST NOT log or echo the raw key. | P1 (Phase 1.5) |
| FR-SKL-3 | The skill is the user-facing surface; it MUST NOT add new RPCs. Anything the skill does is reachable directly via the MCP tools from FR-MCP-2/3. | P1 (Phase 1.5) |
| FR-SKL-4 | The half-built `skills/agent-hub-connect/` from Phase 1 (a WebSocket connector for agents) MUST be retired in favor of the new `skills/agent-hub/` (an MCP onboarding/configuration layer). The two roles are distinct; reusing the name would blur them. Leave a short compatibility note in `skills/agent-hub-connect/SKILL.md` pointing users to `skills/agent-hub/` once the new skill lands. | P2 (Phase 1.5) |

## 10. Non-Functional Requirements

### 10.1 Persistence

| ID | Requirement | Priority |
|----|-------------|----------|
| NFR-PER-1 | Agent registrations, tasks, logs, results, and API keys MUST be persisted to SQLite. | P0 |
| NFR-PER-2 | On startup, the hub MUST restore pending tasks, registered agents, and API keys from SQLite. | P0 |
| NFR-PER-3 | Live state and SQLite MUST remain synchronized for all task and agent lifecycle writes. | P0 |
| NFR-PER-4 | Frequently queried columns MUST be indexed, including task `status`, `assigned_agent_id`, and `created_at`. | P1 |
| NFR-PER-5 | JSON data stored in SQLite MUST use `json.dumps()` or equivalent JSON serialization, not `str()`. | P0 |
| NFR-PER-6 | Activity log SHOULD record agent registrations, task lifecycle events, and key management operations. | P1 |

### 10.2 Reliability

| ID | Requirement | Priority |
|----|-------------|----------|
| NFR-REL-1 | The hub MUST NOT lose queued or assigned tasks on restart. | P0 |
| NFR-REL-2 | The hub MUST handle agent disconnects by re-queuing assigned incomplete tasks. | P0 |
| NFR-REL-3 | The hub SHOULD enforce task timeouts and make timed-out work available for retry when configured. | P1 |
| NFR-REL-4 | HTTP polling agents, if implemented, MUST track which assigned tasks they have already executed. | P1 |
| NFR-REL-5 | The hub SHOULD support bounded growth through configurable limits on stored tasks and queue depth. | P2 |

### 10.3 Security

| ID | Requirement | Priority |
|----|-------------|----------|
| NFR-SEC-1 | All state-mutating endpoints MUST require authentication. | P0 |
| NFR-SEC-2 | The bootstrap admin key MUST be configurable by environment variable or one-time initialization, not hardcoded in source. | P0 |
| NFR-SEC-3 | CORS MUST use specific allowed origins when credentials are enabled. | P1 |
| NFR-SEC-4 | API keys MUST be hashed with SHA-256 or stronger before storage; raw keys MUST only be shown on creation. | P0 |
| NFR-SEC-5 | Authentication-sensitive endpoints SHOULD have rate limiting. | P2 |

### 10.4 Concurrency

| ID | Requirement | Priority |
|----|-------------|----------|
| NFR-CON-1 | Shared live state for registry, queue, and WebSocket connections MUST be synchronized across async handlers and background tasks. | P0 |
| NFR-CON-2 | SQLite access SHOULD use safe connection handling, such as one connection per operation and/or WAL mode. | P1 |

### 10.5 Portability

| ID | Requirement | Priority |
|----|-------------|----------|
| NFR-PRT-1 | The hub MUST NOT use hardcoded absolute paths. Paths MUST be relative or configurable. | P0 |
| NFR-PRT-2 | The hub SHOULD run on Linux, macOS, and Windows. | P1 |
| NFR-PRT-3 | The database path SHOULD be configurable by environment variable, defaulting to `./agent_hub.db`. | P1 |

## 11. Data Model

### 11.1 Agent

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `agent_id` | string | yes | Unique identifier. |
| `name` | string | no | Display name. |
| `description` | string | no | Agent description. |
| `capabilities` | list[string] | yes | Task types this agent can handle. |
| `status` | enum | yes | `idle`, `busy`, or `offline`. |
| `machine_info` | object | no | Hostname, IP, and platform. |
| `owner` | string | no | Person, team, or system that controls the agent. |
| `last_heartbeat` | datetime | yes | Last heartbeat timestamp. |
| `created_at` | datetime | yes | Registration timestamp. |

### 11.2 Task

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string UUID | yes | Unique identifier. |
| `task` | string | yes | Natural language task description. |
| `task_type` | string | yes | Detected or explicitly specified task type. |
| `payload` | object | no | Structured task data. |
| `status` | enum | yes | `queued`, `assigned`, `running`, `completed`, `failed`, or `timeout`. |
| `submitted_by` | string | yes | Human, tool, or agent that submitted the task. |
| `target_agent` | string | no | Requested target agent, if any. |
| `assigned_agent_id` | string | no | Agent actually assigned to execute the task. |
| `result` | object | no | Task output. |
| `logs` | list[string] | no | Incremental log messages. |
| `priority` | integer | no | Higher values dispatch first; default `0`. |
| `timeout` | integer | no | Timeout in seconds; default `300`. |
| `parent_task_id` | string UUID | no | Reserved for future task chaining. |
| `created_at` | datetime | yes | Creation timestamp. |
| `started_at` | datetime | no | Execution start timestamp. |
| `completed_at` | datetime | no | Completion timestamp. |

### 11.3 API Key

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `key_hash` | string | yes | Hash of the raw key. |
| `name` | string | yes | Human-readable identifier. |
| `can_register` | boolean | yes | Can register agents. |
| `can_assign_tasks` | boolean | yes | Can submit tasks. |
| `can_view_agents` | boolean | yes | Can list or view agents. |
| `can_view_tasks` | boolean | yes | Can list or view tasks. |
| `allowed_agents` | list[string] | no | Restricts target agents; empty means any. |
| `is_admin` | boolean | yes | Full administrative access. |
| `active` | boolean | yes | Whether the key is usable. |
| `created_at` | datetime | yes | Creation timestamp. |
| `expires_at` | datetime | no | Optional expiration timestamp. |

## 12. API Specification

### 12.1 Agent Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/register` | `can_register` | Register an agent. |
| `POST` | `/heartbeat` | `can_register` | Update agent status. |
| `POST` | `/unregister` | `can_register` | Remove an agent. |
| `POST` | `/report` | `can_register` | Report task result. |
| `WS` | `/ws` | `can_register` | WebSocket agent connection. |

### 12.2 Human and Task Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/tasks` | `can_assign_tasks` | Submit a task. |
| `GET` | `/tasks/{task_id}` | `can_view_tasks` | Get task status and result. |
| `GET` | `/tasks` | `can_view_tasks` | List tasks with optional filters. |
| `GET` | `/agents` | `can_view_agents` | List agents. |
| `GET` | `/agents/{agent_id}` | `can_view_agents` | Get agent details. |

### 12.3 System Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | none or configured | Health check. |
| `GET` | `/stats` | `can_view_tasks` | Aggregate statistics. |
| `GET` | `/events` | `can_view_tasks` | Optional SSE event stream. |

### 12.4 Admin Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/admin/keys` | `is_admin` | Create API key. |
| `GET` | `/admin/keys` | `is_admin` | List API keys. |
| `DELETE` | `/admin/keys/{name}` | `is_admin` | Revoke API key. |

## 13. Protocol Specification

### 13.1 WebSocket Connection Flow

```
Agent                              Hub
  |                                  |
  |--- WS connect to /ws ---------->|
  |                                  |
  |--- { type: "register",          |
  |      agent_id: "...",           |
  |      capabilities: [...],       |
  |      auth_token: "..." } ------>|
  |                                  |
  |<-- { type: "registered",        |
  |      agent_id: "..." } ---------|
  |                                  |
  |     connection persists          |
  |                                  |
  |<-- { type: "task",              |
  |      task: { task_id, ... } } --|
  |                                  |
  |--- { type: "result",            |
  |      task_id: "...",            |
  |      status: "completed",       |
  |      result: {...} } ---------->|
```

### 13.2 Message Types

Agent to hub:

| Type | Fields | Description |
|------|--------|-------------|
| `register` | `agent_id`, `capabilities`, `auth_token` | First message on connect. |
| `heartbeat` | `status` | Status update. |
| `result` | `task_id`, `status`, `result`, `logs` | Task completion. |
| `log` | `task_id`, `log` | Incremental task log. |
| `status` | `task_id`, `status` | Task status update. |
| `pong` | none | Response to hub ping. |

Hub to agent:

| Type | Fields | Description |
|------|--------|-------------|
| `registered` | `agent_id` | Registration confirmation. |
| `task` | `task` | Task to execute. |
| `ping` | none | Keep-alive check. |
| `error` | `message` | Error notification. |

## 14. Task Lifecycle

```
submit
  |
  v
queued
  |
  | agent available
  v
assigned
  |
  | agent starts work
  v
running
  |
  | success / failure / timeout
  v
completed / failed / timeout
```

If no matching agent is available, the task remains `queued`. If an assigned agent disconnects before completion, the task returns to `queued`. Completed tasks are terminal and MUST NOT be re-executed.

## 15. Implementation Order

1. Hub server: registration, heartbeat, task submission, routing, status queries, and SQLite persistence.
2. Reliability: startup state recovery, disconnect re-queueing, and live state/database synchronization.
3. Agent SDK: WebSocket connection, registration, task callback, result reporting, logs, and reconnect behavior.
4. MCP integration: tools for listing agents, submitting tasks, querying tasks, and reading stats.
5. First working agent: echo or test agent to prove the end-to-end path.
6. Production hardening: admin key persistence, CORS configuration, timeouts, indexes, and optional HTTP fallback.
