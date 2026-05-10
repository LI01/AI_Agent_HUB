# Phase 2.3 Scope — LLM-CLI Worker Skills

**Locked:** 2026-05-09 15:26 local. **PM-approved spec from chat brainstorm.**

## What we're shipping

Three Claude-Code/OpenCode skills + three per-CLI worker adapters that turn `codex`, `claude`, and `opencode` CLIs into agents that register on the hub and execute tasks.

### Skills (under `skills/`)

- **`skills/agent-spawn/`** — interactive: ask hub URL (default from `~/.agent-hub/config.json`), API key (default from same), CLI (`codex` / `claude` / `opencode`), role (`pm` / `architect` / `designer` / `coder` / `reviewer` / `tester` / `generic`), optional `pipeline_id` for shared workspace, `--max-budget-tokens` (suggested per role, configurable). Spawns the chosen adapter as a subprocess. Records `{agent_id, pid, cli, role, hub_url, workdir, started_at}` into `~/.agent-hub/spawned.json`. Verifies CLI is on PATH and `<cli> --version` works before spawning.
- **`skills/agent-tasks/`** — interactive: list shows `agent_id | cli | role | status | capabilities`. Picker → choose agent → choose `task_type` from the agent's advertised capabilities → if Phase 2.2 `payload_schema` is present, prompt for each required field with hints → submit task → live-poll until terminal → display `result.text` and the raw JSON. Show estimated token usage if returned by the CLI.
- **`skills/agent-close/`** — list spawned-by-this-skill agents (PID-known from `spawned.json`) AND any other registered agents fetched via `GET /agents`. Pick one or many → SIGTERM the local PID → wait 5 s → SIGKILL fallback → call REST `POST /unregister?agent_id=<id>` either way. Works on remote-only agents (no local PID) by using `/unregister` only.

### Per-CLI worker adapters (under `clients/`)

- **`clients/codex/llm_agent.py`** — wraps `codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -C <workdir> "<prompt>"`. Parses last assistant text from output.
- **`clients/claude-code/llm_agent.py`** — wraps `claude -p --bare --output-format json --append-system-prompt "<role-prompt>" --add-dir <workdir> "<prompt>"`. Parses `response` field; extracts `usage` for budget tracking.
- **`clients/opencode/llm_agent.py`** — wraps `opencode run --format json "<prompt>"` (and `-c` to continue session for memory across consecutive tasks). Parses last message text; extracts usage if present.

Each adapter:
- Uses `agent_sdk.AgentHub` (sync) to register on WS.
- Runs in `~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/` (option C — shared per-pipeline workspace).
- On task receipt: shells out to its CLI with role-defaulted system prompt + payload prompt, captures stdout/JSON, returns `{"text": ..., "usage": {...} | None, "raw": ..., "cli": "...", "role": "...", "elapsed_s": ...}`.
- Wraps subprocess in `timeout=task.timeout - 5` so the CLI dies before the hub's timeout watcher fires.
- If task payload `max_budget_tokens` is set AND the CLI returns usage AND usage > budget, includes a `payload_schema_warn`-style note in the result and writes one `activity_log` entry via the hub. **Warn-only for v1; no mid-task kill.**

### Role presets (built into both skills and adapters)

| Role | Capabilities | System prompt (one-line gist) | Default budget |
|---|---|---|---|
| pm | `["pm", "plan", "coordinate"]` | "You are the PM. Decompose, sequence, escalate." | 30000 |
| architect | `["architect", "design"]` | "You are the architect. Module boundaries, data flow, tradeoffs." | 50000 |
| designer | `["design", "spec"]` | "You are the designer. Concrete schemas, message shapes." | 40000 |
| coder | `["code"]` | "You implement features. Match style. Test-first." | 30000 |
| reviewer | `["review", "code-review"]` | "You critique. Find real bugs and design issues, ignore style nits." | 20000 |
| tester | `["test"]` | "You write tests that fail without the change and pass with it." | 20000 |
| generic | `["chat"]` | (no preset) | 20000 |

Defaults live in a single `clients/role_presets.py` (or equivalent) imported by all three adapters AND the spawn skill so role definitions don't fork.

### Config + state

- `~/.agent-hub/config.json` (global default): `{hub_url, api_key, role_budgets: {role: int}, role_prompts: {role: str}}`. All fields optional; missing fields use built-in defaults.
- `.agent-hub.local.json` in cwd (per-project override, gitignored): same shape; merged on top of global.
- `~/.agent-hub/spawned.json` (skills internal): `[{agent_id, pid, cli, role, hub_url, workdir, started_at}, ...]`. `agent-close` updates it.

### Auth precondition

Spawn skill checks `<cli> --version` returns 0 and (best-effort) checks the CLI is authenticated by trying a quick no-op invocation. If not, tells the user to run `codex login` / `claude login` / `opencode auth` (whichever applies) and aborts. Does not try to manage CLI auth itself.

## Acceptance criteria

A design pass is acceptable when:

1. Module-by-module change list with file paths + new/modified functions.
2. JSON shapes for each skill's interactive prompts (in SKILL.md format) AND `~/.agent-hub/config.json` AND `~/.agent-hub/spawned.json`.
3. Per-CLI adapter pseudocode showing: WS register, on-task shell-out command, output parsing, error handling.
4. `clients/role_presets.py` (or chosen filename) full preset table + how skills/adapters import it.
5. Workspace strategy — exactly how `~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/` is created, by whom, and what happens if pipeline_id is omitted.
6. Test plan: at least 6 new pytest cases. Format: `<test_name> → <FR/NFR id like FR-LLM-1> → <one-line behavior>`. Cases should cover: spawned.json round-trip; role preset lookup; per-CLI adapter command construction (mock subprocess); budget warning path; close skill SIGTERM-then-SIGKILL; list skill rendering of remote-only vs local-known agents.
7. NO new top-level dependencies. The CLIs themselves are external (codex/claude/opencode). Adapters use `subprocess` (stdlib) + existing `agent_sdk`/`httpx`.

## Open design questions for the designer to answer

a. **Spawn skill UX** — can it run fully non-interactively too (all flags on the command line) for scriptable spawning?
b. **`~/.agent-hub/spawned.json` concurrency** — two skills running at once. File lock? Atomic write?
c. **Pipeline workspace cleanup** — does anything purge `~/.agent-hub/pipelines/<id>/`? Manual? Time-based?
d. **OpenCode session continuity** — `opencode run -c` continues the last session. Should the adapter use one persistent OpenCode session per agent_id (via `-s <session_id>`), so the agent's memory accumulates across tasks? Or fresh session per task for determinism?
e. **Budget tracking for codex** — codex exec output doesn't expose token usage by default. How do we estimate / report?
f. **Role override** — should `--role custom` accept `--capabilities a,b,c --system-prompt "..."` for ad-hoc roles?
g. **Skill 2 payload_schema-aware prompting** — for required fields with `type: object`, does the skill prompt for raw JSON or for each subfield?

## Backward-compatibility

- Existing `clients/codex/agent.py`, `clients/openclaw/agent.py`, `clients/hermes/agent.py` keep working unchanged.
- Existing `skills/agent-hub/` (Phase 1.5 MCP onboarding skill) keeps working — the new skills are additive.
- All Phase 1, 1.5, 2, 2.2 tests must keep passing.
