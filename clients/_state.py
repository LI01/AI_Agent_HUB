"""Phase 2.3 — shared spawned.json IO with cross-process locking.

Skill helpers (spawn appends, close removes, tasks reads) all funnel through
this module so concurrent invocations don't clobber each other and so corrupt
files are quarantined rather than re-overwritten.

Design ref: design/phase2.3/design_v3.md §6.5 + §14-fix.

Stdlib only. POSIX uses `fcntl.flock`; Windows uses `msvcrt.locking`. If
neither is importable (exotic platform) we fall back to no-op locking and
print a one-line warning to stderr — atomic-rename writes still prevent
torn files even without the lock.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time
from typing import Callable

# --- platform-specific lock primitives -----------------------------------

try:  # POSIX
    import fcntl  # type: ignore
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore
    _HAVE_FCNTL = False

try:  # Windows
    import msvcrt  # type: ignore
    _HAVE_MSVCRT = True
except ImportError:
    msvcrt = None  # type: ignore
    _HAVE_MSVCRT = False


# --- public paths --------------------------------------------------------

SPAWNED_PATH: pathlib.Path = pathlib.Path.home() / ".agent-hub" / "spawned.json"


def _lock_path() -> pathlib.Path:
    """Resolve the sidecar lock path from the (possibly monkeypatched)
    SPAWNED_PATH at call time, not import time."""
    return SPAWNED_PATH.with_name(SPAWNED_PATH.name + ".lock")


def _ensure_parent(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


# --- locking -------------------------------------------------------------

def _platform_lock(fh) -> None:
    """Acquire an exclusive lock on `fh`. Best-effort; never raises."""
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        elif _HAVE_MSVCRT:
            # Lock 1 byte at offset 0; LK_LOCK blocks until acquired.
            try:
                fh.seek(0)
            except OSError:
                pass
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            print(
                "agent-hub _state: no fcntl/msvcrt available — "
                "spawned.json IO is not cross-process locked",
                file=sys.stderr,
            )
    except OSError as e:  # pragma: no cover - lock failures are rare
        print(f"agent-hub _state: lock acquire failed: {e}", file=sys.stderr)


def _platform_unlock(fh) -> None:
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        elif _HAVE_MSVCRT:
            try:
                fh.seek(0)
            except OSError:
                pass
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:  # pragma: no cover
        pass


# --- atomic write --------------------------------------------------------

def atomic_write_json(path: pathlib.Path, data) -> None:
    """Serialize `data` as JSON and atomically replace `path`.

    Writes to a temp file in the same directory, fsyncs, then os.replaces
    onto the target. Readers see either the old contents or the new — never
    a partial write.
    """
    _ensure_parent(path)
    encoded = json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False)
    # Use mkstemp so we own the fd and can fsync explicitly.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encoded)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover - filesystems w/o fsync
                pass
        os.replace(str(tmp_path), str(path))
    except Exception:
        # Clean up tmp on any failure so we don't litter the dir.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# --- read with corrupt-file quarantine -----------------------------------

def _quarantine_corrupt(path: pathlib.Path) -> pathlib.Path | None:
    """Rename `path` to `<name>.corrupt-<unix_ts>` next to it. Returns the
    new path, or None if the rename failed."""
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    # If two concurrent calls collide on the same second, append a counter.
    counter = 0
    final = target
    while final.exists():
        counter += 1
        final = path.with_name(f"{path.name}.corrupt-{int(time.time())}-{counter}")
    try:
        os.replace(str(path), str(final))
        return final
    except OSError as e:  # pragma: no cover
        print(f"agent-hub _state: failed to quarantine corrupt file: {e}", file=sys.stderr)
        return None


def _read_records_handle_corrupt() -> list[dict]:
    """Read SPAWNED_PATH. Missing -> []. Corrupt -> quarantine + []."""
    if not SPAWNED_PATH.exists():
        return []
    try:
        with SPAWNED_PATH.open("r", encoding="utf-8") as fh:
            text = fh.read()
        data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        moved = _quarantine_corrupt(SPAWNED_PATH)
        print(
            f"agent-hub _state: spawned.json corrupt ({e}); "
            f"quarantined to {moved}",
            file=sys.stderr,
        )
        return []
    except OSError as e:  # pragma: no cover
        print(f"agent-hub _state: spawned.json read failed: {e}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        moved = _quarantine_corrupt(SPAWNED_PATH)
        print(
            f"agent-hub _state: spawned.json top-level not a list; "
            f"quarantined to {moved}",
            file=sys.stderr,
        )
        return []
    return data


# --- public locked operations -------------------------------------------

def locked_read_spawned() -> list[dict]:
    """Read spawned.json under an exclusive lock. Returns the list of records.

    Corrupt files are renamed to `spawned.json.corrupt-<unix_ts>` and `[]` is
    returned. Missing files return `[]`.
    """
    lock_path = _lock_path()
    _ensure_parent(lock_path)
    # Open in 'a+' so the lock file is created if missing without truncating.
    with open(lock_path, "a+") as lock_fh:
        _platform_lock(lock_fh)
        try:
            return _read_records_handle_corrupt()
        finally:
            _platform_unlock(lock_fh)


def locked_mutate_spawned(
    mutator: Callable[[list[dict]], list[dict]],
) -> list[dict]:
    """Read, transform, and atomically write spawned.json under an exclusive lock.

    `mutator` receives the current list of records and must return the new
    list. The new list is written atomically and also returned to the caller.
    """
    lock_path = _lock_path()
    _ensure_parent(lock_path)
    with open(lock_path, "a+") as lock_fh:
        _platform_lock(lock_fh)
        try:
            current = _read_records_handle_corrupt()
            new = mutator(current)
            if not isinstance(new, list):
                raise TypeError(
                    f"mutator must return a list, got {type(new).__name__}"
                )
            atomic_write_json(SPAWNED_PATH, new)
            return new
        finally:
            _platform_unlock(lock_fh)
