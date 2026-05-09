# Phase 2 Scope (Smallest Credible)

**Locked by:** claude (PM) on 2026-05-09 03:27 local
**Per-user decision:** "Smallest credible (#1 + #10)" from the v2 gap list.
**Out of scope for this Phase 2:** federation, DAGs, Web UI, Prometheus, agent groups, large-scale deployment, capability negotiation, streaming partial results beyond logs, observability (queryable activity log) — all stay deferred.

## Two features to design + implement

### F1 — Agent-to-agent task delegation (was §4.3 item #1)

An agent, while handling a task, can submit a child task to be done by another agent (or itself) and receive that child's result before returning its own result.

Concrete scenarios this unlocks:
- Planner agent → child code tasks → child review task → returns aggregated plan result.
- Coder agent → child "lookup-API-docs" task to a docs agent → uses the result before writing code.
- Removes the need for an external driver script (today's `scripts/e2e/run_pipeline.py` would be replaceable).

Open design questions to answer:
- How does an agent submit a child task — a new WS message type? A REST call? An SDK helper?
- How does the parent agent block on / await the child result without holding the WS in a way that breaks heartbeat?
- What permissions does a child task inherit — its own agent's key, the parent's key, the original submitter's key?
- What happens if the child fails or times out — bubble up to the parent? Mark parent failed?
- Cycle detection: A → B → A. How do we detect and refuse?

### F2 — `parent_task_id` honored by the hub (was §14.9 / #10)

Today the hub stores `parent_task_id` but does nothing with it. Design and implement:
- An index on `tasks.parent_task_id` (NFR-PER-4 follow-on).
- `GET /tasks?parent_task_id=...` filter (FR-QRY-2 extension).
- `parent_task_id` field included in `task_created`, `task_updated`, `task_completed` SSE event payloads (FR-SSE-2 extension).
- Documented semantics: completion of a parent does NOT cascade-cancel children (yet — that's Phase 3); completion of a child does NOT auto-complete the parent. Explicit in the design doc.

## Where Phase 1 + 1.5 left things

- Phase 1 hub at `hub/main.py`, `hub/auth.py`, `hub/database.py`, `hub/registry.py`, `hub/queue.py`, `hub/router.py`, `hub/models.py`. SQLite schema in `database.py:init_db()`.
- WebSocket protocol in `hub/main.py:464` (`/ws` endpoint) + `handle_agent_message`.
- SDK at `agent_sdk/client.py` — `AgentHub` (sync) + `AsyncAgentHub` (async).
- Phase 1.5 MCP at `hub/mcp_protocol.py` + `hub/mcp.py` + embedded `POST /mcp` in `main.py:677`.
- 82 pytest cases at `tests/`. 6 E2E scenarios at `scripts/e2e/`. All green.
- `parent_task_id` already exists as a Task field (`hub/models.py`) and a DB column (`hub/database.py:tasks` schema). Just inert.
- The `examples/echo-agent` and `scripts/e2e/planner.py / coder.py / reviewer.py` are reference agents that demonstrate the existing flows.

## Design constraints

- Same architecture: Python + FastAPI + SQLite + Pydantic.
- No new top-level dependencies unless justified.
- The SDK MUST stay backward-compatible — existing agents that don't use F1 must keep working.
- The WebSocket protocol MAY add new message types but MUST NOT change existing ones.
- API key permission model from `auth.py` MUST be respected — `can_assign_tasks` is the gate for child submission.
- Tests MUST be added for each new behavior; the existing 82-test suite must stay green.

## Acceptance criteria for the design pass

A design is acceptable when:
1. Both F1 and F2 have a complete module-by-module change list (file paths + new/modified functions + JSON shape changes).
2. New WS message types and REST/MCP endpoints are spec'd with full request/response schemas.
3. The five F1 open design questions above are explicitly answered.
4. Cycle detection has a concrete algorithm (e.g. ancestor-chain check on submit; max depth fallback).
5. A test plan adds at least 8 new pytest cases covering: child-submit happy path, child failure bubbling, cycle rejection, depth limit, permission inheritance, `parent_task_id` filter, SSE field, and at least one E2E scenario combining F1+F2.
6. A migration story for the SQLite index (additive — new index, no schema break).
7. Backward-compatibility called out: which existing tests / agents must continue to pass unchanged.
