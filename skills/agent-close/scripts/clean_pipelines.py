"""Phase 2.3 — standalone pipeline workspace purge utility.

Per design `design/phase2.3/design_v3.md` §5 + §13: walks
`~/.agent-hub/pipelines/`, lists workspaces, removes those whose `agent_id` is
NOT in the current `spawned.json`. Stdlib only — replaces the old shell
script. By default prints `would remove ...`; `--apply` actually `rm -rf`s.

Workspace layout (per design §5):
    ~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/

The leaf directory name IS the `agent_id`. We walk depth-2 leaves and compare
each leaf name against the set of agent_ids currently in spawned.json.
Optional `--days N` filter further limits to leaves whose mtime is older than
N days (default 14).
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
import time

_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from clients import _state  # noqa: E402


PIPELINES_ROOT = pathlib.Path.home() / ".agent-hub" / "pipelines"


def _spawned_agent_ids() -> set[str]:
    try:
        records = _state.locked_read_spawned()
    except Exception as e:  # pragma: no cover
        print(f"clean_pipelines: failed to read spawned.json: {e}", file=sys.stderr)
        return set()
    return {r.get("agent_id") for r in records if r.get("agent_id")}


def find_orphan_dirs(
    root: pathlib.Path,
    spawned_ids: set[str],
    older_than_days: float | None = None,
    now: float | None = None,
) -> list[pathlib.Path]:
    """Return depth-2 leaf dirs whose name (agent_id) is not in `spawned_ids`.

    If `older_than_days` is set, also require the leaf's mtime to be older
    than `now - older_than_days * 86400`.
    """
    if not root.exists():
        return []
    cutoff: float | None = None
    if older_than_days is not None:
        if now is None:
            now = time.time()
        cutoff = now - (older_than_days * 86400.0)

    orphans: list[pathlib.Path] = []
    try:
        pipelines = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []

    for pipeline_dir in pipelines:
        try:
            leaves = sorted(p for p in pipeline_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for leaf in leaves:
            agent_id = leaf.name
            if agent_id in spawned_ids:
                continue
            if cutoff is not None:
                try:
                    if leaf.stat().st_mtime > cutoff:
                        continue
                except OSError:
                    continue
            orphans.append(leaf)
    return orphans


def apply_removal(orphans: list[pathlib.Path]) -> list[dict]:
    """`rm -rf` each orphan. Returns per-orphan status dicts."""
    out: list[dict] = []
    for d in orphans:
        rec = {"path": str(d), "removed": False, "error": None}
        try:
            shutil.rmtree(d)
            rec["removed"] = True
        except FileNotFoundError:
            rec["removed"] = True
        except OSError as e:
            rec["error"] = str(e)
        out.append(rec)

        # Best-effort: rmdir parent pipeline dir if now empty.
        parent = d.parent
        try:
            if (
                parent != PIPELINES_ROOT.expanduser().resolve()
                and parent.is_dir()
                and not any(parent.iterdir())
            ):
                parent.rmdir()
        except OSError:
            pass
    return out


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clean-pipelines",
        description=(
            "Walk ~/.agent-hub/pipelines/ and remove workspace leaves whose "
            "agent_id is not currently in spawned.json. Dry-run by default; "
            "pass --apply to actually delete."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually rm -rf the orphan workspaces (default: dry-run).",
    )
    p.add_argument(
        "--days",
        type=float,
        default=14.0,
        help=(
            "Only consider leaves whose mtime is older than N days "
            "(default: 14). Pass 0 to disable the age filter."
        ),
    )
    p.add_argument(
        "--root",
        default=None,
        help="Override pipelines root (default: ~/.agent-hub/pipelines).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    root = (
        pathlib.Path(args.root).expanduser().resolve()
        if args.root
        else PIPELINES_ROOT.expanduser().resolve()
    )
    older_than = None if args.days <= 0 else args.days
    spawned_ids = _spawned_agent_ids()
    orphans = find_orphan_dirs(root, spawned_ids, older_than_days=older_than)

    if not orphans:
        print("clean_pipelines: no orphan workspaces found.")
        return 0

    if not args.apply:
        for d in orphans:
            print(f"would remove {d}")
        print(
            f"clean_pipelines: {len(orphans)} orphan(s); "
            f"re-run with --apply to delete."
        )
        return 0

    results = apply_removal(orphans)
    n_ok = sum(1 for r in results if r["removed"])
    n_fail = len(results) - n_ok
    for r in results:
        if r["removed"]:
            print(f"removed {r['path']}")
        else:
            print(f"FAILED {r['path']}: {r['error']}", file=sys.stderr)
    print(f"clean_pipelines: removed {n_ok}, failed {n_fail}.")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
