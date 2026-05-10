"""Phase 2.3 — agent-spawn skill helper.

Resolves config (deep-merge global + local), runs CLI pre-flight checks,
creates the workspace, spawns the matching adapter as a detached subprocess,
and records the spawn into ~/.agent-hub/spawned.json.

Design ref: design/phase2.3/design_v3.md §2.1 + §5 + §6 + §7 + §10.

Stdlib + existing in-repo modules only.
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid
from typing import Any

# Make the repo root importable when this file is run via
# `python -m skills.agent-spawn.scripts.spawn` OR directly as a script.
_HERE = pathlib.Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from clients import _state  # noqa: E402
from clients.role_presets import (  # noqa: E402
    BUILTIN_PRESETS,
    get_preset,
    load_user_overrides,
)


# --- CLI -> adapter module map (design §2.1) ----------------------------

CLI_MODULE_MAP: dict[str, str] = {
    "codex": "clients.codex.llm_agent",
    "claude": "clients.claude_code.llm_agent",
    "opencode": "clients.opencode.llm_agent",
}

# CLI executable names for the version / auth probes.
CLI_EXECUTABLE: dict[str, str] = {
    "codex": "codex",
    "claude": "claude",
    "opencode": "opencode",
}

# Auth-probe commands and failure markers per CLI (design §2.1 table).
_AUTH_PROBES: dict[str, tuple[list[str], tuple[str, ...]]] = {
    "codex": (
        ["codex", "whoami"],
        ("not logged in", "auth required", "API key missing"),
    ),
    "claude": (
        ["claude", "config", "get"],
        ("Please run claude login", "not authenticated", "API key"),
    ),
    "opencode": (
        ["opencode", "auth", "status"],
        ("not authenticated", "please login", "auth required"),
    ),
}


# --- config loading -----------------------------------------------------

def _read_json_safe(path: pathlib.Path) -> dict:
    """Return parsed JSON object at `path`, or `{}` if missing/unreadable."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def merge_config(global_config: dict, local_config: dict) -> dict:
    """Pure helper: deep-merge `global_config` and `local_config`, local wins.

    Nested dicts merge recursively; non-dict values from local replace
    whatever was in global. Either argument may be None or non-dict; both
    are coerced to `{}` in that case.
    """
    base = global_config if isinstance(global_config, dict) else {}
    over = local_config if isinstance(local_config, dict) else {}
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = merge_config(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    """Read both ~/.agent-hub/config.json and <cwd>/.agent-hub.local.json,
    deep-merge with local precedence (per design §10 #8)."""
    global_path = pathlib.Path.home() / ".agent-hub" / "config.json"
    local_path = pathlib.Path.cwd() / ".agent-hub.local.json"
    return merge_config(_read_json_safe(global_path), _read_json_safe(local_path))


# --- pre-flight checks --------------------------------------------------

class SpawnAbort(RuntimeError):
    """Raised by pre-flight checks when the spawn must not proceed."""


def check_cli_version(cli_name: str) -> str:
    """Run `<cli> --version` (5s timeout). Abort on non-zero or timeout.

    Returns the trimmed version string on success. Per design §10 #7, this is
    the only mandatory abort condition during pre-flight.
    """
    exe = CLI_EXECUTABLE.get(cli_name, cli_name)
    try:
        cp = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise SpawnAbort(f"cli_not_found: {cli_name}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SpawnAbort(f"cli_not_found: {cli_name} (version probe timed out)") from exc
    if cp.returncode != 0:
        raise SpawnAbort(
            f"cli_not_found: {cli_name} (version probe exit={cp.returncode})"
        )
    return (cp.stdout or cp.stderr or "").strip()


def probe_cli_auth(cli_name: str) -> dict:
    """Best-effort auth probe (design §2.1). Only aborts on explicit failure
    markers. Returns a dict describing the outcome.

    Outcome dict shape:
        {"status": "ok" | "skipped" | "warn", "message": "..."}
    Aborts via `SpawnAbort` only when stderr/stdout contains an explicit
    auth/login/API-key failure marker.
    """
    probe = _AUTH_PROBES.get(cli_name)
    if probe is None:
        return {"status": "skipped", "message": "no probe defined for this CLI"}
    cmd, failure_markers = probe
    try:
        cp = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return {"status": "skipped", "message": "probe binary not found"}
    except subprocess.TimeoutExpired:
        return {"status": "warn", "message": "auth probe timed out (advisory)"}

    haystack = (cp.stdout or "") + "\n" + (cp.stderr or "")
    haystack_lower = haystack.lower()
    for marker in failure_markers:
        if marker.lower() in haystack_lower:
            raise SpawnAbort(
                f"auth_failed: {cli_name}: {marker} (from `{' '.join(cmd)}`)"
            )
    if cp.returncode != 0:
        # Non-zero but no explicit marker — warn and continue.
        return {
            "status": "warn",
            "message": (
                f"`{' '.join(cmd)}` exited {cp.returncode} but no failure marker; "
                "continuing"
            ),
        }
    return {"status": "ok", "message": "auth probe passed"}


# --- workspace ---------------------------------------------------------

def create_workspace(pipeline_id: str | None, agent_id: str) -> pathlib.Path:
    """Create ~/.agent-hub/pipelines/<pipeline_id>/<agent_id>/.

    If `pipeline_id` is omitted (None or empty), default to
    `default-<agent_id>` per design §5.
    Returns the workspace path.
    """
    if not pipeline_id:
        pipeline_id = f"default-{agent_id}"
    workdir = (
        pathlib.Path.home() / ".agent-hub" / "pipelines" / pipeline_id / agent_id
    )
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


# --- adapter spawn ------------------------------------------------------

def spawn_adapter(
    cli: str,
    agent_id: str,
    hub_url: str,
    api_key: str,
    role: str,
    workdir: pathlib.Path,
    max_budget_tokens: int | None,
    description: str,
    capabilities: str | None = None,
    system_prompt_extra: str | None = None,
) -> int:
    """Spawn the matching adapter as a detached subprocess. Returns the PID.

    Maps `cli` → adapter module via `CLI_MODULE_MAP`. The API key is provided
    *only* via the child env (`AGENT_HUB_API_KEY=...`) — never on argv — to
    avoid process-listing leakage (design §2.1). All three adapters read the
    key from that env var.

    Custom-role flags (`--capabilities`, `--system-prompt-extra`) are
    forwarded verbatim to the adapter when supplied (design §3.0, §10(f)).
    """
    module = CLI_MODULE_MAP.get(cli)
    if module is None:
        raise ValueError(
            f"unknown cli {cli!r}; known: {sorted(CLI_MODULE_MAP)}"
        )

    workdir = pathlib.Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = [
        sys.executable, "-m", module,
        "--hub", hub_url,
        "--id", agent_id,
        "--role", role,
        "--workdir", str(workdir),
    ]
    if max_budget_tokens is not None:
        cmd += ["--max-budget-tokens", str(int(max_budget_tokens))]

    # --description is only advertised by the codex adapter today; forward it
    # there, and keep the env mirror below for adapters that read it from env.
    if cli == "codex" and description:
        cmd += ["--description", description]

    if capabilities:
        cmd += ["--capabilities", capabilities]
    if system_prompt_extra:
        cmd += ["--system-prompt-extra", system_prompt_extra]

    env = dict(os.environ)
    if api_key:
        env["AGENT_HUB_API_KEY"] = api_key
    if description:
        # Some adapters may pick this up from env; harmless if ignored.
        env.setdefault("AGENT_HUB_AGENT_DESCRIPTION", description)

    stdout_log = workdir / "stdout.log"
    stderr_log = workdir / "stderr.log"
    out_fh = open(stdout_log, "ab")
    err_fh = open(stderr_log, "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            env=env,
            stdout=out_fh,
            stderr=err_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        # Parent doesn't need the log file handles; child has its own dup'd
        # copies. Close ours so we don't hold extra fds.
        try:
            out_fh.close()
        except Exception:
            pass
        try:
            err_fh.close()
        except Exception:
            pass
    return proc.pid


# --- spawned.json record ------------------------------------------------

def append_spawned_record(record: dict) -> list[dict]:
    """Append `record` to ~/.agent-hub/spawned.json under exclusive lock.

    Uses `clients._state.locked_mutate_spawned` per design §6 + §14.
    Returns the new full record list.
    """
    def _mutate(records: list[dict]) -> list[dict]:
        return list(records) + [dict(record)]
    return _state.locked_mutate_spawned(_mutate)


# --- argv resolution ----------------------------------------------------

_VALID_CLIS = ("codex", "claude", "opencode")
_VALID_ROLES = tuple(sorted(BUILTIN_PRESETS.keys())) + ("custom",)


def _short_uuid() -> str:
    return uuid.uuid4().hex[:8]


def _resolve_api_key(args, config: dict) -> str:
    """Apply precedence: --key > AGENT_HUB_API_KEY > local > global > prompt.

    `config` is already the local-over-global merged dict, so for the
    config tier we just read `config["api_key"]`.
    """
    if args.key:
        return args.key
    env_val = os.environ.get("AGENT_HUB_API_KEY")
    if env_val:
        return env_val
    cfg_val = config.get("api_key") if isinstance(config, dict) else None
    if isinstance(cfg_val, str) and cfg_val:
        return cfg_val
    # Interactive fallback (silent prompt). Skip if stdin is not a tty so
    # scripted invocations fail fast instead of hanging.
    if sys.stdin.isatty():
        try:
            return getpass.getpass("Agent Hub API key: ").strip()
        except (KeyboardInterrupt, EOFError):
            pass
    return ""


def _resolve_hub_url(args, config: dict) -> str:
    if args.hub:
        return args.hub
    if isinstance(config, dict) and isinstance(config.get("hub_url"), str):
        return config["hub_url"]
    return "http://localhost:8080"


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-spawn",
        description="Spawn a per-CLI LLM worker agent and register it.",
    )
    p.add_argument("--cli", choices=_VALID_CLIS, required=False)
    p.add_argument(
        "--role",
        default=None,
        help=f"One of {_VALID_ROLES}; defaults to 'generic'",
    )
    p.add_argument("--hub", default=None, help="Hub URL")
    p.add_argument("--key", default=None, help="API key (prefer env)")
    p.add_argument(
        "--id",
        default=None,
        help="Agent ID (default: <cli>-<role>-<short-uuid>)",
    )
    p.add_argument(
        "--pipeline-id",
        default=None,
        help="Pipeline ID (default: default-<agent_id>)",
    )
    p.add_argument(
        "--max-budget-tokens",
        type=int,
        default=None,
        help="Override the role's default token budget",
    )
    p.add_argument(
        "--description",
        default=None,
        help="Optional description string sent at registration",
    )
    p.add_argument(
        "--no-auth-probe",
        action="store_true",
        help="Skip the best-effort auth probe (version check still runs).",
    )
    p.add_argument(
        "--capabilities",
        default=None,
        help=(
            "Comma-separated capability names (only used / required when "
            "--role custom). Forwarded verbatim to the adapter."
        ),
    )
    p.add_argument(
        "--system-prompt-extra",
        default=None,
        help=(
            "Custom system prompt suffix. For --role custom this acts as the "
            "role's system prompt. Forwarded verbatim to the adapter."
        ),
    )
    return p


# --- main orchestration -------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """argparse entry point — orchestrates the full spawn flow."""
    args = _build_argparser().parse_args(argv)
    config = load_config()

    cli = args.cli
    if cli is None:
        if not sys.stdin.isatty():
            print("--cli is required in non-interactive mode", file=sys.stderr)
            return 2
        cli = input(f"cli {list(_VALID_CLIS)}: ").strip() or "codex"
    if cli not in _VALID_CLIS:
        print(f"unknown cli {cli!r}; valid: {list(_VALID_CLIS)}", file=sys.stderr)
        return 2

    role = args.role or "generic"
    if role not in _VALID_ROLES:
        print(
            f"unknown role {role!r}; valid: {list(_VALID_ROLES)}",
            file=sys.stderr,
        )
        return 2

    # Custom-role flag resolution (design §2.1 prompts 3 + 4, §10(f)).
    # `--capabilities` / `--system-prompt-extra` are only meaningful for
    # `--role custom`; built-in roles get them from `clients.role_presets`.
    capabilities = args.capabilities
    system_prompt_extra = args.system_prompt_extra
    if role == "custom":
        if not capabilities:
            if sys.stdin.isatty():
                capabilities = input(
                    "capabilities (comma-separated, required for custom role): "
                ).strip()
            if not capabilities:
                print(
                    "--capabilities is required when --role custom "
                    "(non-empty comma-separated list)",
                    file=sys.stderr,
                )
                return 2
        if system_prompt_extra is None:
            if sys.stdin.isatty():
                try:
                    system_prompt_extra = input(
                        "system_prompt (free text, blank to skip): "
                    )
                except EOFError:
                    system_prompt_extra = ""
            if not system_prompt_extra:
                # Custom role with no system prompt is allowed; warn only.
                system_prompt_extra = system_prompt_extra or None

    agent_id = args.id or f"{cli}-{role}-{_short_uuid()}"
    pipeline_id = args.pipeline_id or f"default-{agent_id}"

    hub_url = _resolve_hub_url(args, config)
    api_key = _resolve_api_key(args, config)
    if not api_key:
        print(
            "no API key resolved (precedence: --key > AGENT_HUB_API_KEY > "
            "local config > global config > prompt); aborting",
            file=sys.stderr,
        )
        return 2

    description = args.description or (
        f"cli={cli};role={role};pipeline={pipeline_id}"
    )

    # Resolve budget via role presets (with user overrides applied) when not
    # passed explicitly.
    if args.max_budget_tokens is not None:
        budget: int | None = int(args.max_budget_tokens)
    else:
        try:
            role_prompts, role_budgets = load_user_overrides()
            preset = get_preset(
                role if role != "custom" else "generic",
                role_prompts=role_prompts,
                role_budgets=role_budgets,
            )
            budget = int(preset["budget"])
        except (ValueError, KeyError):
            budget = None

    # Pre-flight: version (mandatory), then auth probe (advisory).
    try:
        version = check_cli_version(cli)
    except SpawnAbort as exc:
        print(f"spawn aborted: {exc}", file=sys.stderr)
        return 1
    print(f"[agent-spawn] {cli} version: {version}")

    if not args.no_auth_probe:
        try:
            probe_result = probe_cli_auth(cli)
        except SpawnAbort as exc:
            print(f"spawn aborted: {exc}", file=sys.stderr)
            return 1
        print(
            f"[agent-spawn] auth probe: {probe_result['status']} "
            f"({probe_result['message']})"
        )

    # Workspace
    workdir = create_workspace(pipeline_id, agent_id)
    info = {
        "agent_id": agent_id,
        "cli": cli,
        "role": role,
        "hub_url": hub_url,
        "started_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    try:
        with (workdir / ".agent-info.json").open("w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=2)
    except OSError as e:
        print(f"warning: could not write .agent-info.json: {e}", file=sys.stderr)

    # Spawn adapter
    pid = spawn_adapter(
        cli=cli,
        agent_id=agent_id,
        hub_url=hub_url,
        api_key=api_key,
        role=role,
        workdir=workdir,
        max_budget_tokens=budget,
        description=description,
        capabilities=capabilities,
        system_prompt_extra=system_prompt_extra,
    )

    # Record
    record: dict[str, Any] = {
        "agent_id": agent_id,
        "pid": pid,
        "cli": cli,
        "role": role,
        "pipeline_id": pipeline_id,
        "hub_url": hub_url,
        "workdir": str(workdir),
        "started_at": info["started_at"],
    }
    try:
        append_spawned_record(record)
    except Exception as e:  # don't fail the spawn just because we couldn't record
        print(
            f"warning: could not append spawned.json record: {e}",
            file=sys.stderr,
        )

    print(
        f"[agent-spawn] spawned {agent_id} pid={pid} workdir={workdir} "
        f"(cli={cli} role={role} pipeline={pipeline_id})"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
