# Phase 2.4 Design v1 — File Sharing via `payload.files` / `result.files`

**Author:** designer-v1 · **Date:** 2026-05-09 · **Scope:** `design/phase2.4/scope.md`.
**Constraints:** stdlib only, no hub changes, opt-in via shared helper.

## §1 Summary

A new `clients/_files.py` provides `validate_files`, `materialize_files`,
`readback_files`. Each adapter (`codex`, `claude_code`, `opencode`)
gains a 3-line wrapper: validate+materialize before `subprocess.run`,
readback after success. The skill (`skills/agent-tasks/scripts/tasks.py`)
gains repeatable `--file <rel>=<local>` and `--expect-back <rel>` that
merge into `payload.files` / `payload.expect_files_back`. Path traversal
is blocked by 3 layers (string-level, symlink reject, post-resolve
containment). Per-file 1 MB and per-task 5 MB caps enforced both on
input and output. Tasks without `payload.files` continue to work
unchanged; existing tests must keep passing.

## §2 `clients/_files.py` API

### §2.1 Surface

```python
"""Phase 2.4 — payload.files materialization + result.files readback.
Stdlib only. No side effects at import time."""
from __future__ import annotations
import pathlib

MAX_FILE_BYTES: int  = 1 * 1024 * 1024   # 1 MB per file
MAX_TOTAL_BYTES: int = 5 * 1024 * 1024   # 5 MB per task

class InvalidPayloadFiles(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

def validate_files(files: list[dict]) -> None: ...
def materialize_files(workdir: pathlib.Path, files: list[dict]) -> None: ...
def readback_files(workdir: pathlib.Path,
                   expect_back: list[str]) -> tuple[list[dict], list[str]]: ...
```

### §2.2 Closed set of `InvalidPayloadFiles.reason`

| reason             | when                                              |
|--------------------|---------------------------------------------------|
| `not_a_list`       | `files` is not a list                             |
| `entry_not_dict`   | one entry is not an object                        |
| `missing_path`     | entry `path` missing/empty/non-string             |
| `missing_content`  | entry `content` missing/non-string                |
| `path_absolute`    | leading `/`, `\`, or Windows drive letter         |
| `path_traversal`   | any `..` component (post `PurePosixPath`)         |
| `path_escapes`     | resolved target/parent not under workdir; or symlink |
| `file_too_large`   | UTF-8 bytes > `MAX_FILE_BYTES`                    |
| `total_too_large`  | sum of UTF-8 bytes > `MAX_TOTAL_BYTES`            |

Adapter contract: catch `InvalidPayloadFiles` → re-raise as
`TaskFailed({"error":"invalid_payload_files","reason":e.reason,
"detail":e.detail,"cli":CLI_NAME,"role":role})`.

### §2.3 `validate_files` pseudocode

```python
def validate_files(files):
    if not isinstance(files, list):
        raise InvalidPayloadFiles("not_a_list", f"got {type(files).__name__}")
    total = 0
    for i, e in enumerate(files):
        if not isinstance(e, dict):
            raise InvalidPayloadFiles("entry_not_dict", f"index {i}")
        path = e.get("path"); content = e.get("content")
        if not isinstance(path, str) or not path:
            raise InvalidPayloadFiles("missing_path", f"index {i}")
        if not isinstance(content, str):
            raise InvalidPayloadFiles("missing_content", f"index {i} path={path!r}")
        # --- Path-traversal defence (string-level, no IO yet) ---
        if path.startswith("/") or path.startswith("\\"):
            raise InvalidPayloadFiles("path_absolute", path)
        if len(path) >= 2 and path[1] == ":":          # Windows drive
            raise InvalidPayloadFiles("path_absolute", path)
        pp = pathlib.PurePosixPath(path)
        if pp.is_absolute() or any(p == ".." for p in pp.parts):
            raise InvalidPayloadFiles("path_traversal", path)
        # --- Size enforcement (UTF-8 bytes, not chars) ---
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise InvalidPayloadFiles("file_too_large",
                f"path={path!r} size={size} cap={MAX_FILE_BYTES}")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise InvalidPayloadFiles("total_too_large",
                f"sum={total} cap={MAX_TOTAL_BYTES}")
```

`validate_files` does no IO — symlink-escape detection happens in
`materialize_files` where a real workdir exists.

### §2.4 `materialize_files` pseudocode

```python
def materialize_files(workdir, files):
    if not files: return
    validate_files(files)
    workdir_resolved = workdir.resolve()
    for entry in files:
        rel = entry["path"]; content = entry["content"]
        target = workdir / rel
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        # Defence-in-depth: resolved parent must stay under workdir.
        # parent exists post-mkdir, so resolve() is concrete.
        if not _is_relative_to(parent.resolve(), workdir_resolved):
            raise InvalidPayloadFiles("path_escapes",
                f"path={rel!r} resolved_parent={parent.resolve()}")
        # Refuse to write through an existing symlink.
        if target.is_symlink():
            raise InvalidPayloadFiles("path_escapes",
                f"path={rel!r} target is a symlink")
        target.write_text(content, encoding="utf-8")

def _is_relative_to(child, parent) -> bool:
    try: child.relative_to(parent); return True
    except ValueError: return False
```

### §2.5 `readback_files` pseudocode

```python
def readback_files(workdir, expect_back):
    """Returns (files, missing). Never raises on missing/oversize/escape;
    those paths go into `missing`. Only writes (input) raise."""
    if not expect_back: return [], []
    workdir_resolved = workdir.resolve()
    out, missing, total = [], [], 0
    for rel in expect_back:
        if not isinstance(rel, str) or not rel:
            missing.append(str(rel)); continue
        if rel.startswith("/") or rel.startswith("\\"):
            missing.append(rel); continue
        pp = pathlib.PurePosixPath(rel)
        if pp.is_absolute() or any(p == ".." for p in pp.parts):
            missing.append(rel); continue
        target = workdir / rel
        try: resolved = target.resolve()
        except OSError: missing.append(rel); continue
        if not _is_relative_to(resolved, workdir_resolved):
            missing.append(rel); continue
        if not target.is_file() or target.is_symlink():
            missing.append(rel); continue
        try: data = target.read_bytes()
        except OSError: missing.append(rel); continue
        if len(data) > MAX_FILE_BYTES:
            missing.append(rel); continue
        if total + len(data) > MAX_TOTAL_BYTES:
            missing.append(rel); continue   # try to fit later (smaller) files
        try: text = data.decode("utf-8")
        except UnicodeDecodeError:
            missing.append(rel); continue   # binary out of scope (Phase 2.5+)
        out.append({"path": rel, "content": text}); total += len(data)
    return out, missing
```

### §2.6 Path-traversal defence — three layers (all required)

1. **String reject** (validate + readback): `/`, `\`, drive letter,
   any `..` component after `PurePosixPath`.
2. **Symlink reject** (materialize: target; readback: target).
3. **Resolved-parent containment** (materialize): `parent.resolve()`
   must `is_relative_to(workdir.resolve())`.

Defeats `../escape`, `/etc/passwd`, `C:\...`, `a/../../b`, and
pre-planted symlinks (`a -> /tmp` so `a/x` would land outside).

### §2.7 Size enforcement

UTF-8 byte counts (what hits disk). Per-file cap and running per-task
sum checked at validate time (raises) and readback time (skips into
`missing`). Constants centralized so adapters + tests read the same
numbers.

## §3 Adapter integration (codex / claude_code / opencode)

Identical pattern in all three. Codex shown in full; the other two
follow §3.2 / §3.3 callouts.

```python
# new imports at top of clients/codex/llm_agent.py
from clients._files import (
    materialize_files, readback_files, InvalidPayloadFiles,
)

def _run_cli_for_task(task, workdir, role_prompt, budget, role, hub):
    payload = task.get("payload") or {}
    # ... existing prompt/timeout/deadline assembly unchanged ...

    # NEW: materialize BEFORE subprocess.run
    files_in = payload.get("files") if isinstance(payload, dict) else None
    if files_in:
        try:
            materialize_files(workdir, files_in)
        except InvalidPayloadFiles as e:
            raise TaskFailed({
                "error": "invalid_payload_files",
                "reason": e.reason, "detail": e.detail,
                "cli": CLI_NAME, "role": role,
            })

    cp = subprocess.run(cmd, ...)              # existing
    if cp.returncode != 0:
        raise TaskFailed({...})                # existing

    parsed = _parse_codex_output(cp.stdout or "")
    result = {"text": parsed, "raw": cp.stdout or "",
              "cli": CLI_NAME, "role": role, "elapsed_s": elapsed}

    # NEW: readback AFTER success, before budget block
    expect_back = (payload.get("expect_files_back")
                   if isinstance(payload, dict) else None)
    if expect_back:
        files_out, missing = readback_files(workdir, expect_back)
        if files_out: result["files"] = files_out
        if missing:   result["files_missing"] = missing

    # ... existing budget tracking unchanged ...
    return result
```

§3.2 **claude_code**: insert materialize after
`prompt = payload.get("prompt") or task.get("task") or ""` and before
`cmd = _build_claude_cmd(...)`. Insert readback after
`text, usage_total = _parse_claude_response(parsed)` and the `result`
dict assembly, before the budget check.

§3.3 **opencode**: insert materialize before `_build_cmd`. Insert
readback after the events list parses and `result` is populated, before
the budget check. Note: existing `_SESSION_MARKER` (`.opencode-session-started`)
is a reserved filename — `validate_files` will accept any path that
doesn't traverse, so users *could* clobber it; we accept that risk in
v1 (call it out in §8).

§3.4 **Error propagation**

| stage                  | result                                                   |
|------------------------|----------------------------------------------------------|
| validate / materialize | `TaskFailed("invalid_payload_files", reason, detail)`    |
| subprocess timeout/exit| unchanged                                                |
| readback I/O / oversize| NOT a failure; path appears in `result.files_missing`    |

Per scope §F6: readback degrades silently; only writes fail the task.

## §4 Skill UX

### §4.1 New flags (`tasks.py::_build_parser`)

```python
p.add_argument("--file", action="append", default=None,
    metavar="REL=LOCAL", help="Attach local file as payload.files entry. Repeatable.")
p.add_argument("--expect-back", action="append", default=None,
    metavar="REL", help="Request relpath read back into result.files. Repeatable.")
```

### §4.2 Parsing helper

```python
def _parse_file_flags(file_args):
    if not file_args: return []
    out = []
    for spec in file_args:
        if "=" not in spec:
            raise SystemExit(f"--file expects <rel>=<local>, got {spec!r}")
        rel, _, local = spec.partition("=")
        rel = rel.strip(); local = local.strip()
        if not rel or not local:
            raise SystemExit(f"--file empty side: {spec!r}")
        try:
            content = pathlib.Path(local).read_text(encoding="utf-8")
        except OSError as e:
            raise SystemExit(f"--file {spec!r}: cannot read {local}: {e}")
        except UnicodeDecodeError as e:
            raise SystemExit(f"--file {spec!r}: {local} not UTF-8 ({e}); "
                             "binary is Phase 2.5+.")
        out.append({"path": rel, "content": content})
    return out
```

### §4.3 Merge with `--payload` (decision: MERGE, not error)

After payload is assembled (raw `--payload`, prompted, or empty), append
flags:

```python
extra = _parse_file_flags(args.file)
if extra:
    existing = payload.get("files")
    if isinstance(existing, list):    payload["files"] = existing + extra
    elif existing is None:            payload["files"] = extra
    else: raise SystemExit("payload.files set to non-list; --file cannot merge.")

if args.expect_back:
    existing_eb = payload.get("expect_files_back")
    if isinstance(existing_eb, list): payload["expect_files_back"] = existing_eb + list(args.expect_back)
    elif existing_eb is None:         payload["expect_files_back"] = list(args.expect_back)
    else: raise SystemExit("payload.expect_files_back set to non-list; --expect-back cannot merge.")
```

Rationale: lets users pass a JSON template and attach files
ergonomically. Only error path is type-conflict (caller already set
`files` to a non-list).

### §4.4 Interactive UX

`prompt_payload_from_schema` already raw-JSON-falls-back for any array
field, so payloads with `files: array` route through raw-JSON entry.
**No special interactive flow for files in v1** — `--file` is the
ergonomic path.

## §5 Module-by-module change list

| File | New / Modified | Concrete change |
|------|----------------|-----------------|
| `clients/_files.py` | NEW | `MAX_FILE_BYTES`, `MAX_TOTAL_BYTES`, `InvalidPayloadFiles`, `validate_files`, `materialize_files`, `readback_files`, `_is_relative_to` |
| `clients/codex/llm_agent.py` | MOD | Import from `_files`; add materialize call before `subprocess.run`; add readback before budget block |
| `clients/claude_code/llm_agent.py` | MOD | Same pattern (around `_build_claude_cmd` / `_parse_claude_response`) |
| `clients/opencode/llm_agent.py` | MOD | Same pattern (around `_build_cmd` / event parse) |
| `skills/agent-tasks/scripts/tasks.py` | MOD | Add `--file` + `--expect-back`; add `_parse_file_flags`; merge into `payload` in `main()` before `submit_task` |
| `tests/test_unit.py` | MOD | At least 6 new pytest cases (§6) |

No changes to `agent_sdk/`, `hub/`, `clients/role_presets.py`,
`clients/_state.py`, `clients/base.py`.

## §6 Test plan (pytest, all in `tests/test_unit.py`)

Eight new cases. IDs `FR-FILES-1..8`. Function name convention
`test_p24_unit_files_*` / `test_p24_unit_tasks_*` /
`test_p24_unit_codex_*`. Helpers (`tmp_path`, `monkeypatch`,
`pytest.raises`, `pytest.mark.parametrize`) are already in use.

**FR-FILES-1 — validate happy path**: `validate_files([{"path":"a.py","content":"x\n"}, {"path":"sub/b.py","content":"y\n"}])` must not raise.

**FR-FILES-2 — rejects path traversal / absolute** (parametrize over `["../escape","a/../../escape","/etc/passwd","..","sub/../../etc"]`): each raises `InvalidPayloadFiles` with `reason in {"path_traversal","path_absolute"}`.

**FR-FILES-3 — rejects oversized single file**: `content = "a" * (MAX_FILE_BYTES+1)` → raises with `reason == "file_too_large"`.

**FR-FILES-4 — rejects oversized total**: 6 entries × 900 KB each (each within per-file cap, sum > 5 MB) → raises with `reason == "total_too_large"`.

**FR-FILES-5 — materialize nested + blocks symlink escape**: (a) `materialize_files(tmp_path, [{"path":"a/b/c.py","content":"hi\n"}])` then assert `(tmp_path/"a"/"b"/"c.py").read_text() == "hi\n"`. (b) Pre-plant `(tmp_path/"trap").symlink_to(<dir outside>)`; calling materialize with `path="trap/x"` raises with `reason == "path_escapes"`.

**FR-FILES-6 — readback returns content + records missing/oversized**: (a) Write `kept.py`; `readback_files(tmp_path, ["kept.py","gone.py"])` returns `([{"path":"kept.py","content":...}], ["gone.py"])`. (b) Write `ok.txt` (small) and `big.txt` (>1 MB); readback returns only `ok.txt` in `files`, `big.txt` in `missing`.

**FR-FILES-7 — skill `--file` flag embeds correctly**: monkeypatch `tasks.submit_task` to capture the payload; stub `list_agents` / `get_agent` / `poll_task`. Run `tasks.main(["--hub","http://h","--agent","a1","--task-type","chat","--payload",'{"prompt":"hi"}',"--file",f"src.py={local}","--expect-back","src.py","--no-poll"])`. Assert captured `payload["files"] == [{"path":"src.py","content":"hello = 1\n"}]` and `payload["expect_files_back"] == ["src.py"]`.

**FR-FILES-8 — integration: codex adapter materializes + reads back**: monkeypatch `codex_mod.subprocess.run` with a fake that (1) returns `returncode=0, stdout="[assistant]: done\n"`, (2) as a side-effect writes `(tmp_path/"x.py")` with edited content. Build `task = {"task_id":"t1","timeout":30,"payload":{"task":"edit","files":[{"path":"x.py","content":"def f(): return 1\n"}],"expect_files_back":["x.py","missing.py"]}}`. Call `codex_mod._run_cli_for_task(task, tmp_path, role_prompt=None, budget=None, role="coder", hub=FakeHub())`. Assert `result["text"] == "done"`, `result["files"] == [{"path":"x.py","content":"def f(): return 2\n"}]`, `result["files_missing"] == ["missing.py"]`.

(Equivalent integration tests for `claude_code` / `opencode` follow the same shape; coder may add as FR-FILES-9 / -10. Not required for the 6-case minimum.)

## §7 Backward-compat — explicit guarantees

1. **No `payload.files`** — adapters skip materialize block. Behavior unchanged.
2. **No `expect_files_back`** — adapters skip readback block. `result` shape unchanged (no `files`/`files_missing` keys).
3. **Empty lists** (`payload.files=[]`, `expect_files_back=[]`) — also no-ops; `validate_files([])` is a no-op; `readback_files(_, [])` returns `([], [])`; adapters omit empty result keys.
4. **Existing `_PAYLOAD_SCHEMA_V1`** in claude_code/opencode already has `additionalProperties: True`, so `files` / `expect_files_back` flow through with no schema work.
5. **All existing tests in `tests/test_unit.py` MUST pass unchanged.** New helper is additive; adapter changes are gated on truthy `payload.get("files")` / `expect_files_back`.
6. **Hub** receives `payload` as opaque JSON. Zero hub-side changes; `test_rest.py` / `test_websocket.py` continue to pass.
7. **Skill** without flags is byte-identical to today's behavior.

## §8 Open questions for review (codex)

1. **Symlink target writing**: §2.4 refuses to write through an existing symlink. Should we instead overwrite (replace symlink with a real file)? Current: refuse with `path_escapes`.
2. **`--file` UTF-8-only**: §4.2 hard-fails on non-UTF-8 local files. Phase 2.5 brings `content_b64`. Hard-fail OK for v1 UX, or silently base64-encode? Current: hard-fail with clear message.
3. **Total cap on readback** mid-loop: we record offender in `missing` and `continue` so smaller files later may still fit. Alternative: stop and put all remaining in `missing`. Current: continue.
4. **Error key stability**: `error: "invalid_payload_files"` regardless of adapter? Current: yes; adapters set `cli`/`role` alongside.
5. **Resolve target vs parent**: §2.4 resolves `parent.resolve()` (concrete after mkdir) rather than `target.resolve()` (may not exist). Acceptable given step 1+2 already cover obvious cases?
6. **Skill merge vs error** when both `--payload` and `--file` set: current MERGE. Any reason to prefer "error if both"?
7. **Opencode session marker collision**: `validate_files` allows any non-traversing path, so a payload with `path: ".opencode-session-started"` could clobber the marker. Worth a reserved-name check, or accept the risk?
