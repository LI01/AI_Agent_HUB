# Phase 2.3 Design v1 — LLM-CLI Worker Skills + Per-CLI Adapters

**Author:** designer-v1 (sub-agent)
**Date:** 2026-05-09
**Scope:** 3 OpenCode/Claude-Code skills (`agent-spawn`, `agent-tasks`, `agent-close`) + 3 per-CLI worker adapters (`codex`, `claude-code`, `opencode`) + a single role-preset module. Per `design/phase2.3/scope.md`. Additive only — no Phase 1, 1.5, 2, 2.2 redesign. NO new top-level deps. Adapters use `subprocess` (stdlib) + existing `agent_sdk` (which itself uses `httpx`, `websocket-client`).

---

## 1. Summary

- Three new SKILL.md skills land under `skills/`. Each has interactive AND non-interactive (flag-driven) entry forms so they're scriptable. Skills shell out to short Python helper scripts in their bundle (`scripts/spawn.py`, `scripts/tasks.py`, `scripts/close.py`) — the SKILL.md is the LLM-facing prompt; the helpers are deterministic Python.
- Three per-CLI adapter scripts live under `clients/codex/llm_agent.py`, `clients/claude-code/llm_agent.py`, `clients/opencode/llm_agent.py`. Each is invoked by `agent-spawn` as a daemonized subprocess; each registers on the hub via `agent_sdk.AgentHub(unregister_on_stop=True)` and shells out to its CLI in a `task_handler`.
- One source of truth for role presets: `clients/role_presets.py`. Imported by all three adapters AND the spawn skill helper.
- Workspace layout: `~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/`. Spawn skill creates it before the subprocess starts; adapter `cwd`s into it; if `pipeline_id` is omitted at spawn, generate `default-<agent_id>`.
- State: `~/.agent-hub/spawned.json` (skill-managed, atomic-write + advisory file lock); `~/.agent-hub/config.json` (user-edited defaults).
- Budget tracking is **warn-only** in v1 — no mid-task kill. Adapters that get usage from the CLI (claude, opencode) compare to `payload.max_budget_tokens`; codex uses a coarse `len(prompt)+len(output) // 4` estimate.
- All Phase 2.2 capability rules apply unchanged: capability schemas come from role presets and are advertised at registration; the hub's existing dispatch-time validation gate takes care of payload checking.

---

## 2. Skills

All three skills follow the `skills/agent-hub/SKILL.md` shape: a YAML-ish front-matter block (description + triggers), a "Usage" block showing one-line invocation, a "Behavior" section with the LLM-runnable steps, and an "Implementation" pointer to the helper script the host client should `bash`-execute. Helper scripts live in `skills/<name>/scripts/` and are pure Python stdlib + `httpx` (already a dep via agent_sdk).

### 2.1 `skills/agent-spawn/SKILL.md` outline

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
Any flag omitted falls back to: `~/.agent-hub/config.json` → built-in default → interactive prompt (interactive only when `stdin.isatty()`).

**Interactive prompt sequence** (the LLM should ask one at a time, accepting user defaults):

| # | Prompt | Default source |
|---|---|---|
| 1 | `cli` (one of `codex`, `claude`, `opencode`) | none (required) |
| 2 | `role` (one of `pm`, `architect`, `designer`, `coder`, `reviewer`, `tester`, `generic`, `custom`) | `generic` |
| 3 | If `role=custom`: `capabilities` (csv), `system-prompt` (string) | none |
| 4 | `agent_id` | `<cli>-<role>-<short-uuid>` |
| 5 | `pipeline_id` (shared workspace tag; "" = generate `default-<agent_id>`) | `default-<agent_id>` |
| 6 | `hub_url` | config.json → `http://localhost:8080` |
| 7 | `api_key` (read silently; never echoed) | config.json → env `AGENT_HUB_API_KEY` |
| 8 | `max_budget_tokens` | role preset table |

**Pre-flight checks** (the helper script runs these before spawning):

1. `<cli> --version` returns 0 within 5s. Else abort with `cli_not_found: <name>`.
2. CLI auth probe (best-effort, non-blocking): `codex exec --skip-git-repo-check -C /tmp ""` / `claude -p "" --output-format json` / `opencode run --format json ""` with a 3s timeout. If exit != 0 AND stderr contains `auth`/`login`/`key`, print "Run `<cli> login` first and re-run." and abort. Otherwise warn and proceed.
3. `httpx.get(f"{hub_url}/health", timeout=3)` returns 200. Else warn "hub not reachable; agent will retry on its own."
4. Workspace dir creation: `mkdir -p ~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/`. Drop a `.agent-info.json` marker into it: `{agent_id, cli, role, hub_url, started_at}`.

**Subprocess spawn:**

- Command: `python -m clients.<cli>.llm_agent --hub <hub_url> --id <agent_id> --role <role> --workdir <workspace> --max-budget-tokens <n> [--capabilities <csv>] [--system-prompt-extra <str>]`. For the `claude-code` adapter the module path is `clients.claude_code.llm_agent` (Python module names use `_` not `-`; the directory rename / `__init__.py` shim is in §7).
- `cwd`: workspace dir.
- `env`: parent env + `AGENT_HUB_API_KEY=<key>` (key is NEVER passed on the command line — process-list-leak risk). The adapter reads `os.environ["AGENT_HUB_API_KEY"]`.
- Detached: `subprocess.Popen(..., start_new_session=True, stdout=open(workspace/"stdout.log","ab"), stderr=open(workspace/"stderr.log","ab"), stdin=subprocess.DEVNULL)`. The skill returns immediately with the PID. The agent process keeps running after the user's MCP/skill turn ends.
- After spawn: poll `httpx.get(f"{hub_url}/agents/{agent_id}", headers=...)` up to 5×, 500ms apart, to confirm registration. If not registered after 2.5s, print `stderr.log` tail and ask the user to investigate but DO NOT kill — the agent might still be retrying.

**`~/.agent-hub/spawned.json` write** (atomic, see §6 for full schema):

Append a record `{agent_id, pid, cli, role, pipeline_id, hub_url, workdir, started_at}`. Concurrency strategy (Open Q b): the helper acquires `fcntl.flock(LOCK_EX)` on `~/.agent-hub/spawned.json.lock` (created if missing), reads, modifies, then writes via the standard `tmpfile + os.replace` atomic-rename pattern, then releases the lock. Windows fallback: `msvcrt.locking` (also stdlib). Lock acquisition timeout 2s; on contention print "another `agent-spawn`/`agent-close` is in progress, retrying" and retry once.

### 2.2 `skills/agent-tasks/SKILL.md` outline

**Trigger phrases:** "submit a task", "send a task to the agent", "/agent-tasks", "list and pick agent".

**Usage (interactive):**
```bash
/agent-tasks
```

**Usage (non-interactive):**
```bash
/agent-tasks agent=codex-designer-1 task-type=design \
             payload-json='{"spec":"..."}' timeout=300
```

**Interactive flow:**

1. Helper calls `httpx.get(f"{hub_url}/agents", headers=auth)`. (Hub URL + key are taken from `~/.agent-hub/config.json` first; if missing, ask once.)
2. Render the agent table sorted by `(status_rank, agent_id)` where `status_rank = {"idle":0, "busy":1, "offline":2}`:
   ```
   #  agent_id              cli       role       status   capabilities
   1  codex-designer-aa12   codex     designer   idle     design:v1, spec:v1
   2  claude-coder-7f3b     claude    coder      busy     code:v1
   3  opencode-pm-001       opencode  pm         offline  pm:v1, plan:v1, coordinate:v1
   ```
   `cli`/`role` come from the per-agent `description` field (the adapters set `description="cli=<cli>;role=<role>"`). `capabilities` is rendered as `name:v<version>` so users can see when Phase 2.2 versioning is in play.
3. User picks `#`. Hub call `GET /agents/<agent_id>` returns the full capability list (Phase 2.2 shape — `Capability(name, version, payload_schema, result_schema)`).
4. If the agent has more than one capability, ask which `task_type` to use (default = first idle-matching one).
5. **`payload_schema`-aware prompting (Open Q g):** if the resolved capability has a non-null `payload_schema`:
   - Default mode: prompt **per required field** (one prompt per name in `schema["required"]`, hint = field's `type` and any `enum`). For nested objects, recurse one level (prompt for sub-required fields). For arrays of objects with `items.properties`, prompt the user "How many items?" then loop. Anything beyond two levels of nesting OR any `oneOf`/`anyOf`/etc. → fall back to mode 2.
   - Fallback mode: prompt for raw JSON payload, with the schema printed as a hint. Validated locally with the same six-keyword pure-Python validator the hub uses (Phase 2.2 §2.3) before submission, so the user gets immediate feedback rather than a hub round-trip.
   - Override flag: `--raw-json` always uses fallback mode regardless of schema shape.
6. `--task` (natural-language string) is always asked separately (defaults to `"<task_type> task for <agent_id>"`).
7. `--timeout` defaults to 300 (FR-TSK-6). `--max-budget-tokens` defaults to the role preset for the target agent's role (skill reads `description` field to know role).
8. Submit: `POST /tasks` with `{task, task_type, payload, target_agent, timeout}` plus `payload.max_budget_tokens` injected at the top level of `payload` (so adapters see it).
9. **Polling pattern:** 1.5s interval, exponential backoff up to 8s, max wall time = `timeout + 30s`. Each poll prints `[<elapsed>s] status=<s> logs=<n>`. If status transitions to running → fetch and print any new log lines incrementally.
10. Terminal states: `completed` / `failed` / `timeout`. Print `result.text` (the canonical adapter return field — see §3) prominently, then the raw JSON `result` dict in a collapsible/secondary block. If `result.usage` is present, append `(used <n> / budget <m> tokens)`. If `result.payload_schema_warn` is present, surface it in red.

### 2.3 `skills/agent-close/SKILL.md` outline

**Trigger phrases:** "close an agent", "stop the agent", "kill agent", "/agent-close".

**Usage (interactive):**
```bash
/agent-close
```

**Usage (non-interactive):**
```bash
/agent-close ids=codex-designer-aa12,claude-coder-7f3b
/agent-close --all
/agent-close --purge          # also rm -rf the pipeline workspace dir(s)
```

**List union flow:**

1. Read `~/.agent-hub/spawned.json` under shared lock — gives `(agent_id, pid)` pairs we know locally.
2. `httpx.get(f"{hub_url}/agents", headers=auth)` — gives all hub-known agents.
3. Render union, annotating each row with origin: `local+registered` (best — both PID and registry), `local-only` (PID known, no registry entry — agent died or never registered), `registry-only` (registry knows it, no local PID — was spawned elsewhere or `spawned.json` was lost), `local+offline` (PID known, registry says offline).
4. Picker: numbered single OR multi-select (`1,3,5` or `all`).

**Kill sequence (per selected agent):**

```
if local PID is known and PID is alive (os.kill(pid, 0) does not raise):
    os.kill(pid, signal.SIGTERM)
    wait up to 5s, polling pid alive every 250ms
    if still alive: os.kill(pid, signal.SIGKILL); wait up to 2s
unconditionally:
    httpx.post(f"{hub_url}/unregister", params={"agent_id": id}, headers=auth, timeout=3)
remove the entry from spawned.json (under exclusive lock)
if --purge: shutil.rmtree("~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/")
            and if pipeline dir is now empty, rmdir the parent
```

The adapter itself listens for SIGTERM and runs `AgentHub.stop()` in its handler, which (because `unregister_on_stop=True`) makes the REST `/unregister` call from inside the agent process. The skill's explicit `/unregister` call is a belt-and-braces second attempt — idempotent on the hub side (re-unregistering a non-existent agent returns 200 with `already gone`).

**Edge cases:**

| Case | Handling |
|---|---|
| PID dead but registry stale | Skip TERM/KILL; still call `/unregister`. Log `cleaned_stale_registration: <id>`. |
| Registry has agent but no PID known (`registry-only`) | Call `/unregister` only. The remote process owning that agent is unaffected — print a warning. |
| `local+registered` but unregister returns 404 | Treat as success (already gone). Remove from `spawned.json`. |
| PID belongs to a different process now (PID reuse) | Detect via reading `/proc/<pid>/cmdline` on Linux or `psutil` (NOT a new dep — fall back to `ps -p <pid> -o command=` via subprocess on macOS/BSD/Windows). If cmdline doesn't contain `clients.` and `llm_agent`, treat as PID-reused and skip kill. |
| `spawned.json` missing or corrupt | Treat as empty. Log warning. |
| Hub unreachable | Skip `/unregister` for that agent, surface `hub_unreachable` in the per-agent result row, but still SIGTERM the local PID and remove from `spawned.json`. The local agent process's own `unregister_on_stop` will retry (best-effort) on its way out. |

---

## 3. Per-CLI worker adapters

Common shape — all three modules look the same skeleton, differ only in `_run_cli_for_task`.

```python
# clients/<cli>/llm_agent.py — common skeleton
import argparse, json, os, signal, subprocess, sys, time
from pathlib import Path
from agent_sdk import AgentHub
from clients.role_presets import preset_for, capabilities_for, system_prompt_for, default_budget_for

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--hub", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--role", default="generic")
    p.add_argument("--workdir", required=True)
    p.add_argument("--max-budget-tokens", type=int, default=None)
    p.add_argument("--capabilities", default=None,                # custom-role override
                   help="csv; overrides role preset")
    p.add_argument("--system-prompt-extra", default=None)         # appended to preset
    return p

def _resolve_capabilities(args) -> list[dict]:
    """Returns Phase 2.2 Capability dicts (name+version+payload_schema)."""
    if args.capabilities:
        names = [c.strip() for c in args.capabilities.split(",") if c.strip()]
        return [{"name": n, "version": 1, "payload_schema": _PAYLOAD_SCHEMA_V1} for n in names]
    return capabilities_for(args.role)        # role_presets returns list[dict]

_PAYLOAD_SCHEMA_V1 = {
    "type": "object",
    "properties": {
        "prompt":            {"type": "string"},
        "max_budget_tokens": {"type": "integer"},
        "system_prompt_extra": {"type": "string"},
    },
    "required": ["prompt"],
    "additionalProperties": True,             # tolerate future fields
}

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
        unregister_on_stop=True,                  # <-- mandatory per scope
    )

    # Set description so agent-tasks/agent-close skills can render cli/role.
    # (description isn't a top-level AgentHub kwarg today — adapter sends it
    # via the heartbeat/register WS field if exposed, OR via a one-shot REST
    # PATCH /agents/<id>/metadata. v1 uses the simplest path: send it as
    # part of the WS register message by subclassing-via-monkey-patch the
    # `_on_open` hook below. Coder may instead add a `description` kwarg
    # to AgentHub; this is a 5-line additive change. Either is fine.)
    _attach_description(hub, f"cli={CLI_NAME};role={args.role};pipeline={workdir.parent.name}")

    @hub.task_handler
    def handle(task):
        return _run_cli_for_task(task, workdir, role_prompt, budget)

    def _shutdown(signum, frame):
        hub.stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    hub.start()        # blocks until stop()
```

### 3.1 `clients/codex/llm_agent.py`

```python
CLI_NAME = "codex"

def _run_cli_for_task(task, workdir, role_prompt, budget):
    payload = task.get("payload") or {}
    prompt = payload.get("prompt") or task.get("task") or ""
    full_prompt = (role_prompt + "\n\n" + prompt) if role_prompt else prompt
    timeout = int(task.get("timeout") or 300)
    deadline = max(5, timeout - 5)            # die before hub timeout

    cmd = [
        "codex", "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C", str(workdir),
        full_prompt,
    ]
    t0 = time.monotonic()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=deadline)
    except subprocess.TimeoutExpired as exc:
        return {
            "text": "",
            "usage": None,
            "raw": {"stdout": exc.stdout or "", "stderr": exc.stderr or ""},
            "cli": CLI_NAME,
            "role": _ROLE,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": "cli_timeout",
        }
    elapsed = round(time.monotonic() - t0, 2)

    text = _parse_codex_output(cp.stdout)
    # Open Q e: codex exec doesn't emit token usage by default. Estimate
    # via 4-chars-per-token heuristic. Document as approximate. If a
    # future codex version adds --json with usage, switch the parser.
    estimated = (len(full_prompt) + len(text)) // 4
    usage = {"estimated_total_tokens": estimated, "source": "char_heuristic"}
    result = {
        "text": text,
        "usage": usage,
        "raw": {"stdout_tail": cp.stdout[-2000:], "stderr_tail": cp.stderr[-2000:],
                "returncode": cp.returncode},
        "cli": CLI_NAME, "role": _ROLE, "elapsed_s": elapsed,
    }
    if cp.returncode != 0:
        result["error"] = f"cli_exit_{cp.returncode}"
    if budget and estimated > budget:
        result["payload_schema_warn"] = (
            f"estimated tokens {estimated} exceeded max_budget_tokens {budget}"
        )
        # also write one activity_log entry via hub (use AgentHub.send_log)
        # — coder wires this via a closure capturing `hub` + `task["task_id"]`.
    return result

def _parse_codex_output(stdout: str) -> str:
    """codex exec prints prompt echo + reasoning + final assistant text.
    The final assistant text is delimited by the last `---` separator OR the
    last contiguous block after the last `[assistant]:` marker (codex's
    actual format depends on version). v1 strategy: take the last paragraph
    after the final empty-line block. Fallback: return raw stdout."""
    # Pseudocode — coder validates against actual codex 0.x output samples.
    parts = [p.strip() for p in stdout.split("\n\n") if p.strip()]
    return parts[-1] if parts else stdout.strip()
```

**Open Q e answer (budget for codex):** char-heuristic `(len(prompt)+len(output))//4` for v1, with `usage.source="char_heuristic"` so consumers know it's approximate. If `codex exec --json` lands upstream we switch parsers; the result-dict shape doesn't change.

### 3.2 `clients/claude-code/llm_agent.py`

```python
CLI_NAME = "claude"

def _run_cli_for_task(task, workdir, role_prompt, budget):
    payload = task.get("payload") or {}
    prompt = payload.get("prompt") or task.get("task") or ""
    timeout = int(task.get("timeout") or 300)
    deadline = max(5, timeout - 5)

    cmd = ["claude", "-p", "--bare", "--output-format", "json",
           "--add-dir", str(workdir)]
    if role_prompt:
        cmd += ["--append-system-prompt", role_prompt]
    cmd.append(prompt)

    t0 = time.monotonic()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=deadline)
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(exc, t0)

    elapsed = round(time.monotonic() - t0, 2)
    try:
        parsed = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return {"text": "", "usage": None, "raw": {"stdout_tail": cp.stdout[-2000:],
                "stderr_tail": cp.stderr[-2000:], "returncode": cp.returncode},
                "cli": CLI_NAME, "role": _ROLE, "elapsed_s": elapsed,
                "error": "cli_output_unparseable"}

    text = parsed.get("response") or parsed.get("result") or ""   # claude json shape
    usage = parsed.get("usage")     # {input_tokens, output_tokens, ...}
    total = (usage or {}).get("input_tokens", 0) + (usage or {}).get("output_tokens", 0)

    result = {"text": text, "usage": usage, "raw": parsed,
              "cli": CLI_NAME, "role": _ROLE, "elapsed_s": elapsed}
    if cp.returncode != 0:
        result["error"] = f"cli_exit_{cp.returncode}"
    if budget and total and total > budget:
        result["payload_schema_warn"] = (
            f"used tokens {total} exceeded max_budget_tokens {budget}"
        )
    return result
```

Budget is real (claude returns usage). Warn-only — never kills mid-task.

### 3.3 `clients/opencode/llm_agent.py`

```python
CLI_NAME = "opencode"
_SESSION_FILE = "session_id"          # persisted in workdir between tasks

def _run_cli_for_task(task, workdir, role_prompt, budget):
    payload = task.get("payload") or {}
    prompt = payload.get("prompt") or task.get("task") or ""
    if role_prompt:
        prompt = role_prompt + "\n\n" + prompt
    timeout = int(task.get("timeout") or 300)
    deadline = max(5, timeout - 5)

    # Open Q d: ONE persistent session per agent_id. Rationale: the agent's
    # role + workdir already scope memory; a persistent session lets a
    # multi-task pipeline (e.g. PM hands off to coder, coder gets follow-ups
    # on the same module) accumulate cheap context without re-priming. If the
    # operator wants determinism for a one-shot probe, they spawn a fresh
    # agent_id (which gets a fresh workdir, hence a fresh session).
    sess_path = workdir / _SESSION_FILE
    cmd = ["opencode", "run", "--format", "json"]
    if sess_path.exists():
        cmd += ["-c"]               # continue last session in this cwd
    cmd.append(prompt)

    t0 = time.monotonic()
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=deadline, cwd=str(workdir))
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(exc, t0)

    elapsed = round(time.monotonic() - t0, 2)
    try:
        parsed = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return _unparseable_result(cp, t0, elapsed)

    # Mark that we now have a session in this workdir.
    if not sess_path.exists():
        sess_path.write_text(parsed.get("session_id") or "1")

    text = _extract_last_message_text(parsed)
    usage = parsed.get("usage")
    total = (usage or {}).get("total_tokens") or 0

    result = {"text": text, "usage": usage, "raw": parsed,
              "cli": CLI_NAME, "role": _ROLE, "elapsed_s": elapsed}
    if cp.returncode != 0:
        result["error"] = f"cli_exit_{cp.returncode}"
    if budget and total and total > budget:
        result["payload_schema_warn"] = f"used tokens {total} exceeded max_budget_tokens {budget}"
    return result
```

**Open Q d answer:** ONE persistent session per `agent_id` via `opencode run -c` from a stable `cwd` (the workspace dir). The persistent session is *implicit* — opencode's `-c` continues the most-recent session in that cwd — so we don't need a session_id file. We still drop a marker so we know whether this is the first task (no `-c`) or a follow-up. Operators who want a fresh session per task spawn a fresh agent_id with a different workdir.

---

## 4. `clients/role_presets.py`

One file, one source of truth. Imported as `from clients.role_presets import preset_for, capabilities_for, system_prompt_for, default_budget_for`.

```python
# clients/role_presets.py
from __future__ import annotations
import json, os
from pathlib import Path
from typing import Optional

# Capability schema reused across all roles' default tasks. Phase 2.2 shape.
_DEFAULT_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt":              {"type": "string"},
        "max_budget_tokens":   {"type": "integer"},
        "system_prompt_extra": {"type": "string"},
    },
    "required": ["prompt"],
    "additionalProperties": True,
}

ROLE_PRESETS: dict[str, dict] = {
    "pm": {
        "capabilities": ["pm", "plan", "coordinate"],
        "system_prompt": "You are the PM. Decompose, sequence, escalate.",
        "default_budget": 30000,
    },
    "architect": {
        "capabilities": ["architect", "design"],
        "system_prompt": "You are the architect. Module boundaries, data flow, tradeoffs.",
        "default_budget": 50000,
    },
    "designer": {
        "capabilities": ["design", "spec"],
        "system_prompt": "You are the designer. Concrete schemas, message shapes.",
        "default_budget": 40000,
    },
    "coder": {
        "capabilities": ["code"],
        "system_prompt": "You implement features. Match style. Test-first.",
        "default_budget": 30000,
    },
    "reviewer": {
        "capabilities": ["review", "code-review"],
        "system_prompt": "You critique. Find real bugs and design issues, ignore style nits.",
        "default_budget": 20000,
    },
    "tester": {
        "capabilities": ["test"],
        "system_prompt": "You write tests that fail without the change and pass with it.",
        "default_budget": 20000,
    },
    "generic": {
        "capabilities": ["chat"],
        "system_prompt": None,
        "default_budget": 20000,
    },
}

def _load_user_overrides() -> dict:
    """Read ~/.agent-hub/config.json. Returns {} if missing/unreadable."""
    cfg = Path(os.path.expanduser("~/.agent-hub/config.json"))
    try: return json.loads(cfg.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError): return {}

def preset_for(role: str) -> dict:
    base = ROLE_PRESETS.get(role) or ROLE_PRESETS["generic"]
    ov = _load_user_overrides()
    p = (ov.get("role_prompts") or {}).get(role)
    b = (ov.get("role_budgets") or {}).get(role)
    return {
        "capabilities": base["capabilities"],
        "system_prompt": p if p is not None else base["system_prompt"],
        "default_budget": int(b) if b is not None else base["default_budget"],
    }

def capabilities_for(role: str) -> list[dict]:
    return [{"name": n, "version": 1, "payload_schema": _DEFAULT_PAYLOAD_SCHEMA}
            for n in preset_for(role)["capabilities"]]

def system_prompt_for(role: str) -> Optional[str]: return preset_for(role)["system_prompt"]
def default_budget_for(role: str) -> int: return preset_for(role)["default_budget"]
```

The spawn-skill helper imports the same module so the two ends agree on what `role=designer` means.

**Open Q f answer (custom roles):** Yes. The spawn skill accepts `--role custom` plus `--capabilities a,b,c --system-prompt "..."`. The adapter's `--capabilities`/`--system-prompt-extra` flags handle this directly; the spawn skill helper just forwards them.

---

## 5. Workspace strategy

**Layout.** `~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/`. Holds:
- `.agent-info.json` — written by spawn skill at creation time. `{agent_id, cli, role, hub_url, started_at}`.
- `stdout.log`, `stderr.log` — adapter subprocess output (rotated by the spawn skill: rename to `.1` if > 10 MB at spawn).
- `session_id` — opencode adapter only, marker file (§3.3).
- Anything the LLM CLI writes during task execution (codex/claude/opencode `cwd` is this dir).

**Created by:** the spawn skill helper, BEFORE `subprocess.Popen`. (a) adapter's `cwd=` needs it; (b) client-side keeps ownership/permissions matching the user.

**If `pipeline_id` omitted:** generate `default-<agent_id>` so each unscoped agent gets a unique parent — `rm -rf ~/.agent-hub/pipelines/default-<id>/` is always safe.

**Cleanup policy (Open Q c):** manual for v1. Two affordances:
1. `agent-close --purge` removes the per-agent workspace dir AND parent if empty.
2. Bundled `scripts/clean-pipelines.sh` (≤ 15 lines): `find ~/.agent-hub/pipelines -mindepth 2 -maxdepth 2 -type d -mtime +14`, filter against agent_ids in `spawned.json` via `jq`, print `would remove <dir>` (operator adds `--apply` to actually `rm -rf`). No daemon, no cron — operator runs when they want.

Time-based auto-purge is out of scope for v1: workspaces may hold artifacts the user wants to keep.

---

## 6. Config + spawned.json shapes

### 6.1 `~/.agent-hub/config.json`

All fields optional; missing fields fall back to built-in defaults. Skill helpers read this once per invocation.

```jsonc
{
  "hub_url": "http://localhost:8080",
  "api_key": "ah_…",                 // OR set AGENT_HUB_API_KEY env (env wins)
  "default_pipeline_id": null,        // optional auto-fill for spawn skill
  "role_prompts": {
    "coder":    "You implement features in TypeScript. Match Airbnb style.",
    "designer": null                  // null = "use built-in default"
  },
  "role_budgets": {
    "coder":   80000,
    "tester": 30000
  }
}
```

The skill writes a fresh `config.json` on first use if missing, populated only with the user's just-entered `hub_url` and `api_key` (everything else stays default). The file is `chmod 600` because it holds an API key.

### 6.2 `.agent-hub.local.json` (optional, in cwd)

Same shape as `config.json`. Merged on top of the global one (deep-merge, with the local file winning per-key). Documented as "for per-project overrides; gitignore it."

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
  },
  {
    "agent_id":     "claude-coder-7f3b",
    "pid":          82740,
    "cli":          "claude",
    "role":         "coder",
    "pipeline_id":  "default-claude-coder-7f3b",
    "hub_url":      "http://localhost:8080",
    "workdir":      "/Users/leon/.agent-hub/pipelines/default-claude-coder-7f3b/claude-coder-7f3b",
    "started_at":   "2026-05-09T12:31:42Z"
  }
]
```

Atomic write pattern (Open Q b answered in §2.1 — `flock` + tmpfile rename):
```python
import fcntl, json, os, tempfile
LOCK_PATH = Path("~/.agent-hub/spawned.json.lock").expanduser()
DATA_PATH = Path("~/.agent-hub/spawned.json").expanduser()
LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
with open(LOCK_PATH, "a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        records = json.loads(DATA_PATH.read_text()) if DATA_PATH.exists() else []
        records = mutate(records)               # add/remove/update
        tmp = tempfile.NamedTemporaryFile("w", dir=DATA_PATH.parent, delete=False)
        json.dump(records, tmp); tmp.close()
        os.replace(tmp.name, DATA_PATH)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
```

Windows: use `msvcrt.locking(fd, msvcrt.LK_LOCK, 1)` on a single byte of the same lock file. Both are stdlib.

---

## 7. Module-by-module change list

| Path | New / Modified | Purpose |
|---|---|---|
| `skills/agent-spawn/SKILL.md` | NEW | LLM-facing spawn flow per §2.1. |
| `skills/agent-spawn/scripts/spawn.py` | NEW | Pre-flight, workspace creation, subprocess.Popen, spawned.json write. |
| `skills/agent-tasks/SKILL.md` | NEW | LLM-facing list+pick+submit+poll flow per §2.2. |
| `skills/agent-tasks/scripts/tasks.py` | NEW | REST calls, payload_schema-aware prompting, polling loop, result rendering. |
| `skills/agent-close/SKILL.md` | NEW | LLM-facing list+pick+kill flow per §2.3. |
| `skills/agent-close/scripts/close.py` | NEW | Union list, SIGTERM/SIGKILL, /unregister, spawned.json mutation, optional --purge. |
| `skills/agent-close/scripts/clean-pipelines.sh` | NEW | Bundled snippet from §5. |
| `clients/role_presets.py` | NEW | Single-source role preset table per §4. |
| `clients/codex/llm_agent.py` | NEW | Codex adapter per §3.1. |
| `clients/claude-code/llm_agent.py` | NEW | Claude Code adapter per §3.2. |
| `clients/claude_code/__init__.py` | NEW (shim) | One-line `from clients import claude_code` enabler so `python -m clients.claude_code.llm_agent` works without renaming the dir. Helper: `clients/claude_code/__init__.py` is a 1-line `from clients["claude-code"] import *`-style trampoline; alternative is to rename `clients/claude-code/` to `clients/claude_code/` (PREFERRED — cleaner). Coder picks one. |
| `clients/opencode/llm_agent.py` | NEW | OpenCode adapter per §3.3. |
| `clients/_shared.py` | NEW | Tiny helpers: `_timeout_result`, `_unparseable_result`, `_extract_last_message_text`, `_attach_description`. Imported by all three adapters. ≤ 80 lines. |
| `agent_sdk/client.py` | OPTIONAL one-line modify | Add a `description: Optional[str] = None` kwarg to `AgentHub.__init__` and include it in the WS register message. If we don't, adapter uses `_attach_description` to set it via REST PATCH after register. Coder picks one. |
| `tests/test_phase23_skills.py` | NEW | All 6+ pytest cases per §8. |
| `requirements.txt` | NO CHANGE | Per scope hard constraint. |

---

## 8. Test plan

≥ 6 new pytest cases. `test_p23_*` prefix; `FR-LLM-*` IDs.

| Test | ID | Behavior |
|---|---|---|
| `test_p23_llm_1_role_preset_lookup` | FR-LLM-1 | `preset_for("designer")["capabilities"] == ["design","spec"]`; `default_budget_for("coder") == 30000`; `system_prompt_for("generic") is None`. With monkeypatched `~/.agent-hub/config.json` containing `role_budgets.coder=99999`, `default_budget_for("coder") == 99999`. |
| `test_p23_llm_2_spawned_json_roundtrip_and_lock` | FR-LLM-2 | Two threads each write a record; both records present; file is valid JSON; no partial writes (assert via reading mid-write would block). Use `pytest-tmp_path`. |
| `test_p23_llm_3_codex_adapter_command_construction` | FR-LLM-3 | Monkeypatch `subprocess.run` to capture argv; call `_run_cli_for_task({"task":"hello","timeout":60}, workdir, role_prompt="X", budget=1000)`; assert argv == `["codex","exec","--skip-git-repo-check","--dangerously-bypass-approvals-and-sandbox","-C",str(workdir),"X\n\nhello"]` and `timeout=55`. |
| `test_p23_llm_4_claude_adapter_parses_usage_and_warns` | FR-LLM-4 | Mock subprocess returning `{"response":"ok","usage":{"input_tokens":900,"output_tokens":1200}}`. Call adapter with `budget=1000`. Assert `result.usage.input_tokens==900`, `result.text=="ok"`, `result.payload_schema_warn` mentions "used tokens 2100 exceeded max_budget_tokens 1000". |
| `test_p23_llm_5_opencode_adapter_session_persistence` | FR-LLM-5 | First call → argv lacks `-c`, marker file appears. Second call → argv contains `-c`. Assert `cwd=str(workdir)` both times. |
| `test_p23_llm_6_close_skill_sigterm_then_sigkill` | FR-LLM-6 | Spawn a sleep subprocess, record into spawned.json. Call `close.py --ids <id>`. Verify SIGTERM sent first (mock the unkillable behavior), 5s elapses (use `time.sleep` patch), SIGKILL sent. `/unregister` called via httpx mock. spawned.json no longer contains the entry. |
| `test_p23_llm_7_close_skill_lists_remote_only_agent` | FR-LLM-7 | Mock `GET /agents` to return `[{agent_id:"remote-only", status:"idle"}]`. spawned.json is empty. List output contains `remote-only` annotated `registry-only`. Selecting it triggers ONLY `/unregister`, no SIGTERM. |
| `test_p23_llm_8_tasks_skill_payload_schema_prompt` | FR-LLM-8 | Agent advertises `code:v1` with `payload_schema={"type":"object","required":["src"],"properties":{"src":{"type":"string"}}}`. Drive interactive mode with stubbed input. Assert the helper prompts for `src` (not raw JSON). With `--raw-json` flag, helper prompts for the whole JSON blob instead. With unsupported schema (`oneOf`), helper auto-falls-back to raw JSON. |
| `test_p23_llm_9_unregister_on_stop` | FR-LLM-9 | Spawn an adapter against a fake hub; SIGTERM it; assert the fake hub saw a POST `/unregister?agent_id=...` within 3s. (Uses `unregister_on_stop=True` plumbing already in agent_sdk.) |

---

## 9. Backward-compatibility — what must keep passing unchanged

- All Phase 1 / 1.5 / 2 / 2.2 tests pass without edits. Nothing in this design touches `hub/`, `mcp/`, or `agent_sdk/client.py` substantively (the optional `description=` kwarg in §7 is additive with a default `None`).
- `clients/codex/agent.py`, `clients/openclaw/agent.py`, `clients/hermes/agent.py` keep working unchanged. The new `llm_agent.py` files sit alongside them — they don't replace `agent.py`.
- `skills/agent-hub/` (Phase 1.5 MCP onboarding skill) keeps working — the three new skills are additive.
- `~/.agent-hub/` directory: if it doesn't exist when a new skill runs, it's created (`mkdir -p`); existing contents are not touched.
- `requirements.txt` unchanged. No new top-level deps.
- `agent_sdk.AgentHub` API is unchanged for existing callers — `unregister_on_stop` already exists with default `False`; only the new adapters set it to `True`.
- `Capability` shape from Phase 2.2 is consumed as-is — no schema changes to `agents.skills` column or `tasks.min_version` column.

---

## 10. Answers to all 7 open scope questions (a–g)

| Q | Answer |
|---|---|
| **a** Spawn skill non-interactive? | **Yes.** All eight prompts have flag equivalents (§2.1 Usage block). Defaulting cascade: flag → `~/.agent-hub/config.json` → `.agent-hub.local.json` → built-in → interactive prompt (only when `stdin.isatty()`). Pure-flag invocations are fully scriptable. |
| **b** `spawned.json` concurrency? | **Both `flock` + atomic rename.** `fcntl.flock` (Linux/macOS) or `msvcrt.locking` (Windows) on a sidecar `spawned.json.lock` file. Modify under exclusive lock; write via `tempfile.NamedTemporaryFile` + `os.replace`. Lock acquisition timeout 2s with one retry. (§2.1, §6.3) |
| **c** Pipeline workspace cleanup? | **Manual for v1.** Two affordances: `agent-close --purge` flag and a bundled `scripts/clean-pipelines.sh`. No daemon, no time-based auto-purge — the workspace may hold artifacts the user wants to keep. (§5) |
| **d** OpenCode session continuity? | **One persistent session per `agent_id`.** Implicit via `opencode run -c` from a stable `cwd`. First task in a workdir runs without `-c`; subsequent tasks add `-c`. Operators wanting determinism spawn a fresh `agent_id` (= fresh workdir = fresh session). (§3.3) |
| **e** Budget tracking for codex? | **Char-heuristic estimate.** `(len(prompt) + len(stdout)) // 4` reported as `usage.estimated_total_tokens` with `usage.source = "char_heuristic"`. Warn-only, never kills mid-task. Switch to real usage when `codex exec --json` lands upstream. (§3.1) |
| **f** Custom role with `--capabilities` + `--system-prompt`? | **Yes.** Spawn skill flag `--role custom` activates `--capabilities a,b,c` + `--system-prompt "..."`. The adapter already accepts both flags (`--capabilities` and `--system-prompt-extra`); the spawn helper just forwards. Custom roles use `default_budget_for("generic")` unless `--max-budget-tokens` is also given. (§3, §4) |
| **g** Skill 2 payload_schema-aware prompting — raw JSON or per-subfield? | **Per-subfield by default; raw JSON as fallback or via `--raw-json` flag.** Recurse one level into nested objects. Anything beyond two levels of nesting OR any unsupported keyword (`oneOf`, `anyOf`, etc.) auto-falls back to raw JSON mode. Validate locally before submission with the same six-keyword pure-Python validator the hub uses, so the user gets immediate feedback. (§2.2 step 5) |

---

## 11. Open questions for codex review

1. **Adapter description plumbing.** Add `description: Optional[str] = None` to `AgentHub.__init__` (additive, 5-line SDK change) vs. post-register REST PATCH from adapter. SDK kwarg is cleaner; staying out keeps Phase 2.3 isolated to skills/clients.
2. **`clients/claude-code/` rename.** Python modules can't contain hyphens. Rename to `clients/claude_code/` (cleanest) or add a trampoline `__init__.py` shim. I lean rename.
3. **Codex output parser robustness.** §3.1's "last paragraph" heuristic is brittle vs. codex version drift. Alternative: pass full stdout as `text`, let the consumer strip.
4. **Budget warning surfacing.** Scope says one `activity_log` entry per overage. Use existing `send_log` with a `payload_schema_warn:` prefix (v1, simpler) or add a new `send_activity_warn` SDK method backed by a new WS type? §3 picks the prefix path.
5. **Spawned-agent metadata in `agent-tasks`.** §2.2 parses `description` to extract `cli`/`role`. Brittle. Alternative: cross-reference `spawned.json` first, fall back to description only for `registry-only` rows.
6. **`--purge` race.** `shutil.rmtree` while the adapter is mid-write to `workdir/stdout.log` may race. §2.3's flow purges only AFTER the SIGKILL window AND PID-dead confirmation — codex confirm explicit ordering is sufficient.
7. **Pre-flight CLI auth probe false negatives.** §2.1 step-2 probes the CLI with empty input under a 3s timeout. On some CLIs (e.g. `opencode run ""`) this may print a help banner with exit 0 instead of an auth error, mis-classifying unauthenticated as fine. Codex: drop the probe and rely on first-real-task error?

---

**End of design v1.** Three skills + three adapters + one preset module, all additive, no new top-level deps, full backward compatibility with Phase 1/1.5/2/2.2, all 7 open scope questions answered, 9 pytest cases sketched.
