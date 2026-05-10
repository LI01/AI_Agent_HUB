# Phase 2.3 Design v2 — LLM-CLI Worker Skills + Per-CLI Adapters

**Author:** designer-v2 (sub-agent)
**Date:** 2026-05-09
**Scope:** Same as v1. Three skills (`agent-spawn`, `agent-tasks`, `agent-close`) + three per-CLI adapters (`codex`, `claude_code`, `opencode`) + one shared role-preset module + small shared helpers. Additive only. NO new top-level deps.

---

## 0. Diff vs v1

Each fix lists section(s) where it lands.

| # | Where | What changed vs v1 |
|---|---|---|
| 1 | §3, §7 | `description` is now an explicit `AgentHub`/`AsyncAgentHub` ctor kwarg, sent in the WS register frame. Dropped REST-PATCH and `_attach_description` monkey-patch alternatives. |
| 2 | §7 | Module path is `clients.claude_code.llm_agent` (underscore). New package `clients/claude_code/` with real `__init__.py` + a small `README.md` noting it is the module form of `clients/claude-code/`. No trampoline shim. |
| 3 | §3.1 | Codex stdout parser rewritten — searches for `\n[assistant]:` / `\nassistant:` markers; falls back to full stripped stdout. Fixture-based parser tests added in §8. |
| 4 | §3, §7, §6.4 | New SDK method `AgentHub.send_activity_log(task_id, action, details)` → WS `{"type":"activity_log",...}`. New hub handler in `handle_agent_message` calls `db.log_activity("task", ...)` after `reporter_owns_task` gate. Adapters call this once per over-budget task; result still carries `payload_schema_warn`. |
| 5 | §2.2 | `agent-tasks` resolution priority: `spawned.json[agent_id]` first → parsed `description` → fallback `cli="unknown" / role="unknown"`. Documented. |
| 6 | §2.3 | `--purge` now requires `terminated AND not pid_reused AND workdir under ~/.agent-hub/pipelines/`. `registry-only` rows are not purged unless a trusted local workdir was already in `spawned.json`. Verbatim block included. |
| 7 | §2.1 | Auth probe is best-effort and conservative: only aborts when stderr/stdout contains explicit `auth`/`login`/`API key` failure text. Empty-prompt invocations dropped. Version check is the only mandatory abort. First real task still surfaces real auth errors. |
| 8 | §2.1, §6.1, §10 | Config precedence rewritten everywhere: **flag > env (secrets) > .agent-hub.local.json > ~/.agent-hub/config.json > built-in default > interactive prompt**. `api_key`: **--key > AGENT_HUB_API_KEY > local config > global config > silent prompt**. `clients/role_presets.py` deep-merges BOTH config files (it loaded only the global one in v1). |
| 9 | §2.1, §2.2, §2.3 | Each skill now has a full JSON `prompts: [...]` block per codex's verbatim spawn example, with `default_source` chains. |
| 10 | §7 | Module-list table extended with a `Functions/classes` column listing the new/modified callables for every new file. |
| 11 | §3 | `_ROLE` is gone — `role` is now an explicit parameter to `_run_cli_for_task(task, workdir, role_prompt, budget, role, hub)`. Added new SDK exception `class TaskFailed(Exception)` carrying a `result: dict`. SDK `_handle_task` catches `TaskFailed` and emits `status="failed"`. Adapters raise `TaskFailed(result)` on CLI timeout, nonzero exit, unparseable required JSON. |
| 12 | §3 | New shared `_compute_deadline(timeout)` helper: tiny-timeout safe (`if timeout <= 5: max(0.5, timeout * 0.8) else timeout - 5`). All three adapters use it. |
| 13 | §5, §7 | `jq` and `psutil` removed entirely. `clean-pipelines.sh` replaced by `skills/agent-close/scripts/clean_pipelines.py` (stdlib only). PID-reuse check uses `/proc/<pid>/cmdline` on Linux else `subprocess.run(["ps","-p",str(pid),"-o","command="],...)`; inconclusive case skips kill and reports `pid_check_inconclusive`. |
| 14 | §6.5, §7, §8 | New shared module `clients/_state.py` with `locked_read_spawned`, `locked_mutate_spawned`, `atomic_write_json`. All three skills read/write through it. New tests: two-thread concurrent writer, corrupt-file handling (preserved as `.corrupt-<timestamp>`). |

Codex-`accept` items kept as-is, with one rename per finding 15: opencode session marker is now `.opencode-session-started`.

---

## 1. Summary

- Three new SKILL.md skills under `skills/agent-spawn/`, `skills/agent-tasks/`, `skills/agent-close/`. Each has interactive AND non-interactive flag-driven entry forms. SKILL.md is the LLM-facing prompt; deterministic logic lives in `skills/<name>/scripts/*.py` (stdlib + `httpx` already in `agent_sdk`).
- Three per-CLI adapter modules: `clients/codex/llm_agent.py`, `clients/claude_code/llm_agent.py`, `clients/opencode/llm_agent.py`. Each uses `agent_sdk.AgentHub(unregister_on_stop=True, description=...)` and shells out to its CLI in `task_handler`.
- One source of truth for role presets: `clients/role_presets.py`. Imported by all three adapters AND the spawn helper.
- One source of truth for `spawned.json` IO: `clients/_state.py` (`locked_read_spawned`, `locked_mutate_spawned`, `atomic_write_json`). Imported by all three skill helpers.
- Workspace layout: `~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/`. Created by spawn skill before `Popen`. If `pipeline_id` is omitted, generate `default-<agent_id>`.
- Budget tracking is **warn-only**. Adapters that get usage from the CLI compare to `payload.max_budget_tokens`; on overage they (a) call `hub.send_activity_log(task_id, "payload_schema_warn", {...})` once and (b) include `payload_schema_warn` text in the result.
- Phase 2.2 capability/payload-schema rules apply unchanged: capability schemas come from role presets, advertised at registration; the hub's existing dispatch-time validator handles payload checking.

---

## 2. Skills

All three follow `skills/agent-hub/SKILL.md` conventions: front-matter (description + triggers), Usage block, Behavior section (LLM-runnable steps), Implementation pointer to a helper script.

### 2.0 Config precedence (applies to ALL skill helpers)

```
flag > env (for secrets) > .agent-hub.local.json > ~/.agent-hub/config.json > built-in default > interactive prompt
```

For `api_key` specifically:
```
--key > AGENT_HUB_API_KEY > local config api_key > global config api_key > silent prompt
```

The shared `clients/role_presets.py` loads BOTH config files and deep-merges them (local wins per key). All skill helpers call its loader.

### 2.1 `skills/agent-spawn/SKILL.md`

**Trigger phrases:** "spawn an agent", "start an LLM agent", "register a codex agent", "/agent-spawn".

**Usage (interactive):**
```bash
/agent-spawn
```

**Usage (non-interactive, scriptable — Open Q a):**
```bash
/agent-spawn cli=codex role=designer pipeline=phase23 \
             hub=http://localhost:8080 key=$AGENT_HUB_KEY \
             id=codex-designer-1 max-budget-tokens=40000 \
             system-prompt-extra="Focus on JSON schemas." \
             capabilities=design,spec
```

**Interactive prompt JSON (Acceptance #2):**
```json
{
  "prompts": [
    {"name":"cli","type":"enum","choices":["codex","claude","opencode"],"required":true},
    {"name":"role","type":"enum","choices":["pm","architect","designer","coder","reviewer","tester","generic","custom"],"default":"generic"},
    {"name":"capabilities","type":"csv","required_if":{"role":"custom"}},
    {"name":"system_prompt","type":"string","required_if":{"role":"custom"}},
    {"name":"agent_id","type":"string","default":"<cli>-<role>-<short-uuid>"},
    {"name":"pipeline_id","type":"string","default":"default-<agent_id>"},
    {"name":"hub_url","type":"string","default_source":["local_config","global_config","builtin:http://localhost:8080"]},
    {"name":"api_key","type":"secret","default_source":["env:AGENT_HUB_API_KEY","local_config","global_config"]},
    {"name":"max_budget_tokens","type":"integer","default_source":"role_presets"}
  ]
}
```

**Pre-flight checks** (helper script, before `Popen`):

1. **Version check (mandatory).** `<cli> --version` returns 0 within 5s. On non-zero or timeout → abort with `cli_not_found: <name>`.
2. **Auth probe (best-effort, advisory).** Per CLI table below, run a tiny benign command. **Abort only** when stderr/stdout contains an explicit auth/login/API-key failure marker. Otherwise, warn and continue. The first real task surfaces real auth errors as a task failure.

   | CLI | Probe command | Failure markers (any → abort) |
   |---|---|---|
   | codex | `codex whoami` (or `codex auth status` if available) — fall back to skip if subcommand absent | `not logged in`, `auth required`, `API key missing` |
   | claude | `claude config get` — read-only, prints config | `Please run claude login`, `not authenticated`, `API key` |
   | opencode | `opencode auth status` (or skip if absent) | `not authenticated`, `please login`, `auth required` |

   No empty-prompt invocations are used. If a CLI lacks a read-only probe, the skill skips probing and only checks `--version`.
3. **Hub reachability.** `httpx.get(f"{hub_url}/health", timeout=3)`. Non-200 → warn "hub not reachable; agent will retry on its own"; do not abort.
4. **Workspace.** `mkdir -p ~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/`. Drop `.agent-info.json` marker `{agent_id, cli, role, hub_url, started_at}`.

**Subprocess spawn:**

- Command for each CLI:
  - `codex`: `python -m clients.codex.llm_agent --hub <hub_url> --id <agent_id> --role <role> --workdir <ws> --max-budget-tokens <n> [--capabilities <csv>] [--system-prompt-extra <str>]`
  - `claude`: `python -m clients.claude_code.llm_agent ...` (note: underscore — the module name uses `_`, the legacy hyphenated dir is left untouched).
  - `opencode`: `python -m clients.opencode.llm_agent ...`
- `cwd` = workspace dir.
- `env` = parent env + `AGENT_HUB_API_KEY=<key>` (key NEVER passed on argv — process-list leak risk).
- Detached: `subprocess.Popen(..., start_new_session=True, stdout=open(workspace/"stdout.log","ab"), stderr=open(workspace/"stderr.log","ab"), stdin=subprocess.DEVNULL)`. Skill returns immediately with PID.
- Post-spawn: poll `httpx.get(f"{hub_url}/agents/{agent_id}", headers=...)` up to 5×, 500ms apart, to confirm registration. If not registered after 2.5s, print `stderr.log` tail and ask the user to investigate. Do NOT kill — the agent might still be retrying.

**`spawned.json` write:** through `clients._state.locked_mutate_spawned(append=record)` (see §6.5).

### 2.2 `skills/agent-tasks/SKILL.md`

**Trigger phrases:** "submit a task", "send a task to the agent", "/agent-tasks".

**Usage (interactive):**
```bash
/agent-tasks
```

**Usage (non-interactive):**
```bash
/agent-tasks agent=codex-designer-1 task-type=design payload-json='{"spec":"..."}' timeout=300
```

**Interactive prompt JSON (Acceptance #2):**
```json
{
  "prompts": [
    {"name":"hub_url","type":"string","default_source":["local_config","global_config","builtin:http://localhost:8080"]},
    {"name":"api_key","type":"secret","default_source":["env:AGENT_HUB_API_KEY","local_config","global_config"]},
    {"name":"agent_id","type":"picker","source":"GET /agents merged with spawned.json"},
    {"name":"task_type","type":"picker","source":"agent.capabilities"},
    {"name":"payload","type":"dynamic","mode_default":"per_field_from_payload_schema","mode_fallback":"raw_json","override_flag":"--raw-json"},
    {"name":"task","type":"string","default":"<task_type> task for <agent_id>"},
    {"name":"timeout","type":"integer","default":300},
    {"name":"max_budget_tokens","type":"integer","default_source":["role_presets[role]"]}
  ]
}
```

**Interactive flow:**

1. Helper calls `httpx.get(f"{hub_url}/agents", headers=auth)`.
2. **Metadata resolution priority (per finding #5):**
   ```
   spawned.json[agent_id].cli, .role            (preferred — authoritative local state)
   else parse description "cli=...;role=..."   (remote / registry-only agents)
   else cli="unknown", role="unknown"          (no metadata available)
   ```
   `spawned.json` is read once via `clients._state.locked_read_spawned()`.
3. Render the agent table sorted by `(status_rank, agent_id)`:
   ```
   #  agent_id              cli       role       status   capabilities
   1  codex-designer-aa12   codex     designer   idle     design:v1, spec:v1
   2  claude-coder-7f3b     claude    coder      busy     code:v1
   3  opencode-pm-001       opencode  pm         offline  pm:v1, plan:v1, coordinate:v1
   4  legacy-bot            unknown   unknown    idle     chat:v1
   ```
4. User picks `#`. `GET /agents/<agent_id>` returns full capability list (Phase 2.2 `Capability(name, version, payload_schema, result_schema)`).
5. If multiple capabilities, ask which `task_type` (default = first idle-matching).
6. **`payload_schema`-aware prompting (Open Q g):**
   - Default: per-required-field prompts. Hint = `type` + `enum` if present. Recurse one level into nested `type:object`. For `type:array` of objects, ask "How many items?" then loop.
   - Fallback (auto): more than two levels of nesting, OR any `oneOf`/`anyOf`/`allOf`/`$ref` keyword → raw JSON mode with the schema printed as a hint.
   - Override: `--raw-json` always uses raw JSON regardless of schema shape.
   - Both modes validate locally before submission with the same six-keyword pure-Python validator the hub uses (Phase 2.2 §2.3) so the user gets immediate feedback.
7. `task` (natural-language) is asked separately. Default `"<task_type> task for <agent_id>"`.
8. `timeout` defaults to 300 (FR-TSK-6). `max_budget_tokens` defaults to `role_presets[role]` for the resolved role; if role is `unknown`, defaults to `default_budget_for("generic")`.
9. Submit: `POST /tasks` with `{task, task_type, payload, target_agent, timeout}` plus `payload.max_budget_tokens`.
10. **Polling.** 1.5s interval, exponential backoff to 8s, max wall = `timeout + 30s`. Print `[<elapsed>s] status=<s> logs=<n>`. On `running` transition, fetch and incrementally print new log lines.
11. **Terminal states:** `completed` / `failed` / `timeout`. Print `result.text` prominently, then raw JSON in a secondary block. If `result.usage` present → `(used <n> / budget <m> tokens)`. If `result.payload_schema_warn` present → surface in red.

### 2.3 `skills/agent-close/SKILL.md`

**Trigger phrases:** "close an agent", "stop the agent", "kill agent", "/agent-close".

**Usage:**
```bash
/agent-close                                 # interactive
/agent-close ids=codex-designer-aa12,claude-coder-7f3b
/agent-close --all
/agent-close --purge                         # also rm -rf the agent's workspace dir
```

**Interactive prompt JSON (Acceptance #2):**
```json
{
  "prompts": [
    {"name":"hub_url","type":"string","default_source":["local_config","global_config","builtin:http://localhost:8080"]},
    {"name":"api_key","type":"secret","default_source":["env:AGENT_HUB_API_KEY","local_config","global_config"]},
    {"name":"selection","type":"multi_picker","source":"union(spawned.json, GET /agents)","accept":["1,3,5","all"]},
    {"name":"purge","type":"bool","default":false}
  ]
}
```

**List union flow:**

1. `clients._state.locked_read_spawned()` → local `(agent_id, pid, workdir, ...)` records.
2. `httpx.get(f"{hub_url}/agents", headers=auth)` → all hub-known agents.
3. Render union, annotating per row: `local+registered` (PID + registry both), `local-only` (PID known, no registry — agent died or never registered), `registry-only` (registry knows it, no local PID), `local+offline` (PID known, registry says offline).
4. Picker: numbered single OR multi-select (`1,3,5` or `all`).

**Kill sequence (per selected agent):**

```python
local = spawned.get(agent_id)               # may be None
pid_alive = local and pid_is_alive(local.pid)
pid_reused = local and not pid_is_adapter(local.pid)   # see §5
terminated = (local is None) or (not pid_alive) or pid_reused

if local and pid_alive and not pid_reused:
    os.kill(local.pid, signal.SIGTERM)
    for _ in range(20):                      # 5s, 250ms steps
        if not pid_is_alive(local.pid): break
        time.sleep(0.25)
    if pid_is_alive(local.pid):
        os.kill(local.pid, signal.SIGKILL)
        for _ in range(8):                   # 2s, 250ms steps
            if not pid_is_alive(local.pid): break
            time.sleep(0.25)
    terminated = not pid_is_alive(local.pid)

# Always try unregister; idempotent on hub side.
try:
    httpx.post(f"{hub_url}/unregister", params={"agent_id": agent_id},
               headers=auth, timeout=3)
except Exception:
    report["hub_unreachable"] = True

clients._state.locked_mutate_spawned(remove=agent_id)
```

**`--purge` ordering (verbatim per finding #6):**

```python
terminated = no_local_pid or pid_dead_after_term_kill
if purge and terminated and not pid_reused and workdir_under_agent_hub_pipelines(workdir):
    shutil.rmtree(workdir)
    rmdir_parent_if_empty()
elif purge:
    report["purge_skipped_process_still_alive_or_untrusted_path"] = True
```

For `registry-only` rows, `--purge` is suppressed unless the same `agent_id` is also in `spawned.json` with a trusted local `workdir` under `~/.agent-hub/pipelines/`.

**Edge cases:**

| Case | Handling |
|---|---|
| PID dead but registry stale | Skip TERM/KILL; still call `/unregister`. Log `cleaned_stale_registration: <id>`. |
| `registry-only` (no local PID) | Call `/unregister` only. The remote process owning that agent is unaffected — print warning. |
| `unregister` returns 404 | Treat as success (already gone). Remove from `spawned.json`. |
| PID-reused | Detected via `pid_is_adapter(pid)` (§5). Skip kill; still call `/unregister`. |
| `pid_check_inconclusive` (no `/proc`, no `ps`) | Skip kill, report inconclusive, still call `/unregister`. |
| `spawned.json` missing | Treated as empty. |
| `spawned.json` corrupt | Treated as empty + warning + preserved as `spawned.json.corrupt-<ts>` before rewrite (§6.5). |
| Hub unreachable | Skip `/unregister`; surface `hub_unreachable`; still SIGTERM local PID; remove from `spawned.json`. The agent process's own `unregister_on_stop` will retry on its way out. |

---

## 3. Per-CLI worker adapters

Common shape — all three modules share `_build_argparser`, `_resolve_capabilities`, `main`, plus shared helpers from `clients/_shared.py`. Each differs only in `_run_cli_for_task`.

### 3.0 Shared SDK additions (finding #1, #11)

In `agent_sdk/client.py` — all additive, no existing call site changes:

1. New `class TaskFailed(Exception)` carrying `result: dict`.
2. `AgentHub.__init__` and `AsyncAgentHub.__init__` gain `description: Optional[str] = None` kwarg → stored as `self.description`.
3. WS register frame (sync `_on_open`, async `connect`) includes `"description": self.description` when non-null.
4. `_handle_task` (sync + async) catches `TaskFailed` BEFORE the generic `Exception` branch and emits `{"type":"result","task_id":task_id,"status":"failed","result": exc.result}`.
5. New method `send_activity_log(task_id, action, details)` (sync) / `async send_activity_log(...)` (async) → `self._send_json({"type":"activity_log","task_id":...,"action":...,"details":...})`.

### 3.1 Common adapter skeleton

```python
# clients/<cli>/llm_agent.py — common skeleton
import argparse, json, os, signal, subprocess, sys, time
from pathlib import Path
from agent_sdk import AgentHub, TaskFailed
from clients.role_presets import (
    capabilities_for, system_prompt_for, default_budget_for,
)
from clients._shared import compute_deadline, extract_last_message_text

CLI_NAME = "<set-per-module>"

_PAYLOAD_SCHEMA_V1 = {
    "type": "object",
    "properties": {
        "prompt":              {"type": "string"},
        "max_budget_tokens":   {"type": "integer"},
        "system_prompt_extra": {"type": "string"},
    },
    "required": ["prompt"],
    "additionalProperties": True,
}

def _build_argparser():
    p = argparse.ArgumentParser()
    p.add_argument("--hub", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--role", default="generic")
    p.add_argument("--workdir", required=True)
    p.add_argument("--max-budget-tokens", type=int, default=None)
    p.add_argument("--capabilities", default=None)
    p.add_argument("--system-prompt-extra", default=None)
    return p

def _resolve_capabilities(args):
    if args.capabilities:
        names = [c.strip() for c in args.capabilities.split(",") if c.strip()]
        return [{"name": n, "version": 1, "payload_schema": _PAYLOAD_SCHEMA_V1}
                for n in names]
    return capabilities_for(args.role)

def main():
    args = _build_argparser().parse_args()
    api_key = os.environ.get("AGENT_HUB_API_KEY")
    role_prompt = system_prompt_for(args.role)
    if args.system_prompt_extra:
        role_prompt = (role_prompt or "") + "\n\n" + args.system_prompt_extra
    budget = args.max_budget_tokens or default_budget_for(args.role)
    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    hub = AgentHub(
        hub_url=args.hub,
        agent_id=args.id,
        capabilities=_resolve_capabilities(args),
        auth_token=api_key,
        unregister_on_stop=True,
        description=f"cli={CLI_NAME};role={args.role};pipeline={workdir.parent.name}",
    )

    @hub.task_handler
    def handle(task):
        return _run_cli_for_task(task, workdir, role_prompt, budget, args.role, hub)

    def _shutdown(signum, frame):
        hub.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    hub.start()      # blocks until stop()
```

### 3.2 `clients/_shared.py` (helpers, ≤ 80 lines)

Stdlib only. Functions:

- `compute_deadline(timeout) -> float` — finding #12 verbatim: `if timeout <= 5: return max(0.5, timeout * 0.8); else: return timeout - 5`.
- `extract_last_message_text(parsed)` — pulls `parsed["messages"][-1].text/content`, falls back to `parsed.get("text") or parsed.get("response") or ""`.
- `timeout_failure(cli, role, exc, t0)` → `{text:"", usage:None, raw:{stdout/stderr tails from exc}, cli, role, elapsed_s, error:"cli_timeout"}`.
- `unparseable_failure(cli, role, cp, t0)` → `{text:"", usage:None, raw:{stdout_tail, stderr_tail, returncode}, cli, role, elapsed_s, error:"cli_output_unparseable"}`.

### 3.3 `clients/codex/llm_agent.py`

```python
CLI_NAME = "codex"

def _run_cli_for_task(task, workdir, role_prompt, budget, role, hub):
    payload  = task.get("payload") or {}
    prompt   = payload.get("prompt") or task.get("task") or ""
    full     = (role_prompt + "\n\n" + prompt) if role_prompt else prompt
    timeout  = int(task.get("timeout") or 300)
    deadline = compute_deadline(timeout)

    cmd = [
        "codex", "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C", str(workdir),
        full,
    ]
    t0 = time.monotonic()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=deadline)
    except subprocess.TimeoutExpired as exc:
        raise TaskFailed(timeout_failure(CLI_NAME, role, exc, t0))
    elapsed = round(time.monotonic() - t0, 2)

    text = _parse_codex_output(cp.stdout)
    estimated = (len(full) + len(text)) // 4
    usage = {"estimated_total_tokens": estimated, "source": "char_heuristic"}
    result = {"text": text, "usage": usage,
              "raw": {"stdout_tail": cp.stdout[-2000:],
                      "stderr_tail": cp.stderr[-2000:],
                      "returncode": cp.returncode},
              "cli": CLI_NAME, "role": role, "elapsed_s": elapsed}

    if cp.returncode != 0:
        result["error"] = f"cli_exit_{cp.returncode}"
        raise TaskFailed(result)

    if budget and estimated > budget:
        warn = f"estimated tokens {estimated} exceeded max_budget_tokens {budget}"
        result["payload_schema_warn"] = warn
        try:
            hub.send_activity_log(
                task.get("task_id"), "payload_schema_warn",
                {"estimated_total_tokens": estimated, "budget": budget,
                 "source": "char_heuristic", "cli": CLI_NAME, "role": role},
            )
        except Exception:
            pass    # advisory only; never break the task on log failure
    return result

def _parse_codex_output(stdout: str) -> str:
    """Verbatim per codex finding #3: prefer the last [assistant]: block."""
    text = stdout.strip()
    for marker in ("\n[assistant]:", "\nassistant:"):
        if marker in text:
            return text.rsplit(marker, 1)[1].strip()
    return text
```

Parser fixture tests in §8 cover three cases: marker present, no marker (full stdout returned), empty stdout (returns `""`).

**Open Q e (codex budget):** char-heuristic — `(len(prompt) + len(stdout)) // 4`, reported as `usage.estimated_total_tokens` with `usage.source = "char_heuristic"`. Switch to real usage when `codex exec --json` lands upstream; result-dict shape doesn't change.

### 3.4 `clients/claude_code/llm_agent.py`

Same shape as §3.3 (timeout → `TaskFailed(timeout_failure)`, JSON parse fail → `TaskFailed(unparseable_failure)`, nonzero exit → `TaskFailed(result)`, budget overage → `result["payload_schema_warn"] + hub.send_activity_log(...)` once). Differences vs codex adapter:

```python
CLI_NAME = "claude"

cmd = ["claude", "-p", "--bare", "--output-format", "json",
       "--add-dir", str(workdir)]
if role_prompt:
    cmd += ["--append-system-prompt", role_prompt]
cmd.append(prompt)

# After json.loads(cp.stdout) succeeds:
text  = parsed.get("response") or parsed.get("result") or ""
usage = parsed.get("usage")           # claude returns real usage
total = (usage or {}).get("input_tokens", 0) + (usage or {}).get("output_tokens", 0)
# Overage warning text: f"used tokens {total} exceeded max_budget_tokens {budget}"
```

Budget is real (claude returns usage). Warn-only — never kills mid-task.

### 3.5 `clients/opencode/llm_agent.py`

Same shape as §3.3. Differences:

```python
CLI_NAME = "opencode"
_SESSION_MARKER = ".opencode-session-started"   # per finding #15 rename

# Prompt prefixing (role_prompt prepended into prompt before cmd).
sess = workdir / _SESSION_MARKER
cmd = ["opencode", "run", "--format", "json"]
if sess.exists():
    cmd += ["-c"]
cmd.append(prompt)

# subprocess.run(..., cwd=str(workdir))
# After parse: if not sess.exists(): sess.touch()
text  = extract_last_message_text(parsed)
usage = parsed.get("usage")
total = (usage or {}).get("total_tokens") or 0
```

**Open Q d:** ONE persistent session per `agent_id` via `opencode run -c` from a stable `cwd`. The marker `.opencode-session-started` (finding #15 rename) records that this `cwd` has had a first task; subsequent tasks add `-c`. Operators wanting determinism per task spawn a fresh `agent_id` with a fresh workdir.

---

## 4. `clients/role_presets.py`

One file, one source of truth. Imported as `from clients.role_presets import preset_for, capabilities_for, system_prompt_for, default_budget_for, load_user_overrides`.

**Module-level constants:**

- `_DEFAULT_PAYLOAD_SCHEMA = {"type":"object","properties":{"prompt":{"type":"string"},"max_budget_tokens":{"type":"integer"},"system_prompt_extra":{"type":"string"}},"required":["prompt"],"additionalProperties":True}` — Phase 2.2 Capability shape, reused for every role's tasks.
- `ROLE_PRESETS` dict, one entry per role — verbatim from scope §"Role presets":
  - `pm`: caps `["pm","plan","coordinate"]`, prompt "You are the PM. Decompose, sequence, escalate.", budget 30000.
  - `architect`: caps `["architect","design"]`, prompt "You are the architect. Module boundaries, data flow, tradeoffs.", budget 50000.
  - `designer`: caps `["design","spec"]`, prompt "You are the designer. Concrete schemas, message shapes.", budget 40000.
  - `coder`: caps `["code"]`, prompt "You implement features. Match style. Test-first.", budget 30000.
  - `reviewer`: caps `["review","code-review"]`, prompt "You critique. Find real bugs and design issues, ignore style nits.", budget 20000.
  - `tester`: caps `["test"]`, prompt "You write tests that fail without the change and pass with it.", budget 20000.
  - `generic`: caps `["chat"]`, prompt `None`, budget 20000.

**Functions:**

```python
def _read_json(path):                # safe: missing/unreadable/bad JSON → {}
def _deep_merge(base, ov):           # nested dicts merge recursively, local wins
def load_user_overrides():           # per finding #8 — deep-merge global + local
    g = _read_json(Path("~/.agent-hub/config.json").expanduser())
    l = _read_json(Path(".agent-hub.local.json"))
    return _deep_merge(g, l)
def preset_for(role):                # base preset, with overrides applied per-key
def capabilities_for(role):          # -> list[{"name":..., "version":1, "payload_schema":...}]
def system_prompt_for(role):         # -> Optional[str]
def default_budget_for(role):        # -> int
```

`preset_for` reads `ov.get("role_prompts",{}).get(role)` and `ov.get("role_budgets",{}).get(role)` and only substitutes when the user value is non-`None`, so a `null` in the user file means "use built-in default".

**Open Q f:** `--role custom` accepts `--capabilities a,b,c --system-prompt "..."` for ad-hoc roles. The adapter's `--capabilities`/`--system-prompt-extra` flags handle this; the spawn helper just forwards. Custom roles use `default_budget_for("generic")` unless `--max-budget-tokens` is also set.

---

## 5. Workspace strategy

**Layout:** `~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/`. Holds:
- `.agent-info.json` — `{agent_id, cli, role, hub_url, started_at}`. Written by spawn skill.
- `stdout.log`, `stderr.log` — adapter subprocess output. Spawn skill rotates to `.1` if > 10 MB at spawn time.
- `.opencode-session-started` — opencode adapter marker only (§3.5).
- Anything the LLM CLI writes during task execution (CLI `cwd` is this dir).

**Created by:** spawn skill helper, BEFORE `subprocess.Popen`.

**If `pipeline_id` omitted:** generate `default-<agent_id>`. `rm -rf ~/.agent-hub/pipelines/default-<id>/` is always safe.

**Cleanup (Open Q c):** manual for v1. Two affordances:
1. `agent-close --purge` removes the agent's workdir AND parent if empty (with the trusted-path + terminated guard from §2.3).
2. `skills/agent-close/scripts/clean_pipelines.py` (stdlib only — finding #13). Walks `~/.agent-hub/pipelines/`, identifies leaf dirs older than `--days N` (default 14) whose `agent_id` is NOT in `spawned.json`. Prints `would remove <dir>`; `--apply` actually `rm -rf`s. No `jq`, no daemon, no cron.

**PID-reuse check (`pid_is_adapter`, finding #13):**

```python
def pid_is_adapter(pid: int) -> bool | None:
    """Return True if pid is alive AND its cmdline contains 'clients.' and
    'llm_agent'. False if alive but doesn't match. None if inconclusive."""
    if sys.platform == "linux":
        p = Path(f"/proc/{pid}/cmdline")
        try:
            cmd = p.read_bytes().decode("utf-8", "replace")
            return ("clients." in cmd) and ("llm_agent" in cmd)
        except (FileNotFoundError, ProcessLookupError):
            return None        # process doesn't exist
        except PermissionError:
            return None        # can't read — inconclusive
    # macOS / BSD / Windows fallback: ps
    try:
        cp = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None            # ps unavailable — inconclusive
    if cp.returncode != 0 or not cp.stdout.strip():
        return None
    cmd = cp.stdout.strip()
    return ("clients." in cmd) and ("llm_agent" in cmd)
```

When `pid_is_adapter` returns `None`, the close-skill flow reports `pid_check_inconclusive` for that row, skips kill, still calls `/unregister`, and (if `--purge`) skips purge.

---

## 6. Config + state shapes

### 6.1 `~/.agent-hub/config.json` (global)

All fields optional. Skill helpers read both files via `clients.role_presets.load_user_overrides()` (which deep-merges global + local).

```jsonc
{
  "hub_url": "http://localhost:8080",
  "api_key": "ah_…",
  "default_pipeline_id": null,
  "role_prompts": {
    "coder":    "You implement features in TypeScript. Match Airbnb style.",
    "designer": null
  },
  "role_budgets": {
    "coder":   80000,
    "tester":  30000
  }
}
```

The skill writes a fresh `config.json` on first use if missing, populated only with the user's just-entered `hub_url` and `api_key`. `chmod 600` because it holds an API key.

### 6.2 `.agent-hub.local.json` (per-project, in cwd, gitignored)

Same shape as `config.json`. Deep-merged on top of the global one (local wins per key).

### 6.3 `~/.agent-hub/spawned.json`

```jsonc
[
  {
    "agent_id":     "codex-designer-aa12",
    "pid":          82731,
    "cli":          "codex",
    "role":         "designer",
    "pipeline_id":  "phase23",
    "hub_url":      "http://localhost:8080",
    "workdir":      "/Users/leon/.agent-hub/pipelines/phase23/codex-designer-aa12",
    "started_at":   "2026-05-09T12:30:11Z"
  }
]
```

### 6.4 New WS message: `activity_log` (finding #4)

Adapter → hub:
```json
{"type":"activity_log","task_id":"<id>","action":"payload_schema_warn","details":{"used_tokens":2100,"budget":1000,"cli":"claude","role":"coder"}}
```

Hub handler (added to `hub/main.py:handle_agent_message`):
```python
elif msg_type == "activity_log":
    task_id = msg.get("task_id")
    action  = msg.get("action") or "activity"
    details = msg.get("details") or {}
    if task_id and reporter_owns_task(task_id, agent_id):
        db.log_activity("task", task_id, action, details)
```

This is the only new hub message type Phase 2.3 introduces.

### 6.5 `clients/_state.py` — shared spawned.json IO (finding #14)

Stdlib-only module (`fcntl`, `json`, `os`, `sys`, `tempfile`, `time`, `pathlib`). Public API:

- `LOCK_PATH = ~/.agent-hub/spawned.json.lock`, `DATA_PATH = ~/.agent-hub/spawned.json`.
- `atomic_write_json(path, data)` — `tempfile.NamedTemporaryFile` in same dir + `os.replace`.
- `locked_read_spawned() -> list[dict]` — opens `LOCK_PATH` with shared lock (`fcntl.flock LOCK_SH` on POSIX, `msvcrt.locking LK_LOCK` on Windows), then reads via `_read_records_handle_corrupt`.
- `locked_mutate_spawned(mutate) -> list[dict]` — exclusive lock; reads existing records, calls `mutate(records)`, atomic-writes the result.
- `_read_records_handle_corrupt()` — if `DATA_PATH` missing → `[]`. If JSON parse fails OR top-level isn't a list → rename to `spawned.json.corrupt-<ts>` (via `DATA_PATH.with_suffix(...)`), print one-line warning, return `[]`.
- `_platform_lock(fh, exclusive)` / `_platform_unlock(fh)` wrap `fcntl` and `msvcrt` so the caller doesn't branch.

All three skill helpers import this module: spawn appends, close removes, tasks reads.

---

## 7. Module-by-module change list (with functions)

| Path | New / Modified | Functions/classes |
|---|---|---|
| `skills/agent-spawn/SKILL.md` | NEW | (markdown) |
| `skills/agent-spawn/scripts/spawn.py` | NEW | `main`, `load_config`, `merge_config`, `check_cli_version`, `probe_cli_auth`, `create_workspace`, `spawn_adapter`, `append_spawned_record` |
| `skills/agent-tasks/SKILL.md` | NEW | (markdown) |
| `skills/agent-tasks/scripts/tasks.py` | NEW | `main`, `list_agents`, `resolve_metadata`, `render_agent_table`, `prompt_payload_from_schema`, `submit_task`, `poll_task`, `render_terminal_result` |
| `skills/agent-close/SKILL.md` | NEW | (markdown) |
| `skills/agent-close/scripts/close.py` | NEW | `main`, `load_spawned`, `render_union`, `pid_is_alive`, `pid_is_adapter`, `terminate_pid`, `unregister_agent`, `remove_spawned_record`, `purge_workspace` |
| `skills/agent-close/scripts/clean_pipelines.py` | NEW | `main`, `find_orphan_dirs`, `apply_removal` (stdlib only — replaces `clean-pipelines.sh`) |
| `clients/role_presets.py` | NEW | `_read_json`, `_deep_merge`, `load_user_overrides`, `preset_for`, `capabilities_for`, `system_prompt_for`, `default_budget_for`, constants `ROLE_PRESETS`, `_DEFAULT_PAYLOAD_SCHEMA` |
| `clients/_shared.py` | NEW | `compute_deadline`, `extract_last_message_text`, `timeout_failure`, `unparseable_failure` |
| `clients/_state.py` | NEW | `atomic_write_json`, `locked_read_spawned`, `locked_mutate_spawned`, `_read_records_handle_corrupt`, `_platform_lock`, `_platform_unlock` |
| `clients/codex/llm_agent.py` | NEW | `CLI_NAME`, `_PAYLOAD_SCHEMA_V1`, `_build_argparser`, `_resolve_capabilities`, `_run_cli_for_task`, `_parse_codex_output`, `main` |
| `clients/claude_code/__init__.py` | NEW | (empty package init) |
| `clients/claude_code/README.md` | NEW | One-paragraph note: "Module form of `clients/claude-code/`. Python module names cannot contain hyphens; this directory exists so `python -m clients.claude_code.llm_agent` works." |
| `clients/claude_code/llm_agent.py` | NEW | `CLI_NAME`, `_PAYLOAD_SCHEMA_V1`, `_build_argparser`, `_resolve_capabilities`, `_run_cli_for_task`, `main` |
| `clients/opencode/llm_agent.py` | NEW | `CLI_NAME`, `_SESSION_MARKER`, `_PAYLOAD_SCHEMA_V1`, `_build_argparser`, `_resolve_capabilities`, `_run_cli_for_task`, `main` |
| `agent_sdk/client.py` | MODIFIED (additive) | Add `class TaskFailed`. Add `description: Optional[str] = None` ctor kwarg to `AgentHub` and `AsyncAgentHub`; store `self.description`; include in WS register frame when non-null. Update `_handle_task` (sync + async) to catch `TaskFailed` → `status="failed"`. Add `send_activity_log(task_id, action, details)` method on both classes. |
| `hub/main.py` | MODIFIED (additive) | Add `elif msg_type == "activity_log": ...` branch in `handle_agent_message` with `reporter_owns_task` gate, calling `db.log_activity("task", task_id, action, details)`. |
| `tests/test_phase23_skills.py` | NEW | All test cases per §8 |
| `requirements.txt` | NO CHANGE | (per scope hard constraint) |

The hyphenated `clients/claude-code/` directory is left in place (existing file). Spawn maps `cli=claude → module clients.claude_code.llm_agent`. No trampoline shim.

---

## 8. Test plan

≥ 6 new pytest cases. `test_p23_*` prefix; `FR-LLM-*` IDs.

| Test | ID | Behavior |
|---|---|---|
| `test_p23_llm_1_role_preset_lookup_with_local_override` | FR-LLM-1 | `preset_for("designer")["capabilities"] == ["design","spec"]`; `default_budget_for("coder") == 30000`; `system_prompt_for("generic") is None`. With monkeypatched global `~/.agent-hub/config.json` `role_budgets.coder=99999` and local `.agent-hub.local.json` `role_budgets.coder=77777`, `default_budget_for("coder") == 77777` (local wins per finding #8). |
| `test_p23_llm_2_locked_mutate_concurrent_writers` | FR-LLM-2 | Two threads call `locked_mutate_spawned` simultaneously, each appending one record. After both return, `locked_read_spawned()` returns a list of length 2 containing both records (no clobber). |
| `test_p23_llm_2b_corrupt_spawned_file_preserved` | FR-LLM-2b | Pre-write `spawned.json` with `"{not json"`. `locked_read_spawned()` returns `[]` and a sibling `spawned.json.corrupt-<ts>` file exists with the original bytes. Subsequent `locked_mutate_spawned` writes a fresh valid file. |
| `test_p23_llm_3_codex_adapter_command_construction` | FR-LLM-3 | Monkeypatch `subprocess.run` to capture argv. Call `_run_cli_for_task({"task":"hello","timeout":60}, workdir, role_prompt="X", budget=1000, role="generic", hub=fake)`. Assert argv == `["codex","exec","--skip-git-repo-check","--dangerously-bypass-approvals-and-sandbox","-C",str(workdir),"X\n\nhello"]` and the `subprocess.run` `timeout` kwarg == `55` (60 - 5). |
| `test_p23_llm_3b_codex_parser_fixtures` | FR-LLM-3b | `_parse_codex_output("hi\n[assistant]: final answer\n")` → `"final answer"`. `_parse_codex_output("\nassistant: hello world")` → `"hello world"`. `_parse_codex_output("no markers here at all")` → `"no markers here at all"`. `_parse_codex_output("")` → `""`. |
| `test_p23_llm_3c_compute_deadline_tiny_timeout` | FR-LLM-3c | `compute_deadline(60) == 55`. `compute_deadline(5) == 4.0`. `compute_deadline(1) == 0.8`. `compute_deadline(0) == 0.5`. |
| `test_p23_llm_4_claude_adapter_warn_and_activity_log` | FR-LLM-4 | Mock subprocess returning `{"response":"ok","usage":{"input_tokens":900,"output_tokens":1200}}`. Fake hub records `send_activity_log` calls. Call adapter with `budget=1000`. Assert `result["text"] == "ok"`, `result["payload_schema_warn"]` mentions "used tokens 2100 exceeded max_budget_tokens 1000", AND fake hub recorded one `send_activity_log(task_id, "payload_schema_warn", {...})` call with `used_tokens=2100, budget=1000`. |
| `test_p23_llm_4b_claude_adapter_nonzero_exit_raises_taskfailed` | FR-LLM-4b | Mock subprocess returning `returncode=2`. Call adapter; assert `TaskFailed` raised with `result["error"] == "cli_exit_2"`. |
| `test_p23_llm_5_opencode_adapter_session_persistence` | FR-LLM-5 | First call → argv lacks `-c`, marker file `.opencode-session-started` appears in workdir. Second call → argv contains `-c`. Both calls `cwd=str(workdir)`. |
| `test_p23_llm_6_close_skill_sigterm_then_sigkill` | FR-LLM-6 | Spawn a `time.sleep(60)` subprocess, write to spawned.json. Call `close.py --ids <id>`. Mock `pid_is_adapter` → True. Verify SIGTERM sent first, after 5s polling SIGKILL sent, `/unregister` called via httpx mock, spawned.json no longer contains the entry. |
| `test_p23_llm_6b_close_skill_pid_check_inconclusive` | FR-LLM-6b | Mock `pid_is_adapter` → `None`. Verify NO `os.kill` is called, `/unregister` IS called, per-row report contains `pid_check_inconclusive`. |
| `test_p23_llm_6c_close_skill_purge_guard` | FR-LLM-6c | Set `workdir="/tmp/notpipelines/x"` (not under `~/.agent-hub/pipelines/`); call with `--purge`. Verify `shutil.rmtree` is NOT called, report contains `purge_skipped_process_still_alive_or_untrusted_path`. |
| `test_p23_llm_7_close_skill_lists_remote_only_agent` | FR-LLM-7 | Mock `GET /agents` → `[{"agent_id":"remote-only","status":"idle"}]`. spawned.json empty. List output annotates `remote-only` as `registry-only`. Selecting it triggers ONLY `/unregister`, no `os.kill`. `--purge` is suppressed. |
| `test_p23_llm_8_tasks_skill_payload_schema_prompt` | FR-LLM-8 | Agent advertises `code:v1` with `payload_schema={"type":"object","required":["src"],"properties":{"src":{"type":"string"}}}`. With stubbed input, helper prompts for `src`. With `--raw-json`, helper prompts for the whole JSON blob. With `oneOf` schema, helper auto-falls-back to raw JSON. |
| `test_p23_llm_8b_tasks_metadata_resolution_priority` | FR-LLM-8b | spawned.json has `{"agent_id":"X","cli":"codex","role":"designer"}`. `/agents` returns `[{"agent_id":"X","description":"cli=claude;role=coder"}]`. Resolved metadata → `cli="codex", role="designer"` (spawned wins). For `agent_id="Y"` only in `/agents` with `description="cli=opencode;role=pm"` → `cli="opencode", role="pm"`. For `agent_id="Z"` with no description and not in spawned → `cli="unknown", role="unknown"`. |
| `test_p23_llm_9_unregister_on_stop_and_description_register` | FR-LLM-9 | Spawn an adapter against a fake hub; assert the WS register frame contained `"description":"cli=claude;role=designer;pipeline=..."`. SIGTERM the adapter; assert the fake hub saw `POST /unregister?agent_id=...` within 3s. |
| `test_p23_llm_10_taskfailed_emits_failed_status` | FR-LLM-10 | Configure a task handler that raises `TaskFailed({"error":"cli_exit_1","raw":{...}})`. Drive a fake task through the SDK. Assert the WS message sent is `{"type":"result","status":"failed","result":{"error":"cli_exit_1",...}}`. |
| `test_p23_llm_11_hub_activity_log_handler_persists` | FR-LLM-11 | Send WS `{"type":"activity_log","task_id":...,"action":"payload_schema_warn","details":{"used_tokens":2100}}` from an agent that owns the task. Assert `db.log_activity("task", task_id, "payload_schema_warn", {"used_tokens":2100})` was called once. From a non-owner agent, assert NO call (gated by `reporter_owns_task`). |

---

## 9. Backward compatibility

- All Phase 1 / 1.5 / 2 / 2.2 tests pass without edits.
- `agent_sdk` changes are additive only: new `description` kwarg defaults to `None`, new `TaskFailed` class is opt-in (existing handlers ignore it), new `send_activity_log` method is opt-in. No existing call site changes.
- `hub/main.py` change is additive only: new `elif msg_type == "activity_log"` branch is a no-op for any agent that doesn't send it.
- `clients/codex/agent.py`, `clients/openclaw/agent.py`, `clients/hermes/agent.py` keep working unchanged. The new `llm_agent.py` files sit alongside them.
- `skills/agent-hub/` (Phase 1.5 MCP onboarding skill) is untouched.
- `~/.agent-hub/` directory: created if missing; existing contents untouched.
- `requirements.txt` unchanged. No new top-level deps.
- Phase 2.2 `Capability` shape is consumed as-is — no schema changes to `agents.skills` or `tasks.min_version`.

---

## 10. Answers to all 7 open scope questions (a–g)

| Q | Answer |
|---|---|
| **a** Spawn skill non-interactive? | Yes. All prompts have flag equivalents (§2.1). Defaulting cascade per finding #8: `flag > env (secrets) > .agent-hub.local.json > ~/.agent-hub/config.json > built-in default > interactive prompt (only when stdin.isatty())`. Pure-flag invocations are fully scriptable. |
| **b** `spawned.json` concurrency? | `flock` (Linux/macOS) or `msvcrt.locking` (Windows) on a sidecar `spawned.json.lock` file, plus atomic-rename writes. All three skills go through `clients/_state.py` (§6.5). Corrupt files preserved as `.corrupt-<ts>` and treated as empty. |
| **c** Pipeline workspace cleanup? | Manual for v1. Two affordances: `agent-close --purge` (with trusted-path + terminated guard from §2.3) and `skills/agent-close/scripts/clean_pipelines.py` (stdlib only). No daemon, no time-based auto-purge — workspaces may hold artifacts the user wants. |
| **d** OpenCode session continuity? | One persistent session per `agent_id`. Implicit via `opencode run -c` from a stable `cwd`. Marker `.opencode-session-started` (per finding #15 rename) records first-task-done. Operators wanting determinism per task spawn a fresh `agent_id` (= fresh workdir = fresh session). |
| **e** Budget tracking for codex? | Char-heuristic `(len(prompt) + len(stdout)) // 4`, reported as `usage.estimated_total_tokens` with `usage.source = "char_heuristic"`. Warn-only; never kills mid-task. |
| **f** Custom role with `--capabilities` + `--system-prompt`? | Yes. `--role custom` + `--capabilities a,b,c --system-prompt "..."`. Adapter accepts `--capabilities` and `--system-prompt-extra`; spawn helper just forwards. Custom roles use `default_budget_for("generic")` unless `--max-budget-tokens` is explicit. |
| **g** Skill 2 payload_schema-aware prompting? | Per-subfield by default; raw JSON fallback. Recurse one level into nested objects. Anything beyond two levels of nesting OR any unsupported keyword (`oneOf`/`anyOf`/`allOf`/`$ref`) auto-falls-back to raw JSON. `--raw-json` always uses raw JSON. Validate locally (six-keyword pure-Python validator that the hub uses) before submission. |

---

**End of design v2.** All 14 codex revise findings addressed; three accept items kept (with the marker rename from finding #15); no new top-level deps; full backward compatibility with Phase 1/1.5/2/2.2; ≥ 6 pytest cases (16 total here).
