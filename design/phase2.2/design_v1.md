# Phase 2.2 Design v1 — Versioned Capabilities + Discovery + Version-Aware Routing

**Author:** designer-v1 (sub-agent)
**Date:** 2026-05-09
**Scope:** F3 (versioned, schema-bearing capabilities) + F4 (capability discovery + version-aware routing). Per `design/phase2.2/scope.md`. Additive only — no Phase 1, 1.5, or 2 redesign. No DB migration. No new top-level dependency unless justified (§F3.3).

---

## 1. Summary

- `Agent.capabilities` becomes `list[Union[str, Capability]]`. Bare strings normalize to `Capability(name=str, version=1, payload_schema=None, result_schema=None)`. Backward-compat is total: every Phase 1/1.5/2 caller that passes `["echo"]` keeps working.
- The `agents.skills` JSON column is reused as-is. Each list element serializes either as a JSON string (bare) or a JSON object (versioned). Deserialization tolerates both. **No migration.**
- A new `hub/capabilities.py` module owns three responsibilities: (1) `normalize_capability`, (2) `aggregate_capabilities` for `GET /capabilities`, (3) `validate_payload` (jsonschema-based, default mode `warn`). The lib choice is the `jsonschema` PyPI package; rationale in §F3.3.
- `AgentRegistry.find_available(task_type)` becomes `find_available(task_type, min_version=1)`. Algorithm in §F4.2 — O(agents × caps_per_agent), no per-dispatch JSON re-parse, since `Agent.capabilities` is already a Python list at registration time.
- New endpoint `GET /capabilities` (auth: `can_view_agents`) and new MCP tool `list-capabilities` (same gate). `POST /tasks` and `submit-task` MCP tool gain optional `min_version: int = 1`.
- `Task.min_version` is **persisted** so that requeue and pending-rescan keep using the requested floor (§F4.3).
- F3+F4 are completely orthogonal to Phase 2 delegation. The combined-test in §6 covers a parent agent calling `submit_child(min_version=2)`.

---

## 2. F3 — Versioned, schema-bearing capabilities

### 2.1 Capability shape

New Pydantic class in `hub/models.py`:

```python
class Capability(BaseModel):
    name: str = Field(..., min_length=1)
    version: int = Field(default=1, ge=1)              # positive int; bare → 1
    payload_schema: Optional[dict] = None              # JSON Schema draft 2020-12
    result_schema: Optional[dict] = None

    model_config = {"extra": "ignore"}                 # forward-compat: tolerate unknown keys
```

`Agent.capabilities` widens:

```python
class Agent(BaseModel):
    ...
    capabilities: list[Union[str, Capability]] = Field(default_factory=list)
```

The wire-form union is intentional. The hub never *stores* bare strings inside `Agent` — they are normalized at registration time by `normalize_capability` (below) so that downstream code (`registry.find_available`, `aggregate_capabilities`, `validate_payload`) sees a single shape.

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

**Backward-compat statement.** The wire-shape union accepts what every existing caller already sends — `["echo"]`, `["camera-test","echo"]` — and ALSO accepts the new shape — `[{"name":"code","version":2,"payload_schema":{...}}]` — and any mix. `Agent.capabilities` exposed by `GET /agents` is now a list of objects (the post-normalize form). For maximum back-compat we keep `agent_registered` SSE emitting the **input** list verbatim (so dashboards expecting `["echo"]` still get strings); see §F4.4.

### 2.2 DB serialization

`agents.skills` is already `TEXT` storing `_to_json(...)`. We change neither the column nor `_to_json`. The JSON array now holds a free mix:

```json
["echo", {"name": "code", "version": 2, "payload_schema": {"type":"object"}}]
```

`save_agent` writes whatever shape it receives. The current `_to_json` call in `database.save_agent` is unchanged (it serializes `agent.capabilities` after Pydantic round-trip, which means each `Capability` becomes a dict with `name/version/payload_schema/result_schema`; bare strings that were already normalized at register time become objects too).

**Read path.** `_agent_from_row` already calls `_parse_json(row.get("skills"), [])`. We then route the list through `normalize_capabilities(...)` so the in-memory `Agent.capabilities` is always `list[Capability]`. Old DB rows containing `["echo"]` deserialize to `[Capability(name="echo", version=1)]`. **No migration; no schema bump; no `ALTER TABLE`.**

**Bare/object mix during one register call.** Caller sends `capabilities=["echo", {"name":"code","version":2}]`. `register_agent` calls `normalize_capabilities` once at the top, then passes the normalized `list[Capability]` to `registry.register` and `save_agent`. Downstream code never sees the bare/object distinction.

### 2.3 Validation behavior

| Knob | Decision |
|---|---|
| Env var | `AGENT_HUB_VALIDATE_PAYLOAD` ∈ `{off, warn, strict}` |
| **Default** | **`warn`** (rationale below) |
| Library | `jsonschema` (PyPI). Add to `requirements.txt`. |
| When validation runs | **At submit time, in `submit_task` (REST) and `_handle_submit_child` (WS) AND the MCP `submit-task` path which routes through the same FastAPI handler.** Not at dispatch time — the matched agent's capability is already known by then; re-validating per dispatch wastes work and would split the rejection point across two stack frames. |
| Strict-mode error shape (REST) | `HTTP 400 {"detail":{"error":"payload_schema_violation","capability":"code","capability_version":2,"path":"/tokens","message":"…"}}` |
| Strict-mode error shape (MCP) | JSON-RPC `error: {"code": -32602, "message": "payload_schema_violation: <field>: <reason>", "data": {...above without HTTP wrapper}}` |
| Strict-mode error shape (WS submit_child) | `child_rejected: payload_invalid` with `details` echoing the same dict |
| Warn-mode log shape | `db.log_activity("task", task_id, "payload_schema_warn", {"capability": name, "capability_version": v, "path": "/tokens", "message": "…"})` — single entry per failing path; first-failure-wins (no full enumeration) to keep the log thin |
| `off` mode | Skip lookup entirely. Zero CPU cost. |

**Default = `warn` rationale (answer to scope question (c)).** Strict-by-default would break the nine existing tests that submit untyped payloads (e.g. `payload={"echo":"hi"}` against an agent that hasn't declared a schema). `off`-by-default forfeits the observability that motivated the feature. `warn` is the smallest credible interpretation: existing tests pass unchanged, schema authors get evidence in the activity_log, and ops can flip to `strict` at any time without code change.

**Lib choice rationale.** `jsonschema` is a 1-file pure-Python dep with no transitive headache (used widely; no compilation). Hand-rolling JSON Schema validation is a footgun (we'd reinvent ref resolution, format checkers, error paths). The 60 KB on disk is dwarfed by FastAPI itself. Add to `requirements.txt` pinned to `jsonschema>=4,<5`.

**Resolution.** Given a task with `task_type=T` and assigned-or-routing-target agent `A`, look up `A.capabilities` (already normalized) and find the entry with the largest `version` that is `>= task.min_version` and matches `name == T`. Use **its** `payload_schema`. If `A` advertises bare-`T` only (`payload_schema is None`), validation is a no-op for that dispatch. If multiple capability-rows for `T` are present at version >= min_version (a schema-evolving agent), the highest-version one wins.

```python
# hub/capabilities.py
def validate_payload(
    payload: dict,
    schema: Optional[dict],
    *,
    mode: str,                       # "off"|"warn"|"strict"
    capability: str,
    capability_version: int,
    task_id: str,
) -> Optional[dict]:
    """Returns None on success or when validation is suppressed; returns
    an error-detail dict in 'warn' mode (caller logs it) and raises
    PayloadSchemaError in 'strict' mode."""
    if mode == "off" or schema is None:
        return None
    try:
        jsonschema.validate(payload, schema)
        return None
    except jsonschema.ValidationError as exc:
        detail = {
            "error": "payload_schema_violation",
            "capability": capability,
            "capability_version": capability_version,
            "path": "/" + "/".join(str(p) for p in exc.absolute_path),
            "message": exc.message,
        }
        if mode == "strict":
            raise PayloadSchemaError(detail) from exc
        # warn
        db.log_activity("task", task_id, "payload_schema_warn", detail)
        return detail
```

`PayloadSchemaError` is a hub-side exception caught by `submit_task`/`_handle_submit_child`/the MCP error wrapper and translated to the per-surface error shape above.

**Where the env var is read.** Once at module import in `hub/capabilities.py` as `_DEFAULT_MODE = os.getenv("AGENT_HUB_VALIDATE_PAYLOAD", "warn").lower()`. Tests can override via `monkeypatch.setenv` + `importlib.reload` or by passing `mode=...` directly to `validate_payload`.

---

## 3. F4 — Capability discovery + version-aware routing

### 3.1 `GET /capabilities`

**Auth:** `can_view_agents` (mirrors `GET /agents`; same data sensitivity — capability list is already exposed there).

**Request.** No params for v1. `?online=true` (the default) is implicit; we only aggregate over agents with `status != offline`. A future revision can add `?include_offline=true` if a use case appears.

**Response shape.**

```jsonc
{
  "capabilities": [
    {
      "name": "code",
      "version": 2,
      "agent_count": 3,
      "agent_ids": ["coder-1","coder-2","coder-3"],
      "payload_schema": {"type":"object","properties":{...}},  // or null (see §3.1.1)
      "result_schema":  {"type":"object","properties":{...}}   // or null
    },
    {"name":"code","version":1,"agent_count":1,"agent_ids":["legacy-coder"],"payload_schema":null,"result_schema":null},
    {"name":"echo","version":1,"agent_count":2,"agent_ids":["e1","e2"],"payload_schema":null,"result_schema":null}
  ]
}
```

Sort: `name` ascending, then `version` descending. `agent_ids` is included so an operator can correlate without a second `GET /agents` call (cheap; bounded by the 50-agent scale target in `requirements.md` §5).

#### 3.1.1 Schema aggregation rule (answer to scope question (b))

If agents disagree on the schema for `(name, version)`:

1. If **any** advertising agent has `payload_schema is None` (bare or null), the aggregated `payload_schema` is `null` and an `aggregated_schema_conflict: true` boolean is set on the row. Reasoning: the bare advertiser is by definition willing to accept anything; the union of "anything" and any constrained schema is "anything".
2. Otherwise, if all advertising agents send byte-identical schema dicts (after `json.dumps(sort_keys=True)`), use that schema and omit the conflict flag.
3. Otherwise (same `(name, version)`, all schemas non-null but not equal), set `payload_schema` to the schema of the **lexicographically first agent_id** (deterministic across restarts, no JSON merging) and set `aggregated_schema_conflict: true`.

Same rule applies independently to `result_schema`. Conflicts are also written once per aggregation-call to `activity_log` at level `capability_schema_conflict` so operators notice.

This is intentionally simple: the hub does not pretend to "merge" two JSON Schemas — that's a research problem. First-agent-wins + conflict flag gives clients enough information to pick one, and the conflict flag is the operator signal to fix the disagreement at the agent layer.

#### 3.1.2 Implementation sketch

```python
# hub/capabilities.py
def aggregate_capabilities(agents: list[Agent]) -> list[dict]:
    buckets: dict[tuple[str,int], list[tuple[str, Capability]]] = defaultdict(list)
    for agent in agents:
        if agent.status == AgentStatus.OFFLINE:
            continue
        for cap in agent.capabilities:        # already normalized
            buckets[(cap.name, cap.version)].append((agent.agent_id, cap))
    rows = []
    for (name, version), entries in buckets.items():
        rows.append(_merge_bucket(name, version, entries))
    rows.sort(key=lambda r: (r["name"], -r["version"]))
    return rows
```

`_merge_bucket` implements §3.1.1. Cost: O(total_capabilities_across_online_agents). At 50 agents × ~8 caps each = 400 entries; trivial.

### 3.2 Routing algorithm

Today (`hub/registry.py:find_available`):

```python
for agent in self._agents.values():
    if agent.status != AgentStatus.IDLE: continue
    if task_type in agent.capabilities or not agent.capabilities:
        return agent
```

`task_type in agent.capabilities` works today because `agent.capabilities` is `list[str]`. Once we normalize to `list[Capability]`, the `in` operator no longer fires. New algorithm:

```python
def find_available(self, task_type: str, min_version: int = 1) -> Optional[Agent]:
    """Pick the first IDLE agent advertising (task_type, version >= min_version).

    Tiebreak: registry insertion order (preserved by dict iteration since
    Python 3.7), matching today's behavior. No version preference among
    candidates — the FIRST idle agent wins, even if a later agent advertises
    a strictly higher version. This matches the existing 'first idle wins'
    policy and avoids surprising operators who upgrade one agent and watch
    it monopolize the queue.

    Empty-capabilities sentinel preserved: an agent with no capabilities
    matches anything (today's wildcard agent behavior, used by tests).
    """
    with self._lock:
        for agent in self._agents.values():
            if agent.status != AgentStatus.IDLE:
                continue
            if not agent.capabilities:
                return agent                       # wildcard, unchanged
            for cap in agent.capabilities:
                # cap is always Capability post-normalize. Defensive
                # str-fallback kept for unit tests that build Agent
                # directly without going through register_agent.
                cap_name = cap.name if isinstance(cap, Capability) else str(cap)
                cap_ver  = cap.version if isinstance(cap, Capability) else 1
                if cap_name == task_type and cap_ver >= min_version:
                    return agent
    return None
```

`dispatch_pending_for_agent` similarly walks `agent.capabilities`:

```python
# hub/main.py:dispatch_pending_for_agent — the existing predicate
#   task.target_agent or task.task_type in agent.capabilities or not agent.capabilities
# becomes:
def _agent_can_handle(agent: Agent, task: Task) -> bool:
    if not agent.capabilities:
        return True
    floor = task.min_version or 1
    for cap in agent.capabilities:
        if cap.name == task.task_type and cap.version >= floor:
            return True
    return False
```

**Complexity.** `find_available` is O(N agents × M caps_per_agent), same big-O as today (the `in` operator on a list is also O(M)). Caps are normalized once at register time so per-dispatch cost is just dict iteration + comparison; no JSON parsing or `BaseModel` instantiation in the hot path. At the documented 10–50 agents × ~8 caps each, this is sub-microsecond.

If a future workload pushes caps_per_agent to hundreds, the obvious next step is `Agent._cap_index: dict[str, int]` (name → max version) maintained by `register`. Out of scope for v1; documented as the cheap escape hatch.

### 3.3 `Task.min_version` and `TaskSubmitRequest.min_version`

**Decision (answer to scope question (a)): persist on `Task`.**

Rationale: a `min_version: 2` task that is queued because no v2 agent is online MUST still pick a v2 agent when one comes up minutes later. Two paths consume it:

1. `dispatch_pending_for_agent(agent_id)` rescans `queue.list_pending()` and re-routes — needs the floor on the persisted task.
2. `requeue_assigned_to(agent_id)` (when an agent disconnects mid-task) puts the task back in `pending`; the next dispatch must keep the same min_version constraint.

Adding the field to `Task` is a Pydantic-only change; the SQLite `tasks` table does **not** require an `ALTER TABLE` because we serialize `min_version` inside the existing `payload` JSON or, cleaner, alongside priority. **However:** scope.md says "DB must NOT require a migration." The `payload` field is already a JSON column we own and can stash a reserved key in. To stay surgical we choose:

> **Persist `min_version` as `payload.__min_version` (double-underscore reserved key).** `save_task` and `parse_task_row` honor it: when serializing, copy `task.min_version` into `payload["__min_version"]` if non-default; when deserializing, pop `__min_version` off `payload` back into `Task.min_version`. This keeps the DB schema unchanged and survives restart. The reserved-key namespace (`__`-prefixed) is documented as off-limits to user payloads.

This is the only place we deviate from "pure additive Pydantic" because of the no-migration constraint. If a future PR is willing to do an `_ensure_column` add (the existing `_ensure_column` helper in `database.py` was made for exactly this), we can lift `min_version` to a real column. Today: stash in payload, marker key, documented.

`TaskSubmitRequest.min_version: int = 1` is plain Pydantic.

### 3.4 MCP changes

| Tool | Change |
|---|---|
| `register-agent` | `inputSchema.capabilities.items` widens from `STRING` to `oneOf: [STRING, {object:{name,version,payload_schema,result_schema}}]`. Server normalizes via `normalize_capabilities`. |
| `submit-task` | Adds optional `min_version: INTEGER` (default 1). Same wiring through `TaskSubmitRequest`. |
| **`list-capabilities` (NEW)** | `inputSchema: {}`. Returns the same shape as `GET /capabilities`. Auth gate: **`can_view_agents`** (answer to scope question (e)). Reasoning: `list-agents` is gated by `can_view_agents`; `list-capabilities` is strictly less sensitive (it's an aggregation, not raw agent state). Admin-only would be paranoid. |
| Backend wiring | `InProcessHubBackend` adds `("GET","/capabilities")` → `main.list_capabilities(self._authorization)`. `HttpHubBackend` is unchanged (uses path-based dispatch). |

Updated `register-agent` `inputSchema` snippet:

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

## 4. Answers to the 5 open scope questions

| # | Question | Answer |
|---|---|---|
| a | Should `min_version` be on `Task` or on `TaskSubmitRequest` only? | **Both.** Pydantic field on both. Persisted via `payload.__min_version` reserved key (no DB migration). §3.3 covers the requeue and rescan rationale. |
| b | What does aggregation do when two agents advertise `code:v2` with conflicting schemas? | **First-non-null-by-agent-id wins, conflict flag set, single activity_log entry.** Bare-advertiser anywhere in the bucket forces aggregated schema to `null`. Hub does not attempt JSON Schema merge. §3.1.1. |
| c | `AGENT_HUB_VALIDATE_PAYLOAD` default? | **`warn`.** Strict-by-default breaks existing tests; `off`-by-default forfeits the observability the feature exists for. §F3.3. |
| d | Bare strings get `version=1` or `version=0`? | **`version=1`.** A submit with `min_version=1` (the default) MUST match a bare `"echo"`. With `version=0` we'd silently miss every existing agent. The cost is that schema-aware agents must start their version at `2` to differentiate; we document this as the convention. |
| e | MCP `list-capabilities`: admin-only or `can_view_agents`? | **`can_view_agents`.** Same gate as `list-agents`; strictly less sensitive (it's an aggregation). §3.4. |

---

## 5. Module-by-module change list

| File | Modified | New | Purpose |
|---|---|---|---|
| `hub/models.py` | `Agent.capabilities` widens to `list[Union[str, Capability]]`. `Task` gains `min_version: int = 1`. `TaskSubmitRequest` gains `min_version: int = 1`. | `Capability` Pydantic model. | F3 wire shape + version floor. |
| `hub/capabilities.py` | — | `normalize_capability`, `normalize_capabilities`, `aggregate_capabilities`, `validate_payload`, `PayloadSchemaError`. Module-level `_DEFAULT_MODE = os.getenv("AGENT_HUB_VALIDATE_PAYLOAD","warn").lower()`. | F3 logic, F4 aggregation. |
| `hub/registry.py` | `register(...)` calls `normalize_capabilities` once before constructing `Agent`. `find_available(task_type, min_version=1)` per §3.2. | — | F4 routing. |
| `hub/router.py` | `route(...)` accepts `min_version` and forwards to `registry.find_available(task_type, min_version)`. Empty-target path unchanged. | — | F4 routing. |
| `hub/database.py` | `save_agent`: feed `agent_data["skills"]` through normalization → JSON-serializable list of dicts. `_agent_from_row` callers (in `main.py`) re-normalize on read. `save_task`: copy `task_data["min_version"]` into `payload["__min_version"]` if `> 1`. `parse_task_row`: pop `__min_version` back out into the dict. | — | F3 storage, F4 min_version persistence. |
| `hub/main.py` | `_agent_from_row` runs `normalize_capabilities` on the JSON-decoded skills. `register_agent` (REST) and the WS register handler call `normalize_capabilities` on `req.capabilities`/`first_msg["capabilities"]`. `submit_task` reads `req.min_version`, plumbs into `Task` and `route_and_dispatch`. `route_and_dispatch` and `dispatch_pending_for_agent` use `_agent_can_handle(agent, task)` (§3.2). `submit_task` and `_handle_submit_child` invoke `validate_payload(...)` after task-type resolution + capability lookup. | `list_capabilities` REST handler. `_agent_can_handle` helper. `_resolve_payload_schema(task, agent)` helper. | F4 endpoint, version-aware routing, validation hook. |
| `hub/mcp_protocol.py` | `register-agent` schema widens (oneOf string/object). `submit-task` schema gains `min_version`. `InProcessHubBackend` route table gains `GET /capabilities`. `call_tool` gains `list-capabilities` branch. | `list-capabilities` entry in `TOOLS`. | F4 MCP surface. |
| `requirements.txt` | Add `jsonschema>=4,<5`. | — | Validation lib. |
| `tests/test_unit.py` | — | 5 new unit tests (§6). | Coverage. |
| `tests/test_rest.py` | — | 2 new HTTP tests (§6). | Coverage. |
| `tests/test_websocket.py` (or new `test_phase22.py`) | — | 1 new combined-with-delegation test (§6). | F1+F3 interop. |

No new files beyond `hub/capabilities.py` and the optional `tests/test_phase22.py`. No DB migration.

---

## 6. Test plan

≥ 8 new pytest cases. Names use the `test_p22_` prefix; ID suffixes follow the `FR-CAP-*` family so traceability matches `test_plan.md` style.

| Test | ID | Behavior |
|---|---|---|
| `test_p22_cap_1_bare_string_normalizes_v1` | FR-CAP-1 | `register_agent(capabilities=["echo"])` → `agent.capabilities[0] == Capability(name="echo", version=1, payload_schema=None, result_schema=None)`. Existing `find_available("echo")` still matches. |
| `test_p22_cap_2_object_capability_registration` | FR-CAP-2 | `register_agent(capabilities=[{"name":"code","version":2,"payload_schema":{"type":"object","required":["src"]}}])` round-trips through DB unchanged: re-load via `_agent_from_row` → `Capability(name="code", version=2, payload_schema={...})`. |
| `test_p22_cap_3_mixed_list_serializes_and_loads` | FR-CAP-2 | Register `["echo", {"name":"code","version":2}]`. Inspect `agents.skills` JSON column directly: must be `[{"name":"echo","version":1,...}, {"name":"code","version":2,...}]`. Reload → both round-trip. |
| `test_p22_rte_1_min_version_match` | FR-RTE-7 | Two idle agents: A1 advertises `code:v1`, A2 advertises `code:v2`. `find_available("code", min_version=2)` returns A2. `find_available("code", min_version=1)` returns the first registered (A1, preserving Phase 1 tiebreak). |
| `test_p22_rte_2_min_version_miss_queues` | FR-RTE-8 | One idle agent advertising `code:v1`. `submit_task(task_type="code", min_version=2)` → task stays `queued`, no dispatch. Then register A2 with `code:v2` → pending dispatch picks it up. |
| `test_p22_disc_1_get_capabilities_aggregates` | FR-CAP-3 | Three agents advertising `code:v2` (two with identical schema, one with `payload_schema=null`); two agents advertising `echo:v1`. `GET /capabilities` returns 2 `code` rows (v2 with `agent_count=3`, `payload_schema=null`, `aggregated_schema_conflict=true`; nothing else) and one `echo` row with `agent_count=2`. Sorted name-asc, version-desc. Auth: returns 403 without `can_view_agents`. |
| `test_p22_val_1_warn_mode_logs_and_dispatches` | FR-CAP-4 | `AGENT_HUB_VALIDATE_PAYLOAD` unset (default `warn`). Agent advertises `code:v2` with `payload_schema={"type":"object","required":["src"]}`. `submit_task(task_type="code", min_version=2, payload={"wrong":"yes"})` → returns 200 with `status=assigned`, AND `activity_log` has one `payload_schema_warn` row pointing at the missing `/src` field. |
| `test_p22_val_2_strict_mode_rejects_400` | FR-CAP-5 | `monkeypatch.setenv("AGENT_HUB_VALIDATE_PAYLOAD","strict")` + reload `hub.capabilities`. Same agent + bad payload → REST returns HTTP 400 with the `payload_schema_violation` detail dict. Task is NOT created (assert `len(queue.list_all())` unchanged). |
| `test_p22_mcp_1_list_capabilities_tool` | FR-CAP-6 | MCP `tools/list` includes `list-capabilities` with `can_view_agents` semantics. `tools/call list-capabilities` returns the same shape as `GET /capabilities`. With a key lacking `can_view_agents`, the call surfaces an MCP `error.code: -32001`. |
| `test_p22_del_1_submit_child_with_min_version` | FR-CAP-7 (combines FR-DEL-1) | **F1+F3 interop.** Spawn agent A (capabilities `["plan"]`) and two agents B1 (`["code"]` bare) and B2 (`[{"name":"code","version":2,"payload_schema":{"type":"object","required":["src"]}}]`). A receives a `plan` task. From inside the SDK handler, A calls `submit_child(target_agent=None, task_type="code", min_version=2, payload={"src":"x"})` — wait, scope says `target_agent` is required for `submit_child`. So instead: A calls `submit_child(target_agent="b2", task_type="code", min_version=2, payload={"src":"x"})`. The hub validates the payload against B2's schema (default warn mode → no warn entry because the payload is valid), routes the child to B2, B2 completes, A receives `child_completed`. Negative half: a *second* `submit_child(target_agent="b1", task_type="code", min_version=2, …)` is rejected because B1 advertises only v1; hub responds `child_rejected: capability_unavailable` (new reason — the routing pre-check catches this; see implementation note below). |

**Implementation note for the combined test.** `_handle_submit_child` (Phase 2) currently queues the child and then routes via `route_and_dispatch`, which would just sit `queued` forever if B1 can't satisfy `min_version=2`. F4 adds a pre-flight check inside `_handle_submit_child` after step 7 (post-`queue.create`) but before step 8 (registry bookkeeping):

```python
if target_agent_id is not None:
    target = registry.get(target_agent_id)
    if not _agent_can_handle(target, child_task):
        # roll back the row + emit child_rejected
        queue.delete(child_id); db.delete_task(child_id)
        return _send_child_rejected(parent_agent_id, request_id,
                                    "capability_unavailable",
                                    f"{target_agent_id} does not advertise {task_type}@v{min_version}+")
```

This is the only Phase-2 code touched by Phase 2.2. It's additive — does not alter the cycle-check, self-fast-path, or `finalize_task` flow.

---

## 7. Backward-compatibility — what must keep passing unchanged

**Tests that MUST keep passing without edits:**

- `test_unit.py::test_u_reg_3_idle_agents_filter` — registers `["echo"]`, expects `find_available("echo")` match. Normalization → `Capability(name="echo", version=1)`; `min_version` defaults to 1; matches.
- `test_unit.py::test_u_rte_1..6` — all router unit tests pass capability-strings. Same path.
- `test_unit.py::test_u_que_*`, `test_u_mod_*` — unrelated.
- `test_rest.py::*` — every existing `POST /tasks` call omits `min_version`; default 1 matches every Phase 1 agent. `GET /agents` response gains `version`/`payload_schema` keys per capability — tests assert presence-of, not absence-of, so they're tolerant.
- `test_websocket.py::*` — registration messages send bare-string capabilities; normalization is invisible.
- `test_mcp_protocol.py::*` — `submit-task` schema additively gains `min_version`; existing `tools/list` snapshot tests (if any) need a one-line schema-update or assertion-relaxation. **Check this first** — if there's a pinned schema string, that's the only test that might need a touch.
- All Phase 2 delegation tests (`test_p2_del_*`) — `submit_child` payload doesn't grow a `min_version` field by default; `_handle_submit_child` uses `min_version=msg.get("min_version",1)` which keeps every Phase 2 test on the v1 floor.
- All E2E scripts in `scripts/e2e/` — capabilities are bare strings throughout; routing matches.

**Behaviors that MUST keep passing unchanged:**

- Empty-capabilities agent (`capabilities=[]`) is a wildcard for all `task_type`. Code path explicit in §3.2.
- Tiebreak among same-version idle agents: registry insertion order. §3.2 documents this is preserved.
- `agent_registered` SSE payload still includes the *input* `capabilities` (the wire shape the client sent), so dashboards parsing `["echo"]` keep working. Internal `Agent.capabilities` is normalized; the SSE wrapper builds `{"agent_id": ..., "capabilities": req.capabilities}` from the request, not from the registry copy.
- `dispatch_pending_for_agent` rescan-on-IDLE keeps working: `_agent_can_handle` is the only change inside the loop.
- `requeue_assigned_to(agent_id)` keeps working: `min_version` lives on the persisted task (via `payload.__min_version`), survives requeue, is re-applied on next dispatch.
- `AGENT_HUB_VALIDATE_PAYLOAD` unset → behavior is `warn`, which is *additive* (logs only, never rejects). Tests that don't grep activity_log are unaffected.

---

## 8. Open questions for review (codex challenge surface)

1. **Reserved-key persistence for `min_version`.** Stashing `__min_version` inside `payload` keeps the no-migration constraint, but it pollutes a user-owned namespace. Codex may argue for the cleaner `_ensure_column(cursor, "tasks", "min_version", "INTEGER DEFAULT 1")` add — which `database.py` already supports. Worth reconsidering since `_ensure_column` is *not* a "migration" in the disruptive sense; it's a guarded `ALTER TABLE`.
2. **Tiebreak when multiple idle agents satisfy `min_version`.** Currently first-registered wins regardless of version. Alternative: pick the *lowest* version that satisfies the floor, leaving higher-version agents free for higher-floor traffic. Operationally nicer; semantically a behavior change for Phase 1 tests if we're not careful (only relevant when `min_version > 1`, which Phase 1 never sets — so probably safe but worth a pass).
3. **Schema conflict policy.** First-by-agent-id is deterministic but arbitrary. Codex may push for **reject the registration** of a conflicting schema instead — operator-visible at the moment of error rather than buried in an aggregation flag.
4. **`payload_schema` for bare-advertiser dominance.** §3.1.1 says "any bare → aggregated null." Alternative: emit one row per *distinct* schema (so `code:v2` → "schema A: 2 agents" + "schema B: 1 agent" + "no schema: 1 agent"). Richer for clients; uglier in the dashboard.
5. **Validation at dispatch time as a second gate.** We chose submit-time only. If an agent re-registers between submit and dispatch with a *new* schema, the queued task's payload was validated against the old schema. Acceptable? Likely yes (the agent advertising the new schema also needs to handle pre-existing queued work) but worth flagging.
6. **`list-capabilities` and `allowed_agents`.** A key with `allowed_agents=["coder-1"]` can `submit-task` only against `coder-1`, but `list-capabilities` would show all agents' capabilities. Should the aggregation be filtered by `allowed_agents`? Phase 1 `list-agents` does NOT filter; we're keeping parity for v1. Codex may push for filtering.

---

**End of design.** Implementation belongs to coder sub-agents; no production code is written in this document.
