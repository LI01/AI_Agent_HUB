"""Phase 2.3 — agent-close skill helper.

Implements the kill sequence specified in
`design/phase2.3/design_v3.md` §2.3 verbatim, plus the trusted-path-guarded
`--purge` from design §5 + §10 fix #13.

PID-check semantics (verbatim from design §2.3):
    pid_check is True       -> alive AND looks like an adapter -> SIGTERM/SIGKILL
    pid_check is False      -> alive but cmdline doesn't match -> PID reused, skip kill
    pid_check is None       -> cmdline check inconclusive       -> skip kill AND skip purge
    not pid_alive           -> already terminated

`/unregister` is ALWAYS attempted (idempotent server-side).

Stdlib + httpx (already a transitive dep via agent_sdk). No new top-level deps.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
from typing import Optional

# Allow `python skills/agent-close/scripts/close.py` from a checkout without
# `pip install -e .` by inserting the repo root on sys.path.
_THIS = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from clients import _state  # noqa: E402

try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover - httpx is a transitive dep
    httpx = None  # type: ignore


PIPELINES_ROOT = pathlib.Path.home() / ".agent-hub" / "pipelines"


# ---------------------------------------------------------------------------
# spawned.json access
# ---------------------------------------------------------------------------

def load_spawned() -> list[dict]:
    """Wrap `clients._state.locked_read_spawned()`."""
    return _state.locked_read_spawned()


def remove_spawned_record(agent_id: str) -> None:
    """Remove the matching record from spawned.json under an exclusive lock."""
    def mutator(records: list[dict]) -> list[dict]:
        return [r for r in records if r.get("agent_id") != agent_id]
    _state.locked_mutate_spawned(mutator)


# ---------------------------------------------------------------------------
# Hub registry merge
# ---------------------------------------------------------------------------

def render_union(
    spawned_records: list[dict], registered_agents: list[dict]
) -> list[dict]:
    """Join local-spawned with `GET /agents`. Each row gets a `source` tag:

      - `local`        — in spawned.json only (local PID known, hub doesn't know)
      - `remote-only`  — in `GET /agents` only (registry-only)
      - `local+remote` — in both
    """
    by_id_local = {r.get("agent_id"): r for r in spawned_records if r.get("agent_id")}
    by_id_remote = {a.get("agent_id"): a for a in registered_agents if a.get("agent_id")}
    union_ids = list(dict.fromkeys(list(by_id_local.keys()) + list(by_id_remote.keys())))

    rows: list[dict] = []
    for aid in union_ids:
        local = by_id_local.get(aid)
        remote = by_id_remote.get(aid)
        if local and remote:
            source = "local+remote"
        elif local:
            source = "local"
        else:
            source = "remote-only"
        rows.append({
            "agent_id": aid,
            "source": source,
            "local": local,
            "remote": remote,
        })
    return rows


# ---------------------------------------------------------------------------
# PID checks
# ---------------------------------------------------------------------------

def pid_is_alive(pid: int) -> bool:
    """`os.kill(pid, 0)` returns True if alive, False if dead, False on
    `ProcessLookupError`. Other OSErrors (e.g. PermissionError = signal denied
    but process exists) are treated as alive."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it.
        return True
    except OSError:
        return False


def pid_is_adapter(pid: int) -> Optional[bool]:
    """Per design §10 fix #13 + design §3 verbatim block.

    Linux reads `/proc/<pid>/cmdline`, otherwise
    `subprocess.run(["ps","-p",str(pid),"-o","command="], ...)`.

    Returns:
        True   — cmdline contains both `clients.` and `llm_agent`.
        False  — cmdline checked successfully and does NOT match.
        None   — check is inconclusive (file missing, ps fails, etc.).
    """
    if pid is None or pid <= 0:
        return None

    if sys.platform.startswith("linux"):
        proc = pathlib.Path(f"/proc/{pid}/cmdline")
        try:
            cmd = proc.read_bytes().decode("utf-8", "replace")
        except (FileNotFoundError, ProcessLookupError):
            return None
        except PermissionError:
            return None
        except OSError:
            return None
        if not cmd:
            return None
        return ("clients." in cmd) and ("llm_agent" in cmd)

    # macOS / BSD / Windows fallback: ps
    try:
        cp = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    except OSError:
        return None
    if cp.returncode != 0:
        return None
    cmd = (cp.stdout or "").strip()
    if not cmd:
        return None
    return ("clients." in cmd) and ("llm_agent" in cmd)


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------

def terminate_pid(pid: int, grace_s: float = 5.0) -> bool:
    """SIGTERM; wait up to `grace_s` seconds; SIGKILL fallback if still alive.

    Returns True if the process is no longer alive after the sequence.
    """
    if not pid_is_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        # Could not signal (permissions, etc.) — fall through to wait/check.
        pass

    # Poll every 250ms up to grace_s.
    steps = max(1, int(grace_s / 0.25))
    for _ in range(steps):
        if not pid_is_alive(pid):
            return True
        time.sleep(0.25)

    if not pid_is_alive(pid):
        return True

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        pass

    # Up to 2s for SIGKILL to take effect.
    for _ in range(8):
        if not pid_is_alive(pid):
            return True
        time.sleep(0.25)

    return not pid_is_alive(pid)


# ---------------------------------------------------------------------------
# Hub unregister
# ---------------------------------------------------------------------------

def unregister_agent(
    hub_url: str, api_key: Optional[str], agent_id: str
) -> dict:
    """Best-effort `POST /unregister?agent_id=<id>`. Returns a small status dict
    `{"ok": bool, "status_code": int|None, "error": str|None}`.

    Never raises. 404 is treated as success (already gone)."""
    if httpx is None:
        return {"ok": False, "status_code": None, "error": "httpx_unavailable"}
    if not hub_url:
        return {"ok": False, "status_code": None, "error": "no_hub_url"}

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        resp = httpx.post(
            f"{hub_url.rstrip('/')}/unregister",
            params={"agent_id": agent_id},
            headers=headers,
            timeout=3,
        )
    except Exception as e:
        return {"ok": False, "status_code": None, "error": str(e)}

    sc = resp.status_code
    if sc == 404 or 200 <= sc < 300:
        return {"ok": True, "status_code": sc, "error": None}
    return {"ok": False, "status_code": sc, "error": f"http_{sc}"}


# ---------------------------------------------------------------------------
# Workspace purge
# ---------------------------------------------------------------------------

def workdir_under_agent_hub_pipelines(workdir) -> bool:
    """True iff `workdir` resolves to a path under `~/.agent-hub/pipelines/`."""
    if not workdir:
        return False
    try:
        wd = pathlib.Path(workdir).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    try:
        root = PIPELINES_ROOT.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    try:
        wd.relative_to(root)
    except ValueError:
        return False
    # Must not be the root itself.
    return wd != root


def purge_workspace(workdir) -> dict:
    """`shutil.rmtree(workdir)` ONLY when called from the safe path.

    Caller in `main()` MUST have already checked the gates per design §10
    fix #13 (terminated + not pid_reused + not pid_check_inconclusive +
    workdir_under_agent_hub_pipelines).

    Also rmdir parent if empty (per design §5).
    Returns `{"removed": bool, "parent_removed": bool, "error": str|None}`.
    """
    wd = pathlib.Path(workdir).expanduser().resolve()
    out = {"removed": False, "parent_removed": False, "error": None}
    try:
        shutil.rmtree(wd)
        out["removed"] = True
    except FileNotFoundError:
        out["removed"] = True  # already gone
    except OSError as e:
        out["error"] = str(e)
        return out

    # Try to rmdir parent if empty (design §5).
    parent = wd.parent
    try:
        # Only rmdir parents that are still under PIPELINES_ROOT and are empty.
        if (
            parent != PIPELINES_ROOT.expanduser().resolve()
            and workdir_under_agent_hub_pipelines(parent)
            and not any(parent.iterdir())
        ):
            parent.rmdir()
            out["parent_removed"] = True
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# Per-agent close routine (design §2.3 verbatim)
# ---------------------------------------------------------------------------

def close_one_agent(
    agent_id: str,
    spawned_by_id: dict,
    hub_url: str,
    api_key: Optional[str],
    purge: bool,
) -> dict:
    """Close a single agent. Returns a structured per-row report dict."""
    report: dict = {"agent_id": agent_id}
    local = spawned_by_id.get(agent_id)

    pid = local.get("pid") if local else None
    workdir = local.get("workdir") if local else None

    pid_alive = bool(local and pid and pid_is_alive(pid))
    pid_check = pid_is_adapter(pid) if pid_alive else None

    pid_check_inconclusive = bool(pid_alive and pid_check is None)
    pid_reused = bool(pid_alive and pid_check is False)
    terminated = (local is None) or (not pid_alive)

    if pid_check_inconclusive:
        report["pid_check_inconclusive"] = True
    elif pid_reused:
        report["pid_reused"] = True
    elif pid_alive and pid_check is True:
        terminated = terminate_pid(pid, grace_s=5.0)
        report["sigterm_sent"] = True
        if terminated:
            report["terminated"] = True
        else:
            report["termination_failed"] = True

    if terminated and not pid_check_inconclusive and not pid_reused:
        report["terminated"] = True

    # Always best-effort /unregister.
    if hub_url:
        unreg = unregister_agent(hub_url, api_key, agent_id)
        if unreg["ok"]:
            report["unregistered"] = True
        else:
            report["hub_unreachable"] = True
            report["unregister_error"] = unreg.get("error")
    else:
        report["unregister_skipped_no_hub_url"] = True

    # Purge guard per design §2.3 / §10 fix #13.
    if purge:
        if (
            terminated
            and not pid_reused
            and not pid_check_inconclusive
            and workdir
            and workdir_under_agent_hub_pipelines(workdir)
        ):
            purge_res = purge_workspace(workdir)
            if purge_res["removed"]:
                report["purged"] = True
                if purge_res["parent_removed"]:
                    report["parent_dir_removed"] = True
            else:
                report["purge_failed"] = purge_res.get("error")
        else:
            report["purge_skipped_process_still_alive_or_untrusted_path"] = True

    # Remove spawned.json record (only if it existed locally).
    if local is not None:
        try:
            remove_spawned_record(agent_id)
            report["spawned_record_removed"] = True
        except Exception as e:  # pragma: no cover
            report["spawned_record_remove_error"] = str(e)

    return report


# ---------------------------------------------------------------------------
# Argparse + main
# ---------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-close",
        description=(
            "Close LLM-CLI agents previously spawned via /agent-spawn. "
            "Joins local spawned.json with hub /agents and terminates each "
            "selected agent (PID-reuse safe), best-effort /unregister, "
            "removes the spawned record, and (with --purge) deletes the "
            "trusted workspace."
        ),
    )
    p.add_argument("--hub", default=None, help="Hub URL")
    p.add_argument(
        "--key",
        default=None,
        help="API key (prefer AGENT_HUB_API_KEY env to avoid argv leakage)",
    )
    p.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="AGENT_ID",
        help="Specific agent_id to close. May be repeated.",
    )
    p.add_argument(
        "--all-spawned",
        action="store_true",
        help="Close every agent currently in spawned.json.",
    )
    p.add_argument(
        "--purge",
        action="store_true",
        help=(
            "Also rm -rf the agent's workdir (only when terminated AND "
            "workdir is under ~/.agent-hub/pipelines/)."
        ),
    )
    return p


def _resolve_targets(args, spawned: list[dict]) -> list[str]:
    """Build the ordered, deduplicated list of agent_ids to act on."""
    ids: list[str] = []
    seen: set[str] = set()
    if args.all_spawned:
        for r in spawned:
            aid = r.get("agent_id")
            if aid and aid not in seen:
                ids.append(aid)
                seen.add(aid)
    for aid in args.agent or []:
        if aid and aid not in seen:
            ids.append(aid)
            seen.add(aid)
    return ids


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)

    api_key = args.key or os.environ.get("AGENT_HUB_API_KEY")
    hub_url = args.hub or os.environ.get("AGENT_HUB_URL") or ""

    spawned = load_spawned()
    spawned_by_id = {r.get("agent_id"): r for r in spawned if r.get("agent_id")}

    targets = _resolve_targets(args, spawned)
    if not targets:
        # Non-interactive default: nothing selected, nothing to do.
        msg = (
            "agent-close: no targets. Use --agent <id> (repeatable) "
            "or --all-spawned. Interactive picker not yet wired."
        )
        print(msg, file=sys.stderr)
        return 2

    reports: list[dict] = []
    for aid in targets:
        rep = close_one_agent(
            aid, spawned_by_id, hub_url, api_key, purge=args.purge
        )
        reports.append(rep)

    print(json.dumps({"closed": reports}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
