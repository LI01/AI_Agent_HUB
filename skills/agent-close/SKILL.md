# Agent Close

Close, stop, kill, or unregister LLM-CLI worker agents previously spawned via
`/agent-spawn`. Joins the local `~/.agent-hub/spawned.json` records with the
hub's `GET /agents` registry, then for each selected agent: terminates the
adapter process (PID-reuse safe), best-effort `POST /unregister`, removes the
local spawned record, and (with `--purge`) recursively deletes the workspace
under `~/.agent-hub/pipelines/` (trusted-path guarded).

## Trigger phrases

"close agent", "stop agent", "unregister agent", "kill agent",
"/agent-close", "shut down agent", "remove agent".

## Usage

```bash
# Interactive picker (lists union of spawned.json + GET /agents).
/agent-close

# Close one or more by id (repeat --agent).
/agent-close --agent codex-designer-aa12 --agent claude-coder-7f3b

# Close every agent in spawned.json.
/agent-close --all-spawned

# Also purge the agent workspace under ~/.agent-hub/pipelines/.
/agent-close --agent codex-designer-aa12 --purge
```

Flags:

- `--hub <url>` — hub URL (defaults via config cascade per design §2.0).
- `--key <api-key>` — API key (NEVER passed on argv when avoidable; the
  helper prefers `AGENT_HUB_API_KEY` env if present).
- `--agent <agent_id>` — close a specific agent. Can be repeated.
- `--all-spawned` — close every agent currently in `~/.agent-hub/spawned.json`.
- `--purge` — also `rm -rf` the workspace under `~/.agent-hub/pipelines/`
  AFTER successful termination AND only when the path is trusted.

## Behavior

For each selected `agent_id` the helper runs the kill sequence specified in
`design/phase2.3/design_v3.md` §2.3 verbatim:

```
local = spawned.get(agent_id)
pid_alive = bool(local and pid_is_alive(local.pid))
pid_check = pid_is_adapter(local.pid) if pid_alive else None
pid_check_inconclusive = bool(pid_alive and pid_check is None)
pid_reused              = bool(pid_alive and pid_check is False)
terminated              = (local is None) or (not pid_alive)

if pid_check_inconclusive:
    report["pid_check_inconclusive"] = True
elif pid_reused:
    report["pid_reused"] = True
elif pid_alive and pid_check is True:
    SIGTERM, wait up to 5s, SIGKILL fallback
    terminated = not pid_is_alive(local.pid)
```

- `/unregister` is ALWAYS attempted (idempotent on the hub side; 404 = OK).
- Local `spawned.json` record is removed via `clients._state.locked_mutate_spawned`.
- `--purge` is gated by `terminated AND not pid_reused AND not pid_check_inconclusive
  AND workdir under ~/.agent-hub/pipelines/`. Otherwise reports
  `purge_skipped_process_still_alive_or_untrusted_path`.

For `registry-only` rows (in `GET /agents` but not in `spawned.json`), only
`/unregister` runs — no kill, no purge.

## Implementation

- `skills/agent-close/scripts/close.py` — main entry. Functions: `main`,
  `load_spawned`, `render_union`, `pid_is_alive`, `pid_is_adapter`,
  `terminate_pid`, `unregister_agent`, `remove_spawned_record`,
  `purge_workspace`.
- `skills/agent-close/scripts/clean_pipelines.py` — standalone purge utility
  per design §5. Walks `~/.agent-hub/pipelines/`, lists workspaces, removes
  those whose `agent_id` is NOT in the current `spawned.json`. Stdlib only.

## Safety notes

- The PID-check distinguishes alive-and-adapter (kill), alive-but-reused
  (skip kill), alive-and-inconclusive (skip kill, skip purge), and dead
  (already terminated). `pid_check is None` is treated as inconclusive,
  NOT as "not an adapter" (avoids the `not None == True` trap).
- `--purge` only removes paths under `~/.agent-hub/pipelines/`. Anywhere else
  is treated as untrusted.
- The raw API key must not be printed or logged.
