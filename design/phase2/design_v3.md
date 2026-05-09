# Phase 2 Design v3

**Author:** designer-v3 (sub-agent)
**Date:** 2026-05-09
**Scope:** F1 (agent-to-agent delegation) + F2 (`parent_task_id` honored). Per
`design/phase2/scope.md`. Anything else is out of scope.

---

## 0. Diff vs v2 (this revision only)

Only one codex v2 finding remained open (#2: A→A is allowed by cycle check but
the existing direct-target router rejects it because the parent's registry
status is `BUSY`, so the child can never be dispatched). v3 fixes exactly that.
Everything else from v2 is preserved verbatim or with minimal touch-ups.

Concrete changes:

1. **§2.2.1 — `_handle_submit_child` self-child fast path.** When
   `target_agent == this_ws_agent_id` (the parent agent), skip
   `Router.route()` entirely. Directly call `queue.assign(child_id,
   parent_agent_id)`, persist `assigned_agent_id`, and `manager.send` the
   `task` payload over the parent agent's existing WS — even though the
   registry status is `BUSY`. Do **not** flip the registry to BUSY again
   (it is already BUSY) and do **not** set it back to IDLE. The existing
   direct-target dispatch path (`route_and_dispatch`) is unchanged for
   non-self targets.
2. **§2.5 — Cycle check unchanged.** Algorithm in v2 is correct (immediate
   self allowed, indirect cycle rejected, depth cap). v3 only adds a note
   that the self-child fast path in §2.2 is what makes the "allow"
   decision actually executable.
3. **§2.6 — Multi-task idle accounting in `finalize_task`.** Today, both
   the WS `result` path (`hub/main.py:560-563`) and the REST `/report`
   path (`:465-467`) flip the assigned agent to `IDLE` and call
   `dispatch_pending_for_agent` unconditionally on any terminal child
   status. v3 moves the BUSY→IDLE flip *and* the
   `dispatch_pending_for_agent` call inside `finalize_task`, gated by:

   > After persisting the terminal status, if `queue.list_by_agent(agent_id)`
   > contains zero tasks in `(ASSIGNED, RUNNING)`, then mark the agent
   > IDLE in registry + DB and call `dispatch_pending_for_agent`.
   > Otherwise leave it BUSY and do NOT dispatch.

   The §2.6 authoritative table is updated to reflect this (new
   "self-child completes while parent still running" row, and the
   "agent idle?" column is added).
4. **§5 — Two new test cases**:
   - `test_p2_del_2c_self_delegation_completes` — A submits child to A;
     child runs over A's WS while A's parent task is RUNNING; child
     completes; parent receives `child_completed`; parent then completes;
     both rows terminal. Asserts the self-child fast path actually
     dispatches without waiting for an IDLE flip that would never come.
   - `test_p2_del_2d_self_child_does_not_release_parent_for_other_dispatch` —
     A is BUSY running parent + a self-child; a queued top-level task
     matching A's capabilities is created. When the self-child terminates,
     A stays BUSY (parent still owns one ASSIGNED/RUNNING row in
     `list_by_agent`), and `dispatch_pending_for_agent` is NOT called for
     A. The queued task is dispatched only after the parent itself
     terminates.
5. **§4 module list — `finalize_task` and `_handle_submit_child` rows
   updated** to call out the self-fast-path branch and the agent-idle
   gating rule. No new files beyond v2 (`hub/delegation.py` still hosts
   the in-memory maps and cycle helpers).
6. **Cross-reference to Phase 1 dispatch-overflow bugfix.** The
   `claude_bugfix-request_dispatch-overflow_20260508-2304.md` /
   `codex_bugfix_dispatch-overflow_20260508-2317.md` exchange already
   patched the symptom of "one agent receives a second top-level task
   while still owning a non-terminal one." The §2.6 multi-task idle
   accounting in v3 is the same family of accounting and is consistent
   with that patch — the F1 self-child path is the first legitimate
   producer of "one agent owns ≥2 non-terminal tasks at once," so the
   rule must hold here too.

No other sections changed. The 7 fixes from v2 (target required; cycle
algo; SDK reader decoupling; no `Task.blocked_on`; centralized
`finalize_task`; exact-target permission; `task_event_data` helper;
`top_level=true` filter) and the 8 accept items from v1 stay intact.

---

## 1. Summary

**F1 ships:**
- New WS messages: `submit_child` (agent→hub) → `child_accepted`/`child_rejected` (hub→agent), plus unsolicited `child_completed` push when the child reaches a terminal status.
- `target_agent` is **required** (no capability-routed delegation in Phase 2).
- Hub-side child tracking via two in-memory maps (no new `Task` field). Ancestor-chain + depth cap (default `8`, env-overridable) enforce safe recursion. Immediate self-delegation A→A is allowed; A→B→A is rejected. **The self-child fast path in §2.2 is what makes A→A executable; otherwise the direct-target router would refuse to dispatch to a BUSY agent.**
- SDK helper `submit_child(...)` on both `AgentHub` and `AsyncAgentHub`. Reader loop decoupled from handler execution so parent waits never deadlock (§2.2.6).
- Failure bubbling rule: child failure does **not** auto-fail the parent; the hub delivers `child_completed{status: failed|timeout}` and the parent agent decides.
- Centralized `finalize_task(task_id, status, result, logs)` drives every terminal transition (REST `/report`, WS `result`, timeout watcher) and is the single caller of `_notify_parent_of_child()`. It is also the single site that decides whether to flip the assigned agent BUSY→IDLE and call `dispatch_pending_for_agent` — gated by `queue.list_by_agent(agent_id)` having zero non-terminal tasks for that agent (§2.6).

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

**Q5 — Cycle detection.** §2.5. Walk ancestor chain from `parent_task_id`; reject if `target_agent` matches `assigned_agent_id` of any **non-immediate** ancestor (allowing A→A). Hard depth cap `MAX_TASK_DEPTH=8` as fallback. The "allow A→A" branch only works because §2.2's self-child fast path bypasses the IDLE check that the direct-target router would otherwise enforce.

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

**Validation order (hub, in `_handle_submit_child(parent_agent_id, msg)`):**
1. `target_agent` present and non-empty → else `child_rejected: bad_request`.
2. Parent task exists and `assigned_agent_id == parent_agent_id` → else `not_owner`.
3. Parent task status ∈ {`assigned`, `running`} → else `parent_terminal`.
4. `validate_key` + `can_assign_task(key, target_agent)` → else `forbidden`.
5. `check_delegation(parent_task_id, target_agent)` (§2.5) → else `cycle` / `depth_limit_exceeded`.
6. **Create the child row** via `queue.create(...)` with `parent_task_id=parent_task_id`, `target_agent=target_agent`, `submitted_by=f"agent:{parent_agent_id}"`. Persist via `db.create_task(child)`.
7. **Branch on self vs. other:**
   - **Self-child fast path** (`target_agent == parent_agent_id`):
     - Call `queue.assign(child_id, parent_agent_id)` directly. This sets the child's status to `ASSIGNED` and writes `assigned_agent_id`. Persist via `save_task(child)`.
     - Do **not** call `Router.route()` — its IDLE precondition would refuse, because the parent agent's registry is already `BUSY` (it is running the parent task).
     - Do **not** flip the registry agent to `BUSY` — it is already `BUSY` (no-op write would be harmless but is skipped to keep the activity log clean).
     - Emit `task_created` and `task_updated` SSE via `task_event_data(child_id, ...)`.
     - `await manager.send(parent_agent_id, {"type":"task","task":task_message(child)})` over the existing WS — this is the same WS the parent agent is already reading; the SDK reader-loop decoupling from §2.2.6 is what guarantees the parent's handler-thread can receive this `task` while it sits awaiting the future.
   - **Other-target path** (default): `await route_and_dispatch(child_id)` — the existing direct-target dispatch (which checks the *target's* IDLE state, queues if BUSY, dispatches when target frees). This is unchanged from v2.
8. Register `_children_by_parent[parent_id].add(child_id)` and `_parent_of_child[child_id] = parent_id`.
9. Send `child_accepted` (with the child's `task_id`; `status` is `assigned` for self-fast-path, `queued` or `assigned` for other-target depending on whether `route_and_dispatch` found the target IDLE).

**Why the self-fast-path is safe.**
- The parent is the only WS owner of the child (`assigned_agent_id == parent_agent_id`); there is no risk of two agents racing for the same row.
- The SDK reader loop is now decoupled from handler execution (§2.2.6); the same WS can receive the `task` push, dispatch it to a worker, and resolve the parent's child-future when the worker reports `result` back over the same WS.
- The agent's BUSY status is preserved throughout, so `dispatch_pending_for_agent` will not pick a second *top-level* task for this agent — the existing FR-CON-x gating still holds.
- §2.6's idle-accounting rule guarantees that completing the self-child does **not** flip the agent IDLE while the parent is still RUNNING.

#### 2.2.2 / 2.2.3 / 2.2.4 — Hub → Agent

```json
// child_accepted
{"type":"child_accepted","request_id":"<echoed>","child_task_id":"<uuid>","status":"queued"}

// child_rejected (reason ∈ {bad_request, not_owner, parent_terminal, forbidden, cycle, depth_limit_exceeded})
{"type":"child_rejected","request_id":"<echoed>","reason":"cycle","message":"…"}

// child_completed (unsolicited; status ∈ {completed, failed, timeout})
{"type":"child_completed","parent_task_id":"<id>","child_task_id":"<id>","status":"completed","result":{...}}
```

`child_completed` is pushed by `_notify_parent_of_child()` from inside `finalize_task()` (§2.6) when the child has a `parent_task_id`, the parent task is still in `assigned`/`running`, and the parent agent's WS is connected. Otherwise dropped with a `child_completed_dropped` activity-log entry. For the self-child case, the parent's WS is the same WS that delivered the original `task` and that received the self-child's `result` — the push is local and reliable.

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

`_handle_task` is now an independent `asyncio.Task`. The reader keeps looping. A handler that `await self.submit_child(...)` blocks on a `Future` resolved by the reader. **For self-child:** the same reader receives the hub's `task` push for the child, schedules a *second* `_handle_task` task on the same loop, that task runs the agent's handler, sends back a `result`, the hub finalizes and pushes `child_completed`, the reader resolves the parent's future.

**Sync fix.** `AgentHub.__init__` adds:
- `self._send_lock = threading.Lock()` — every `_ws.send` call is wrapped (worker threads and the reader thread cannot interleave a partial frame).
- `self._executor = ThreadPoolExecutor(max_workers=int(os.getenv("AGENT_HUB_HANDLER_WORKERS","4")))` — handlers run on workers. **Default 4** is enough headroom for self-child (parent + child concurrently) without unbounded growth; agents that run deeper self-recursion can raise it via env.
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

For the **self-child** case specifically, the agent legitimately owns ≥2 non-terminal tasks at once (parent + 1..N self-children at varying depths). The dispatcher must not interpret "one of those finished" as "agent is free." That is exactly what §2.6's idle-accounting rule enforces.

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

`target_agent` is always present (required per §2.2.1) → check is deterministic. Immediate self A→A allowed; indirect cycle A→B→A rejected; depth cap as belt-and-braces guard. **Allowing A→A is operationally meaningful only because of §2.2.1 step 7's self-child fast path** — without it, the cycle check would say "ok" but the direct-target router would refuse to dispatch to a BUSY agent (codex v2 finding #2).

### 2.6 Failure / timeout bubbling, `finalize_task` helper, multi-task idle accounting

Today three code paths transition a task to a terminal state — REST `POST /tasks/{id}/report` (`hub/main.py:471`), WS `result` (`hub/main.py:562`), and timeout watcher `scan_timeouts_once()` (`hub/main.py:383`). v2 introduces a single helper they all call (codex #5); v3 also folds the BUSY→IDLE flip and the `dispatch_pending_for_agent` call into it, gated by an "agent has zero non-terminal tasks left" check (codex v2 #2):

```python
async def finalize_task(task_id, status, result, logs=None) -> None:
    """The single terminal-state writer.
    1. queue.update_status(task_id, status, result)
    2. db.update_task(...)
    3. emit task_completed/task_updated SSE via task_event_data() (§3.3)
    4. _notify_parent_of_child(task_id) if this task has a parent
    5. agent-idle accounting (NEW in v3):
       agent_id = the task's assigned_agent_id (before clearing, if any)
       if agent_id is not None:
           remaining = [t for t in queue.list_by_agent(agent_id)
                        if t.status in (TaskStatus.ASSIGNED, TaskStatus.RUNNING)]
           if not remaining:
               registry.heartbeat(agent_id, AgentStatus.IDLE)
               db.update_agent_status(agent_id, AgentStatus.IDLE.value)
               await dispatch_pending_for_agent(agent_id)
           # else: agent stays BUSY; no dispatch. Self-child completing
           # while parent still RUNNING falls into this branch.
    """
```

Every existing `queue.update_status(..., terminal_status, ...)` call is replaced by `await finalize_task(...)`. **Only `finalize_task` calls `_notify_parent_of_child()`.** The previous unconditional `registry.heartbeat(agent_id, AgentStatus.IDLE) + db.update_agent_status(...) + dispatch_pending_for_agent(...)` blocks at `hub/main.py:465-467` (REST `/report`) and `:560-563` (WS `result`) are deleted — that work now lives entirely inside `finalize_task` step 5, gated by `list_by_agent`.

This also subsumes the Phase 1 `claude_bugfix-request_dispatch-overflow_*` patch's intent ("don't dispatch a second task to an agent that is still finishing one"); F1 self-children are the first legitimate producer of "one agent owns ≥2 non-terminal tasks at once," so the same accounting must hold here.

**Authoritative table** (v3 — added "agent idle?" column and a self-child row):

| Event                                       | Path                                                  | Parent task | child_completed | Agent → IDLE? | Dispatch pending? |
|---------------------------------------------|-------------------------------------------------------|-------------|-----------------|---------------|-------------------|
| Top-level task completes (WS `result`)      | `handle_agent_message:result` → `finalize_task`       | n/a         | no              | yes (no other non-terminal owned) | yes |
| Top-level task completes (REST `/report`)   | `report_task` → `finalize_task`                       | n/a         | no              | yes           | yes |
| Top-level task times out                    | `scan_timeouts_once` → `finalize_task`                | n/a         | no              | yes           | yes |
| Child (cross-agent) completes               | `handle_agent_message:result` → `finalize_task`       | unchanged   | yes             | yes (child agent has no other) | yes (child agent) |
| Child (cross-agent) fails / times out       | `report_task` / `scan_timeouts_once` → `finalize_task`| unchanged   | yes (failed/timeout) | yes      | yes |
| **Self-child completes while parent still RUNNING** | `handle_agent_message:result` → `finalize_task` | unchanged | yes        | **NO** — `list_by_agent` still has the parent in (ASSIGNED, RUNNING) | **NO** |
| **Self-child fails/times out, parent still RUNNING** | `…` → `finalize_task` | unchanged | yes (failed/timeout) | NO | NO |
| Self-child completes, parent already terminal | `…` → `finalize_task` | n/a (terminal) | dropped + activity log | yes (no remaining non-terminal) | yes |
| Parent terminates while self-child still RUNNING | parent's own `finalize_task` | terminal | no | NO — child still ASSIGNED/RUNNING | NO; child finishes later, then its `finalize_task` flips agent IDLE |
| Child agent disconnects mid-run             | Existing requeue (FR-CON-6) — not terminal             | unchanged   | no              | n/a           | n/a |
| Parent WS disconnects while child running   | Parent requeues (FR-CON-6); child finishes later → drop + activity log | requeued | dropped (logged) | per child's own finalize | per child's own finalize |

Rule of thumb: **the hub never touches the parent's task state because of a child event**, and the hub never marks an agent `IDLE` while it still owns any `ASSIGNED`/`RUNNING` row.

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

For the self-child case, `pa == child_task.assigned_agent_id` and the push lands on the same WS the parent is reading.

### 2.7 Permission model

Concrete:
- WS register stores the API key on `ConnectionManager.session_keys`.
- `submit_child` revalidates that key via `access_control.validate_key` on every call (no caching).
- `access_control.can_assign_task(key, target_agent, requester="agent")` — exact-target check (codex #6, since `target_agent` is required). `is_admin` bypasses `allowed_agents` as today.
- `submitted_by = "agent:<parent_agent_id>"` on the child task.
- Activity log: `db.log_activity("task", child_id, "delegated_by_agent", {"parent_task_id":…,"parent_agent_id":…})`.
- Self-target (`target_agent == parent_agent_id`) is permitted iff the parent's key allows assigning to itself — i.e. either `is_admin` or `parent_agent_id ∈ allowed_agents`. This is the same check as any other target; no special case.

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
| `hub/queue.py` | `TaskQueue.list_all(parent_task_id=None, top_level=False)` | — | F2 filter. `list_by_agent` is reused as-is by `finalize_task`'s idle-accounting check. |
| `hub/delegation.py` (new) | — | `_children_by_parent`, `_parent_of_child` (guarded by `TaskQueue._lock`); `register_child`, `release_child`; `MAX_TASK_DEPTH`; `_ancestor_chain`; `check_delegation`; `rebuild_from_db()` (called by `load_state`) | F1 child bookkeeping. |
| `hub/router.py` | — | — | Unchanged. F1's other-target path uses the existing direct-target dispatch; F1's self-target path bypasses `Router.route()` entirely (§2.2.1 step 7). |
| `hub/auth.py` | — | — | No new flag; `can_assign_tasks` reused. Self-target permission is the same `allowed_agents` check as any target. |
| `hub/main.py` | `ConnectionManager` (+ `session_keys`); `websocket_endpoint` (store/clear key); `handle_agent_message` (+ `submit_child` branch; replace inline terminal writes with `finalize_task`; **delete the inline `registry.heartbeat(IDLE) + db.update_agent_status + dispatch_pending_for_agent` block at `:560-563` — that work is now inside `finalize_task` step 5**); `list_tasks` (+ `parent_task_id`, `top_level` params, mutual-exclusion); `report_task` (call `finalize_task`; **delete the inline IDLE-flip block at `:465-467`**); `scan_timeouts_once` (call `finalize_task` on timeout terminal); every `emit_event("task_*", …)` callsite uses `task_event_data` | `task_event_data(task_id, **extra)`; `finalize_task(task_id, status, result, logs=None)` — now also owns BUSY→IDLE accounting and `dispatch_pending_for_agent` invocation, gated by `queue.list_by_agent(agent_id)` having zero non-terminal tasks; `_handle_submit_child(parent_agent_id, msg)` — includes the self-child fast path branch (§2.2.1 step 7); `_notify_parent_of_child(child_task)` | F1 wire handler + F2 SSE/REST + central terminal handler + multi-task idle accounting. |
| `agent_sdk/client.py` | `AgentHub.__init__` (+ `_send_lock`, `_executor`, pending-wait maps, `_current_task_id` thread-local); `_send_json` (use lock); `_on_message` (route `task` to executor; handle `child_*` inline); `_handle_task` (set/clear `_current_task_id`); `AsyncAgentHub.run` (use `asyncio.create_task`; route `child_*`); `AsyncAgentHub._handle_task` (set/clear ContextVar) | `submit_child(...)` on both classes; `ChildRejectedError`, `ChildWaitTimeout`, `ChildNotInTaskContext`; pending-wait helpers; module-level `current_task_id` ContextVar (async) | F1 SDK helper + reader-loop deadlock fix. Self-child works without SDK changes beyond what v2 already specified — the same handler can submit_child to itself; the executor / create_task arrangement guarantees the second `task` push is processed concurrently. |
| `tests/test_websocket.py` | — | 8 new tests (was 6; adds `test_p2_del_2c_self_delegation_completes` and `test_p2_del_2d_self_child_does_not_release_parent_for_other_dispatch`) | F1 WS coverage incl. self-fast-path. |
| `tests/test_rest.py` | — | 2 new tests | F2 REST/SSE. |
| `tests/test_unit.py` | — | 2 new tests | Cycle helper + `task_event_data`. |
| `tests/test_sdk.py` (new) | — | 1 SDK no-deadlock test | Codex acceptance #5 rigor. |
| `scripts/e2e/run_e2e_delegation.py` (new) | — | F1+F2 combined E2E | Combined E2E. |

---

## 5. Test plan

≥10 new pytest cases (16 listed). Names `test_p2_…`. New IDs: `FR-DEL-1` (submit), `FR-DEL-2` (cycle), `FR-DEL-3` (depth), `FR-DEL-4` (failure bubble), `FR-DEL-5` (permissions), `FR-DEL-6` (parent disconnect), `FR-DEL-7` (immediate self allowed end-to-end), `FR-DEL-8` (SDK no-deadlock), `FR-DEL-9` (self-child does not release parent), `FR-PAR-1` (filter), `FR-PAR-2` (SSE field).

| Test | ID | Behavior |
|------|----|---------|
| `test_p2_del_1_submit_child_happy_path` | DEL-1 | submit_child with `target_agent` → `child_accepted` then `child_completed` with result. |
| `test_p2_del_1b_missing_target_rejected` | DEL-1 (neg) | submit_child with no `target_agent` → `child_rejected: bad_request`. |
| `test_p2_del_2_cycle_rejected_indirect` | DEL-2 | A→B→A → `child_rejected: cycle`. |
| `test_p2_del_2b_immediate_self_allowed` | DEL-7 | A→A passes the cycle check (`child_accepted`). |
| `test_p2_del_2c_self_delegation_completes` (NEW v3) | DEL-7 | Agent A is running parent task. A `submit_child(target_agent=A, …)`. Hub takes the self-child fast path: child row is created, assigned to A, sent over A's existing WS without waiting for A to be IDLE. The SDK handler thread (worker / `asyncio.create_task`) picks up the child, executes the task handler, sends `result`. Hub `finalize_task` pushes `child_completed` to A. A then completes the parent. Asserts: both rows terminal with `completed`; child has `parent_task_id == parent.task_id`; A is IDLE only after parent finalize, never between child finalize and parent finalize; SSE shows `task_created` for child immediately after `child_accepted`. |
| `test_p2_del_2d_self_child_does_not_release_parent_for_other_dispatch` (NEW v3) | DEL-9 | Setup: agent A (capability `x`) is running parent P. Driver sends `submit_child(target_agent=A)` to spawn self-child C; both ASSIGNED/RUNNING for A. Driver also enqueues a top-level task T with `task_type=x` (no `target_agent`). Test orders events: child C reports `result completed` first (still over A's WS). Assert: A's registry status remains `BUSY` after C's `finalize_task` returns (because `queue.list_by_agent(A)` still contains P with status RUNNING); `dispatch_pending_for_agent(A)` is NOT called (verified via spy or via T staying in `queued` for ≥1 dispatch tick); T is dispatched to A only after P's own `finalize_task` runs (no other non-terminal owned). |
| `test_p2_del_3_depth_limit_rejected` | DEL-3 | At chain depth 8, next submit_child → `depth_limit_exceeded`. |
| `test_p2_del_4_child_failure_bubbles_to_parent` | DEL-4 | Child returns `failed` → parent receives `child_completed{status=failed}`; parent's task unchanged. |
| `test_p2_del_4b_child_timeout_bubbles_to_parent` | DEL-4 | Child times out via `scan_timeouts_once` → parent receives `child_completed{status=timeout}` (proves centralized `finalize_task`). |
| `test_p2_del_5_permission_inheritance` | DEL-5 | Without `can_assign_tasks` → `forbidden`; with it → ok; revoking key mid-session blocks the next call. |
| `test_p2_del_6_parent_disconnect_orphans_child_safely` | DEL-6 | Parent WS closes mid-child; child finishes; activity log has `child_completed_dropped`; child row queryable. |
| `test_p2_del_7_not_owner_rejected` | DEL-1 (neg) | Agent B sends submit_child for parent assigned to A → `not_owner`. |
| `test_p2_del_8_sdk_no_deadlock` | DEL-8 | In-process hub + AsyncAgentHub: handler awaits submit_child to a second AsyncAgentHub on the same loop; pings + child_completed flow without deadlock. Mirror with sync `AgentHub` + threadpool. Also covers self-child no-deadlock by parameterising `target_agent` over `[other, self]`. |
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

The §2.6 idle-accounting rule is observably equivalent to today's behavior whenever `list_by_agent(agent_id)` has at most one non-terminal row (which is always true in Phase 1: top-level tasks only). The new branch where it differs (`list_by_agent` returns ≥2 non-terminal rows because of self-children) cannot be triggered by any pre-Phase-2 test or agent. Existing tests therefore see identical IDLE flips and dispatch sequencing.

**Visible additions for clients:**
- SSE `task_*` payloads gain a `parent_task_id` key (always present, `null` for top-level). Standard unknown-key tolerance means existing consumers are unaffected.
- `GET /tasks` accepts new `parent_task_id` (uuid) and `top_level` (bool) params. Existing callers that don't pass them see no change.

**Invisible changes:**
- Async SDK `run()` switches `await self._handle_task(...)` → `asyncio.create_task(self._handle_task(...))`. Behavior identical for handlers that don't call `submit_child` (hub busy-state already prevents concurrent top-level tasks per agent).
- Sync SDK gains a worker-thread pool; `_on_message` no longer executes handlers on the reader thread. Same observable behavior for non-delegating handlers.
- BUSY→IDLE flips and `dispatch_pending_for_agent` calls move from inline blocks at `hub/main.py:465-467` and `:560-563` into `finalize_task` step 5. Same observable behavior except in the new self-child case.

---

## 7. Acceptance criteria coverage (vs. scope.md §5–7)

1. **Module-by-module change list** — §4 (paths, modified vs. new functions, JSON shape changes, central helpers, self-fast-path branch, `finalize_task` agent-idle gating).
2. **WS / REST / MCP schemas** — §2.2 (request/response/reject) + §3.2 (REST). Explicit: no MCP delegation endpoint.
3. **Five F1 open questions answered** — §2.1 Q1–Q5.
4. **Cycle detection concrete algorithm** — §2.5 (deterministic because `target_agent` required; immediate self allowed and operationally executable via §2.2.1 step 7; indirect rejected; depth cap fallback).
5. **Test plan ≥8 cases** — §5 lists 16 covering child-submit happy path, child failure bubbling (incl. timeout via `finalize_task`), cycle rejection, depth limit, permission inheritance, parent_task_id filter + `top_level`, SSE field, F1+F2 E2E, immediate self allowed (cycle-check + end-to-end self-completion + idle accounting), SDK no-deadlock, and `task_event_data` unit.
6. **Migration story for the SQLite index** — §3.1 + §3.4 (additive `CREATE INDEX IF NOT EXISTS`).
7. **Backward-compatibility called out** — §6 (all 82 pytest cases and 6 E2E scenarios; SSE/REST changes additive; SDK signatures unchanged; idle-accounting rule no-op for current tests).
