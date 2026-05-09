# Phase 2 Design v1

**Author:** designer-v1 (sub-agent)
**Date:** 2026-05-09
**Scope:** F1 (agent-to-agent delegation) + F2 (parent_task_id honored). Per
`design/phase2/scope.md`. Anything else is out of scope.

---

## 1. Summary

**F1 ships:**
- A new WS request/response pair (`submit_child` → `child_accepted` /
  `child_rejected`) plus an unsolicited `child_completed` push, letting an
  agent currently executing a task spawn a child task whose result it later
  receives over the same WebSocket.
- Hub-side child-tracking (parent → list of in-flight child task_ids) and
  ancestor-chain bookkeeping for cycle / depth detection.
- New SDK helper `submit_child(...)` on both `AgentHub` and `AsyncAgentHub`
  that returns the child result (or raises) once the hub pushes
  `child_completed`.
- Cycle prevention: ancestor-chain check + hard depth cap (default `8`).
- Failure bubbling rule: child failure does NOT auto-fail the parent; it is
  delivered to the parent as a normal `child_completed` payload with
  `status=failed/timeout` and the parent agent decides.

**F2 ships:**
- Additive SQLite index on `tasks(parent_task_id)`.
- `GET /tasks?parent_task_id=...` filter (combinable with existing `status`
  / `agent_id` filters).
- `parent_task_id` field added to `task_created`, `task_updated`,
  `task_completed` SSE payloads.
- Documented semantics: parent termination does NOT cascade to children;
  child termination does NOT auto-complete the parent. Phase 3 territory.

**Out of scope (explicit):** DAGs, fan-out/fan-in joins, parent
cascade-cancel, MCP `submit-child` tool (delegation is agent-only),
streaming partial child results beyond the existing `log` push, capability
negotiation, federation.

---

## 2. F1 — Agent-to-agent delegation

### 2.1 Design questions answered (from scope.md §F1)

**Q1. How does an agent submit a child task — WS message? REST? SDK?**
**A.** A new WebSocket message type `submit_child` over the agent's existing
`/ws` connection. Reasons:
- Keeps the request on the same authenticated channel that already
  carries `task` and `result` — no extra REST round-trip, no second key
  to manage.
- Hub already correlates this WS to the parent's `agent_id`, so it can
  derive `submitted_by = agent:<parent_agent_id>` and parent context for
  free.
- Avoids the need for a new REST endpoint with a new auth surface.
- Backward-compat: agents that never send `submit_child` see no change.

REST is **not** added for this. The MCP server is **not** extended (MCP is
the human/orchestrator surface, not the agent surface — agents use WS/SDK).

**Q2. How does the parent block on / await the child without breaking
heartbeat?**
**A.** The parent agent does **not** block its WebSocket reader. The SDK
implements `submit_child` with an `asyncio.Future` (or threading `Event`
in sync mode) keyed on the child task_id. The WS reader loop continues to
service `ping`, `task` (other tasks would only arrive if this agent is
re-marked idle, which it is not while busy), and the new `child_accepted`
/ `child_rejected` / `child_completed` frames. When `child_completed`
arrives, the SDK resolves the future. Heartbeats continue from the SDK's
periodic background task (or via the hub's `ping` → SDK `pong` flow that
already exists). No timer or sleep blocks the reader.

The parent task's hub-side state is set to a new pseudo-status
`waiting_for_child` (see §2.4) only at the *registry* level for the
parent's agent — its `Task.status` stays `running`, but the registry
status of the parent agent stays `BUSY` so the dispatcher won't push it
another top-level task. A flag `Task.blocked_on: list[str]` (in-memory
only, not persisted in v1) tracks which children it is waiting for.

**Q3. What permissions does a child task inherit?**
**A.** The child is authorized by **the parent agent's API key** (the
`auth_token` used at WS register time). Specifically:
- The hub looks up the parent agent's session permission (already cached
  on the WS connection — see §2.3) and runs it through
  `access_control.can_assign_task(auth_key, target_agent, requester="agent")`.
- A new permission flag `can_delegate` is **NOT** added in v1. We reuse
  `can_assign_tasks`. Justification: the gate that controls "can submit a
  task" is the right gate for "can submit a child task." The original
  human submitter's key is *not* re-checked — once delegated, the
  parent agent owns the call.
- `submitted_by` on the child task is set to `"agent:<parent_agent_id>"`
  so audit logs and queries can distinguish agent-originated work.

**Q4. What happens if the child fails or times out?**
**A.** The hub sends `child_completed` to the parent's WS with the child's
final `status` (`completed`, `failed`, or `timeout`) and `result`. The hub
does **not** mutate the parent's status. The parent agent's task handler
is responsible for deciding whether to fail-up, retry, or proceed. If the
parent agent's WS disconnects while a child is still in flight, the
parent's normal disconnect handling fires (FR-CON-6 requeue), the child
continues to run independently, and on requeue/retry the new parent
incarnation will not see the orphaned child's result — it must
re-delegate. (Documented limitation; Phase 3 may add child cancellation.)

If the parent itself times out (FR-EXE-5) before the child completes,
parent transitions to `timeout` per existing policy. The orphaned child
keeps running; on completion the hub sees `parent_task_id` points at a
terminal task and emits a `child_completed` event that has nowhere to go
— it is dropped after a single warning to the activity log. Child result
is still persisted and retrievable via `GET /tasks/{child_id}`.

**Q5. Cycle detection.**
**A.** Algorithm in §2.5. Two complementary checks:
1. Ancestor-chain walk: starting from `parent_task_id` of the proposed
   child, walk up the chain following `parent_task_id`. If the proposed
   `target_agent` (or any agent in the matched-capability set if no
   target) appears as an `assigned_agent_id` in any ancestor task, reject
   with `cycle`. O(depth).
2. Hard depth cap: default `AGENT_HUB_MAX_TASK_DEPTH = 8`. Reject with
   `depth_limit_exceeded` once depth would reach 9. Belt-and-braces
   safety so even if cycle detection is bypassed (e.g. capability-routed
   target choice that lands on a different agent each time), the chain
   cannot recurse forever.

### 2.2 Wire protocol changes

#### 2.2.1 Agent → Hub: new message type `submit_child`

```json
{
  "type": "submit_child",
  "request_id": "client-generated-uuid-v4",
  "parent_task_id": "<task_id this agent is currently executing>",
  "task": "look up the asyncio.gather signature",
  "task_type": "docs-lookup",
  "payload": {"symbol": "asyncio.gather"},
  "target_agent": "docs-agent-1",
  "priority": 0,
  "timeout": 60
}
```

Required fields: `type`, `request_id`, `parent_task_id`. Either `task`
or `payload` must be non-empty (mirrors `POST /tasks`). All other fields
optional with the same defaults as `TaskSubmitRequest`. The `request_id`
is the agent-side correlation ID; it is echoed in `child_accepted` /
`child_rejected` so the SDK can match the response without race.

**Validation order (hub side):**
1. Parent task exists and `assigned_agent_id == this_ws_agent_id`. Else
   `child_rejected: not_owner`.
2. Parent task status is `assigned` or `running`. Else
   `child_rejected: parent_terminal`.
3. Permission check via parent agent's session key against
   `can_assign_task(target_agent)`. Else `child_rejected: forbidden`.
4. Cycle / depth check (§2.5). Else `child_rejected: cycle` /
   `depth_limit_exceeded`.
5. Create + route + dispatch the child like any other task.

#### 2.2.2 Hub → Agent: `child_accepted`

```json
{
  "type": "child_accepted",
  "request_id": "<echoed>",
  "child_task_id": "<new uuid>",
  "status": "queued"  // or "assigned"
}
```

#### 2.2.3 Hub → Agent: `child_rejected`

```json
{
  "type": "child_rejected",
  "request_id": "<echoed>",
  "reason": "cycle",   // one of: not_owner, parent_terminal, forbidden,
                       //         cycle, depth_limit_exceeded, bad_request
  "message": "human-readable detail"
}
```

#### 2.2.4 Hub → Agent: `child_completed` (unsolicited)

```json
{
  "type": "child_completed",
  "parent_task_id": "<parent>",
  "child_task_id": "<child>",
  "status": "completed",   // or "failed", "timeout"
  "result": {...}          // may be null on failure with no payload
}
```

This is pushed when the child reaches a terminal status AND the parent's
WebSocket is still connected AND the parent task is still in
`assigned`/`running`. Otherwise dropped (with `db.log_activity` entry
`"child_completed_dropped"`).

#### 2.2.5 No new REST endpoints

Children show up in the existing `GET /tasks/{id}` and `GET /tasks` APIs.
The new `parent_task_id` filter (§3) makes them queryable. No
`POST /tasks/{id}/children` etc. — keeps the surface small.

#### 2.2.6 SDK additions

New methods on both `AgentHub` (sync) and `AsyncAgentHub` (async). Sync:

```python
def submit_child(
    self,
    *,
    task: str = "",
    task_type: str | None = None,
    payload: dict | None = None,
    target_agent: str | None = None,
    timeout: int = 300,
    priority: int = 0,
    wait_timeout: float | None = None,   # SDK-side wait, default = task timeout + 30s
) -> dict:  # returns {"status": "...", "result": {...}, "child_task_id": "..."}
    ...
```

Async signature is identical with `await`. Internally:
1. Resolve current `parent_task_id` from a context var set by the SDK
   when it dispatched a `task` to the user handler (the SDK already
   knows the current task because `_handle_task` owns it).
2. Generate `request_id`, send `submit_child`, store a `Future`/`Event`
   keyed by `request_id` and (after `child_accepted`) by `child_task_id`.
3. The WS reader resolves the future on `child_completed` or raises
   `ChildRejectedError(reason)` on `child_rejected`.
4. If `wait_timeout` elapses, raises `ChildWaitTimeout` (SDK-side only;
   the hub's own timeout still applies independently).

**Backward-compat statement.** No existing SDK method changes signature.
Existing agents that never call `submit_child` get no new dependencies and
no behavior change. The new WS message types are additive; the hub
ignores `submit_child` if the agent's parent task is missing
(`child_rejected: not_owner`), so a stale agent that re-sends
`submit_child` does not crash the hub.

### 2.3 Hub-side state

Two new in-memory structures (in `hub/queue.py` or a small new
`hub/delegation.py` — see §4):

```python
# parent_task_id -> set of in-flight child task_ids
self._children_by_parent: dict[str, set[str]] = {}

# child_task_id -> parent_task_id  (reverse index, O(1) on completion)
self._parent_of_child: dict[str, str] = {}
```

Both guarded by the existing `TaskQueue._lock` (RLock). No new lock is
introduced — all delegation bookkeeping mutates the queue under the same
lock that already serializes task state, eliminating a class of
parent/child race conditions.

A third per-WS structure on `ConnectionManager`:

```python
# ws_agent_id -> the API key used at register time
# (already implicit but currently not stored — required for permission
#  re-validation on submit_child)
self.session_keys: dict[str, str] = {}
```

Set on successful WS registration, cleared on disconnect. Re-validated
through `access_control.validate_key` on every `submit_child` so
revocation takes effect immediately (mirrors FR-MCP-4 pattern).

**Persistence:** `_children_by_parent` and `_parent_of_child` are
**rebuilt on hub restart** in `load_state()` by scanning persisted tasks
where `parent_task_id IS NOT NULL` AND `status NOT IN ('completed',
'failed', 'timeout')`. No new schema column; F2's index makes the scan
fast.

### 2.4 Concurrency model

- Single lock: `TaskQueue._lock` (existing RLock).
- All reads/writes of `_children_by_parent`, `_parent_of_child`, and the
  parent's `blocked_on` field happen inside that lock.
- The push side (hub → parent's WS via `manager.send`) happens
  **outside** the lock to avoid awaiting on the network while holding it.
- `child_completed` push semantics: snapshot-then-emit. The hub takes
  the lock, removes the child from `_children_by_parent[parent_id]`,
  reads the parent agent_id, then drops the lock and sends. If the send
  fails (parent WS gone), nothing is rolled back — the child result is
  still persisted and queryable.

### 2.5 Cycle detection algorithm

```python
MAX_TASK_DEPTH = int(os.getenv("AGENT_HUB_MAX_TASK_DEPTH", "8"))

def _ancestor_chain(task_id: str) -> list[Task]:
    """Walk parent_task_id pointers up to root or MAX_TASK_DEPTH+1.

    O(depth). Stops at first None or once depth exceeds the cap.
    """
    chain = []
    current_id = task_id
    while current_id is not None and len(chain) <= MAX_TASK_DEPTH + 1:
        task = queue.get(current_id) or db.get_task(current_id)
        if task is None:
            break
        chain.append(task)
        current_id = (task.parent_task_id if hasattr(task, "parent_task_id")
                      else task.get("parent_task_id"))
    return chain

def check_delegation(parent_task_id: str,
                     proposed_target_agent: str | None,
                     proposed_task_type: str) -> tuple[bool, str | None]:
    chain = _ancestor_chain(parent_task_id)
    # Depth: chain length INCLUDES the parent. New child would be chain+1.
    if len(chain) >= MAX_TASK_DEPTH:
        return False, "depth_limit_exceeded"
    # Cycle: target agent appears as assigned_agent_id of any ancestor.
    # If no explicit target, we cannot pre-check by capability (router
    # may pick anyone) — so we skip the cycle check in that case and
    # rely on depth cap. This is a deliberate v1 trade-off, called out
    # in open questions.
    if proposed_target_agent:
        for ancestor in chain:
            if ancestor.assigned_agent_id == proposed_target_agent:
                return False, "cycle"
    return True, None
```

Default cap of `8` is concrete. Override via env. Picked because the only
real workflow today is plan → code → review (depth 3), so 8 leaves
plenty of headroom while preventing pathological recursion.

### 2.6 Failure / timeout bubbling

Authoritative table:

| Event                                         | Hub action                                    | Parent task status | child_completed sent? |
|-----------------------------------------------|-----------------------------------------------|--------------------|-----------------------|
| Child completes                               | Mark child completed; persist                 | unchanged          | yes                   |
| Child fails                                   | Mark child failed; persist                    | unchanged          | yes (status=failed)   |
| Child times out                               | Mark child timeout (existing path)            | unchanged          | yes (status=timeout)  |
| Child agent disconnects mid-run               | Existing requeue logic (FR-CON-6)             | unchanged          | no (child not terminal yet) |
| Parent terminates while child still running   | Existing parent termination path              | per existing       | event becomes a no-op when child later completes |
| Parent WS disconnects while child running     | Parent task requeues (FR-CON-6); child keeps going | requeued      | dropped on later completion (logged) |

Rule of thumb: **the hub never touches the parent's task state because of
a child event.** The parent agent decides.

### 2.7 Permission model

Concrete:
- WS register stores the API key on `ConnectionManager.session_keys`.
- `submit_child` revalidates that key via `access_control.validate_key`
  on every call (no caching of the Permission object).
- The check is `access_control.can_assign_task(key, target_agent,
  requester="agent")` — the existing function already supports a
  `requester` arg; we reuse it. `is_admin` keys bypass `allowed_agents`
  as today.
- `submitted_by` on the child task = `"agent:<parent_agent_id>"`.
- Activity log entry: `db.log_activity("task", child_id,
  "delegated_by_agent", {"parent_task_id": ..., "parent_agent_id": ...})`.

**No new permission flag.** `can_assign_tasks` is the gate. If an
operator wants an agent that can be tasked but cannot delegate, they
issue an API key with `can_assign_tasks=False` (the agent can still run
work delivered to it because that path uses `can_register`, not
`can_assign_tasks`).

---

## 3. F2 — `parent_task_id` honored

### 3.1 Schema change

Single additive index. Added at the end of `init_db()`:

```sql
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
```

`IF NOT EXISTS` makes it migration-safe — running against an existing DB
just creates the index in place. No data migration. No column changes.
Downgrade-safe (an older binary ignores the extra index).

### 3.2 REST change

`GET /tasks` gains a third optional filter:

```
GET /tasks?status=...&agent_id=...&parent_task_id=...
```

All three filters are AND-combined. Empty / missing means no filter.
`parent_task_id` is matched exact-equal against the column (no LIKE, no
wildcard). Special value `parent_task_id=__none__` matches rows where
`parent_task_id IS NULL` (top-level tasks). This is the only "magic"
string and is documented in the OpenAPI description.

Implementation touches:
- `hub/main.py: list_tasks(...)` — add the kwarg, forward to queue.
- `hub/queue.py: TaskQueue.list_all(...)` — add the kwarg, filter
  in-memory list (preserves existing pagination-free behavior).
- `hub/database.py: list_tasks(...)` — add the kwarg, build SQL with
  the new clause and use `idx_tasks_parent`.

Auth gate unchanged: `can_view_tasks`.

### 3.3 SSE change

`emit_event(...)` payloads gain `parent_task_id` for the three task
events. Exact additions:

```jsonc
// task_created
{"type":"task_created","data":{"task_id":"...","task_type":"...",
                               "parent_task_id":"<id>" | null}}

// task_updated
{"type":"task_updated","data":{"task_id":"...","status":"...",
                               "agent_id":"..." | null,
                               "parent_task_id":"<id>" | null,
                               "log":"<log line>" | null}}

// task_completed
{"type":"task_completed","data":{"task_id":"...","status":"completed",
                                 "result":{...},
                                 "parent_task_id":"<id>" | null}}
```

`parent_task_id` is always present in the payload (value `null` for
top-level tasks), so subscribers don't have to write `data.get(...)`
defensively. Existing fields are unchanged. Events that don't refer to a
task (`agent_*`) are unchanged.

The implementation lifts `parent_task_id` from `queue.get(task_id)` at
emit time; if the task has been evicted by then (shouldn't happen in
v1 — no eviction), we fall back to `null`.

### 3.4 Migration

- The `IF NOT EXISTS` index DDL runs on every `init_db()` call (already
  the existing pattern). First call against an existing DB creates the
  index over current rows (SQLite handles this in O(N log N) over the
  task table; the target deployment is hundreds of tasks/day, so this
  is < 1 second).
- No `ALTER TABLE`. No data movement. No schema-version bump.
- A `db.log_activity("system", "phase2_index", "applied", {})` entry is
  written once after `init_db()` if the index was newly created
  (detected by `cursor.execute("PRAGMA index_list('tasks')")`). Skipped
  on subsequent boots. (Optional — nice-to-have for ops visibility.)

### 3.5 Documented semantics (must appear in `summary.md` follow-up)

> When parent_task_id is set, it is purely a back-pointer for query and
> observability. Phase 2 does not implement cascade behavior:
> - Completing a parent task does not cancel or mark complete its
>   children.
> - Completing a child task does not modify the parent.
> - Failing or timing out a parent does not affect children.
> - Failing or timing out a child does not affect the parent.
>
> Agents that delegate via F1 are responsible for waiting on and
> interpreting child results themselves.

---

## 4. Module-by-module change list

| File | Modified | New | Purpose |
|------|----------|-----|---------|
| `hub/models.py` | `Task` (+ `blocked_on: list[str] = []` in-mem only) | `SubmitChildRequest` (Pydantic, used internally for WS msg validation) | Track which children a parent waits on; validate WS submit_child shape. |
| `hub/database.py` | `init_db()` (+ `idx_tasks_parent`); `list_tasks(parent_task_id=None)` | `_index_present(name)` helper | F2 index + filter. |
| `hub/queue.py` | `TaskQueue.list_all(parent_task_id=None)`; `_children_by_parent`, `_parent_of_child`; `register_child(parent_id, child_id)`, `release_child(child_id) -> parent_id | None` | — | F1 child bookkeeping under the existing lock. |
| `hub/router.py` | — | — | No changes. F1 routing reuses `Router.route`. |
| `hub/auth.py` | — | — | No new permission flag. `can_assign_task` reused. |
| `hub/main.py` | `ConnectionManager` (+ `session_keys: dict[str,str]`); `websocket_endpoint` (store key on connect, drop on disconnect); `handle_agent_message` (+ `submit_child` branch); `list_tasks` (+ `parent_task_id` query param); `emit_event` callsites for `task_created` / `task_updated` / `task_completed` (+ `parent_task_id` in payload); `report_task` and `handle_agent_message:result` (after persisting child terminal status, call `_notify_parent_of_child`) | `_handle_submit_child(agent_id, msg)` — validate, route, dispatch, ack; `_notify_parent_of_child(child_task: Task)` — push `child_completed` to parent's WS; `_check_delegation(parent_task_id, target_agent)` — cycle/depth check; `MAX_TASK_DEPTH` constant from env | F1 wire handler + F2 SSE/REST. |
| `agent_sdk/client.py` | `AgentHub._on_message` (+ branches for `child_accepted`/`child_rejected`/`child_completed`); `AsyncAgentHub.run` ditto; both `_handle_task` set/clear a `_current_task_id` context var | `submit_child(...)` on both classes; `ChildRejectedError`, `ChildWaitTimeout` exceptions; internal `_pending_children: dict[str, Future]` | F1 SDK helper. |
| `tests/test_websocket.py` | — | 5+ new tests (see §5) | F1 WS coverage. |
| `tests/test_rest.py` | — | 2+ new tests (`parent_task_id` filter, SSE field) | F2 REST/SSE coverage. |
| `tests/test_unit.py` | — | 1 new test (cycle/depth helper) | Pure unit. |
| `scripts/e2e/` | — | `run_e2e_delegation.py` — F1+F2 combined E2E | Combined E2E. |

---

## 5. Test plan

At least 8 new pytest cases. Names use snake_case prefixed `test_p2_…`.
Map to acceptance criteria via FR / NFR ids; new IDs introduced are:
`FR-DEL-1` (delegation submit), `FR-DEL-2` (cycle reject), `FR-DEL-3`
(depth reject), `FR-DEL-4` (child failure bubble), `FR-DEL-5`
(permission inheritance), `FR-DEL-6` (parent disconnect handling),
`FR-PAR-1` (parent_task_id filter), `FR-PAR-2` (SSE parent_task_id
field).

| Test name | ID | One-line behavior |
|-----------|----|-------------------|
| `test_p2_del_1_submit_child_happy_path` | FR-DEL-1 | Parent sends `submit_child`; receives `child_accepted` then `child_completed` with the child's result. |
| `test_p2_del_2_cycle_rejected` | FR-DEL-2 | Parent A → child targeting agent already in ancestor chain returns `child_rejected: cycle`; no child task created. |
| `test_p2_del_3_depth_limit_rejected` | FR-DEL-3 | At chain depth 8, next `submit_child` returns `child_rejected: depth_limit_exceeded`. |
| `test_p2_del_4_child_failure_bubbles_to_parent` | FR-DEL-4 | Child returns `failed`; parent receives `child_completed` with `status=failed`; parent's own task status remains agent-controlled (still `running` until parent decides). |
| `test_p2_del_5_permission_inheritance` | FR-DEL-5 | Agent registered with a key lacking `can_assign_tasks` cannot `submit_child` (`forbidden`). With it, it can. Revoking key mid-session blocks the next `submit_child`. |
| `test_p2_del_6_parent_disconnect_orphans_child_safely` | FR-DEL-6 | Parent WS closes mid-child; child finishes; hub logs `child_completed_dropped`; child's row in DB is `completed` and queryable. |
| `test_p2_del_7_not_owner_rejected` | FR-DEL-1 (neg) | Agent B sends `submit_child` referencing a parent assigned to A → `child_rejected: not_owner`. |
| `test_p2_par_1_filter_by_parent_task_id` | FR-PAR-1 | `GET /tasks?parent_task_id=<plan>` returns exactly the children of that plan; `parent_task_id=__none__` returns top-level tasks. |
| `test_p2_par_2_sse_includes_parent_task_id` | FR-PAR-2 | SSE subscriber sees `parent_task_id` in `task_created`, `task_updated`, `task_completed` payloads (null for top-level, set for children). |
| `test_p2_unit_cycle_helper` | FR-DEL-2 | Unit test of `_ancestor_chain` + `_check_delegation` against a synthetic chain. |

**E2E scenario combining F1 + F2** — `scripts/e2e/run_e2e_delegation.py`:

1. Start hub + planner + coder + reviewer (existing harness).
2. Submit one parent `plan` task to the planner using a *human* key.
3. Planner uses `submit_child` (via SDK) to create `code` child; awaits
   completion, then submits `review` child; awaits.
4. Planner returns its aggregated result.
5. Driver asserts:
   - Parent task `completed`, top-level (parent_task_id = null).
   - Both children `completed`, `parent_task_id == plan.task_id`.
   - `GET /tasks?parent_task_id=<plan_id>` returns exactly those two
     children.
   - SSE replay (subscribed before submit) included `parent_task_id` for
     all three task events on the children.
6. This is the same shape as `run_pipeline.py` but without the external
   driver — proving F1 makes that script redundant per scope.md.

---

## 6. Backward-compatibility statement

Must continue to pass unchanged:

- All 82 existing pytest cases in `tests/`. Specifically:
  - `tests/test_websocket.py` — every test uses only existing message
    types; the new `submit_child` branch is additive.
  - `tests/test_rest.py` — `GET /tasks` without `parent_task_id` behaves
    identically. `POST /tasks` with `parent_task_id` already worked
    (Phase 1 stored it, F2 now also indexes it — same behavior from the
    client's view).
  - `tests/test_persistence_security.py` — schema change is pure
    `CREATE INDEX IF NOT EXISTS`, no column added, `init_db()` is
    idempotent.
  - `tests/test_mcp_protocol.py` — MCP not touched.
  - `tests/test_unit.py` — modules touched (`queue.py`, `models.py`)
    keep their existing public surface; only additive members.
- All 6 E2E scenarios in `scripts/e2e/` — `run_pipeline.py` (E2E-2 / -3),
  `run_e2e4_review_loop.py`, `run_e2e5_capability_change.py`,
  `run_e2e7_concurrent.py`. These use the **external driver** flow,
  which is not affected by F1 (agent-side feature) and benefits from F2
  (extra indexed column + SSE field, both backward-compat).
- The `examples/echo-agent` and `scripts/e2e/{planner,coder,reviewer}.py`
  reference agents — unchanged. Planner/coder/reviewer can OPTIONALLY be
  rewritten to use `submit_child`, but the existing external-driver
  flow keeps working.
- The Phase 1.5 MCP tools — no shape change. `submit-task` continues to
  accept `parent_task_id` as before.

What changes (visibly to clients):

- `task_created` / `task_updated` / `task_completed` SSE payloads gain a
  `parent_task_id` key (always present, `null` for top-level). SSE
  consumers that ignore unknown keys (the standard practice) are
  unaffected. This is a documented additive change.
- `GET /tasks` accepts a new `parent_task_id` query param. Existing
  callers that don't pass it see no change.

---

## 7. Open questions for review (codex)

1. **Cycle check without explicit target.** Router picks the agent at
   dispatch, after our cycle check. We lean on the depth cap. Accept,
   or delay cycle check to dispatch (needs provisional accept + later
   reject)?
2. **`blocked_on` not persisted.** If hub crashes mid-child, parent
   requeues (FR-CON-6) and loses the orphan link. v1-acceptable since
   the parent re-delegates on retry. Flag.
3. **`__none__` magic string** for "no parent" filter vs. a second
   `top_level=true` param. Happy to flip.
4. **`MAX_TASK_DEPTH = 8`** — 5x headroom over plan→code→review.
   Bigger, smaller, dynamic-per-key?
5. **Reuse `can_assign_tasks` vs. new `can_delegate`.** Picked reuse
   for surface minimalism. Override?
6. **Orphaned-parent `child_completed`** dropped with activity log.
   Alternative: deliver via SSE to `submitted_by` (out of scope per
   "no partial streaming"). Flag.
7. **SDK `_current_task_id`** via context var (async) /
   `threading.local` (sync) vs. requiring the agent to pass
   `parent_task_id` explicitly. Confirm.
8. **`submit_child` auth at call-time** adds one DB hit per delegate
   for revocation safety. Acceptable at expected volumes; flag.
