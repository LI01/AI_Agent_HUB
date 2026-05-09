# Agent Hub — Phase 1 Test Report

**Tested by:** claude
**Last update:** 2026-05-09 (Phase 2.2 F3+F4 added: 109 → 127)
**Suite:** `tests/` (pytest), 127 cases
**Result:** **127/127 pass.** Phase 2.2 delivered autonomously by sub-agents in 16 invocations (`Agent-comm/claude_phase2.2-delivered_20260509-0631.md`).
**Run command:** `.venv/bin/python -m pytest tests/ -v`

---

## Coverage by Phase

| Phase | Plan section | Files | Cases | Pass | Fail |
|-------|--------------|-------|-------|------|------|
| A — Unit + REST integration | `test_plan.md` §2, §3 | `tests/test_unit.py`, `tests/test_rest.py` | 55 | 55 | 0 |
| B — WebSocket + SDK | `test_plan.md` §4, §6 | `tests/test_websocket.py` | 10 | 10 | 0 |
| C — Persistence + concurrency + security | `test_plan.md` §5.2, §8, §9 | `tests/test_persistence_security.py` | 12 | 12 | 0 |
| D — MCP protocol (Phase 1.5) | `requirements.md` §9.9 FR-MCP-7 | `tests/test_mcp_protocol.py` | 5 | 5 | 0 |
| E — F1 delegation + F2 parent_task_id (Phase 2) | `design/phase2/design_v3.md` §5 | `tests/test_unit.py`, `tests/test_websocket.py`, `tests/test_rest.py`, `tests/test_persistence_security.py`, `tests/test_sdk.py` | 27 | 27 | 0 |
| F — F3 versioned capabilities + F4 discovery (Phase 2.2) | `design/phase2.2/design_v2.md` §6 | `tests/test_unit.py`, `tests/test_persistence_security.py`, `tests/test_rest.py`, `tests/test_websocket.py`, `tests/test_mcp_protocol.py` | 18 | 18 | 0 |
| **Total** | | | **127** | **127** | **0** |

**Phase D (MCP) coverage:** `tools/list` exposes the FR-MCP-2/3 schemas; `tools/call submit-task` round-trips through `get-task`; permission failures map to JSON-RPC errors without leaking raw keys; revoked-mid-session keys fail on the next tool call (per-call `access_control` revalidation works); stdio entrypoint smoke does not write to the wrong DB path.

**E2E (`scripts/e2e/`, non-pytest):** §14 scenarios E2E-1, E2E-2, E2E-3, E2E-4, E2E-5, E2E-7 all PASS plus the new F1+F2 combined `run_e2e_delegation.py` (E2E-6 SSE remains skipped per its conditional flag). See `Agent-comm/claude_test-phase-E2E-v2_*.md` and `Agent-comm/claude_phase2-delivered_*.md`.

Phase reports for the agent communication channel: `Agent-comm/claude_test-phase-A_20260508-2139.md`, `Agent-comm/claude_test-phase-BC_20260508-2258.md`.

---

## Coverage by Phase A — Unit + REST

| Section | Cases | Notes |
|---------|-------|-------|
| §2.1 U-AUTH | 7 | hash, perms, allowed_agents, admin, expired, inactive, env-bootstrap |
| §2.2 U-REG | 4 | duplicate upsert per design §2.1 |
| §2.3 U-QUE | 4 | priority + FIFO within priority |
| §2.4 U-RTE | 6 | targeting + capability matching + NL detection |
| §2.5 U-MOD | 3 | JSON round-trip, UUIDv4, defaults |
| §3.1 I-REG | 6 | register/heartbeat/unregister + auto-dispatch |
| §3.2 I-TSK | 5 | minimal, payload, auth, target, priority |
| §3.3 I-QRY | 8 | get/list/filter/health/stats + perm gating |
| §3.4 I-ACL | 5 | create returns raw once, list omits secrets, revoke |
| Regressions for design-review §1 | 6 | 1.1, 1.2, 1.4, 1.6, 1.8, 1.9 all green |
| Findings | 1 | Finding A1 — see below |

## Coverage by Phase B — WebSocket + SDK

| Section | Cases | Notes |
|---------|-------|-------|
| §4.1 I-WS connection lifecycle | 4 | register ack, invalid token closed, first-msg-must-be-register, reconnect closes old (regression §1.7) |
| §4.2 I-WS task delivery | 4 | task delivered, result+logs persisted, log appended, disconnect requeues |
| §4 WS report ownership | 1 | regression §1.6 on WS path |
| §6 SDK | 1 | `AsyncAgentHub.run()` end-to-end |

Each WS test spawns a fresh uvicorn process on a free port with its own SQLite DB.

## Coverage by Phase C — Persistence + Concurrency + Security

| Section | Cases | Notes |
|---------|-------|-------|
| §5.1 P-SYN | 2 | indexes (NFR-PER-4), WAL mode (NFR-CON-2) |
| §5.2 P-REC | 3 | queued tasks survive restart, assigned→requeued on restart, keys persist |
| §8 C-CON | 2 | 20 parallel registers, 50 parallel submits → all unique UUIDs |
| §9 SEC | 4 | unauth rejected, only hashes in DB, raw key not in `/admin/keys`, hardcoded `agent-hub-seed-admin-key-12345` rejected |
| §1.5 timeout regression | 1 | scanner flips expired tasks to terminal |

---

## Critical-Bug Regressions — All Green

Every item from `Agent-comm/claude_design-review_20260508-1701.md` §1 has an explicit test:

| Item | Test |
|------|------|
| 1.1 `_agent_from_row` enum mapping | `test_regression_1_1_agent_status_restored_correctly` |
| 1.2 DB persists `assigned_agent_id` on assign | `test_regression_1_2_db_assigned_agent_id_persisted` |
| 1.3 Restart re-queue order | `test_p_rec_2_assigned_requeued_on_restart` |
| 1.4 `Router.detect_task_type` public | `test_regression_1_4_detect_task_type_is_public` |
| 1.5 Timeout enforcement | `test_regression_1_5_timeout_marks_task` |
| 1.6 `/report` ownership (HTTP + WS) | `test_regression_1_6_report_ownership_check`, `test_i_ws_report_ownership_via_ws` |
| 1.7 WS reconnect closes old | `test_i_ws_8_reconnect_closes_old` |
| 1.8 Terminal-state guard | `test_regression_1_8_terminal_guard` |
| 1.9 `parse_task_row` reads description | `test_regression_1_9_parse_task_text` |

---

## Open Finding

### A1 — Single idle agent over-dispatched at registration

**Severity:** major (P0 routing correctness; masked in production by WS heartbeats)
**Affects:** FR-RTE-2, FR-RTE-5
**Repro:** `tests/test_rest.py::test_finding_dispatch_overflow_to_single_idle_agent`

`hub/main.py:316-328` `dispatch_pending_for_agent` does not flip the agent to BUSY after `route_and_dispatch`, so a single newly-registered idle agent claims every queued matching task at once. Real WS agents mask this by sending busy heartbeats; the hub should not depend on agent cooperation for routing correctness.

**Suggested fix:** in `route_and_dispatch`, after a successful `router.route(...)`, mark the agent BUSY in the registry before returning. Drop back to IDLE on `result` / disconnect / requeue (already handled in those code paths).

Bugfix request: `Agent-comm/claude_bugfix-request_dispatch-overflow_20260508-2304.md`.

---

## Not Tested — Honest Punch List

- §7 MCP — re-scoped per design review #2 (`mcp/server.py` is a CLI/REST bridge, not a real MCP protocol server). Defer to Phase 1.5.
- §10 portability — only Linux/macOS hit; Windows not exercised.
- §11 dedicated smoke script — covered by REST + WS suites; no separate `scripts/smoke.sh`.
- C-CON-3/4/5 — race scenarios under heavy WS churn; basic concurrency considered sufficient for MVP.
- `/unregister` body shape — tests use the existing query-parameter style; tracked as polish.
- NFR-SEC-3 CORS preflight + NFR-SEC-5 rate limiting — not exercised; rate limiting is P2.

---

## Ship Readiness

**Conditional pass.** Core MVP is functional, reliable, and secure under tested conditions. Blocked on Finding A1.

Once codex posts the A1 fix, rerun `.venv/bin/python -m pytest tests/` (~10s) and a final ship-readiness verdict can be issued.
