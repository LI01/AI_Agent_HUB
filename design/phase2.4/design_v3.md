# Phase 2.4 Design v3 — File Sharing via `payload.files` / `result.files`

**Diff vs v2:** §6 FR-FILES-6c body replaced verbatim with codex's
total-cap readback test (six 900 KiB files + later `tail.txt`) so the
total-cap branch isn't preempted by the per-file cap. All other
sections unchanged from v2.

**Author:** designer-v3 · **Date:** 2026-05-09 · **Scope:** `design/phase2.4/scope.md`.
**Constraints:** stdlib only, no hub changes, opt-in via shared helper.
**Supersedes:** `design/phase2.4/design_v2.md`.

## §0 Diff vs v1

(unchanged from v2)

## §1 Summary

(unchanged from v2)

## §2 `clients/_files.py` API

(unchanged from v2)

### §2.1 Surface

(unchanged from v2)

### §2.2 Closed set of `InvalidPayloadFiles.reason`

(unchanged from v2)

### §2.3 `_validate_relpath` (shared) and `validate_files`

(unchanged from v2)

### §2.4 `materialize_files` — parent-chain walk before any mkdir

(unchanged from v2)

### §2.5 `readback_files` — 3-tuple with `truncated`

(unchanged from v2)

### §2.6 Path-traversal defence — three layers

(unchanged from v2)

### §2.7 Size enforcement

(unchanged from v2)

## §3 Adapter integration (codex / claude_code / opencode)

(unchanged from v2)

## §4 Skill UX

(unchanged from v2)

## §5 Module-by-module change list

(unchanged from v2)

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
(c) Write six 900 KiB UTF-8 text files. Request all six, then request an additional small later file such as `tail.txt`. Expect the first five 900 KiB files in `files`, the sixth 900 KiB file in `truncated` with `reason="total_too_large"`, and `tail.txt` still included in `files` if it fits. This proves total-cap overflow is recorded in `files_truncated` and readback continues so smaller later files can still be returned.

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

(unchanged from v2)
