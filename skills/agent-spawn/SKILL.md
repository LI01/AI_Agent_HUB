# Agent Spawn

Spawn a per-CLI LLM worker agent (codex / claude / opencode) and register it
with the Agent Hub.

This skill creates a workspace under `~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/`,
runs the matching adapter as a detached subprocess, and records the resulting
PID in `~/.agent-hub/spawned.json` for later management by `/agent-tasks` and
`/agent-close`.

**Trigger phrases:** "spawn agent", "create agent", "register agent",
"spawn an agent", "start an LLM agent", "register a codex agent",
"/agent-spawn".

## Usage

Interactive (the LLM walks the user through the prompts below):

```bash
/agent-spawn
```

Non-interactive (fully scriptable):

```bash
/agent-spawn --cli codex --role designer --pipeline-id phase23 \
             --hub http://localhost:8080 \
             --id codex-designer-1 --max-budget-tokens 40000 \
             --description "phase23 schema designer"
```

The API key is read from `--key`, then `$AGENT_HUB_API_KEY`, then the local
config (`./.agent-hub.local.json`), then the global config
(`~/.agent-hub/config.json`). It is never accepted on the command line of the
spawned adapter (it is forwarded via the `--key` flag of the existing adapter,
sourced from the precedence chain above).

## Behavior

1. **Resolve config.** Deep-merge global `~/.agent-hub/config.json` with local
   `./.agent-hub.local.json` (local wins per key).
2. **Pre-flight checks** before spawning:
   - **Version check (mandatory).** `<cli> --version` returns 0 within 5s.
     On non-zero or timeout → abort with `cli_not_found: <name>`.
   - **Auth probe (best-effort).** Run a tiny benign read-only command per
     CLI. Abort only when stderr/stdout contains an explicit auth/login/API
     key failure marker. Otherwise warn and continue.
3. **Workspace.** `mkdir -p ~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/`.
   When `--pipeline-id` is omitted, generate `default-<agent_id>`.
4. **Spawn the adapter** as `python -m clients.<cli_module>.llm_agent ...`,
   detached (`start_new_session=True`), with stdout/stderr redirected to
   `stdout.log`/`stderr.log` inside the workspace. Skill returns the PID
   immediately.
5. **Record** the spawn into `~/.agent-hub/spawned.json` via
   `clients._state.locked_mutate_spawned`.

The CLI → adapter module map is fixed:

| `--cli` | adapter module |
|---|---|
| `codex` | `clients.codex.llm_agent` |
| `claude` | `clients.claude_code.llm_agent` |
| `opencode` | `clients.opencode.llm_agent` |

## Interactive prompt sequence

If `--cli`, `--role`, etc. are omitted, prompt the user (stdin) in this order
(matches design §2.1 prompt JSON):

1. `cli` — one of `codex`, `claude`, `opencode`.
2. `role` — preset name (`pm`, `architect`, `designer`, `coder`, `reviewer`,
   `tester`, `generic`, or `custom`). Default `generic`.
3. `capabilities` — comma-separated capability names. **Only prompted when
   `role == "custom"`** (required in that case); skipped for built-in roles.
4. `system_prompt` — free-text custom system prompt. **Only prompted when
   `role == "custom"`**; skipped for built-in roles.
5. `agent_id` — default `<cli>-<role>-<short-uuid>`.
6. `pipeline_id` — default `default-<agent_id>`.
7. `hub_url` — default `http://localhost:8080` (overridden by config).
8. `api_key` — silent prompt; sourced from env/config first.
9. `max_budget_tokens` — default from `clients.role_presets`.
10. `description` — default `cli=<cli>;role=<role>;pipeline=<pipeline_id>`.

The raw API key must not be printed or logged.

## Implementation

The deterministic logic lives in
`skills/agent-spawn/scripts/spawn.py` (stdlib + existing modules only). The
LLM-facing skill orchestrates the prompts, then shells out:

```bash
python -m skills.agent-spawn.scripts.spawn --cli codex --role designer ...
```

See `design/phase2.3/design_v3.md` §2.1, §5, §6, §7 for the full design.
