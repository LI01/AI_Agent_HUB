# Phase 2.5 Scope — MCP Spec Compliance + Cross-Client Verification

**Locked:** 2026-05-10 16:52 local.

## What we're shipping

A full audit and fix pass on the hub's MCP server so it loads cleanly in **Claude Code**, **Codex**, and **OpenCode**. Today, Claude Code rejects `tools/list` because at least one tool's `outputSchema` violates the MCP spec (top-level `outputSchema.type` must be `"object"`). Suspected additional bugs in tool-call response shape ("server returned an array, client expected a record"). The fix surface is the hub's MCP protocol layer (`hub/mcp_protocol.py`, `hub/mcp.py`) and its tests.

## Goals

1. **Spec compliance.** Every MCP tool in `TOOLS` has:
   - `inputSchema.type == "object"` (already true; verify).
   - `outputSchema.type == "object"` *when present* — never `"array"` or other.
   - `structuredContent` returned from `tools/call` is an object matching `outputSchema` (when declared).
   - `content[0].text` mirror is the JSON-encoded `structuredContent`.
2. **Cross-client verification.** Each of Claude Code / Codex / OpenCode:
   - Connects to the hub via its preferred MCP transport (stdio for desktop tools; HTTP allowed if their client supports header auth without OAuth probe).
   - `tools/list` succeeds; all 13 tools discovered.
   - Invokes 3 representative tools end-to-end: `list-agents`, `submit-task`, `list-capabilities`. Returns parsed by the client.
3. **Both deployment paths working.**
   - **Local stdio:** `python -m hub.mcp` launched by Claude Code (config already in `~/.claude.json`). The fix lives in this repo, so stdio benefits immediately.
   - **Remote HTTP:** the hub running at `10.9.0.10:8300/mcp` must also serve the fixed schemas. The remote deploy is out-of-band — we deliver a patched hub the user can redeploy.
4. **Test suite green** for all MCP-related tests. The 4 pre-existing failures in `tests/test_unit.py` (opencode/claude adapter unit tests, unrelated to MCP) are out of scope for this phase.

## Non-goals

- **No new MCP tools.** This phase is fixing existing tools, not adding capabilities.
- **No OAuth implementation on the hub.** Bearer-header auth is the contract. Clients that probe OAuth and don't honor headers (e.g. Claude Code over HTTP today) should be addressed via stdio mode or a separate phase.
- **No protocol-version negotiation overhaul.** Use what the SDK currently negotiates (`2025-03-26`). Don't try to support older protocol versions.
- **No `resources` or `prompts` capabilities** — tools only, as today.
- **Don't refactor `hub/main.py` REST handlers** — those return what they return; wrap at the MCP boundary in `hub/mcp_protocol.py` when needed.
- **No CLI changes to the three clients themselves** — work with them as installed (`claude` 2.1.x, `codex` 0.130, `opencode`).
- **Don't fix the 4 pre-existing adapter unit-test failures.** Different phase.

## Constraints

- The remote hub at `10.9.0.10:8300` must keep REST API shape unchanged (`/capabilities` still returns top-level array per Fix F6) — only the **MCP boundary** wrapping changes.
- Backwards-compat for existing MCP callers: the only known MCP consumer today is internal testing + the new patched fixture. The `list-capabilities` MCP result shape is allowed to change because there are no external callers (verified via `grep`).
- Each tool's MCP-side wrapping must be documented in code with a one-line comment pointing to the MCP spec requirement, so the rationale survives future refactors.

## In-flight work to integrate

The PM (Claude in this session) already made an exploratory edit to `hub/mcp_protocol.py` patching `list-capabilities` outputSchema and wrapping its return as `{"capabilities": [...]}`. This is **uncommitted** and **should be treated as designer input, not a final answer** — designer-v1 may keep it, generalize it, or replace it. Tests `tests/test_mcp_protocol.py::test_p22_mcp_1_list_capabilities_tool` were updated to match the new shape and pass (6/6 MCP protocol tests green).

## Budget

- **Subagent invocations: 30.**
- Calendar time: best-effort same-session.

## Success criteria

1. `design_v<final>.md` reviewer-APPROVED, design frozen.
2. All coder slices reviewer-APPROVED.
3. `pytest tests/test_mcp_protocol.py` — all tests green.
4. Full `pytest` — no regression vs baseline (188 passed pre-existing). The 4 pre-existing failures remain; no new failures introduced.
5. Cross-client matrix (3 clients × {`tools/list`, `list-agents`, `submit-task`, `list-capabilities`}) — all 12 cells pass.
6. Final delivery doc lists every tool's outputSchema status (compliant / not declared) and what changed.
