# Phase 2.4 Design v2 — File Sharing via `payload.files` / `result.files`

**Author:** designer-v2 · **Date:** 2026-05-09 · **Scope:** `design/phase2.4/scope.md`.
**Constraints:** stdlib only, no hub changes, opt-in via shared helper.
**Supersedes:** `design/phase2.4/design_v1.md` (codex REVISE 2026-05-09 19:18).

## §0 Diff vs v1

All five fixes use codex's verbatim replacement blocks from
`Agent-comm/codex_review_phase2.4-design-v1_20260509-1918.md`.

| # | Codex finding | Applied in |
|---|---------------|------------|
| 1 | Symlink defense: validate parent chain BEFORE any mkdir; reject existing symlink / out-of-workdir parent; reject final symlink target. | §2.4 rewritten; §6 FR-FILES-5b adds `trap -> outside`, `trap/sub/x`, asserts `outside/sub` not created. |
| 2 | Windows-backslash traversal: reject any `\`, reject `<letter>:` drive prefix, `PurePosixPath` + reject absolute / `..` / empty parts. BOTH input and readback. | §2.3 new shared `_validate_relpath`; §2.5 readback reuses; §6 FR-FILES-9 adds `r"a\..\escape"`, `r"dir\file.py"`. |
| 3 | `result.files_truncated`: `readback_files` returns 3-tuple `(files, missing, truncated)`. `truncated` entries: `{"path","reason":"file_too_large"\|"total_too_large","size","cap"}`. | §2.5, §2.7, §3 adapter assigns all three; §6 FR-FILES-6b/6c. |
| 4 | Error code shape: adapter maps `file_too_large`/`total_too_large` → `error="payload_too_large"`; everything else → `"invalid_payload_files"`. `reason`/`detail`/`cli`/`role` stable. | §2.2 table; §3.4 verbatim block. |
| 5 | Truthy gating: adapter uses `"files" in payload and payload["files"] is not None`; `materialize_files` calls `validate_files` BEFORE the empty-list early-return; non-list `expect_files_back` raises `invalid_expect_files_back` (no char-iteration). | §2.4, §2.5, §3, §2.2 grows `invalid_expect_files_back`. |

Codex `[accept]` kept: UTF-8 byte size (§2.7); MERGE flag (§4.3). Codex Q1–Q7 baked in: refuse symlinks (Q1, §2.4); hard-fail non-UTF-8 `--file` (Q2, §4.2); oversized readback → `files_truncated` (Q3, §2.5); stable error keys (Q4, §3.4); parent chain before mkdir (Q5, §2.4); MERGE (Q6, §4.3); `.opencode-session-started` reserved in `validate_files` (Q7, §2.3).

## §1 Summary

`clients/_files.py` exposes `validate_files`, `materialize_files`,
`readback_files`. Each adapter validates+materializes before
`subprocess.run`, reads back after success. `tasks.py` gains
`--file <rel>=<local>` and `--expect-back <rel>` (repeatable, MERGE
into payload). Traversal blocked in 3 layers: `_validate_relpath`
(reject `\`, drive letters, absolute, `..`, empty parts); parent-chain
walk (reject symlink/escape parent BEFORE any mkdir); final-target
symlink reject. Caps: 1 MiB/file, 5 MiB/task — raise on input, record
`files_truncated` on output. Tasks without `payload.files` unchanged.

## §2 `clients/_files.py` API

### §2.1 Surface

```python
"""Phase 2.4 — payload.files materialization + result.files readback.
Stdlib only. No side effects at import time."""
from __future__ import annotations
import pathlib

MAX_FILE_BYTES: int  = 1 * 1024 * 1024   # 1 MiB per file (UTF-8 bytes)
MAX_TOTAL_BYTES: int = 5 * 1024 * 1024   # 5 MiB per task (UTF-8 bytes)

# Adapter-private filenames the payload must never write.
RESERVED_NAMES: frozenset[str] = frozenset({".opencode-session-started"})

class InvalidPayloadFiles(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

def validate_files(files: list[dict]) -> None: ...
def materialize_files(workdir: pathlib.Path, files: list[dict]) -> None: ...
def readback_files(
    workdir: pathlib.Path,
    expect_back: list[str],
) -> tuple[list[dict], list[str], list[dict]]: ...
```

### §2.2 Closed set of `InvalidPayloadFiles.reason`

| reason                      | when                                                          | adapter `error` key      |
|-----------------------------|---------------------------------------------------------------|--------------------------|
| `not_a_list`                | `files` is not a list                                         | `invalid_payload_files`  |
| `entry_not_dict`            | one entry is not an object                                    | `invalid_payload_files`  |
| `missing_path`              | entry `path` missing/empty/non-string                         | `invalid_payload_files`  |
| `missing_content`           | entry `content` missing/non-string                            | `invalid_payload_files`  |
| `path_absolute`             | leading `/`, or `<letter>:` Windows drive prefix              | `invalid_payload_files`  |
| `path_traversal`            | any `\`, any `..`, any empty part, or POSIX-absolute          | `invalid_payload_files`  |
| `path_escapes`              | parent-chain symlink, parent escape, or target is symlink     | `invalid_payload_files`  |
| `path_reserved`             | path in `RESERVED_NAMES` (e.g. `.opencode-session-started`)   | `invalid_payload_files`  |
| `file_too_large`            | UTF-8 bytes > `MAX_FILE_BYTES`                                | `payload_too_large`      |
| `total_too_large`           | sum of UTF-8 bytes > `MAX_TOTAL_BYTES`                        | `payload_too_large`      |
| `invalid_expect_files_back` | `payload.expect_files_back` present but not a list            | `invalid_payload_files`  |

Adapter mapping (§3.4): `reason in {"file_too_large","total_too_large"}` → `error="payload_too_large"`; else `"invalid_payload_files"`. All errors carry `reason`, `detail`, `cli`, `role`.

### §2.3 `_validate_relpath` (shared) and `validate_files`

Single helper used by both `validate_files` and `readback_files`.

```python
def _validate_relpath(path: str) -> pathlib.PurePosixPath:
    """Reject Windows separators, drive letters, absolute, .., empty parts."""
    if not isinstance(path, str) or not path:
        raise InvalidPayloadFiles("missing_path", f"got {type(path).__name__}")
    # Finding #2 verbatim:
    if "\\" in path:
        raise InvalidPayloadFiles("path_traversal", path)
    if len(path) >= 2 and path[1] == ":":
        raise InvalidPayloadFiles("path_absolute", path)
    pp = pathlib.PurePosixPath(path)
    if pp.is_absolute() or any(part in ("", "..") for part in pp.parts):
        raise InvalidPayloadFiles("path_traversal", path)
    return pp
```

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
            raise InvalidPayloadFiles("missing_content",
                                      f"index {i} path={path!r}")
        _validate_relpath(path)            # raises path_absolute/_traversal
        if path in RESERVED_NAMES:         # codex Q7
            raise InvalidPayloadFiles("path_reserved", f"path={path!r}")
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise InvalidPayloadFiles("file_too_large",
                f"path={path!r} size={size} cap={MAX_FILE_BYTES}")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise InvalidPayloadFiles("total_too_large",
                f"sum={total} cap={MAX_TOTAL_BYTES}")
```

`validate_files` does no IO — symlink-escape detection lives in
`materialize_files` where a real workdir exists.

### §2.4 `materialize_files` — parent-chain walk before any mkdir

Finding #1 verbatim parent-chain validator + finding #5 validate-first.

```python
def materialize_files(workdir, files):
    validate_files(files)                  # finding #5: BEFORE empty-return
    if not files:
        return
    workdir_resolved = workdir.resolve()
    for entry in files:
        rel = entry["path"]; content = entry["content"]
        # Finding #1 verbatim parent-chain validator:
        rel_path = pathlib.PurePosixPath(rel)
        cur = workdir_resolved
        for part in rel_path.parts[:-1]:
            cur = cur / part
            if cur.exists():
                if cur.is_symlink():
                    raise InvalidPayloadFiles("path_escapes",
                        f"path={rel!r} parent symlink={cur}")
                if not _is_relative_to(cur.resolve(), workdir_resolved):
                    raise InvalidPayloadFiles("path_escapes",
                        f"path={rel!r} parent escapes={cur}")
            else:
                cur.mkdir()
        target = workdir_resolved.joinpath(*rel_path.parts)
        if target.exists() and target.is_symlink():
            raise InvalidPayloadFiles("path_escapes",
                f"path={rel!r} target is a symlink")
        target.write_text(content, encoding="utf-8")

def _is_relative_to(child, parent) -> bool:
    try: child.relative_to(parent); return True
    except ValueError: return False
```

Property: `trap -> outside` pre-planted + payload `trap/sub/x` → loop
sees `cur = workdir/trap` is a symlink → raises `path_escapes` BEFORE
creating `outside/sub`. FR-FILES-5b asserts `outside/sub` does not
exist post-raise. Symlinks are refused, never replaced (codex Q1).

### §2.5 `readback_files` — 3-tuple with `truncated`

Codex finding #3 signature change. Non-list `expect_back` raises
(finding #5); per-entry non-string/invalid go to `missing`.

```python
def readback_files(
    workdir, expect_back,
) -> tuple[list[dict], list[str], list[dict]]:
    """Returns (files, missing, truncated). Per-cap skips → truncated;
    misses/binary/escape → missing. Only expect_back shape can raise."""
    if expect_back is None:
        return [], [], []
    if not isinstance(expect_back, list):
        # Finding #5: don't iterate a string char-by-char.
        raise InvalidPayloadFiles(
            "invalid_expect_files_back",
            f"got {type(expect_back).__name__}",
        )
    if not expect_back:
        return [], [], []
    workdir_resolved = workdir.resolve()
    out, missing, truncated, total = [], [], [], 0
    for rel in expect_back:
        if not isinstance(rel, str) or not rel:
            missing.append(str(rel)); continue
        try: _validate_relpath(rel)        # finding #2: same rules
        except InvalidPayloadFiles: missing.append(rel); continue
        target = workdir / rel
        try: resolved = target.resolve()
        except OSError: missing.append(rel); continue
        if not _is_relative_to(resolved, workdir_resolved):
            missing.append(rel); continue
        if target.is_symlink() or not target.is_file():
            missing.append(rel); continue
        try: data = target.read_bytes()
        except OSError: missing.append(rel); continue
        # Finding #3 verbatim — record in `truncated`, not `missing`.
        if len(data) > MAX_FILE_BYTES:
            truncated.append({"path": rel, "reason": "file_too_large",
                              "size": len(data), "cap": MAX_FILE_BYTES})
            continue
        if total + len(data) > MAX_TOTAL_BYTES:
            truncated.append({"path": rel, "reason": "total_too_large",
                              "size": len(data), "cap": MAX_TOTAL_BYTES})
            continue                       # Q3: continue, smaller-later may fit
        try: text = data.decode("utf-8")
        except UnicodeDecodeError: missing.append(rel); continue  # binary: 2.5+
        out.append({"path": rel, "content": text}); total += len(data)
    return out, missing, truncated
```

### §2.6 Path-traversal defence — three layers

1. **String** (input + readback, `_validate_relpath`): reject `\`,
   `<letter>:`, POSIX-absolute, `..`, empty parts; input also rejects
   `RESERVED_NAMES`.
2. **Parent-chain** (materialize): walk existing parents from
   workdir down, reject any symlink or escape, only `mkdir` what
   doesn't exist — BEFORE any `mkdir` runs.
3. **Target** (materialize + readback): reject symlink target; readback
   also `target.resolve()` + re-check containment.

Defeats `../escape`, `/etc/passwd`, `C:\...`, `a\..\escape`,
`a/../../b`, `dir\file.py`, and pre-planted parent symlinks.

### §2.7 Size enforcement

UTF-8 byte counts (codex `[accept]`): per-file 1 MiB, per-task 5 MiB.
Input raises (`file_too_large`/`total_too_large` →
`error="payload_too_large"`). Readback records into
`result.files_truncated` (never `files_missing`; codex Q3).

## §3 Adapter integration (codex / claude_code / opencode)

Identical pattern in all three. Codex shown in full; the others
follow §3.2 / §3.3 callouts.

```python
# new imports at top of clients/codex/llm_agent.py
from clients._files import (
    materialize_files, readback_files, InvalidPayloadFiles,
)

def _run_cli_for_task(task, workdir, role_prompt, budget, role, hub):
    payload = task.get("payload") or {}
    # ... existing prompt/timeout/deadline assembly unchanged ...

    # NEW (finding #5): explicit key-present + is-not-None so malformed
    # empties hit validate_files instead of being silently skipped.
    if isinstance(payload, dict) and "files" in payload \
            and payload["files"] is not None:
        try:
            materialize_files(workdir, payload["files"])
        except InvalidPayloadFiles as e:
            # Finding #4 verbatim mapping:
            error = ("payload_too_large"
                     if e.reason in {"file_too_large", "total_too_large"}
                     else "invalid_payload_files")
            raise TaskFailed({"error": error, "reason": e.reason,
                              "detail": e.detail, "cli": CLI_NAME, "role": role})

    cp = subprocess.run(cmd, ...)              # existing
    if cp.returncode != 0:
        raise TaskFailed({...})                # existing

    parsed = _parse_codex_output(cp.stdout or "")
    result = {"text": parsed, "raw": cp.stdout or "",
              "cli": CLI_NAME, "role": role, "elapsed_s": elapsed}

    # NEW: readback AFTER success, before budget block.
    if isinstance(payload, dict) and "expect_files_back" in payload \
            and payload["expect_files_back"] is not None:
        try:
            files_out, missing, truncated = readback_files(
                workdir, payload["expect_files_back"])
        except InvalidPayloadFiles as e:  # invalid_expect_files_back only
            raise TaskFailed({"error": "invalid_payload_files",
                              "reason": e.reason, "detail": e.detail,
                              "cli": CLI_NAME, "role": role})
        # Finding #3 verbatim — assign all three when non-empty.
        if files_out: result["files"] = files_out
        if missing:   result["files_missing"] = missing
        if truncated: result["files_truncated"] = truncated

    # ... existing budget tracking unchanged ...
    return result
```

§3.2 **claude_code**: materialize after
`prompt = payload.get("prompt") or task.get("task") or ""` and before
`cmd = _build_claude_cmd(...)`. Readback after
`text, usage_total = _parse_claude_response(parsed)` and `result`
assembly, before the budget check. Same finding #4 + #5 guards.

§3.3 **opencode**: materialize before `_build_cmd`; readback after
events parse and `result` populated, before budget. The reserved-name
check in `validate_files` (§2.3 `RESERVED_NAMES`) already rejects
`.opencode-session-started` — no opencode-specific adapter code (Q7).

§3.4 **Error propagation**

| stage                         | result                                                                |
|-------------------------------|-----------------------------------------------------------------------|
| validate / materialize: size  | `TaskFailed(error="payload_too_large", reason, detail, cli, role)`    |
| validate / materialize: shape | `TaskFailed(error="invalid_payload_files", reason, detail, cli, role)`|
| readback: invalid expect_back | `TaskFailed(error="invalid_payload_files", reason="invalid_expect_files_back", ...)` |
| subprocess timeout/exit       | unchanged                                                             |
| readback: per-file misses     | NOT a failure → `result.files_missing`                                |
| readback: per-cap skips       | NOT a failure → `result.files_truncated`                              |

## §4 Skill UX

### §4.1 New flags (`tasks.py::_build_parser`)

```python
p.add_argument("--file", action="append", default=None,
    metavar="REL=LOCAL", help="Attach local file as payload.files entry. Repeatable.")
p.add_argument("--expect-back", action="append", default=None,
    metavar="REL", help="Request relpath read back into result.files. Repeatable.")
```

### §4.2 Parsing helper

Hard-fails non-UTF-8 (codex Q2 — silent base64 is Phase 2.5+).

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

### §4.3 Merge with `--payload` (decision: MERGE — codex `[accept]` + Q6)

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

Duplicate-path semantics (codex review note): later `payload.files`
entries overwrite earlier ones at materialize time (last-writer-wins
on the same `path`). Documented; tests verify.

### §4.4 Interactive UX

`prompt_payload_from_schema` already raw-JSON-falls-back for any array
field, so payloads with `files: array` route through raw-JSON entry.
**No special interactive flow for files in v1.**

## §5 Module-by-module change list

| File | New / Modified | Concrete change |
|------|----------------|-----------------|
| `clients/_files.py` | NEW | `MAX_FILE_BYTES`, `MAX_TOTAL_BYTES`, `RESERVED_NAMES`, `InvalidPayloadFiles`, `_validate_relpath`, `validate_files`, `materialize_files`, `readback_files`, `_is_relative_to` |
| `clients/codex/llm_agent.py` | MOD | Import; `"files" in payload and not None` materialize; readback w/ 3-tuple |
| `clients/claude_code/llm_agent.py` | MOD | Same pattern around `_build_claude_cmd` / `_parse_claude_response` |
| `clients/opencode/llm_agent.py` | MOD | Same pattern around `_build_cmd` / event parse |
| `skills/agent-tasks/scripts/tasks.py` | MOD | Add `--file` + `--expect-back`; add `_parse_file_flags`; merge before `submit_task` |
| `tests/test_unit.py` | MOD | 10 new pytest cases (§6) |

No changes to `agent_sdk/`, `hub/`, `clients/role_presets.py`,
`clients/_state.py`, `clients/base.py`.

## §6 Test plan (pytest, `tests/test_unit.py`)

Ten cases `FR-FILES-1..10`, naming `test_p24_unit_{files,tasks,codex}_*`.

**FR-FILES-1 — validate happy path**: `validate_files([{"path":"a.py","content":"x\n"},{"path":"sub/b.py","content":"y\n"}])` does not raise.

**FR-FILES-2 — traversal/absolute** (parametrize `["../escape","a/../../escape","/etc/passwd","..","sub/../../etc"]`): each raises with `reason in {"path_traversal","path_absolute"}`.

**FR-FILES-3 — single-file size boundary**: `"a"*MAX_FILE_BYTES` accepted; `"a"*(MAX_FILE_BYTES+1)` raises `file_too_large`.

**FR-FILES-4 — total size boundary**: 5×900 KiB accepted; 6×900 KiB raises `total_too_large`. Adapter wrapper asserts `TaskFailed.error == "payload_too_large"` (finding #4).

**FR-FILES-5 — materialize + symlink defense (no side effects)**:
(a) `[{"path":"a/b/c.py","content":"hi\n"}]` → `(tmp_path/"a"/"b"/"c.py").read_text() == "hi\n"`.
(b) Pre-plant `outside = tmp_path.parent/"outside"; outside.mkdir(); (tmp_path/"trap").symlink_to(outside)`. `materialize_files(tmp_path, [{"path":"trap/sub/x","content":"z"}])` raises `path_escapes`; assert `(outside/"sub").exists() is False` (finding #1).
(c) `(tmp_path/"link").symlink_to(<outside file>)`; `[{"path":"link","content":"y"}]` raises `path_escapes` (target branch).

**FR-FILES-6 — readback files/missing/truncated**:
(a) Write `kept.py`; `readback_files(tmp_path,["kept.py","gone.py"])` → `([{"path":"kept.py",...}], ["gone.py"], [])`.
(b) Write `ok.txt` (small) + `big.txt` (>cap); → `files=[ok]`, `missing=[]`, `truncated=[{"path":"big.txt","reason":"file_too_large","size":...,"cap":MAX_FILE_BYTES}]` (finding #3).
(c) Write 3×2 MiB files (sum > 5 MiB); first 2 in `files`, 3rd in `truncated` with `reason="total_too_large"` (Q3 — continue).

**FR-FILES-7 — skill `--file` flag embeds**: monkeypatch `tasks.submit_task` to capture payload; stub `list_agents`/`get_agent`/`poll_task`. Run `tasks.main(["--hub","http://h","--agent","a1","--task-type","chat","--payload",'{"prompt":"hi"}',"--file",f"src.py={local}","--expect-back","src.py","--no-poll"])`. Assert `payload["files"] == [{"path":"src.py","content":"hello = 1\n"}]` and `payload["expect_files_back"] == ["src.py"]`. With `--payload '{"files":[...]}'` + `--file` MERGE concatenates.

**FR-FILES-8 — codex adapter integration**: monkeypatch `codex_mod.subprocess.run` to (1) return `returncode=0, stdout="[assistant]: done\n"`, (2) side-effect write `(tmp_path/"x.py")` edited content. Task `{"task_id":"t1","timeout":30,"payload":{"task":"edit","files":[{"path":"x.py","content":"def f(): return 1\n"}],"expect_files_back":["x.py","missing.py"]}}`. Call `_run_cli_for_task(task, tmp_path, role_prompt=None, budget=None, role="coder", hub=FakeHub())`. Assert `result["text"]=="done"`, `result["files"]==[{"path":"x.py","content":"def f(): return 2\n"}]`, `result["files_missing"]==["missing.py"]`, `"files_truncated" not in result`.

**FR-FILES-9 — Windows backslash** (parametrize `[r"a\..\escape", r"dir\file.py", r"sub\..\..\etc"]`): `validate_files([{"path":p,"content":"x"}])` raises with `reason == "path_traversal"`. `readback_files(tmp_path,[p])` puts `p` into `missing` (no raise per-entry).

**FR-FILES-10 — malformed-but-present payload** (finding #5):
(a) `validate_files("nope")` raises `not_a_list`.
(b) Adapter `payload={"files":"nope"}` → `TaskFailed.error=="invalid_payload_files"`, `reason="not_a_list"` (truthy gating would silently skip).
(c) `validate_files([])` is a no-op.
(d) `materialize_files(tmp_path, [])` does nothing AND does not raise (validate runs, then early-return).
(e) `readback_files(tmp_path, "kept.py")` raises `InvalidPayloadFiles("invalid_expect_files_back", ...)` — no char-iteration.
(f) Adapter wrapper for (e) → `TaskFailed.error=="invalid_payload_files"`, `reason="invalid_expect_files_back"`.
(g) `validate_files([{"path":".opencode-session-started","content":"x"}])` raises `path_reserved` (Q7).

## §7 Backward-compat — explicit guarantees

1. **No `payload.files` key** — materialize skipped; behavior unchanged.
2. **No `expect_files_back` key** — readback skipped; `result` shape unchanged.
3. **`payload.files = None`** — adapter skips (`is not None` guard, finding #5).
4. **`payload.files = []`** — `validate_files([])` no-op; result keys absent.
5. **`payload.files = "nope"` (malformed present)** — adapter NOW raises `TaskFailed("invalid_payload_files","not_a_list",...)` instead of silently skipping (intentional regression vs v1 truthy gating; finding #5). No existing test exercises this shape.
6. **`_PAYLOAD_SCHEMA_V1`** in claude_code/opencode already has `additionalProperties: True`; new fields flow through unchanged.
7. **All existing `tests/test_unit.py` cases pass unchanged.** Helper is additive; adapter blocks gated on explicit key-presence.
8. **Hub** receives `payload` as opaque JSON. Zero hub changes; `test_rest.py` / `test_websocket.py` pass unchanged.
9. **Skill** without flags is byte-identical to today.
