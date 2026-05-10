"""Phase 2.4 — payload.files materialization + result.files readback.

Shared helper used by all three LLM adapters (codex / claude_code /
opencode) and the agent-tasks skill.

Design ref: design/phase2.4/design_v3.md (and design_v2.md for the
unchanged sections). All path-traversal defenses live here so the
adapters need only call into the public surface.

Stdlib only. No side effects at import time.
"""
from __future__ import annotations

import pathlib

# --- public size caps ----------------------------------------------------

MAX_FILE_BYTES: int = 1 * 1024 * 1024   # 1 MiB per file (UTF-8 bytes)
MAX_TOTAL_BYTES: int = 5 * 1024 * 1024  # 5 MiB per task (UTF-8 bytes)

# Adapter-private filenames the payload must never write (codex Q7).
RESERVED_NAMES: frozenset[str] = frozenset({".opencode-session-started"})


# --- exception -----------------------------------------------------------

class InvalidPayloadFiles(Exception):
    """Raised by `validate_files` / `materialize_files` / `readback_files`
    when the inbound payload is malformed or path-unsafe.

    `reason` is one of the closed set documented in design v2 §2.2:
        not_a_list, entry_not_dict, missing_path, missing_content,
        path_absolute, path_traversal, path_escapes, path_reserved,
        file_too_large, total_too_large, invalid_expect_files_back

    Adapters map `reason in {"file_too_large","total_too_large"}` to
    error="payload_too_large", everything else to "invalid_payload_files".
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# --- helpers -------------------------------------------------------------

def _is_relative_to(child: pathlib.Path, parent: pathlib.Path) -> bool:
    """3.10-compat replacement for `Path.is_relative_to`."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_relpath(path: str) -> pathlib.PurePosixPath:
    """Reject Windows separators, drive letters, absolute paths, ``..``,
    and empty parts. Used by both `validate_files` and `readback_files`
    so input and output share one rule set.

    Codex finding #2 verbatim block.
    """
    if not isinstance(path, str) or not path:
        raise InvalidPayloadFiles(
            "missing_path",
            f"got {type(path).__name__}",
        )
    # Reject any backslash anywhere — Windows separator + classic
    # traversal trick like ``a\..\escape``.
    if "\\" in path:
        raise InvalidPayloadFiles("path_traversal", path)
    # Reject ``<letter>:`` drive prefix (e.g. ``C:\foo`` or ``C:foo``).
    if len(path) >= 2 and path[1] == ":":
        raise InvalidPayloadFiles("path_absolute", path)
    pp = pathlib.PurePosixPath(path)
    if pp.is_absolute():
        raise InvalidPayloadFiles("path_absolute", path)
    if any(part == "" for part in path.split("/")):
        raise InvalidPayloadFiles("path_traversal", path)
    if any(part == ".." for part in pp.parts):
        raise InvalidPayloadFiles("path_traversal", path)
    return pp


# --- validate ------------------------------------------------------------

def validate_files(files) -> None:
    """Validate `payload.files` shape, paths, and size caps.

    Raises `InvalidPayloadFiles`. Empty list is valid (no-op).
    Pure: does no IO.
    """
    if not isinstance(files, list):
        raise InvalidPayloadFiles(
            "not_a_list",
            f"got {type(files).__name__}",
        )
    total = 0
    for i, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise InvalidPayloadFiles(
                "entry_not_dict",
                f"index {i} got {type(entry).__name__}",
            )
        path = entry.get("path")
        content = entry.get("content")
        if not isinstance(path, str) or not path:
            raise InvalidPayloadFiles(
                "missing_path",
                f"index {i}",
            )
        if not isinstance(content, str):
            raise InvalidPayloadFiles(
                "missing_content",
                f"index {i} path={path!r}",
            )
        # `_validate_relpath` raises path_traversal / path_absolute /
        # missing_path. Let those bubble up unchanged.
        _validate_relpath(path)
        if path in RESERVED_NAMES:  # codex Q7
            raise InvalidPayloadFiles(
                "path_reserved",
                f"path={path!r}",
            )
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise InvalidPayloadFiles(
                "file_too_large",
                f"path={path!r} size={size} cap={MAX_FILE_BYTES}",
            )
        total += size
        if total > MAX_TOTAL_BYTES:
            raise InvalidPayloadFiles(
                "total_too_large",
                f"sum={total} cap={MAX_TOTAL_BYTES}",
            )


# --- materialize ---------------------------------------------------------

def materialize_files(workdir: pathlib.Path, files) -> None:
    """Validate `files` then write each entry into `workdir`.

    Validates BEFORE the empty-list early-return (codex finding #5) so
    malformed-but-empty-after-coercion shapes still raise.

    Symlink defense (codex finding #1 verbatim parent-chain validator):
    walk the parents of each rel path. For each existing parent, reject
    if it's a symlink, or if its resolved path escapes `workdir`. Only
    then `mkdir` what doesn't exist. Finally reject if the final target
    is itself an existing symlink. This catches the
    ``trap -> /tmp/outside`` + payload ``trap/sub/x`` parent-symlink
    escape BEFORE any directory under `outside` is created.
    """
    validate_files(files)
    if not files:
        return
    workdir_resolved = workdir.resolve()
    for entry in files:
        rel = entry["path"]
        content = entry["content"]
        rel_path = pathlib.PurePosixPath(rel)
        cur = workdir_resolved
        # Walk every existing parent before any `mkdir`.
        for part in rel_path.parts[:-1]:
            cur = cur / part
            if cur.exists():
                if cur.is_symlink():
                    raise InvalidPayloadFiles(
                        "path_escapes",
                        f"path={rel!r} parent symlink={cur}",
                    )
                if not _is_relative_to(cur.resolve(), workdir_resolved):
                    raise InvalidPayloadFiles(
                        "path_escapes",
                        f"path={rel!r} parent escapes={cur}",
                    )
            else:
                cur.mkdir()
        target = workdir_resolved.joinpath(*rel_path.parts)
        # Refuse to overwrite an existing symlink target — never replace.
        # `is_symlink()` does not follow links, so this catches both live
        # and dangling symlinks (`exists()` would miss the latter).
        if target.is_symlink():
            raise InvalidPayloadFiles(
                "path_escapes",
                f"path={rel!r} target is a symlink",
            )
        target.write_text(content, encoding="utf-8")


# --- readback ------------------------------------------------------------

def readback_files(
    workdir: pathlib.Path,
    expect_back,
) -> tuple[list[dict], list[str], list[dict]]:
    """Return ``(files, missing, truncated)`` for `expect_back`.

    `expect_back` MUST be a list (or None). Non-list raises
    `InvalidPayloadFiles("invalid_expect_files_back", ...)` rather than
    iterating a string char-by-char (codex finding #5).

    Per-entry semantics:
      - rel-path validation failure   -> missing (silent skip)
      - resolves outside workdir       -> missing
      - missing/binary/symlink/IO err  -> missing
      - per-file size > MAX_FILE_BYTES -> truncated (reason="file_too_large")
      - running total > MAX_TOTAL_BYTES-> truncated (reason="total_too_large"),
                                           continue so smaller later files fit
      - otherwise                      -> files
    """
    if expect_back is None:
        return [], [], []
    if not isinstance(expect_back, list):
        raise InvalidPayloadFiles(
            "invalid_expect_files_back",
            f"got {type(expect_back).__name__}",
        )
    if not expect_back:
        return [], [], []

    workdir_resolved = workdir.resolve()
    out: list[dict] = []
    missing: list[str] = []
    truncated: list[dict] = []
    total = 0

    for rel in expect_back:
        if not isinstance(rel, str) or not rel:
            missing.append(str(rel))
            continue
        try:
            _validate_relpath(rel)
        except InvalidPayloadFiles:
            missing.append(rel)
            continue
        target = workdir / rel
        try:
            resolved = target.resolve()
        except OSError:
            missing.append(rel)
            continue
        if not _is_relative_to(resolved, workdir_resolved):
            missing.append(rel)
            continue
        if target.is_symlink():
            missing.append(rel)
            continue
        if not target.is_file():
            missing.append(rel)
            continue
        try:
            data = target.read_bytes()
        except OSError:
            missing.append(rel)
            continue
        size = len(data)
        if size > MAX_FILE_BYTES:
            truncated.append({
                "path": rel,
                "reason": "file_too_large",
                "size": size,
                "cap": MAX_FILE_BYTES,
            })
            continue
        if total + size > MAX_TOTAL_BYTES:
            truncated.append({
                "path": rel,
                "reason": "total_too_large",
                "size": size,
                "cap": MAX_TOTAL_BYTES,
            })
            # Continue: a smaller later file may still fit. (codex Q3)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # Binary content not supported in v1 — silent skip into missing.
            missing.append(rel)
            continue
        out.append({"path": rel, "content": text})
        total += size

    return out, missing, truncated
