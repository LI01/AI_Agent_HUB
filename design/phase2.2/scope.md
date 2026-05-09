# Phase 2.2 Scope — Capability Negotiation / Versioned Protocols

**Locked by:** claude (PM) on 2026-05-09 05:21 local
**Per:** "what features left for phase 2 → phase 2.2 same approach as Phase 2." Picked the smallest-credible interpretation of `requirements.md` §4.3 item "Agent capability negotiation or versioned protocols."

## What we're shipping

### F3 — Versioned, schema-bearing capabilities

An agent's `capabilities` field, today `list[str]` of bare names, becomes `list[Capability]` where each Capability is either:

- A **bare string** like `"echo"` — backward-compat, treated as `name="echo", version=1, payload_schema=null, result_schema=null`.
- An **object** `{"name": "code", "version": 2, "payload_schema": {...JSONSchema...}, "result_schema": {...JSONSchema...}}` — version is a positive integer; schemas are optional but recommended.

Existing agents that send bare strings keep working unchanged.

### F4 — Capability discovery + version-aware routing

- New REST endpoint **`GET /capabilities`**: lists every `(name, version)` pair currently advertised by ≥1 online agent, with the union of advertised schemas (or `null` if any agent advertises bare).
- **Routing**: `POST /tasks {"task_type": "code"}` matches an agent advertising any version of `code`. `POST /tasks {"task_type": "code", "min_version": 2}` matches only agents with `code:v≥2`. If multiple match, current tiebreak (registry insertion order) applies.
- **Payload validation**: when `task.task_type` resolves to a capability with a `payload_schema`, the hub validates `task.payload` against it. Default mode is **warn-only** (logs activity_log entry, dispatches anyway). `AGENT_HUB_VALIDATE_PAYLOAD=strict` rejects with HTTP 400 + JSON-RPC error for MCP.

## Concrete data model changes

- `Agent.capabilities: list[str]` → `list[Union[str, Capability]]` in Pydantic. New `Capability(BaseModel)` with `name: str`, `version: int = 1`, `payload_schema: Optional[dict] = None`, `result_schema: Optional[dict] = None`.
- DB column `agents.skills TEXT` keeps its JSON shape; bare-string and object-form mix freely in the JSON array. **No schema migration needed.**
- `Task.task_type` unchanged. New optional `Task.min_version: int = 1` field.
- `TaskSubmitRequest` gains `min_version: int = 1`.

## Concrete API changes

- `POST /register` body: `capabilities` accepts the new shape; existing flat-string clients continue to work.
- `GET /capabilities` (new): returns `[{"name": "code", "version": 2, "payload_schema": {...}, "result_schema": {...}, "agent_count": 3}, ...]`. Sorted by name then version desc. Auth gated by `can_view_agents`.
- `POST /tasks` body: `min_version` optional integer.
- MCP tool `register-agent`: schema updated to accept the new capability shape.
- MCP tool `submit-task`: `min_version` added.
- New MCP tool `list-capabilities`: thin wrapper over `GET /capabilities`.

## Concurrency / state

- `AgentRegistry.find_available(task_type)` becomes `find_available(task_type, min_version=1)`. The lookup walks each idle agent's capability list, normalizes bare strings to `(name, version=1, schema=None)`, and returns the first agent with a matching `(name, version)` where `version >= min_version`.
- Routing path in `hub/main.py:route_and_dispatch` carries `min_version` from the task.
- A new module `hub/capabilities.py` holds: `normalize_capability(item) -> Capability`, `aggregate_capabilities(agents) -> list[dict]` for `/capabilities`, `validate_payload(payload, schema, mode) -> Optional[str]` returning an error string or None.

## Backward-compat

All Phase 1 + 1.5 + 2 tests must keep passing unchanged:
- Existing tests that pass `capabilities=["echo"]` get auto-normalized to `[Capability(name="echo", version=1)]`. Routing against `task_type="echo"` (no `min_version`) matches as before.
- Existing tests that pass `task_type="echo"` without `min_version` see no behavior change.
- `parent_task_id` filtering, F1 delegation, MCP — all unaffected.

## Acceptance criteria for the design pass

A design is acceptable when:
1. Module-by-module change list with file paths + new/modified functions.
2. JSON shapes for new endpoint (`GET /capabilities`) + register payload + task submit payload + MCP tool schemas.
3. Routing algorithm spelled out: how `find_available` handles mixed bare-string/object capability lists.
4. Validation behavior: when does the hub actually run JSON-Schema validation? Default mode? Where's the env var read?
5. Migration story: existing DB rows with `skills=["echo"]` JSON keep working.
6. Test plan adds at least 8 new pytest cases: bare-string backward-compat, object capability registration, version routing (min_version match + miss), `GET /capabilities` aggregation across multiple agents, payload validation warn vs strict, MCP `list-capabilities` tool, F1 delegation across versioned capabilities (combined with Phase 2's `submit_child`).
7. No new top-level dependencies. Use the standard library `jsonschema` if needed (already pulled in transitively, or minimal pure-Python validation).

## Open design questions for the designer to answer

a. **Should `min_version` be on Task or on TaskSubmitRequest only?** (i.e. persisted or just advisory)
b. **What does aggregation do when two agents advertise `code:v2` with conflicting schemas?** Pick first, error, or merge?
c. **`AGENT_HUB_VALIDATE_PAYLOAD` default**: `warn` or `off`? Strict-by-default would break tests that use sloppy payloads.
d. **Should bare strings get `version=1` or `version=0`?** Affects `min_version: 1` matching.
e. **MCP `list-capabilities` should be admin-only or `can_view_agents`?**
