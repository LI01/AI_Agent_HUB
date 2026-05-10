# Phase 2.4 Scope — File Sharing via `payload.files` / `result.files`

**Locked:** 2026-05-09 19:10 local. Per chat brainstorm option A.

## What we're shipping

Adapters materialize files from the task's `payload` to their workdir before invoking the CLI, and optionally read files back into `result` after the CLI completes. Cross-host, no shared filesystem needed, no new hub endpoints.

### F5 — `payload.files` materialization (input)

Submitter (or parent agent) attaches a list:

```json
{
  "task_type": "review",
  "payload": {
    "prompt": "Review binary_search.py for real bugs.",
    "files": [
      {"path": "binary_search.py", "content": "def binary_search(arr, target):\n    ..."},
      {"path": "tests/test_bs.py", "content": "..."}
    ]
  }
}
```

Adapter behavior, BEFORE `subprocess.run([...])`:

1. Validate each entry has `path` (string, no leading `/`, no `..` segments) and `content` (string).
2. Reject paths that escape the workdir (defence against `../../etc/passwd`).
3. Reject the whole task with `payload_too_large` if any single file > **1 MB** OR total content > **5 MB**.
4. Create parent dirs as needed.
5. Write each file's `content` to `<workdir>/<path>` (text mode, UTF-8).

If validation fails, raise `TaskFailed({"error": "invalid_payload_files", "detail": "..."})` so the task ends `failed` cleanly.

### F6 — `result.files` readback (output)

Submitter optionally requests files back:

```json
{
  "payload": {
    "prompt": "Edit binary_search.py to fix the off-by-one.",
    "files": [{"path": "binary_search.py", "content": "..."}],
    "expect_files_back": ["binary_search.py"]
  }
}
```

Adapter behavior, AFTER successful `subprocess.run`:

1. For each path in `payload.expect_files_back`: read `<workdir>/<path>`.
2. If a requested file is missing or unreadable, skip it (do NOT fail the task — return what's available); include it in `result.files_missing` instead.
3. Apply the same size limits to the readback (1 MB per file, 5 MB total). Truncate / skip with a `result.files_truncated` note if exceeded.
4. Build `result["files"] = [{"path": ..., "content": ...}, ...]`.

If `expect_files_back` is omitted, the adapter does not read any files back.

### F7 — Skill UX: `/agent-tasks --file <relpath>=<local-path>`

The `agent-tasks` skill grows a flag:

- `--file binary_search.py=./local/binary_search.py` reads the local file, embeds its UTF-8 content as one entry in `payload.files`.
- Repeatable: `--file plan.md=./plan.md --file fib.py=./fib.py`.
- Also: `--expect-back <relpath>` (repeatable) → adds to `payload.expect_files_back`.
- The interactive UX picks up the same fields when the schema doesn't constrain them.

### Out of scope (Phase 2.5+ / Phase 3)

- **Binary content** via `content_b64`. Easy follow-on (just one alternate field in the entry shape).
- **Auto-detect changed files** in workdir after CLI run. Explicit-list-only is safer.
- **Hub-side file store** (`POST /pipelines/{id}/files`) — separate phase; this design intentionally keeps the hub stateless about file content.
- **Streaming large files**. Tasks with payload > 5 MB are rejected; users move to the Phase 3 file store.
- **Per-file mode bits / permissions** (e.g. executable). Phase 2.5+ if needed.

## Module-by-module change preview

The designer should produce concrete pseudocode/snippets for these:

- `clients/codex/llm_agent.py` — add file materialization before `subprocess.run`; readback after.
- `clients/claude_code/llm_agent.py` — same.
- `clients/opencode/llm_agent.py` — same.
- `clients/_files.py` (NEW shared helper) — `materialize_files(workdir, files) -> None`, `readback_files(workdir, expect_back) -> list[dict]`, `validate_files(files) -> None`. All three adapters import from here so behavior doesn't fork.
- `skills/agent-tasks/scripts/tasks.py` — add `--file` and `--expect-back` flags; embed into payload.
- `tests/test_unit.py` — at least 6 new pytest cases.

## Hard constraints

- NO new top-level dependencies. Stdlib only (`pathlib`, `os`).
- NO hub changes. The payload field is just JSON the hub already passes through unmodified.
- Path traversal MUST be blocked. Test: `payload.files = [{"path": "../escape", "content": "x"}]` → reject.
- Backward-compat: tasks without `payload.files` continue to work exactly as today. Adapters that don't load the new helper still work (the helper is opt-in via import).

## Acceptance criteria

A design pass is acceptable when:

1. `clients/_files.py` API spec: signatures, return types, exception types (`InvalidPayloadFiles`, etc.).
2. Pseudocode for adapter integration showing exactly where the materialize/readback calls go.
3. Path validation algorithm spelled out (no `/`, no `..`, no symlink escape).
4. Size enforcement: per-file and per-task caps with the exact error shape.
5. `/agent-tasks` flag shape: `--file <rel>=<local>` and `--expect-back <rel>`. Interactive UX integration.
6. Test plan with at least 6 new pytest cases.
7. Backward-compat statement enumerating what must keep passing.
