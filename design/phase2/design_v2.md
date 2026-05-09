# Phase 2 Design v2

**Author:** designer-v2 (sub-agent)
**Date:** 2026-05-09
**Scope:** F1 (agent-to-agent delegation) + F2 (parent_task_id honored). Per
`design/phase2/scope.md`. Anything else is out of scope.

**What changed from v1 (one line per codex `revise` finding):**
(1) `submit_child` now requires `target_agent`; (2) cycle check allows immediate self A→A, rejects only non-immediate A→…→A; (3) SDK read-loop decoupled from handler execution (async `create_task`, sync worker pool + send lock) so child waits cannot deadlock the WS reader; (4) `Task.blocked_on` removed — registry maps are the only truth; (5) centralised `finalize_task()` helper drives `_notify_parent_of_child()` for every terminal path including the timeout watcher; (6) permission gate is exact-target since `target_agent` is required; (7) new `task_event_data(task_id, **extra)` helper used at every task-event callsite so `parent_task_id` is uniformly present; (8) magic string `__none__` dropped — exact-match `?parent_task_id=<uuid>` plus a separate `?top_level=true` boolean.

---

## 1. Summary

**F1 ships:**
- New WS messages: `submit_child` (agent→hub) → `child_accepted`/`child_rejected` (hub→agent), plus unsolicited `child_completed` push when the child reaches a terminal status.
- `target_agent` is **required** (no capability-routed delegation in Phase 2).
- Hub-side child tracking via two in-memory maps (no new `Task` field). Ancestor-chain + depth cap (default `8`, env-overridable) enforce safe recursion. Immediate self-delegation A→A is allowed; A→B→A is rejected.
- SDK helper `submit_child(...)` on both `AgentHub` and `AsyncAgentHub`. Reader loop decoupled from handler execution so parent waits never deadlock (§2.2.6).
- Failure bubbling rule: child failure does **not** auto-fail the parent; the hub delivers `child_completed{status: failed|timeout}` and the parent agent decides.
- Centralized `finalize_task(task_id, status, result, logs)` drives every terminal transition (REST `/report`, WS `result`, timeout watcher) and is the single caller of `_notify_parent_of_child()`.

**F2 ships:**
- Additive SQLite index `idx_tasks_parent`.
- `GET /tasks?parent_task_id=<uuid>` exact match; separate `?top_level=true` for `parent_task_id IS NULL`.
- `parent_task_id` added to `task_created`/`task_updated`/`task_completed` SSE payloads via a single `task_event_data(...)` helper.
- Documented semantics: no cascade in either direction. Agent owns the join.

**Out of scope (explicit):** DAGs, fan-out/fan-in joins, parent cascade-cancel, MCP `submit-child` tool (delegation stays agent-only), streaming partial child results beyond `log`, capability negotiation, federation, **targetless `submit_child`**.

---

## 2. F1 — Agent-to-agent delegation

### 2.1 Open questions answered (scope.md §F1)

**Q1 — Wire surface.** A new WS message type `submit_child` over the agent's existing `/ws` connection. `target_agent` is **required**. Reasons: same authenticated channel as `task`/`result`; required target makes routing, `allowed_agents` enforcement, and cycle detection deterministic. Submit without `target_agent` → `child_rejected: bad_request`. **No** new REST endpoint and **no** MCP delegation tool — MCP remains the human/orchestrator surface.

**Q2 — Parent must not block its WS reader.** Two SDK fixes (corrected from v1, detailed in §2.2.6): async `run()` schedules handlers via `asyncio.create_task` instead of `await`-ing them inline; sync `_on_message` dispatches handlers to a thread-pool, with a `threading.Lock` guarding `_ws.send`. The reader keeps draining `ping`, `child_*`, etc. while a handler awaits a child future. Hub-side: parent task stays `running`, parent agent registry stays `BUSY` (so no second top-level task is dispatched). **No new `Task` field** — `_children_by_parent` non-empty is the truth (§2.4).

**Q3 — Permissions.** The child is authorized by the parent agent's API key (the `auth_token` used at WS register). Hub caches it on `ConnectionManager.session_keys[agent_id]` and revalidates via `access_control.validate_key` on every `submit_child` (immediate revocation, mirrors FR-MCP-4). The check is `access_control.can_assign_task(key, target_agent, requester="agent")` — an exact-target check because `target_agent` is required (Q1). No new permission flag (`can_assign_tasks` is the gate, codex #11). `is_admin` keys bypass `allowed_agents` as today. `submitted_by` on the child = `"agent:<parent_agent_id>"`.

**Q4 — Child fail/timeout.** Hub sends `child_completed` with `status=completed|failed|timeout` and `result`. Parent's task status is **not** mutated by the hub. If the parent's WS disconnects mid-child, parent requeues per FR-CON-6 and the child runs to completion independently; the new parent incarnation must re-delegate (codex #9 accepted this as Phase 2 limitation). If the parent itself times out before the child finishes, the eventual child completion finds the parent terminal and the push is dropped with a `child_completed_dropped` activity-log entry; the child's row is still persisted and queryable.

**Q5 — Cycle detection.** §2.5. Walk ancestor chain from `parent_task_id`; reject if `target_agent` matches `assigned_agent_id` of any **non-immediate** ancestor (allowing A→A). Hard depth cap `MAX_TASK_DEPTH=8` as fallback.

### 2.2 Wire protocol

#### 2.2.1 Agent → Hub: `submit_child`

```json
{
  "type": "submit_child",
  "request_id": "client-uuid-v4",
  "parent_task_id": "<task_id this agent is currently executing>",
  "target_agent": "docs-agent-1",
  "task": "look up asyncio.gather signature",
  "task_type": "docs-lookup",
  "payload": {"symbol": "asyncio.gather"},
  "priority": 0,
  "timeout": 60
}
```

Required: `type`, `request_id`, `parent_task_id`, `target_agent`. Either `task` or `payload` must be non-empty (mirrors `POST /tasks`). Other fields default like `TaskSubmitRequest`. `request_id` is echoed in `child_accepted`/`child_rejected`.

**Validation order (hub):**
1. `target_agent` present and non-empty → else `child_rejected: bad_request`.
2. Parent task exists and `assigned_agent_id == this_ws_agent_id` → else `not_owner`.
3. Parent task status ∈ {`assigned`, `running`} → else `parent_terminal`.
4. `validate_key` + `can_assign_task(key, target_agent)` → else `forbidden`.
5. `check_delegation(parent_task_id, target_agent)` (§2.5) → else `cycle` / `depth_limit_exceeded`.
6. Create child via existing direct-target dispatch (`Router.route_to_agent`); register `_children_by_parent[parent_id].add(child_id)` and `_parent_of_child[child_id] = parent_id`.
7. Send `child_accepted`.

#### 2.2.2 / 2.2.3 / 2.2.4 — Hub → Agent

```json
// child_accepted
{"type":"child_accepted","request_id":"<echoed>","child_task_id":"<uuid>","status":"queued"}

// child_rejected (reason ∈ {bad_request, not_owner, parent_terminal, forbidden, cycle, depth_limit_exceeded})
{"type":"child_rejected","request_id":"<echoed>","reason":"cycle","message":"…"}

// child_completed (unsolicited; status ∈ {completed, failed, timeout})
{"type":"child_completed","parent_task_id":"<id>","child_task_id":"<id>","status":"completed","result":{...}}
```

`child_completed` is pushed by `_notify_parent_of_child()` from inside `finalize_task()` (§2.6) when the child has a `parent_task_id`, the parent task is still in `assigned`/`running`, and the parent agent's WS is connected. Otherwise dropped with a `child_completed_dropped` activity-log entry.

#### 2.2.5 No new REST or MCP endpoints

Children are queryable via existing `GET /tasks/{id}` and `GET /tasks` (with the new `parent_task_id` filter from §3). No MCP delegation tool — codex #15 accepted.

#### 2.2.6 SDK additions and reader-loop redesign (critical fix)

**The deadlock to avoid.** v1 said "the WS reader resolves the future on `child_completed`." That is false against today's SDK shape:

- `AsyncAgentHub.run()` does `await self._handle_task(...)` — if the handler awaits a child future, the reader never reaches its next `recv()`, the push never arrives, the future never resolves. **Hard deadlock.**
- `AgentHub._on_message` (sync, websocket-client callback) calls `_handle_task(...)` inline on the reader thread — same shape, same deadlock.

**Async fix.** `run()` becomes:

```python
async def run(self):
    await self.connect()
    while True:
        msg = await self.recv()
        t = msg.get("type")
        if t == "task":
            asyncio.create_task(self._handle_task(msg.get("task", {})))
        elif t == "ping":
            await self.send({"type": "pong"})
        elif t == "child_accepted":  self._resolve_pending_request(msg)
        elif t == "child_rejected":  self._reject_pending_request(msg)
        elif t == "child_completed": self._resolve_pending_child(msg)
```

`_handle_task` is now an independent `asyncio.Task`. The reader keeps looping. A handler that `await self.submit_child(...)` blocks on a `Future` resolved by the reader.

**Sync fix.** `AgentHub.__init__` adds:
- `self._send_lock = threading.Lock()` — every `_ws.send` call is wrapped (worker threads and the reader thread cannot interleave a partial frame).
- `self._executor = ThreadPoolExecutor(max_workers=int(os.getenv("AGENT_HUB_HANDLER_WORKERS","4")))` — handlers run on workers.
- Pending-wait maps: `_pending_requests: dict[str, threading.Event]` (keyed by `request_id`), `_pending_children: dict[str, threading.Event]` (keyed by `child_task_id`), `_pending_results: dict[str, dict]`.
- `_current_task_id = threading.local()` — per-handler context.

`_on_message` becomes a thin router: on `task`, `self._executor.submit(self._handle_task, ...)` and return; on `child_*`, resolve pending waits inline; on `ping`, `_send_json({"type":"pong"})`. The reader thread is no longer blocked by handler execution.

**Public API on both classes:**

```python
def submit_child(
    self, *,
    target_agent: str,                    # REQUIRED
    task: str = "",
    task_type: str | None = None,
    payload: dict | None = None,
    timeout: int = 300,
    priority: int = 0,
    wait_timeout: float | None = None,    # default = task timeout + 30s
) -> dict:                                # {"status","result","child_task_id"}
    """Raises ChildNotInTaskContext, ChildRejectedError(reason,message), ChildWaitTimeout."""
```

Internal flow: (1) read current `parent_task_id` from context-var (async) / thread-local (sync); raise `ChildNotInTaskContext` if unset (codex #13). (2) Generate `request_id`, register a wait primitive, send `submit_child`. (3) On `child_accepted`, capture `child_task_id` and register a second wait primitive keyed by it. On `child_rejected`, raise. (4) Block up to `wait_timeout` on the child wait. The reader resolves it on `child_completed`. (5) Return payload, or `ChildWaitTimeout` if elapsed.

**Backward-compat.** No existing SDK method changes signature. The async `await → create_task` change is invisible to handlers that don't call `submit_child` (the hub's busy-state already prevents concurrent top-level tasks per agent). The sync executor is the equivalent arrangement.

### 2.3 Hub-side state

In a small new `hub/delegation.py`:

```python
_children_by_parent: dict[str, set[str]] = {}   # parent_task_id -> children
_parent_of_child:    dict[str, str]      = {}   # child_task_id -> parent
```

Both guarded by the existing `TaskQueue._lock` (RLock) — no new lock. On `ConnectionManager`, a new `session_keys: dict[str, str]` (ws agent_id → API key used at register), set on registration, cleared on disconnect.

**Persistence.** Maps are rebuilt on hub restart in `load_state()` by scanning persisted tasks where `parent_task_id IS NOT NULL` AND `status NOT IN ('completed','failed','timeout')`. F2's index makes the scan fast. **No new schema column.**

### 2.4 Concurrency model and non-fields

Single lock (`TaskQueue._lock`). Map mutations happen inside it; WS sends happen outside it. **No `Task.blocked_on` field** (codex #4) — `Task` Pydantic model is unchanged, so REST/SSE/SDK responses gain nothing new on `Task`. The "parent is waiting" condition is implicit:
- Parent's `Task.status` stays `running`/`assigned`.
- Parent agent's registry status stays `BUSY` (dispatcher won't push another top-level task).
- `_children_by_parent[parent_id]` non-empty is the in-memory truth.

### 2.5 Cycle detection algorithm

```python
MAX_TASK_DEPTH = int(os.getenv("AGENT_HUB_MAX_TASK_DEPTH", "8"))

def _ancestor_chain(task_id: str | None) -> list[Task]:
    """Walk parent_task_id pointers up to root or MAX_TASK_DEPTH+1.
    First element is the immediate parent. O(depth)."""
    chain: list[Task] = []
    current_id = task_id
    while current_id is not None and len(chain) <= MAX_TASK_DEPTH + 1:
        task = queue.get(current_id) or db.get_task(current_id)
        if task is None: break
        chain.append(task)
        current_id = task.parent_task_id
    return chain

def check_delegation(parent_task_id: str, target_agent: str) -> tuple[bool, str | None]:
    chain = _ancestor_chain(parent_task_id)
    # New child would be chain+1; reject when current chain already at cap.
    if len(chain) >= MAX_TASK_DEPTH:
        return False, "depth_limit_exceeded"
    # Skip chain[0] (immediate parent) so A->A is allowed.
    for ancestor in chain[1:]:
        if ancestor.assigned_agent_id == target_agent:
            return False, "cycle"
    return True, None
```

`target_agent` is always present (required per §2.2.1) → check is deterministic. Immediate self A→A allowed; indirect cycle A→B→A rejected; depth cap as belt-and-braces guard.

### 2.6 Failure / timeout bubbling and the `finalize_task` helper

Today three code paths transition a task to a terminal state — REST `POST /tasks/{id}/report` (`hub/main.py:471`), WS `result` (`hub/main.py:562`), and timeout watcher `scan_timeouts_once()` (`hub/main.py:383`). v2 introduces a single helper they all call (codex #5):

```python
async def finalize_task(task_id, status, result, logs=None) -> None:
    """The single terminal-state writer.
    1. queue.update_status(task_id, status, result)
    2. db.update_task(...)
    3. emit task_completed/task_updated SSE via task_event_data() (§3.3)
    4. _notify_parent_of_child(task_id) if this task has a parent
    5. unmark agent busy (existing path)"""
```

Every existing `queue.update_status(..., terminal_status, ...)` call is replaced by `await finalize_task(...)`. **Only `finalize_task` calls `_notify_parent_of_child()`.**

Authoritative table:

| Event                                       | Path                                                  | Parent task | child_completed |
|---------------------------------------------|-------------------------------------------------------|-------------|-----------------|
| Child completes (WS `result`)               | `handle_agent_message:result` → `finalize_task`        | unchanged   | yes             |
| Child fails (WS `result` status=failed)     | `handle_agent_message:result` → `finalize_task`        | unchanged   | yes (failed)    |
| Child fails via REST `/report`              | `report_task` → `finalize_task`                        | unchanged   | yes             |
| Child times out                             | `scan_timeouts_once` → `finalize_task`                 | unchanged   | yes (timeout)   |
| Child agent disconnects mid-run             | Existing requeue (FR-CON-6) — not terminal             | unchanged   | no              |
| Parent terminates while child running       | Parent's own `finalize_task`                           | per existing| no              |
| Parent WS disconnects while child running   | Parent requeues (FR-CON-6); child finishes later → drop + activity log | requeued | dropped (logged) |

`_notify_parent_of_child(child_task)` (called from `finalize_task` when `child_task.parent_task_id` is set):

```python
async def _notify_parent_of_child(child_task: Task) -> None:
    parent_id = _parent_of_child.pop(child_task.task_id, None)
    if parent_id is None: return
    siblings = _children_by_parent.get(parent_id)
    if siblings is not None:
        siblings.discard(child_task.task_id)
        if not siblings: _children_by_parent.pop(parent_id, None)
    parent = queue.get(parent_id)
    if parent is None or parent.status not in (TaskStatus.ASSIGNED, TaskStatus.RUNNING):
        db.log_activity("task", child_task.task_id, "child_completed_dropped",
                        {"parent_task_id": parent_id, "reason": "parent_terminal"})
        return
    pa = parent.assigned_agent_id
    if pa is None or pa not in manager.connections:
        db.log_activity("task", child_task.task_id, "child_completed_dropped",
                        {"parent_task_id": parent_id, "reason": "parent_disconnected"})
        return
    await manager.send(pa, {"type":"child_completed","parent_task_id":parent_id,
                            "child_task_id":child_task.task_id,
                            "status":child_task.status.value,"result":child_task.result})
```

Rule of thumb: **the hub never touches the parent's task state because of a child event.** The parent agent decides.

### 2.7 Permission model

Concrete:
- WS register stores the API key on `ConnectionManager.session_keys`.
- `submit_child` revalidates that key via `access_control.validate_key` on every call (no caching).
- `access_control.can_assign_task(key, target_agent, requester="agent")` — exact-target check (codex #6, since `target_agent` is required). `is_admin` bypasses `allowed_agents` as today.
- `submitted_by = "agent:<parent_agent_id>"` on the child task.
- Activity log: `db.log_activity("task", child_id, "delegated_by_agent", {"parent_task_id":…,"parent_agent_id":…})`.

No new permission flag — `can_assign_tasks` is the gate (codex #11).

---

## 3. F2 — `parent_task_id` honored

### 3.1 Schema change

```sql
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
```

Appended to `init_db()`. Migration-safe, downgrade-safe. No `ALTER TABLE`, no data movement, no schema-version bump. No activity-log entry on creation (codex #16).

### 3.2 REST change

`GET /tasks` gains two new optional params:
- `parent_task_id=<uuid>` — exact equal match, **no magic strings** (codex #8). Invalid UUID → 422 (existing FastAPI validator).
- `top_level=true` — filters `WHERE parent_task_id IS NULL`. `top_level=false` is a no-op.
- Mutually exclusive: supplying both → 400 with `"top_level cannot be combined with parent_task_id"`.
- AND-combined with existing `status` and `agent_id`. Empty / missing → no filter.

Touches `hub/main.py:list_tasks`, `hub/queue.py:TaskQueue.list_all`, `hub/database.py:list_tasks`. Auth gate unchanged: `can_view_tasks`.

### 3.3 SSE change — single `task_event_data` helper

Per codex #7, `emit_event` is generic and callsites build payloads manually → partial coverage is the failure mode. v2 introduces:

```python
def task_event_data(task_id: str, **extra: Any) -> dict:
    """Canonical SSE payload builder for task_* events.
    Always includes 'task_id' and 'parent_task_id' (null for top-level)."""
    task = queue.get(task_id)
    parent_task_id = (task.parent_task_id if task is not None
                      else db.get_parent_task_id(task_id))
    return {"task_id": task_id, "parent_task_id": parent_task_id, **extra}
```

**Every** `emit_event("task_*", {...})` callsite is rewritten to use it. Concrete callsites in current `hub/main.py`:

| Callsite                          | Event           | New form |
|-----------------------------------|-----------------|----------|
| `:315` route assignment           | task_updated    | `task_event_data(task_id, status=…, agent_id=…)` |
| `:376` timeout requeue            | task_updated    | `task_event_data(task.task_id, status="queued")` |
| `:383` timeout terminal           | task_updated    | via `finalize_task` |
| `:471/472` REST `/report`         | task_completed/updated | via `finalize_task` |
| `:550` WS `status`                | task_updated    | `task_event_data(task_id, status=…)` |
| `:562` WS `result` terminal       | task_completed  | via `finalize_task` |
| `:571` WS `log`                   | task_updated    | `task_event_data(task_id, log=…)` |
| `:600` REST `POST /tasks`         | task_created    | `task_event_data(task_id, task_type=…)` |
| (new) submit_child accepted       | task_created    | `task_event_data(child_id, task_type=…, submitted_by=…)` |

Resulting shapes (always present, `null` for top-level):

```jsonc
{"type":"task_created","data":{"task_id":"…","parent_task_id":null,"task_type":"plan"}}
{"type":"task_updated","data":{"task_id":"…","parent_task_id":"…","status":"running","agent_id":"…"}}
{"type":"task_completed","data":{"task_id":"…","parent_task_id":"…","status":"completed","result":{…}}}
```

Subscribers that ignore unknown keys (standard practice) are unaffected.

### 3.4 Migration

`IF NOT EXISTS` runs on every `init_db()`. First call against an existing DB creates the index over current rows in O(N log N) — sub-second at expected volumes. No data movement. No activity-log entry (codex #16).

### 3.5 Documented semantics (must appear in `summary.md` follow-up)

> `parent_task_id` is purely a back-pointer for query and observability.
> Phase 2 implements no cascade behavior:
> - Completing a parent does not cancel or mark complete its children.
> - Completing a child does not modify the parent.
> - Failing/timing out a parent does not affect children.
> - Failing/timing out a child does not affect the parent.
>
> Agents that delegate via F1 are responsible for waiting on and
> interpreting child results themselves.

---

## 4. Module-by-module change list

| File | Modified | New | Purpose |
|------|----------|-----|---------|
| `hub/models.py` | — | `SubmitChildRequest` (Pydantic, internal, validates WS msg shape) | **No new field on `Task`** (codex #4). |
| `hub/database.py` | `init_db()` (+ index); `list_tasks(parent_task_id=None, top_level=False)`; new `get_parent_task_id(task_id)` (cheap fallback for `task_event_data`) | — | F2 index + filter. |
| `hub/queue.py` | `TaskQueue.list_all(parent_task_id=None, top_level=False)` | — | F2 filter. |
| `hub/delegation.py` (new) | — | `_children_by_parent`, `_parent_of_child` (guarded by `TaskQueue._lock`); `register_child`, `release_child`; `MAX_TASK_DEPTH`; `_ancestor_chain`; `check_delegation`; `rebuild_from_db()` (called by `load_state`) | F1 child bookkeeping. |
| `hub/router.py` | — | — | Unchanged. F1 uses the existing direct-target dispatch. |
| `hub/auth.py` | — | — | No new flag; `can_assign_tasks` reused. |
| `hub/main.py` | `ConnectionManager` (+ `session_keys`); `websocket_endpoint` (store/clear key); `handle_agent_message` (+ `submit_child` branch; replace inline terminal writes with `finalize_task`); `list_tasks` (+ `parent_task_id`, `top_level` params, mutual-exclusion); `report_task` (call `finalize_task`); `scan_timeouts_once` (call `finalize_task` on timeout terminal); every `emit_event("task_*", …)` callsite uses `task_event_data` | `task_event_data(task_id, **extra)`; `finalize_task(task_id, status, result, logs=None)`; `_handle_submit_child(agent_id, msg)`; `_notify_parent_of_child(child_task)` | F1 wire handler + F2 SSE/REST + central terminal handler. |
| `agent_sdk/client.py` | `AgentHub.__init__` (+ `_send_lock`, `_executor`, pending-wait maps, `_current_task_id` thread-local); `_send_json` (use lock); `_on_message` (route `task` to executor; handle `child_*` inline); `_handle_task` (set/clear `_current_task_id`); `AsyncAgentHub.run` (use `asyncio.create_task`; route `child_*`); `AsyncAgentHub._handle_task` (set/clear ContextVar) | `submit_child(...)` on both classes; `ChildRejectedError`, `ChildWaitTimeout`, `ChildNotInTaskContext`; pending-wait helpers; module-level `current_task_id` ContextVar (async) | F1 SDK helper + reader-loop deadlock fix. |
| `tests/test_websocket.py` | — | 6 new tests | F1 WS coverage. |
| `tests/test_rest.py` | — | 2 new tests | F2 REST/SSE. |
| `tests/test_unit.py` | — | 2 new tests | Cycle helper + `task_event_data`. |
| `tests/test_sdk.py` (new) | — | 1 SDK no-deadlock test | Codex acceptance #5 rigor. |
| `scripts/e2e/run_e2e_delegation.py` (new) | — | F1+F2 combined E2E | Combined E2E. |

---

## 5. Test plan

≥8 new pytest cases (14 listed). Names `test_p2_…`. New IDs: `FR-DEL-1` (submit), `FR-DEL-2` (cycle), `FR-DEL-3` (depth), `FR-DEL-4` (failure bubble), `FR-DEL-5` (permissions), `FR-DEL-6` (parent disconnect), `FR-DEL-7` (immediate self allowed), `FR-DEL-8` (SDK no-deadlock), `FR-PAR-1` (filter), `FR-PAR-2` (SSE field).

| Test | ID | Behavior |
|------|----|---------|
| `test_p2_del_1_submit_child_happy_path` | DEL-1 | submit_child with `target_agent` → `child_accepted` then `child_completed` with result. |
| `test_p2_del_1b_missing_target_rejected` | DEL-1 (neg) | submit_child with no `target_agent` → `child_rejected: bad_request`. |
| `test_p2_del_2_cycle_rejected_indirect` | DEL-2 | A→B→A → `child_rejected: cycle`. |
| `test_p2_del_2b_immediate_self_allowed` | DEL-7 | A→A is **accepted**; child runs and completes. |
| `test_p2_del_3_depth_limit_rejected` | DEL-3 | At chain depth 8, next submit_child → `depth_limit_exceeded`. |
| `test_p2_del_4_child_failure_bubbles_to_parent` | DEL-4 | Child returns `failed` → parent receives `child_completed{status=failed}`; parent's task unchanged. |
| `test_p2_del_4b_child_timeout_bubbles_to_parent` | DEL-4 | Child times out via `scan_timeouts_once` → parent receives `child_completed{status=timeout}` (proves centralized `finalize_task`). |
| `test_p2_del_5_permission_inheritance` | DEL-5 | Without `can_assign_tasks` → `forbidden`; with it → ok; revoking key mid-session blocks the next call. |
| `test_p2_del_6_parent_disconnect_orphans_child_safely` | DEL-6 | Parent WS closes mid-child; child finishes; activity log has `child_completed_dropped`; child row queryable. |
| `test_p2_del_7_not_owner_rejected` | DEL-1 (neg) | Agent B sends submit_child for parent assigned to A → `not_owner`. |
| `test_p2_del_8_sdk_no_deadlock` | DEL-8 | In-process hub + AsyncAgentHub: handler awaits submit_child to a second AsyncAgentHub on the same loop; pings + child_completed flow without deadlock. Mirror with sync `AgentHub` + threadpool. |
| `test_p2_par_1_filter_by_parent_task_id` | PAR-1 | `?parent_task_id=<plan>` returns exactly the children. `?top_level=true` returns top-level. Both → 400. |
| `test_p2_par_2_sse_includes_parent_task_id` | PAR-2 | SSE subscriber sees `parent_task_id` in `task_created/_updated/_completed` (null for top-level, set for children). |
| `test_p2_unit_cycle_helper` | DEL-2 | Unit test of `_ancestor_chain` + `check_delegation`: immediate self allowed, indirect cycle rejected, depth boundary. |
| `test_p2_unit_task_event_data` | PAR-2 | Unit test: `parent_task_id` always present, `null` for orphan, value for child. |

**E2E** — `scripts/e2e/run_e2e_delegation.py`:
1. Start hub + planner + coder + reviewer.
2. Submit one parent `plan` task to the planner with a *human* key.
3. Planner uses `submit_child(target_agent="coder")` then `submit_child(target_agent="reviewer")` (both via SDK), awaits each.
4. Planner returns aggregated result.
5. Driver asserts: parent task `completed` and top-level; both children `completed` with `parent_task_id == plan.task_id`; `GET /tasks?parent_task_id=<plan_id>` returns exactly those two; `GET /tasks?top_level=true` includes the plan and excludes children; SSE replay shows `parent_task_id` populated for child events and `null` for the plan.
6. Same shape as `run_pipeline.py` minus the external driver — proving F1 makes that script redundant per scope.md.

---

## 6. Backward-compatibility statement

**Continues to pass unchanged:** all 82 existing pytest cases (`test_websocket.py` uses only existing message types — `submit_child` is additive; `test_rest.py` — `GET /tasks` without the new params is identical; `POST /tasks` with `parent_task_id` already worked, F2 only adds the index; `test_persistence_security.py` — pure `CREATE INDEX IF NOT EXISTS`, no new column or `Task` field per codex #4; `test_mcp_protocol.py` — MCP untouched; `test_unit.py` — additive only). All 6 E2E scenarios in `scripts/e2e/` (external-driver flow, unaffected by F1, benefits from F2). `examples/echo-agent` and `scripts/e2e/{planner,coder,reviewer}.py` reference agents — unchanged. Phase 1.5 MCP tools — `submit-task` still accepts `parent_task_id` as before.

**Visible additions for clients:**
- SSE `task_*` payloads gain a `parent_task_id` key (always present, `null` for top-level). Standard unknown-key tolerance means existing consumers are unaffected.
- `GET /tasks` accepts new `parent_task_id` (uuid) and `top_level` (bool) params. Existing callers that don't pass them see no change.

**Invisible changes:**
- Async SDK `run()` switches `await self._handle_task(...)` → `asyncio.create_task(self._handle_task(...))`. Behavior identical for handlers that don't call `submit_child` (hub busy-state already prevents concurrent top-level tasks per agent).
- Sync SDK gains a worker-thread pool; `_on_message` no longer executes handlers on the reader thread. Same observable behavior for non-delegating handlers.

---

## 7. Acceptance criteria coverage (vs. scope.md §5–7)

1. **Module-by-module change list** — §4 (paths, modified vs. new functions, JSON shape changes, central helpers).
2. **WS / REST / MCP schemas** — §2.2 (request/response/reject) + §3.2 (REST). Explicit: no MCP delegation endpoint.
3. **Five F1 open questions answered** — §2.1 Q1–Q5.
4. **Cycle detection concrete algorithm** — §2.5 (deterministic because `target_agent` required; immediate self allowed; indirect rejected; depth cap fallback).
5. **Test plan ≥8 cases** — §5 lists 14 covering child-submit happy path, child failure bubbling (incl. timeout via `finalize_task`), cycle rejection, depth limit, permission inheritance, parent_task_id filter + `top_level`, SSE field, F1+F2 E2E, immediate self allowed, SDK no-deadlock, and `task_event_data` unit.
6. **Migration story for the SQLite index** — §3.1 + §3.4 (additive `CREATE INDEX IF NOT EXISTS`).
7. **Backward-compatibility called out** — §6 (all 82 pytest cases and 6 E2E scenarios; SSE/REST changes additive; SDK signatures unchanged).
