# Agent Tasks

Submit a task to an LLM-CLI agent registered with Agent Hub, then poll until
it reaches a terminal status.

This skill is the operator-side counterpart to `agent-spawn` and `agent-close`.
It picks an agent from the hub registry (merged with locally-tracked
`spawned.json` records), prompts for the task payload using the agent's
advertised `payload_schema`, submits via `POST /tasks`, and polls until
`completed` / `failed` / `timeout`.

## Trigger phrases

- "list agents"
- "submit task"
- "submit a task"
- "send a task to the agent"
- "assign task"
- "agent task"
- `/agent-tasks`

## Usage

Interactive (the default):

```bash
/agent-tasks
```

Non-interactive (skip prompts where flags are supplied):

```bash
/agent-tasks --hub http://localhost:8080 \
             --key $AGENT_HUB_API_KEY \
             --agent codex-designer-aa12 \
             --task-type design \
             --payload '{"prompt":"draft the schema"}'
```

Other flags:

- `--min-version <int>` — minimum capability version to require (default 1).
- `--timeout <int>` — per-task timeout in seconds (default 300, matches the
  hub's default).
- `--max-budget-tokens <int>` — passed through in `payload.max_budget_tokens`.
- `--no-poll` — submit and return the `task_id` immediately, no polling.
- `--interactive` — force interactive prompting even when other flags are
  supplied.
- `--file <rel>=<local>` — attach a local UTF-8 text file as a
  `payload.files` entry. `<rel>` is the path inside the agent's workdir
  (relative, no `..`, no leading `/`, no `\`). `<local>` is the path on
  your machine. Repeatable. Merges with any `files` already supplied via
  `--payload`. Hard-fails on binary input — base64 fallback is Phase
  2.5+. Example: `--file plan.md=./design/plan.md`.
- `--expect-back <rel>` — request that the agent's workdir be read back
  at `<rel>` into `result.files` after the task completes. Repeatable.
  Merges with any `expect_files_back` already supplied via `--payload`.
  Files read back are subject to the per-file (1 MiB) and per-task
  (5 MiB) caps; oversize files are reported in `result.files_truncated`.

## Behavior

1. Resolve `hub_url` and `api_key` from flags / env / config (precedence:
   flag > env > local config > global config > built-in default > prompt).
2. `GET /agents` and merge with `clients._state.locked_read_spawned()`.
3. Render the agent table. Per design v3 §2.2 metadata priority:
   `spawned.json[agent_id].cli/role` > parsed `description="cli=...;role=..."`
   > `"unknown"`.
4. User picks an agent (or `--agent` skips this step). `GET /agents/<id>`
   gives the full capability list.
5. User picks a `task_type` (or `--task-type` skips this step).
6. Build the payload from the capability's `payload_schema`:
   - For `type:object` with `properties`, prompt per field.
   - Recurse one level into nested `type:object` properties (max depth 2).
   - Anything unsupported (`oneOf`, `anyOf`, `allOf`, `$ref`) at any level
     falls back to raw JSON.
   - `--payload <json>` skips prompting entirely.
7. `POST /tasks` with `{task, task_type, payload, target_agent, min_version,
   timeout}`.
8. Poll `GET /tasks/<id>` until terminal (or `--no-poll` returns immediately).

## Implementation

Deterministic logic lives in `scripts/tasks.py`. The LLM should invoke that
script, not re-implement the flow:

```bash
python -m skills.agent-tasks.scripts.tasks --hub <url> --key <key> [...]
```

or directly:

```bash
python skills/agent-tasks/scripts/tasks.py --hub <url> --key <key> [...]
```

Design ref: `design/phase2.3/design_v3.md` §2.2, §7, §10.
