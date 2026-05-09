# Phase 2.2 Design v2 — Versioned Capabilities + Discovery + Version-Aware Routing

**Author:** designer-v2 (sub-agent)
**Date:** 2026-05-09
**Scope:** F3 (versioned, schema-bearing capabilities) + F4 (capability discovery + version-aware routing). Per `design/phase2.2/scope.md`. Additive only — no Phase 1, 1.5, or 2 redesign. **No new top-level dependencies.** Uses one guarded additive column (`tasks.min_version`) via the existing `_ensure_column` helper, which scope explicitly permits.

---

## 0. Diff vs v1 — what changed in response to codex review v1

| Fix | Where | Summary |
|-----|-------|---------|
| **F1** (drop `jsonschema` dep) | §2.3, §5 | Removed the `requirements.txt` line. `hub/capabilities.py` now ships a tiny pure-Python validator supporting exactly `type`, `required`, `properties`, `items`, `enum`, `additionalProperties`. Any other JSON Schema keyword yields a deterministic detail `unsupported_keyword: <name>`. |
| **F2** (no `payload.__min_version`) | §3.3, §5 | Dropped the reserved-key hack. `database.init_db()` calls `_ensure_column(cursor, "tasks", "min_version", "INTEGER NOT NULL DEFAULT 1")`. `save_task` writes `task_data.get("min_version", 1)`; `parse_task_row` reads `row.get("min_version") or 1`. Pre-existing rows default to `1` via the column default. The field is plumbed through `Task`, `TaskSubmitRequest`, `SubmitChildRequest`, `TaskQueue.create`, `main.save_task`, `submit_task`, `_handle_submit_child`. |
| **F3** (direct-target capability + min_version check) | §3.2, §5 | Added a single `_agent_can_handle(agent, task)` helper used at four sites: (a) `route_and_dispatch` direct-target branch, (b) `dispatch_pending_for_agent` per-pending loop, (c) `_handle_submit_child` other-target path before dispatch, (d) `_handle_submit_child` self-fast-path before assign. Rejection for `submit_child` uses new `child_rejected: capability_unavailable`. |
| **F4** (validation runs at dispatch as authoritative gate) | §2.3, §3.2, §5 | Submit-time check is now an optional preflight (REST/MCP `strict` rejects 400 before task creation when a matching schema-bearing target is already known). The authoritative check runs **immediately before dispatch** against the selected agent's resolved capability. Strict-mode failure on an already-queued task: hub finalizes the task as `failed` with `payload_schema_violation`, emits `task_completed` (per Phase 2 finalize_task), logs detail, does NOT dispatch. Warn-mode logs and dispatches anyway. |
| **F5** (Pydantic Capability JSON serialization) | §2.2, §5 | `_to_json` in `database.py` is `json.dumps(value)` — it cannot serialize `Capability` Pydantic instances. Fix lives in `database.save_agent`: it normalizes `agent_data["skills"]` items to JSONable dicts via `capability_to_dict(cap)` (or `cap.model_dump(mode="json")`) before calling `_to_json`. Centralizing it in `database.save_agent` keeps `hub/main.py` free of serialization concerns. |
| **F6** (`GET /capabilities` top-level array) | §3.1 | Response is now a **top-level JSON array**, not `{"capabilities": [...]}`. `agent_count` stays. `agent_ids` is documented as an additive optional field, not a wrapper. |
| **F7** (no per-GET activity_log writes) | §3.1.1 | Aggregation returns `aggregated_schema_conflict: true` only — never writes to `activity_log`. Conflict logging moved to **registration time**: when an agent registers and its schema for `(name, version)` conflicts with another online agent's schema for the same `(name, version)`, log once at that moment. |

The 6 codex `[answer to open Q#]` items (guarded column, registry insertion order, no-reject-on-schema-conflict, bare-advertiser dominance for null aggregate, dispatch-time as second gate, `can_view_agents` gate) are accepts of v1 answers and are preserved.

---

## 1. Summary

- `Agent.capabilities` becomes `list[Union[str, Capability]]`. Bare strings normalize to `Capability(name=str, version=1, payload_schema=None, result_schema=None)`. Backward-compat is total.
- `agents.skills` JSON column is reused as-is. `database.save_agent` converts `Capability` Pydantic instances to plain dicts before `_to_json` (F5). On read, `main.py` re-normalizes through `normalize_capabilities`.
- New `tasks.min_version INTEGER NOT NULL DEFAULT 1` column, added by guarded `_ensure_column` (F2). Existing rows default to 1.
- New `hub/capabilities.py` module owns: `normalize_capability`, `aggregate_capabilities`, pure-Python `validate_payload` (F1), `capability_to_dict`, `_agent_can_handle`.
- `AgentRegistry.find_available(task_type, min_version=1)` per §3.2. Direct-target paths also enforce capability + min_version (F3).
- New endpoint `GET /capabilities` returns a top-level JSON array (F6), gated by `can_view_agents`. New MCP tool `list-capabilities` mirrors it.
- Validation runs at **two** points: optional submit preflight, **authoritative dispatch-time** check against the selected agent's schema (F4). `AGENT_HUB_VALIDATE_PAYLOAD ∈ {off, warn, strict}`, default `warn`.
- F3+F4 are orthogonal to Phase 2 delegation. The combined test (§6) covers `submit_child(min_version=2)` against both compatible and incompatible targets.

---

## 2. F3 — Versioned, schema-bearing capabilities

### 2.1 Capability shape

New Pydantic class in `hub/models.py`:

```python
class Capability(BaseModel):
    name: str = Field(..., min_length=1)
    version: int = Field(default=1, ge=1)              # positive int; bare → 1
    payload_schema: Optional[dict] = None              # subset of JSON Schema (see §2.3)
    result_schema: Optional[dict] = None

    model_config = {"extra": "ignore"}                 # forward-compat: tolerate unknown keys
```

`Agent.capabilities` widens to `list[Union[str, Capability]]`. The hub never *stores* bare strings inside `Agent` — they are normalized at registration time.

```python
# hub/capabilities.py
def normalize_capability(item: Union[str, dict, Capability]) -> Capability:
    if isinstance(item, Capability):
        return item
    if isinstance(item, str):
        return Capability(name=item, version=1)
    if isinstance(item, dict):
        return Capability(**item)
    raise TypeError(f"capability must be str|dict|Capability, got {type(item)}")

def normalize_capabilities(items: list) -> list[Capability]:
    return [normalize_capability(x) for x in items or []]
```

### 2.2 DB serialization (Fix F5)

`agents.skills` column shape is unchanged. The fix is at the *write* boundary: `_to_json` in `database.py` calls `json.dumps(value)` and cannot serialize `Capability` Pydantic instances. We add a one-liner JSONable conversion in `database.save_agent` before the `_to_json` call. The cleanest location is `database.save_agent` itself, so all callers (REST register, WS register, MCP register) share one normalization point.

```python
# hub/capabilities.py
def capability_to_dict(cap) -> dict:
    """JSONable dict form of a Capability (or pass-through if already dict)."""
    if isinstance(cap, dict):
        return cap
    if isinstance(cap, Capability):
        # Pydantic v2 model_dump(mode="json") yields plain dict/list/str/int.
        return cap.model_dump(mode="json")
    if isinstance(cap, str):
        return {"name": cap, "version": 1, "payload_schema": None, "result_schema": None}
    raise TypeError(f"capability must be Capability|dict|str, got {type(cap)}")
```

```python
# hub/database.py:save_agent — only the skills line changes
skills_jsonable = [capability_to_dict(c) for c in (agent_data.get("skills") or [])]
# then existing line:
cursor.execute(
    "INSERT OR REPLACE INTO agents (... skills ...) VALUES (... ? ...)",
    (..., _to_json(skills_jsonable, []), ...),
)
```

`main.save_agent` continues to pass `agent.capabilities` (a `list[Capability]`) through `agent_data["skills"]` — no changes needed in `main.save_agent`.

**Read path.** `_agent_from_row` (in `main.py`) calls `_parse_json(row.get("skills"), [])` then routes the list through `normalize_capabilities(...)` so the in-memory `Agent.capabilities` is always `list[Capability]`. Old DB rows with `["echo"]` deserialize to `[Capability(name="echo", version=1)]`. **No migration of agents.skills column.**

### 2.3 Validation behavior (Fixes F1 + F4)

| Knob | Decision |
|---|---|
| Env var | `AGENT_HUB_VALIDATE_PAYLOAD` ∈ `{off, warn, strict}` |
| **Default** | **`warn`** |
| Library | **None.** Pure-Python validator inside `hub/capabilities.py` (Fix F1). |
| **When validation runs** | **Two points (Fix F4):** (1) **Submit preflight** — at `submit_task`/`_handle_submit_child` if a matching online schema-bearing capability is already known. In `strict` mode this rejects before task creation. (2) **Dispatch-time authoritative check** — inside `route_and_dispatch` immediately after the agent is selected and before `send_task_to_agent`. The selected agent's resolved `(name, version)` schema wins. |
| Strict-mode REST error (preflight) | `HTTP 400 {"detail":{"error":"payload_schema_violation","capability":"code","capability_version":2,"path":"/tokens","message":"…"}}` — task NOT created |
| Strict-mode MCP error (preflight) | JSON-RPC `error: {"code": -32602, "message": "payload_schema_violation: …", "data": {...}}` |
| Strict-mode WS submit_child error (preflight) | `child_rejected: payload_invalid` with `details` echoing the same dict |
| **Strict-mode dispatch-time failure (already-queued task)** | Hub calls existing `finalize_task(task_id, status=FAILED, result={"error":"payload_schema_violation", "details":{...}})`, emits `task_completed` (Phase 2 hook), logs `payload_schema_violation` to `activity_log`. Task is NOT dispatched. Parent/delegation behavior is unchanged — `_notify_parent_of_child` fires for failed children just like for completed ones. |
| Warn-mode log shape | `db.log_activity("task", task_id, "payload_schema_warn", {"capability": name, "capability_version": v, "path": "/tokens", "message": "…"})` — single entry per failing path; first-failure-wins |
| `off` mode | Skip lookup entirely. Zero CPU cost. |

**Default = `warn` rationale.** Strict-by-default would break existing tests that submit untyped payloads. `off`-by-default forfeits observability. `warn` is the smallest credible default.

**Pure-Python validator (Fix F1).** Supports exactly six JSON Schema keywords:

| Keyword | Semantics |
|---------|-----------|
| `type` | `"object"`, `"array"`, `"string"`, `"integer"`, `"number"`, `"boolean"`, `"null"`. Type-check via `isinstance` (with `int`/`bool` distinction handled). |
| `required` | List of property names that MUST appear in an object. Missing → detail `path=/<name>`, `message="required field missing"`. |
| `properties` | Dict of `name → subschema`. Recurse into present properties only. |
| `items` | Single subschema applied to every array element. (Tuple form `items: [...]` is **not** supported → `unsupported_keyword: items_tuple` detail.) |
| `enum` | Value must `in` the listed values. |
| `additionalProperties` | If `false`, properties not in `properties` produce a detail. If `True` or omitted, no-op. (Subschema form not supported → `unsupported_keyword: additionalProperties_subschema`.) |

Any other top-level keyword (`oneOf`, `anyOf`, `pattern`, `format`, `$ref`, `minimum`, `maximum`, etc.) returns a deterministic detail `{"error": "payload_schema_violation", "message": "unsupported_keyword: <name>"}` so callers see exactly *which* keyword they used. Schemas that contain ONLY supported keywords validate strictly; schemas that mix supported + unsupported keywords flag the unsupported one and treat the rest of the schema as a no-op for that path. This bounds the validator at ≈80 lines and is documented in the module docstring.

```python
# hub/capabilities.py — sketch
_SUPPORTED = {"type", "required", "properties", "items", "enum", "additionalProperties"}
_TYPE_MAP = {
    "object": dict, "array": list, "string": str, "integer": int,
    "number": (int, float), "boolean": bool, "null": type(None),
}

def _validate(value, schema, path: str) -> Optional[dict]:
    # check unsupported keywords first (deterministic)
    for kw in schema:
        if kw not in _SUPPORTED:
            return {"path": path, "message": f"unsupported_keyword: {kw}"}
    # type
    if "type" in schema:
        expected = _TYPE_MAP.get(schema["type"])
        if expected is None:
            return {"path": path, "message": f"unsupported_keyword: type_{schema['type']}"}
        # bool is a subclass of int — distinguish
        if schema["type"] == "integer" and isinstance(value, bool):
            return {"path": path, "message": "expected integer, got boolean"}
        if not isinstance(value, expected):
            return {"path": path, "message": f"expected {schema['type']}"}
    # enum
    if "enum" in schema and value not in schema["enum"]:
        return {"path": path, "message": "value not in enum"}
    # object
    if isinstance(value, dict):
        for req_name in schema.get("required", []) or []:
            if req_name not in value:
                return {"path": f"{path}/{req_name}", "message": "required field missing"}
        props = schema.get("properties") or {}
        for k, v in value.items():
            if k in props:
                err = _validate(v, props[k], f"{path}/{k}")
                if err: return err
            elif schema.get("additionalProperties") is False:
                return {"path": f"{path}/{k}", "message": "additional property not allowed"}
            elif isinstance(schema.get("additionalProperties"), dict):
                return {"path": f"{path}/{k}", "message": "unsupported_keyword: additionalProperties_subschema"}
    # array
    if isinstance(value, list) and "items" in schema:
        if isinstance(schema["items"], list):
            return {"path": path, "message": "unsupported_keyword: items_tuple"}
        for i, item in enumerate(value):
            err = _validate(item, schema["items"], f"{path}/{i}")
            if err: return err
    return None

def validate_payload(payload, schema, *, mode, capability, capability_version, task_id):
    if mode == "off" or schema is None:
        return None
    err = _validate(payload, schema, "")
    if err is None:
        return None
    detail = {
        "error": "payload_schema_violation",
        "capability": capability,
        "capability_version": capability_version,
        "path": err["path"] or "/",
        "message": err["message"],
    }
    if mode == "strict":
        raise PayloadSchemaError(detail)
    db.log_activity("task", task_id, "payload_schema_warn", detail)
    return detail
```

**Resolution rule.** Given a task with `task_type=T` and an agent `A`, look up `A.capabilities` (already normalized) and find the entry matching `name == T` with the largest `version >= task.min_version`. Use **its** `payload_schema`. If the chosen entry has `payload_schema is None`, validation is a no-op for that dispatch.

**Where the env var is read.** Once at module import in `hub/capabilities.py` as `_DEFAULT_MODE = os.getenv("AGENT_HUB_VALIDATE_PAYLOAD", "warn").lower()`. Tests override via `monkeypatch.setenv` + `importlib.reload` or by passing `mode=...` directly.

---

## 3. F4 — Capability discovery + version-aware routing

### 3.1 `GET /capabilities` (Fix F6)

**Auth:** `can_view_agents`.

**Request.** No params for v1.

**Response shape — top-level JSON array (Fix F6):**

```jsonc
[
  {
    "name": "code",
    "version": 2,
    "agent_count": 3,
    "agent_ids": ["coder-1","coder-2","coder-3"],   // additive optional field; not a wrapper
    "payload_schema": {"type":"object","properties":{...}},  // or null (see §3.1.1)
    "result_schema":  {"type":"object","properties":{...}},  // or null
    "aggregated_schema_conflict": false              // omitted if false (default)
  },
  {"name":"code","version":1,"agent_count":1,"agent_ids":["legacy-coder"],"payload_schema":null,"result_schema":null},
  {"name":"echo","version":1,"agent_count":2,"agent_ids":["e1","e2"],"payload_schema":null,"result_schema":null}
]
```

Sort: `name` ascending, then `version` descending. `agent_ids` is documented as an **additive optional** field that may be omitted in a future revision; clients MUST treat the response as a flat array of capability rows, not as a wrapper.

#### 3.1.1 Schema aggregation rule (Fix F7)

If agents disagree on the schema for `(name, version)`:

1. If **any** advertising agent has `payload_schema is None`, the aggregated `payload_schema` is `null` and `aggregated_schema_conflict: true` is set on the row.
2. Otherwise, if all schemas are byte-identical (after `json.dumps(sort_keys=True)`), use that schema and omit the conflict flag.
3. Otherwise, set `payload_schema` to the schema of the **lexicographically first agent_id** and set `aggregated_schema_conflict: true`.

Same rule applies to `result_schema`.

**Fix F7: NO activity_log write happens during aggregation.** A `GET /capabilities` call is read-only and idempotent; writing to `activity_log` on every dashboard refresh would flood the log. Instead, conflict-detection logging happens at **registration time**: in `register_agent` (REST + WS + MCP), after the new agent is added to the registry, walk the existing online agents and, for each `(name, version)` that the new agent advertises with a non-null schema, compare against any other online agent's schema for the same `(name, version)`. If they differ (and neither side is null per rule 1), emit a single `db.log_activity("agent", new_agent_id, "capability_schema_conflict", {"capability": name, "version": v, "other_agent_id": ..., "delta": "byte_diff"})` entry.

This is O(new_agent_caps × online_agents); at the documented scale (50 agents, 8 caps each) it's negligible. Bare-vs-bare and bare-vs-schema combinations don't log (they're not "conflicts"; rule 1 already collapses them to null in aggregation).

#### 3.1.2 Implementation sketch

```python
# hub/capabilities.py
def aggregate_capabilities(agents: list[Agent]) -> list[dict]:
    buckets: dict[tuple[str,int], list[tuple[str, Capability]]] = defaultdict(list)
    for agent in agents:
        if agent.status == AgentStatus.OFFLINE:
            continue
        for cap in agent.capabilities:
            buckets[(cap.name, cap.version)].append((agent.agent_id, cap))
    rows = [_merge_bucket(name, version, entries)
            for (name, version), entries in buckets.items()]
    rows.sort(key=lambda r: (r["name"], -r["version"]))
    return rows  # list, not wrapped
```

`_merge_bucket` implements §3.1.1 (no DB writes).

### 3.2 Routing algorithm (Fixes F3 + F4)

```python
# hub/registry.py
def find_available(self, task_type: str, min_version: int = 1) -> Optional[Agent]:
    """Pick the first IDLE agent advertising (task_type, version >= min_version).
    Tiebreak: registry insertion order.
    Empty-capabilities sentinel preserved (wildcard agent).
    """
    with self._lock:
        for agent in self._agents.values():
            if agent.status != AgentStatus.IDLE:
                continue
            if not agent.capabilities:
                return agent                         # wildcard
            for cap in agent.capabilities:
                cap_name = cap.name if isinstance(cap, Capability) else str(cap)
                cap_ver  = cap.version if isinstance(cap, Capability) else 1
                if cap_name == task_type and cap_ver >= min_version:
                    return agent
    return None
```

**The single helper used at every routing/assignment site (Fix F3):**

```python
# hub/capabilities.py
def _agent_can_handle(agent: Agent, task: Task) -> bool:
    """True iff `agent` advertises a capability matching `task.task_type` at
    version >= task.min_version. Empty capabilities = wildcard (matches anything)."""
    if not agent.capabilities:
        return True
    floor = task.min_version or 1
    for cap in agent.capabilities:
        cap_name = cap.name if isinstance(cap, Capability) else str(cap)
        cap_ver  = cap.version if isinstance(cap, Capability) else 1
        if cap_name == task.task_type and cap_ver >= floor:
            return True
    return False
```

**Direct-target enforcement (Fix F3) — four call sites:**

(a) **`route_and_dispatch` direct-target branch.** Today the router just checks "target idle?". Change:

```python
# hub/main.py:route_and_dispatch
if task.target_agent:
    target = registry.get(task.target_agent)
    if not target or target.status != AgentStatus.IDLE:
        return None
    if not _agent_can_handle(target, task):
        # In strict mode this is a hard reject if reachable from submit_task, but
        # for already-queued tasks we leave them queued (operator may bring up
        # a compatible agent later). One-time activity log to surface the gap.
        db.log_activity("task", task.task_id, "direct_target_capability_mismatch",
                        {"target_agent": task.target_agent, "task_type": task.task_type,
                         "min_version": task.min_version})
        return None
    # fall through to existing dispatch-time validation (F4 §2.3)
```

(b) **`dispatch_pending_for_agent`** — replace the inline `task.task_type in agent.capabilities or not agent.capabilities` predicate with `_agent_can_handle(agent, task)`. (The `task.target_agent or ...` early-out stays; it's about target filtering, not capability.)

```python
# hub/main.py:dispatch_pending_for_agent — replacement predicate
if task.target_agent or _agent_can_handle(agent, task):
    routed = await route_and_dispatch(task.task_id)
    ...
```

Note the direct-target route path still hits `_agent_can_handle` inside `route_and_dispatch` (call site (a)) so direct-target with min_version mismatch fails there too.

(c) **`_handle_submit_child` other-target path** — before `route_and_dispatch(child_id)`:

```python
if req.target_agent and req.target_agent != parent_agent_id:
    target = registry.get(req.target_agent)
    if target is None or not _agent_can_handle(target, child):
        # Roll back: delete child row + delegation registration; emit child_rejected.
        delegation.release_child(child_id)
        queue.delete(child_id)
        db.delete_task(child_id)
        await _send_child_rejected(parent_agent_id, request_id,
                                   "capability_unavailable",
                                   f"{req.target_agent} cannot handle "
                                   f"{task_type}@v{req.min_version}+")
        return
```

(d) **`_handle_submit_child` self-fast-path** — before `queue.assign(child_id, parent_agent_id)`:

```python
if req.target_agent == parent_agent_id:
    parent_agent = registry.get(parent_agent_id)
    if parent_agent is None or not _agent_can_handle(parent_agent, child):
        delegation.release_child(child_id)
        queue.delete(child_id); db.delete_task(child_id)
        await _send_child_rejected(parent_agent_id, request_id,
                                   "capability_unavailable",
                                   f"self does not advertise "
                                   f"{task_type}@v{req.min_version}+")
        return
    # self-fast-path uses BUSY (not IDLE) because the parent is still RUNNING the parent task;
    # _agent_can_handle does not require IDLE, only capability+version match. The status check
    # is implicit via queue.assign() returning False if the agent slot can't accept.
```

**Note on IDLE requirement:** for (a) direct-target via `route_and_dispatch`, IDLE is required (the agent must take the task immediately). For (c) other-target submit_child, IDLE is also required because the dispatch path is `route_and_dispatch`. For (d) self-child fast-path, the parent agent is BUSY (running the parent task) — the existing self-assign semantics already handle this; we only add the capability check, not an IDLE check.

**Dispatch-time validation gate (Fix F4).** After agent selection in `route_and_dispatch`, immediately before `send_task_to_agent`:

```python
# hub/main.py:route_and_dispatch — inserted after registry.heartbeat(agent_id, BUSY) succeeds
selected = registry.get(agent_id)
schema = _resolve_payload_schema(task, selected)   # picks largest-version matching cap's payload_schema
try:
    validate_payload(task.payload or {}, schema, mode=current_mode(),
                     capability=task.task_type,
                     capability_version=_resolve_cap_version(task, selected),
                     task_id=task.task_id)
except PayloadSchemaError as exc:
    # Strict-mode dispatch-time failure: finalize as failed, no dispatch.
    # Roll back the BUSY transition we just made.
    registry.heartbeat(agent_id, AgentStatus.IDLE)
    db.update_agent_status(agent_id, AgentStatus.IDLE.value)
    finalize_task(task.task_id, TaskStatus.FAILED,
                  result={"error": "payload_schema_violation", "details": exc.detail})
    db.log_activity("task", task.task_id, "payload_schema_violation", exc.detail)
    return None
```

In `warn` mode the call returns the detail dict (already logged as `payload_schema_warn`) and dispatch proceeds normally.

**Complexity.** `find_available` and `_agent_can_handle` are both O(M caps_per_agent). At 50 agents × ~8 caps each, sub-microsecond.

### 3.3 `Task.min_version` persisted via guarded column (Fix F2)

**Persisted on `Task` via a real DB column.** `payload.__min_version` is gone.

```python
# hub/database.py:init_db (additive line in the tasks-block)
_ensure_column(cursor, "tasks", "min_version", "INTEGER NOT NULL DEFAULT 1")
```

`_ensure_column` is the existing helper (already used for `tasks.target_agent`). Idempotent. Existing rows acquire `min_version=1` via the column default.

```python
# hub/database.py:save_task — write
"INSERT OR REPLACE INTO tasks (... min_version, ...) VALUES (... ?, ...)",
(..., int(task_data.get("min_version", 1)), ...),

# hub/database.py:parse_task_row — read
out["min_version"] = int(row.get("min_version") or 1)
```

**Plumbing the field.**

| Type / function | Change |
|---|---|
| `Task` (Pydantic) | `min_version: int = 1` |
| `TaskSubmitRequest` | `min_version: int = 1` |
| `SubmitChildRequest` | `min_version: int = 1` |
| `TaskQueue.create(...)` | accept `min_version: int = 1` kwarg, set on the in-memory `Task` |
| `main.save_task(task)` | passes `task.min_version` into `task_data["min_version"]` (a small change to whatever `to_dict` helper builds the row dict) |
| `main.submit_task` | reads `req.min_version`, passes through to `queue.create(...)` and `route_and_dispatch` (which reads from `task.min_version`) |
| `main.list_tasks` / `parse_task_row` consumers | `min_version` becomes part of the task dict; existing consumers ignore unknown keys |
| `main._handle_submit_child` | reads `req.min_version`, passes to `queue.create(...)` |

Routing (`router.route`, `find_available`, `_agent_can_handle`) reads `task.min_version` directly from the persisted `Task`. Requeue (`requeue_assigned_to`) is unaffected because the column persists across requeue.

### 3.4 MCP changes

| Tool | Change |
|---|---|
| `register-agent` | `inputSchema.capabilities.items` widens from `STRING` to `oneOf: [STRING, {object:{name,version,payload_schema,result_schema}}]`. Server normalizes via `normalize_capabilities`. |
| `submit-task` | Adds optional `min_version: INTEGER` (default 1). Plumbed through `TaskSubmitRequest`. |
| **`list-capabilities` (NEW)** | `inputSchema: {}`. Returns the same top-level array as `GET /capabilities`. Auth gate: `can_view_agents`. |
| Backend wiring | `InProcessHubBackend` adds `("GET","/capabilities")` → `main.list_capabilities(self._authorization)`. `HttpHubBackend` is unchanged (path-based dispatch). |

```python
"capabilities": {
    "type": "array",
    "items": {
        "oneOf": [
            STRING,
            {
                "type": "object",
                "properties": {
                    "name": STRING,
                    "version": {"type": "integer", "minimum": 1},
                    "payload_schema": OBJECT,
                    "result_schema": OBJECT,
                },
                "required": ["name"],
                "additionalProperties": True,
            },
        ]
    },
}
```

---

## 4. Answers to the 5 open scope questions (preserved from v1; codex accepted)

| # | Question | Answer |
|---|---|---|
| a | `min_version` on Task or TaskSubmitRequest only? | **Both.** Persisted via guarded `tasks.min_version` column (Fix F2). |
| b | Aggregation when two agents advertise `code:v2` with conflicting schemas? | **First-by-agent-id wins, conflict flag set.** Bare-advertiser anywhere collapses aggregate to null. No registration rejection (codex Q#11). Conflict logged ONCE at registration time, not per-aggregation-call (Fix F7). |
| c | `AGENT_HUB_VALIDATE_PAYLOAD` default? | **`warn`.** |
| d | Bare strings `version=1` or `version=0`? | **`version=1`.** |
| e | MCP `list-capabilities`: admin-only or `can_view_agents`? | **`can_view_agents`.** Codex Q#14 accepts this and notes `allowed_agents` constrains assignment, not visibility. |

---

## 5. Module-by-module change list

| File | Modified | New | Purpose |
|---|---|---|---|
| `hub/models.py` | `Agent.capabilities` widens to `list[Union[str, Capability]]`. `Task` gains `min_version: int = 1`. `TaskSubmitRequest` gains `min_version: int = 1`. `SubmitChildRequest` gains `min_version: int = 1`. | `Capability` Pydantic model. | F3 wire shape + version floor. |
| `hub/capabilities.py` | — | `Capability` import; `normalize_capability`, `normalize_capabilities`, `capability_to_dict`, `aggregate_capabilities`, `validate_payload`, `_validate` (pure-Python, no `jsonschema` dep — Fix F1), `_agent_can_handle`, `_resolve_payload_schema`, `_resolve_cap_version`, `PayloadSchemaError`, `current_mode()`. Module-level `_DEFAULT_MODE = os.getenv("AGENT_HUB_VALIDATE_PAYLOAD","warn").lower()`. | F3 logic, F4 aggregation + routing predicate + validation. |
| `hub/registry.py` | `register(...)` calls `normalize_capabilities` once before constructing `Agent`. `find_available(task_type, min_version=1)` per §3.2. | — | F4 routing. |
| `hub/router.py` | `route(...)` accepts `min_version` and forwards to `registry.find_available(task_type, min_version)`. Empty-target path unchanged. | — | F4 routing. |
| `hub/database.py` | **(F2)** `init_db()` adds `_ensure_column(cursor, "tasks", "min_version", "INTEGER NOT NULL DEFAULT 1")`. `save_task` writes `task_data.get("min_version", 1)`. `parse_task_row` reads `row.get("min_version") or 1`. **(F5)** `save_agent` converts `agent_data["skills"]` items to JSONable dicts via `capability_to_dict(...)` before `_to_json`. | — | F2 persistence column, F5 serialization. |
| `hub/queue.py` (or wherever `TaskQueue.create` lives) | `create(...)` accepts `min_version: int = 1` kwarg, sets it on the `Task`. | — | F2 plumbing. |
| `hub/main.py` | `_agent_from_row` runs `normalize_capabilities` on the JSON-decoded skills. `register_agent` (REST) and the WS register handler call `normalize_capabilities` on `req.capabilities`/`first_msg["capabilities"]` AND the registration-time conflict scan (Fix F7). `submit_task` reads `req.min_version`, plumbs into `queue.create` and `route_and_dispatch`. **(F4)** `submit_task` runs an optional preflight when a matching online schema-bearing capability is already known; in `strict` mode preflight raises HTTPException(400) before task creation. **(F4)** `route_and_dispatch` runs the authoritative dispatch-time validation against the selected agent's resolved schema; strict-mode failure → `finalize_task(FAILED, ...)`, log, no dispatch. **(F3)** `route_and_dispatch` direct-target branch enforces `_agent_can_handle`. `dispatch_pending_for_agent` uses `_agent_can_handle`. `_handle_submit_child` self-fast-path and other-target path enforce `_agent_can_handle`; rejection → `child_rejected: capability_unavailable`. | `list_capabilities` REST handler (returns top-level array — Fix F6). | F3+F4+F5+F6+F7. |
| `hub/mcp_protocol.py` | `register-agent` schema widens (oneOf string/object). `submit-task` schema gains `min_version`. `InProcessHubBackend` route table gains `GET /capabilities`. `call_tool` gains `list-capabilities` branch (returns top-level array per F6). MCP `submit-task` strict-mode preflight surfaces `error.code: -32602` with `payload_schema_violation`. | `list-capabilities` entry in `TOOLS`. | F4 MCP surface. |
| `requirements.txt` | **NO CHANGE (Fix F1).** No new top-level deps. | — | — |
| `tests/test_unit.py` | — | 5 new unit tests (§6). | Coverage. |
| `tests/test_rest.py` | — | 2 new HTTP tests (§6). | Coverage. |
| `tests/test_websocket.py` (or new `tests/test_phase22.py`) | — | 1 new combined-with-delegation test (§6). | F1+F3 interop. |

---

## 6. Test plan

≥ 8 new pytest cases. `test_p22_*` prefix; `FR-CAP-*` IDs.

| Test | ID | Behavior |
|---|---|---|
| `test_p22_cap_1_bare_string_normalizes_v1` | FR-CAP-1 | `register_agent(capabilities=["echo"])` → `agent.capabilities[0] == Capability(name="echo", version=1, payload_schema=None, result_schema=None)`. Existing `find_available("echo")` still matches. |
| `test_p22_cap_2_object_capability_registration` | FR-CAP-2 | `register_agent(capabilities=[{"name":"code","version":2,"payload_schema":{"type":"object","required":["src"]}}])` round-trips through DB unchanged. **F5 coverage**: directly inspect `agents.skills` JSON column and verify it contains plain dicts (not Pydantic `repr`-style strings). |
| `test_p22_cap_3_mixed_list_serializes_and_loads` | FR-CAP-2 | Register `["echo", {"name":"code","version":2}]`. JSON column round-trips both. |
| `test_p22_rte_1_min_version_match` | FR-RTE-7 | A1 advertises `code:v1`, A2 advertises `code:v2`. `find_available("code", min_version=2)` returns A2. `find_available("code", min_version=1)` returns first registered. |
| `test_p22_rte_2_min_version_miss_queues_then_dispatches` | FR-RTE-8 | One idle agent advertising `code:v1`. `submit_task(task_type="code", min_version=2)` → task stays `queued`, no dispatch. **F2 coverage**: restart in-memory state and reload from DB; `task.min_version == 2` survives. Then register A2 with `code:v2` → pending dispatch picks it up. |
| `test_p22_rte_3_direct_target_capability_check` | FR-RTE-9 | **F3 coverage**: agent A1 advertises `code:v1`. `submit_task(task_type="code", min_version=2, target_agent="a1")` → task does NOT dispatch, `direct_target_capability_mismatch` activity log entry exists, task stays `queued`. |
| `test_p22_disc_1_get_capabilities_aggregates` | FR-CAP-3 | Three agents advertising `code:v2` (two with identical schema, one bare); two agents advertising `echo:v1`. `GET /capabilities` returns a **top-level array** (F6 coverage — assert `isinstance(json, list)`) with one `code:v2` row (`agent_count=3`, `payload_schema=null`, `aggregated_schema_conflict=true`) and one `echo:v1` row. Sorted name-asc, version-desc. **F7 coverage**: assert that calling `GET /capabilities` 3× does NOT add 3× `capability_schema_conflict` rows to `activity_log` (only the single registration-time entries from when the conflicting agents registered). 403 without `can_view_agents`. |
| `test_p22_val_1_warn_mode_logs_and_dispatches` | FR-CAP-4 | Default `warn`. Agent advertises `code:v2` with `payload_schema={"type":"object","required":["src"]}`. `submit_task(task_type="code", min_version=2, payload={"wrong":"yes"})` → returns 200 with `status=assigned`, AND `activity_log` has one `payload_schema_warn` row pointing at `/src`. **F1 coverage**: assert no `jsonschema` import is used (or assert pure-python validator path is exercised). |
| `test_p22_val_2_strict_preflight_rejects_400` | FR-CAP-5 | `monkeypatch.setenv("AGENT_HUB_VALIDATE_PAYLOAD","strict")` + reload `hub.capabilities`. Same agent + bad payload → REST returns HTTP 400 with `payload_schema_violation`. Task NOT created (assert `len(queue.list_all())` unchanged). |
| `test_p22_val_3_strict_dispatch_time_fails_queued_task` | FR-CAP-5b | **F4 coverage**: in `warn` mode, submit a task whose target schema-bearing agent is NOT online yet (so submit preflight skips). Task is `queued`. Flip `AGENT_HUB_VALIDATE_PAYLOAD=strict` and reload. Bring up the schema-bearing agent. Dispatch-time validator catches the bad payload → task transitions to `failed` with `result={"error":"payload_schema_violation",...}`, `task_completed` event emitted, `payload_schema_violation` in activity_log, no `task` message sent over WS. |
| `test_p22_val_4_unsupported_keyword_deterministic` | FR-CAP-5c | **F1 coverage**: agent advertises `code:v2` with `payload_schema={"type":"object","oneOf":[...]}`. Strict-mode submit → rejection detail `message: "unsupported_keyword: oneOf"`. |
| `test_p22_mcp_1_list_capabilities_tool` | FR-CAP-6 | MCP `tools/list` includes `list-capabilities`. `tools/call list-capabilities` returns the top-level array (F6). With a key lacking `can_view_agents`, surfaces MCP `error.code: -32001`. |
| `test_p22_del_1_submit_child_with_min_version` | FR-CAP-7 (combines FR-DEL-1) | **F1 + F3 + F4 interop.** Spawn agent A (`["plan"]`), B1 (`["code"]`, bare), B2 (`[{"name":"code","version":2,"payload_schema":{"type":"object","required":["src"]}}]`). A receives `plan` task. (a) `submit_child(target_agent="b2", task_type="code", min_version=2, payload={"src":"x"})` → B2 receives the child, completes, A gets `child_completed`. (b) `submit_child(target_agent="b1", task_type="code", min_version=2, …)` → `child_rejected: capability_unavailable` (F3 self/other-target gate); child row was rolled back (assert `db.get_task(child_id) is None`). |

---

## 7. Backward-compatibility — what must keep passing unchanged

**Tests that MUST keep passing without edits:**

- `test_unit.py::test_u_reg_3_idle_agents_filter` — `["echo"]` → `Capability(name="echo", version=1)`; `min_version` defaults to 1; matches.
- `test_unit.py::test_u_rte_1..6` — router unit tests pass capability strings; `_agent_can_handle` returns True for the default `min_version=1` against bare-`echo` (normalizes to v1).
- `test_unit.py::test_u_que_*`, `test_u_mod_*` — unrelated.
- `test_rest.py::*` — every existing `POST /tasks` omits `min_version`; default 1 matches every Phase 1 agent.
- `test_websocket.py::*` — registration with bare-string capabilities; normalization invisible.
- `test_mcp_protocol.py::*` — `submit-task` schema additively gains `min_version` (optional); existing tools/list snapshot tests need at most a one-line update.
- All Phase 2 delegation tests (`test_p2_del_*`) — `submit_child` payload doesn't grow `min_version` by default; `_handle_submit_child` reads `min_version=msg.get("min_version", 1)`. `_agent_can_handle` returns True for v1 floor against bare capabilities.
- All E2E scripts in `scripts/e2e/` — capabilities are bare strings throughout.

**Behaviors that MUST keep passing unchanged:**

- Empty-capabilities agent (`capabilities=[]`) is a wildcard for all `task_type`. `_agent_can_handle` returns True.
- Tiebreak among same-version idle agents: registry insertion order. §3.2.
- `agent_registered` SSE payload still includes the *input* `capabilities` (the wire shape the client sent). Internal `Agent.capabilities` is normalized; the SSE wrapper builds the event from the request, not the registry copy.
- `dispatch_pending_for_agent` rescan-on-IDLE: `_agent_can_handle` is the only change inside the loop.
- `requeue_assigned_to(agent_id)`: `task.min_version` lives on the persisted column (Fix F2), survives requeue.
- `AGENT_HUB_VALIDATE_PAYLOAD` unset → behavior is `warn`, additive (logs only, never rejects).
- DB schema additions are guarded: `_ensure_column` is idempotent; old DBs auto-add `tasks.min_version` on next `init_db()` call with default 1.

---

## 8. Notes for the coder sub-agent

1. **`_resolve_payload_schema(task, agent)`** picks the highest-version capability matching `task.task_type` with `version >= task.min_version`, returns its `payload_schema` (or None). Used by validation gates only.
2. **`finalize_task`** (Phase 2) is the canonical FAILED-transition entrypoint; reuse it from the dispatch-time strict-mode failure path so `task_completed` SSE + parent notification semantics are preserved.
3. **Registration-time conflict scan (F7)** runs after the new agent is added to the registry; it MUST NOT block registration on conflict (codex Q#11 accepts this).
4. **Child-rejection roll-back (F3 case (c)/(d))** must release `delegation` bookkeeping AND delete the queue row AND delete the DB row to leave no orphans. Test `test_p22_del_1` verifies this.
5. **Idle requirement summary**: `route_and_dispatch` direct-target requires IDLE; `_handle_submit_child` other-target requires IDLE (because it goes through `route_and_dispatch`); self-fast-path does NOT require IDLE (parent is BUSY).

---

**End of design v2.** All 7 codex revise findings addressed; all 6 codex accepts preserved; no new top-level dependencies; no payload-namespace pollution; direct-target capability gating wired at all four call sites; dispatch-time validation is the authoritative gate; `GET /capabilities` returns a top-level array; conflict logging moved to registration time.
