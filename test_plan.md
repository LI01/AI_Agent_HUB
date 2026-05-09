# Agent Hub — Test Plan

Scope: Phase 1 MVP per `requirements.md`. Tests are keyed to requirement IDs (FR-* / NFR-*) so coverage can be tracked while implementation is in flight.

---

## 1. Strategy

### 1.1 Test Layers

| Layer | Tooling | Purpose |
|-------|---------|---------|
| Unit | `pytest` | Pure logic: routing, auth checks, queue ordering, model serialization. |
| Integration | `pytest` + `httpx.AsyncClient` + `websockets` + temp SQLite file | End-to-end through FastAPI and the SDK against a real DB. |
| Protocol | `pytest` driving raw WebSocket frames | Verifies message contracts independent of the SDK. |
| SDK | `pytest` against a live hub fixture | Confirms `agent_sdk` honors the protocol and reconnect rules. |
| MCP | `pytest` invoking `mcp/server.py` in CLI mode + tool-call mode | Confirms MCP surface matches REST behavior. |
| Smoke / E2E | shell script | Boots hub, echo agent, submits task, asserts result. |

### 1.2 Fixtures

- `tmp_db` — fresh SQLite at `tmp_path/agent_hub.db`; env var `AGENT_HUB_DB` points to it.
- `hub_app` — FastAPI app instance with `tmp_db` and a known seed admin key.
- `hub_server` — uvicorn started on a random free port for WebSocket / SDK tests.
- `admin_key`, `agent_key`, `submitter_key`, `viewer_key` — pre-created keys with scoped permissions.
- `ws_agent` — helper that opens `/ws`, sends `register`, returns the connection.
- `clock` — freezegun / monkeypatched `datetime.utcnow` for heartbeat-timeout and expiration tests.

### 1.3 Conventions

- Every test is hermetic: no shared DB, no shared port, no reliance on test order.
- Time-dependent tests use the `clock` fixture, never `time.sleep` longer than 100 ms.
- Each FR/NFR ID below maps to at least one test case; gaps are explicit.

---

## 2. Unit Tests

### 2.1 Auth (`hub/auth.py`)

| ID | Case | Expected |
|----|------|----------|
| U-AUTH-1 | Hash a key with SHA-256, compare hash to stored value. | Match. (NFR-SEC-4) |
| U-AUTH-2 | Permission check: key with `can_register=False` requesting `/register`. | Denied. (FR-ACL-2) |
| U-AUTH-3 | `allowed_agents=["a1"]` submitter targeting `a2`. | Denied. (FR-ACL-3) |
| U-AUTH-4 | Admin key passes every permission check. | Allowed everywhere. (FR-ACL-4) |
| U-AUTH-5 | Expired key (expires_at in past). | Denied. (FR-ACL-8) |
| U-AUTH-6 | Inactive (`active=False`) key. | Denied. |
| U-AUTH-7 | Bootstrap admin key sourced from env var, not constant in source. | Loaded from env. (NFR-SEC-2) |

### 2.2 Registry (`hub/registry.py`)

| ID | Case | Expected |
|----|------|----------|
| U-REG-1 | Register agent with duplicate `agent_id`. | Reject or upsert per spec; documented either way. (FR-REG-1) |
| U-REG-2 | `mark_offline` on disconnect updates status. | Status=`offline`. (FR-CON-4) |
| U-REG-3 | `idle_agents_for(capability)` returns only matching idle agents. | Filtering correct. (FR-RTE-2) |
| U-REG-4 | Heartbeat updates `last_heartbeat` and status. | Persisted. (FR-CON-3) |

### 2.3 Queue (`hub/queue.py`)

| ID | Case | Expected |
|----|------|----------|
| U-QUE-1 | Enqueue three tasks priorities `[0, 5, 1]`, dequeue order. | `[5, 1, 0]`. (FR-TSK-7) |
| U-QUE-2 | FIFO within same priority. | Insertion order preserved. |
| U-QUE-3 | Dequeue when empty. | Returns `None`, no exception. |
| U-QUE-4 | Re-queue an assigned task. | Reappears at head of its priority bucket. (FR-CON-6) |

### 2.4 Router (`hub/router.py`)

| ID | Case | Expected |
|----|------|----------|
| U-RTE-1 | `target_agent` set, agent online + idle. | Routes directly. (FR-RTE-1) |
| U-RTE-2 | `target_agent` set, agent offline. | Task remains `queued`. (FR-RTE-1) |
| U-RTE-3 | `target_agent` set, agent online but `busy`. | Stays `queued` until idle. |
| U-RTE-4 | No `target_agent`, two idle agents both match. | One is chosen deterministically (document tiebreak). (FR-RTE-2) |
| U-RTE-5 | No matching capability anywhere. | `queued`, no error. (FR-RTE-3) |
| U-RTE-6 | Task type detection from natural language `"echo foo"` → `task_type="echo"`. | Inferred. (FR-RTE-4) |

### 2.5 Models / Serialization

| ID | Case | Expected |
|----|------|----------|
| U-MOD-1 | JSON columns serialized with `json.dumps`, not `str()`. | Round-trips through `json.loads`. (NFR-PER-5) |
| U-MOD-2 | Task UUID is RFC-4122 v4. | Validates as UUID4. (FR-TSK-3) |
| U-MOD-3 | Default `timeout=300`, `priority=0`. | Defaults applied when missing. (FR-TSK-6/7) |

---

## 3. Integration — REST

Run against `hub_app` via `httpx.AsyncClient`.

### 3.1 Agent Lifecycle

| ID | Case | Expected |
|----|------|----------|
| I-REG-1 | `POST /register` with `can_register` key. | 200, agent persisted to SQLite. (FR-REG-1, FR-REG-4) |
| I-REG-2 | `POST /register` without auth. | 401. (FR-ACL-7) |
| I-REG-3 | `POST /register` with key lacking `can_register`. | 403. (FR-REG-2) |
| I-REG-4 | `POST /heartbeat` flips `idle`→`busy`. | Status updated, visible via `GET /agents/{id}`. (FR-CON-3) |
| I-REG-5 | `POST /unregister` removes agent. | 204; `GET /agents/{id}` 404. (FR-REG-5) |
| I-REG-6 | Register agent while a matching task is queued. | Task auto-dispatches on registration. (FR-REG-6) |

### 3.2 Task Submission

| ID | Case | Expected |
|----|------|----------|
| I-TSK-1 | `POST /tasks` minimal body `{task:"echo hi"}`. | 201, UUID returned, persisted. (FR-TSK-1, FR-TSK-3, FR-TSK-4) |
| I-TSK-2 | `POST /tasks` with structured `payload`. | Stored as JSON, retrievable intact. |
| I-TSK-3 | `POST /tasks` without auth. | 401. (FR-ACL-7) |
| I-TSK-4 | `POST /tasks` with `target_agent`. | Stored, routing attempts that agent. (FR-TSK-5) |
| I-TSK-5 | `POST /tasks` with `timeout=10`. | Stored; task becomes `timeout` if no result by deadline. (FR-TSK-6, FR-EXE-5) |
| I-TSK-6 | `POST /tasks` with `priority=10` while one priority-0 task is queued. | Higher dispatched first. (FR-TSK-7) |

### 3.3 Queries

| ID | Case | Expected |
|----|------|----------|
| I-QRY-1 | `GET /tasks/{id}` returns status, result, logs. | Full payload. (FR-QRY-1) |
| I-QRY-2 | `GET /tasks?status=queued`. | Filter applied. (FR-QRY-2) |
| I-QRY-3 | `GET /tasks?agent_id=a1`. | Only that agent's tasks. (FR-QRY-2) |
| I-QRY-4 | `GET /agents`. | All registered agents with status. (FR-QRY-3) |
| I-QRY-5 | `GET /agents/{id}`. | Single agent detail. (FR-QRY-4) |
| I-QRY-6 | `GET /health`. | 200, includes agent count, task count, db stats. (FR-QRY-5) |
| I-QRY-7 | `GET /stats`. | Aggregate counts (online agents, queued/running/completed). (FR-QRY-6) |
| I-QRY-8 | `GET /tasks` without view permission. | 403. (FR-QRY-7) |

### 3.4 Admin / API Keys

| ID | Case | Expected |
|----|------|----------|
| I-ACL-1 | `POST /admin/keys` with admin key. | 201, raw key returned in response body once. (FR-ACL-4, NFR-SEC-4) |
| I-ACL-2 | Subsequent `GET /admin/keys` does not include raw key. | Only metadata + hash prefix. (NFR-SEC-4) |
| I-ACL-3 | `DELETE /admin/keys/{name}`. | Key inactive; further use 401. |
| I-ACL-4 | Non-admin calling `/admin/keys`. | 403. |
| I-ACL-5 | Bootstrap admin key from env var on first start. | Created once, hashed in DB. (FR-ACL-5, NFR-SEC-2) |
| I-ACL-6 | Restart hub; previously created keys still valid. | Persisted across restart. (FR-ACL-6) |

---

## 4. Integration — WebSocket Protocol

Drive raw WebSocket frames against `hub_server`.

### 4.1 Connection Lifecycle

| ID | Case | Expected |
|----|------|----------|
| I-WS-1 | Connect to `/ws`, send `register` with valid `auth_token`. | Receive `{"type":"registered","agent_id":...}`. (FR-CON-1) |
| I-WS-2 | Connect, send `register` with invalid token. | Receive `error`, server closes. (FR-ACL-1) |
| I-WS-3 | Connect, send any message before `register`. | `error`, closed. |
| I-WS-4 | Send `heartbeat` with status `busy`. | Registry updated. (FR-CON-3) |
| I-WS-5 | Stop heartbeats for > timeout. | Agent marked `offline`. (FR-CON-4) |
| I-WS-6 | Hub sends `ping`, agent replies `pong`. | Connection remains open. |
| I-WS-7 | Disconnect mid-task (assigned, not completed). | Task re-queued; status `queued`; logs preserved. (FR-CON-6, NFR-REL-2) |
| I-WS-8 | Reconnect with same `agent_id`. | Treated as same agent (or rejected per spec — document). |

### 4.2 Task Delivery

| ID | Case | Expected |
|----|------|----------|
| I-WS-9 | Submit task targeting connected agent. | Agent receives `{"type":"task","task":{...}}` within 1s. (FR-EXE-1) |
| I-WS-10 | Agent replies `{"type":"result","status":"completed","result":{...}}`. | Task row updated, logs persisted, status `completed`. (FR-EXE-2, FR-EXE-4) |
| I-WS-11 | Agent sends incremental `log` messages. | Appended to `tasks.logs` JSON column. (FR-EXE-3) |
| I-WS-12 | Agent reports `failed`. | Status `failed`, result captured. (FR-EXE-2) |
| I-WS-13 | Resubmit a `completed` task's id. | Hub does NOT re-dispatch. (FR-EXE-6) |
| I-WS-14 | Task exceeds `timeout`. | Status `timeout`; per config, requeued or terminal. (FR-EXE-5, NFR-REL-3) |

---

## 5. Persistence & Recovery

### 5.1 Synchronization

| ID | Case | Expected |
|----|------|----------|
| P-SYN-1 | After every task state change, row in DB matches in-memory queue. | Match. (NFR-PER-3) |
| P-SYN-2 | After agent registration, DB row exists before WS register-ack. | Persisted-first ordering. (FR-REG-4) |
| P-SYN-3 | Indexes exist on `tasks.status`, `tasks.assigned_agent_id`, `tasks.created_at`. | Verify via `PRAGMA index_list`. (NFR-PER-4) |

### 5.2 Restart Recovery

| ID | Case | Expected |
|----|------|----------|
| P-REC-1 | Submit 3 queued tasks, restart hub. | All 3 still `queued` and dispatchable. (NFR-REL-1, NFR-PER-2) |
| P-REC-2 | Task `assigned` to agent X, kill hub before completion. | On restart, task `queued` again (agent considered offline). (NFR-REL-2) |
| P-REC-3 | API keys created pre-restart still authenticate post-restart. | Pass. (FR-ACL-6) |
| P-REC-4 | Activity log entries persisted across restart. | Visible. (NFR-PER-6) |

---

## 6. SDK Tests (`agent_sdk`)

Run against `hub_server`.

| ID | Case | Expected |
|----|------|----------|
| S-SDK-1 | `Client.connect()` registers and returns. | Agent visible in `GET /agents`. (FR-SDK-2) |
| S-SDK-2 | `task_handler` invoked with delivered task. | Return value sent as `result`. (FR-SDK-3) |
| S-SDK-3 | Sync and async client variants both work. | Same observable behavior. (FR-SDK-4) |
| S-SDK-4 | Kill hub, restart; SDK reconnects with backoff. | Reconnects; capped backoff. (FR-SDK-5, FR-CON-5) |
| S-SDK-5 | `client.log(task_id, "...")` during handler. | Log appears in `GET /tasks/{id}`. (FR-SDK-6, FR-EXE-3) |
| S-SDK-6 | Handler raises an exception. | SDK reports `failed` with traceback summary, agent stays connected. |

---

## 7. MCP Integration (`mcp/server.py`)

| ID | Case | Expected |
|----|------|----------|
| M-MCP-1 | `list-agents` tool returns the same data as `GET /agents`. | Parity. (FR-MCP-2) |
| M-MCP-2 | `submit-task` tool creates a task; `get-task` returns its result after completion. | End-to-end via MCP. (FR-MCP-2) |
| M-MCP-3 | `register-agent`, `list-tasks`, `stats` tools each match REST output. | Parity. (FR-MCP-2) |
| M-MCP-4 | Admin tools (`create-api-key`, `list-api-keys`, `revoke-api-key`) gated by admin key. | Non-admin caller rejected. (FR-MCP-3) |
| M-MCP-5 | CLI mode: `python mcp/server.py ... agents` prints agent list. | Exit 0, parseable output. (FR-MCP-4) |

---

## 8. Concurrency

| ID | Case | Expected |
|----|------|----------|
| C-CON-1 | 50 simultaneous WS agents register in parallel. | All succeed; no duplicate `agent_id` corruption. (NFR-CON-1) |
| C-CON-2 | 200 task submissions in parallel. | All persisted with unique UUIDs; counts match. |
| C-CON-3 | Two agents both eligible; submit 100 tasks. | All dispatched; no task delivered twice. |
| C-CON-4 | Disconnect agent while result message is in flight. | Either result accepted or task requeued — never both. (NFR-REL-2) |
| C-CON-5 | SQLite under load: WAL or per-op connection (whichever spec chose). | No `database is locked` failures. (NFR-CON-2) |

---

## 9. Security

| ID | Case | Expected |
|----|------|----------|
| SEC-1 | `Authorization: Bearer wrong` on every mutating endpoint. | 401. (NFR-SEC-1, FR-ACL-7) |
| SEC-2 | Inspect DB: `key_hash` column never stores raw keys. | Only hashes. (NFR-SEC-4) |
| SEC-3 | Raw key returned exactly once on creation; cannot be re-fetched. | `GET /admin/keys` omits raw. |
| SEC-4 | Default seed admin key constant from `summary.md` is NOT accepted unless env var set. | Refuses fallback hardcoded key. (NFR-SEC-2) |
| SEC-5 | CORS preflight from disallowed origin with credentials. | Rejected. (NFR-SEC-3) |
| SEC-6 | Rate limit on `/register` and `/admin/keys` (if implemented). | Throttles after N/min. (NFR-SEC-5) |
| SEC-7 | Submit task with `payload` containing 1 MB string. | Stored or rejected with bounded-growth error, not silent truncation. (NFR-REL-5) |

---

## 10. Portability

| ID | Case | Expected |
|----|------|----------|
| PRT-1 | Grep source for absolute paths (`/root/`, `/Users/`, `/home/`). | None outside docs/examples. (NFR-PRT-1) |
| PRT-2 | `AGENT_HUB_DB=/tmp/x.db` overrides default. | DB created at that path. (NFR-PRT-3) |
| PRT-3 | Boot hub on Linux, macOS, Windows runners. | All start, smoke test passes. (NFR-PRT-2) |

---

## 11. End-to-End Smoke

Shell script `scripts/smoke.sh`:

1. Start hub on random port with fresh DB.
2. Use seed admin key to create `agent_key` and `submitter_key`.
3. Start `examples/echo-agent` with `agent_key`.
4. `POST /tasks {"task":"echo hello"}` with `submitter_key`.
5. Poll `GET /tasks/{id}` until `status=completed` (timeout 10s).
6. Assert `result.echo == "hello"`.
7. Kill agent; submit another task; assert it stays `queued`.
8. Restart agent; assert task transitions to `completed`.
9. Kill hub; restart; assert both task records present, statuses preserved.

Exit non-zero on any failure. This is the gating script for "hub works end-to-end."

---

## 12. Coverage Matrix (Gaps)

Requirements with no current test coverage are flagged here so the implementing agent can either add them or mark as out-of-scope:

- FR-SSE-1/2/3 (SSE events) — P2, deferred unless implemented. Add if `/events` ships.
- FR-RTE-4 (NL task-type detection) — needs sample corpus once detection rules exist.
- NFR-REL-5 (bounded growth limits) — only meaningful once limits are configurable.

---

## 13. CI Wiring (recommended)

- `pytest -q` on every PR — unit + integration.
- `scripts/smoke.sh` as a post-build step.
- Matrix: Python 3.11 / 3.12 × {Linux, macOS}.
- Fail build on any `database is locked`, unhandled exception in server logs, or task left in `assigned` state at suite end.

---

## 14. Real-World End-to-End Scenarios — Phase 2 Exploratory (Non-Gating)

> **Scope note:** §14 is a Phase 2 exploratory suite. None of these scenarios gate Phase 1 ship readiness. Their purpose is to surface the design gaps an MVP hub leaves open when real multi-agent pipelines are wired through it. Failures here should be filed against Phase 2 design, not Phase 1 bugs.
>
> Reviewed by codex in `Agent-comm/codex_review_test-plan-section-14_20260509-0135.md`.

The cases in §1–§13 are deterministic and isolated. This section adds *realistic* multi-agent workflows where the hub coordinates real (or scripted) agents on a real coding task. Use these to catch contract gaps that unit tests miss: payload shapes the SDK serializes vs. what an agent actually emits, dispatch timing under load, the human-in-the-loop UX.

### 14.1 Orchestration model — driver-owned

Three orchestration models are possible: driver-owned (a human/script submits each child task), agent-owned (agents call REST `/tasks` to spawn siblings), and hub-owned (the hub interprets a declarative plan). For the first version of §14 we use **driver-owned** because it keeps §14 a test harness rather than a new product feature. Agent-owned and hub-owned are deferred to Phase 2 design.

Implications:

- Agents do **not** need `can_assign_tasks`; only the driver does.
- Agents return structured results; the driver inspects them and submits follow-up tasks.
- This avoids requiring `parent_task_id` semantics inside the hub.

### 14.2 Prerequisites

- Harness can start a local hub with a generated or known `AGENT_HUB_ADMIN_KEY` and a fresh `AGENT_HUB_DB_PATH` (`tempfile.mkdtemp() + "/agent_hub.db"`). The hub is owned by the test process — not a pre-existing external service.
- The harness mints, per run: one driver key with `can_assign_tasks=true`, `can_view_tasks=true`; and per-role agent keys (planner, coder, reviewer) with `can_register=true` only.
- A per-pipeline scratch directory derived from `pipeline_id`; agents write code into the path the driver passes in `payload.scratch_path`.
- Either real LLM credentials (one per agent) or scripted agents that mimic the roles deterministically. Default to scripted agents for CI; LLM agents for manual exploration.

### 14.3 Scenarios

| ID | Scenario | Expected | Notes |
|----|----------|----------|-------|
| E2E-1 | **Harness fixture (not a standalone test).** `hub_with_agents(profile="full")` (see §14.5) starts a hub on a free port with a fresh scratch DB, mints driver + 3 agent keys, spawns planner/coder/reviewer, polls `/health` and `/agents` until ready, and yields a `HarnessHandle(hub_url, driver_key, agent_keys, scratch_dir)`. | Context manager yields within 10s with all 3 agents `idle`; cleanup tears down agents and hub on exit. | Used by every later scenario; not a confidence signal on its own (overlaps with existing registration tests + smoke). |
| E2E-2 | **Three-agent coding pipeline.** Driver mints a `pipeline_id` (UUID) and a per-pipeline scratch path `scratch/pipelines/{pipeline_id}/fib/`. Driver submits `{"task_type":"plan","payload":{"goal":"add a fibonacci(n) function with tests to <scratch_path>","scratch_path":"<scratch_path>","pipeline_id":"<id>"}}` — every child task carries `pipeline_id` and `scratch_path` in payload, and the driver sets `parent_task_id` on each child to the planner task id (or pipeline root id). Planner returns subtask specs; driver submits each. | `<scratch_path>/fib.py` + `<scratch_path>/test_fib.py` exist; `pytest <scratch_path>` passes; reviewer result has non-empty `issues` or `approved=true`. | Core scenario. Driver-owned dispatch with explicit `parent_task_id` so §14.9 has exercisable data. |
| E2E-3 | **Failure recovery in a pipeline.** Kill the coder agent mid-task during E2E-2 (a kill thread waits until the first code task hits `running`/`assigned`, then SIGKILLs the coder). Harness restarts the coder. Requires the scripted coder to honor `E2E_CODER_DELAY` (≥1s) so the kill lands inside the task, not after `completed`. | Killed task requeued (FR-CON-6 / NFR-REL-2), reassigned to the restarted coder via `dispatch_pending_for_agent` on re-register, pipeline still completes. | Realistic agent crash. Without the delay knob the task finishes in ms and there is no kill window. |
| E2E-4 | **Reviewer loop.** Reviewer returns `{"approved": false, "issues": [...]}`. Driver re-submits a `code` task with the issues. Iterate up to 3 rounds. | `approved=true` within 3 iterations, or driver records a clear reason and exits. | Catches contract drift between agents. |
| E2E-5 | **Capability change on re-registration.** Bootstrap profile: planner + coder only (no reviewer). Coder starts with `["code"]`. Driver submits a `review` task — task stays `queued`. Coder reconnects with `["code","review"]` (same `agent_id`). Implementation: the scripted coder reads its declared capabilities from `E2E_CODER_CAPABILITIES` (comma-separated) so `restart_agent("coder", env_overlay={"E2E_CODER_CAPABILITIES": "code,review"})` flips them without changing `agent_id` or key. Re-registered coder must also implement a review handler. | Queued review task dispatches to the re-registered coder within 2s of reconnect; `assigned_agent_id == "coder"`. | Uses a different bootstrap profile from §14.2 (no reviewer). Catches realistic long-running agent upgrades and registry/queue rescan behavior. |
| E2E-6 | **Observer via SSE.** *Conditional — run only when `/events` is in scope for the release.* A separate process subscribes to `/events` for the duration of E2E-2. | Observer receives at least one `task_created` and one `task_completed` per pipeline step, in order. | Confirms FR-SSE-* under a realistic event flow. |
| E2E-7 | **Concurrent pipelines.** Run E2E-2 three times in parallel against the same hub. Each pipeline mints its own `pipeline_id` and writes to its own `scratch/pipelines/{pipeline_id}/fib/`. | All three pipelines complete; pytest passes per pipeline path; no task ever has `pipeline_id` X but is dispatched to handle a payload from `pipeline_id` Y; no deadlock. | Per-pipeline scratch isolation lets us distinguish hub misrouting from filesystem contention. |

### 14.4 Reference agent skeletons

Place under `scripts/e2e/`. Agents use the existing `agent_sdk.AgentHub` for WS. They do **not** import `httpx` because they don't submit child tasks (driver-owned model).

```python
# scripts/e2e/planner.py
import sys
from agent_sdk import AgentHub

HUB, KEY = sys.argv[1], sys.argv[2]

def plan(task):
    payload = task["payload"]
    goal = payload["goal"]
    scratch = payload["scratch_path"]   # set by the driver per pipeline
    # Real agent: ask an LLM. Scripted: hardcoded breakdown that derives all paths from `scratch`.
    return {"subtasks": [
        {"task_type": "code",   "payload": {"goal": goal,                  "file": f"{scratch}/fib.py"}},
        {"task_type": "code",   "payload": {"goal": "tests for " + goal,   "file": f"{scratch}/test_fib.py"}},
        {"task_type": "review", "payload": {"path": scratch}},
    ]}

AgentHub(HUB, "planner", ["plan"], auth_token=KEY, task_handler=plan).start()
```

```python
# scripts/e2e/coder.py    — writes payload.file based on payload.goal; returns {"path": file}.
#                           Determine "is this a test file?" by inspecting payload.file
#                           (e.g. basename startswith "test_"), NOT by substring-matching
#                           payload.goal — the impl-file goal "...add fibonacci with tests"
#                           also contains the substring "test", which would misroute it.
#                           Honors `E2E_CODER_DELAY` env var (seconds) so E2E-3 has a
#                           reliable kill window mid-task.
# scripts/e2e/reviewer.py — runs pytest on payload.path; returns {"approved": bool, "issues": [...]}
```

The driver propagates `scratch_path` and `pipeline_id` into every child payload (see §14.6); planner/coder/reviewer never hardcode paths.

The driver and any explorations from inside the suite use a small helper:

```python
# scripts/e2e/hub_http.py
import httpx, time

def submit_task(hub, key, **fields):
    r = httpx.post(f"{hub}/tasks", headers={"Authorization": f"Bearer {key}"}, json=fields)
    r.raise_for_status()
    return r.json()["task_id"]

def get_task(hub, key, tid):
    return httpx.get(f"{hub}/tasks/{tid}", headers={"Authorization": f"Bearer {key}"}).json()

def wait_task(hub, key, tid, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = get_task(hub, key, tid)
        if body["status"] in ("completed", "failed", "timeout"):
            return body
        time.sleep(0.2)
    raise TimeoutError(tid)
```

If Phase 2 later needs agent-native delegation, add a `TaskContext.submit_child(...)` helper rather than broadening `AgentHub` globally.

### 14.5 Harness — single owner of process lifecycle

`scripts/e2e/run_pipeline.py` is the test entry point and **owns the lifecycle** of the hub and agents. It does NOT shell out to a long-lived `bootstrap.sh` and read a manifest — that pattern was inconsistent (the bootstrap would either exit and tear down processes the driver still needs, or never exit and never run assertions). The harness instead uses a Python context manager:

```python
# scripts/e2e/harness.py — used by run_pipeline.py
@contextmanager
def hub_with_agents(profile="full", agent_env: dict[str, dict[str, str]] | None = None):
    """Yields HarnessHandle(hub_url, driver_key, agent_keys, scratch_dir,
                            kill_agent, restart_agent).
    profile="full"        → planner + coder + reviewer
    profile="no-reviewer" → planner + coder (used by E2E-5)
    agent_env             → optional per-role env-var overlay, e.g.
                            {"coder": {"E2E_CODER_DELAY": "2.0"}} for E2E-3.
    kill_agent(role)                              → SIGKILL the named role's subprocess (E2E-3).
    restart_agent(role, env_overlay: dict | None) → respawn the role's subprocess; env_overlay
                            merges on top of the per-role agent_env from hub_with_agents and
                            persists across subsequent restarts (E2E-5 uses this to flip the
                            coder's `E2E_CODER_CAPABILITIES`).
    """
    scratch_db = tempfile.mkdtemp() + "/agent_hub.db"
    port = _free_port()
    hub_proc = _spawn_hub(port, scratch_db, admin_key)
    try:
        _wait_for_health(port, timeout=10)
        driver_key = _mint_key(port, "driver", can_assign_tasks=True, can_view_tasks=True)
        agent_keys = {role: _mint_key(port, role, can_register=True)
                      for role in _roles_for(profile)}
        agent_procs = [_spawn_agent(role, port, key) for role, key in agent_keys.items()]
        _wait_for_agents(port, list(agent_keys), timeout=10)
        yield HarnessHandle(...)
    finally:
        for p in agent_procs: _kill(p)
        _kill(hub_proc)
        shutil.rmtree(scratch_db_dir)
```

A scenario then reads:

```python
with hub_with_agents(profile="full") as h:
    pid = uuid.uuid4().hex
    scratch = h.scratch_dir / "pipelines" / pid / "fib"
    plan_id = submit_task(h.hub_url, h.driver_key,
                          task_type="plan",
                          payload={"goal": "...", "scratch_path": str(scratch), "pipeline_id": pid})
    plan = wait_task(h.hub_url, h.driver_key, plan_id)
    for sub in plan["result"]["subtasks"]:
        sub_id = submit_task(h.hub_url, h.driver_key,
                             task_type=sub["task_type"],
                             payload={**sub["payload"], "pipeline_id": pid, "scratch_path": str(scratch)},
                             parent_task_id=plan_id)
        wait_task(h.hub_url, h.driver_key, sub_id)
```

Cleanup is scoped to the test process. There is no separate manifest file and no `bootstrap.sh up/down` to coordinate. CI runs `pytest scripts/e2e/` (or equivalent) and that's it.

### 14.6 Driver responsibilities

`scripts/e2e/run_pipeline.py`:

- Mints a fresh `pipeline_id` per pipeline.
- Computes a unique scratch path keyed by `pipeline_id`.
- On every child task: includes `pipeline_id` + `scratch_path` in `payload`, and sets `parent_task_id` to the planner task id (or pipeline root id).
- Polls `wait_task` for each child.
- Prints a transcript that includes `pipeline_id`, every task id, and its `parent_task_id`.
- Asserts §14.7 criteria; exits non-zero on failure.
- Is the human stand-in. Phase 2 may collapse much of this into hub-side delegation.

### 14.7 Pass/fail criteria summary

A scenario passes when:

- All expected files are present with correct content under the per-pipeline `scratch_path`.
- `pytest` succeeds when the scenario produces tests.
- No task ends in `failed` or `timeout` unless the scenario expects it.
- No agent is stuck in `busy` after the pipeline completes — driver polls `GET /agents` once after the last child completes and asserts every agent is `idle` (or `offline` for E2E-3's killed-and-not-restarted variant).
- Every child task in the transcript has `parent_task_id` set to the planner/root task id, and `payload.pipeline_id` matches the driver's `pipeline_id`.
- For concurrent pipelines (E2E-7): every child task body's `payload.pipeline_id` equals the driver-minted `pipeline_id` for that pipeline, AND every `result.path` (when present) starts with that pipeline's `scratch_path`. This catches cross-pipeline misrouting that would otherwise hide behind correct-looking per-pipeline pytest exits.

### 14.8 Known gaps these tests will surface

Expected against the current Phase 1 implementation. **All Phase 2 design items, not Phase 1 bugs.**

- **`parent_task_id` is stored and forwarded but not honored.** No parent→child index, no parent completion notification, no cascade cancel. Driver must poll explicitly.
- **No agent-to-agent submit shortcut.** Driver-owned model in 14.1 sidesteps this; agent-owned would need it.
- **No per-pipeline isolation.** Concurrent pipelines (E2E-7) share the hub task pool; a slow reviewer can delay other pipelines' dispatch even when their scratch paths are isolated.
- **No queryable activity log surface.** The hub writes activity-log rows but exposes no endpoint to query them, and not every task lifecycle event lands there. Asserting "the activity log shows the full causal chain" is a DB-inspection-only check today; full causal-chain observability is a Phase 2 work item.

### 14.9 Phase 2 design hint — minimum hub change to lose driver polling

Smallest credible patch (not full delegation):

- `GET /tasks?parent_task_id=...` filter.
- `parent_task_id` field added to `task_created`, `task_updated`, `task_completed` SSE payloads.
- Index on `tasks.parent_task_id`.

This lets a driver subscribe to events keyed by `parent_task_id` instead of polling, while keeping orchestration outside the hub. Auto-creating child tasks from planner output is a larger Phase 2 design pass.
