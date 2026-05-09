"""F1 child bookkeeping — per design/phase2/design_v3.md §2.3, §2.4, §2.5.

Pure in-memory helpers for tracking parent/child task relationships and
enforcing delegation safety (cycle detection + depth cap). Intentionally
does **not** import `hub.database` or `hub.main` so it is independently
unit-testable; callers pass DB accessors as arguments.

Concurrency: the two maps are guarded by the existing `TaskQueue._lock`
(an `RLock`). Callers register that lock via `set_lock(lock)` once the
queue is constructed. Until then a no-op lock is used (safe for unit
tests that don't need cross-thread guarantees).
"""
from __future__ import annotations

import os
import threading
from typing import Callable, Iterable, Optional


MAX_TASK_DEPTH: int = int(os.getenv("AGENT_HUB_MAX_TASK_DEPTH", "8"))


# parent_task_id -> list[child_task_id]
_children_by_parent: dict[str, list[str]] = {}
# child_task_id -> parent_task_id
_parent_of_child: dict[str, str] = {}


class _NullLock:
    """Re-entrant no-op lock used until the real `TaskQueue._lock` is wired in."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


_lock: object = _NullLock()


def set_lock(lock) -> None:
    """Wire in the shared `TaskQueue._lock` (an RLock). Per design §2.4 — no new lock."""
    global _lock
    _lock = lock


def reset_state() -> None:
    """Clear in-memory maps. Used by tests and `load_state` rebuild."""
    with _lock:
        _children_by_parent.clear()
        _parent_of_child.clear()


def register_child(parent_task_id: str, child_task_id: str) -> None:
    """Add a child under its parent in both maps."""
    with _lock:
        siblings = _children_by_parent.setdefault(parent_task_id, [])
        if child_task_id not in siblings:
            siblings.append(child_task_id)
        _parent_of_child[child_task_id] = parent_task_id


def release_child(child_task_id: str) -> None:
    """Remove a child from both maps when it terminates."""
    with _lock:
        parent_id = _parent_of_child.pop(child_task_id, None)
        if parent_id is None:
            return
        siblings = _children_by_parent.get(parent_id)
        if siblings is None:
            return
        try:
            siblings.remove(child_task_id)
        except ValueError:
            pass
        if not siblings:
            _children_by_parent.pop(parent_id, None)


def _ancestor_chain(
    task_id: Optional[str],
    db_get_task: Callable[[str], object],
) -> list[str]:
    """Walk `parent_task_id` pointers from `task_id` toward the root.

    Returns task IDs in order `[task_id, parent, grandparent, ...]`,
    bounded by `MAX_TASK_DEPTH + 1` to keep the loop O(depth) even if the
    DB returns an unexpected cycle.

    `db_get_task(task_id)` should return an object with a `parent_task_id`
    attribute (or `None` if the task is missing).
    """
    chain: list[str] = []
    current_id = task_id
    seen: set[str] = set()
    while current_id is not None and len(chain) <= MAX_TASK_DEPTH + 1:
        if current_id in seen:
            break
        seen.add(current_id)
        chain.append(current_id)
        task = db_get_task(current_id)
        if task is None:
            break
        current_id = getattr(task, "parent_task_id", None)
    return chain


def check_delegation(
    parent_task_id: Optional[str],
    target_agent: str,
    parent_agent_id: str,
    db_get_task: Callable[[str], object],
) -> Optional[str]:
    """Enforce depth cap and cycle rule per design §2.5 (v3).

    Returns:
        None — delegation is allowed.
        "depth_limit_exceeded" — adding a child would exceed `MAX_TASK_DEPTH`.
        "cycle" — `target_agent` matches a non-immediate ancestor's
            `assigned_agent_id` (immediate self A→A is allowed).

    `db_get_task(task_id)` returns an object with `parent_task_id` and
    `assigned_agent_id` attributes (or `None` if missing).
    """
    if parent_task_id is None:
        # Top-level task: depth 0, no ancestors to cycle against.
        return None

    chain = _ancestor_chain(parent_task_id, db_get_task)

    # Depth: the new child would sit at len(chain) + 1. Reject when
    # the existing chain already meets or exceeds the cap.
    if len(chain) >= MAX_TASK_DEPTH:
        return "depth_limit_exceeded"

    # Cycle: skip the immediate parent (chain[0]) so A→A is allowed.
    # Reject if `target_agent` matches the assigned_agent_id of any
    # non-immediate ancestor.
    for ancestor_id in chain[1:]:
        ancestor = db_get_task(ancestor_id)
        if ancestor is None:
            continue
        if getattr(ancestor, "assigned_agent_id", None) == target_agent:
            return "cycle"

    return None


def rebuild_from_db(load_tasks_iter: Iterable[tuple[str, Optional[str]]]) -> None:
    """Repopulate `_parent_of_child` (and the reverse map) from persisted rows.

    Called by `load_state` after a hub restart. Iterator yields
    `(task_id, parent_task_id)` pairs — `parent_task_id` may be `None`
    for top-level tasks (those entries are skipped).
    """
    with _lock:
        _children_by_parent.clear()
        _parent_of_child.clear()
        for task_id, parent_task_id in load_tasks_iter:
            if parent_task_id is None:
                continue
            _parent_of_child[task_id] = parent_task_id
            siblings = _children_by_parent.setdefault(parent_task_id, [])
            if task_id not in siblings:
                siblings.append(task_id)
