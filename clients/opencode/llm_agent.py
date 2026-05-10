"""Phase 2.3 §3.3 / §3.5 / §10 d — opencode CLI worker adapter.

Wraps the `opencode` CLI as an Agent-Hub worker. One persistent session per
`agent_id`: the first task invocation runs `opencode run --format json <msg>`
and drops a `.opencode-session-started` marker into the workdir; subsequent
invocations add `-c` (continue) so the same session is resumed.

Opencode does not have a `--append-system-prompt` flag like claude, so the
role prompt is concatenated into the user prompt with a `\n\n---\n\n`
separator.

Stdlib only (plus agent_sdk + clients.role_presets).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agent_sdk import AgentHub, TaskFailed
from clients.role_presets import get_preset, load_user_overrides


CLI_NAME = "opencode"
_SESSION_MARKER = ".opencode-session-started"
_PROMPT_SEPARATOR = "\n\n---\n\n"

_PAYLOAD_SCHEMA_V1: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "max_budget_tokens": {"type": "integer"},
        "system_prompt_extra": {"type": "string"},
    },
    "required": ["prompt"],
    "additionalProperties": True,
}


def compute_deadline(timeout: int | float) -> float:
    """Per design v3 finding #12 — leave headroom under the task timeout so
    the SDK can return a structured failure before the hub times us out."""
    if timeout <= 5:
        return max(0.5, timeout * 0.8)
    return float(timeout - 5)


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clients.opencode.llm_agent")
    p.add_argument("--hub", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--role", default="generic")
    p.add_argument("--workdir", required=True)
    p.add_argument("--max-budget-tokens", type=int, default=None)
    p.add_argument("--capabilities", default=None,
                   help="comma-separated capability names; overrides role preset")
    p.add_argument("--system-prompt-extra", default=None)
    return p


def _resolve_capabilities(args) -> list[dict]:
    if args.capabilities:
        names = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    else:
        try:
            preset = get_preset(args.role)
        except ValueError:
            preset = get_preset("generic")
        names = list(preset["capabilities"])
    return [
        {"name": n, "version": 1, "payload_schema": _PAYLOAD_SCHEMA_V1}
        for n in names
    ]


def _resolve_role_prompt(role: str, extra: str | None) -> str | None:
    role_prompts, _ = load_user_overrides()
    try:
        preset = get_preset(role, role_prompts=role_prompts)
        prompt = preset["prompt"]
    except ValueError:
        prompt = None
    if extra:
        prompt = (prompt or "") + ("\n\n" if prompt else "") + extra
    return prompt


def _resolve_budget(role: str, override: int | None) -> int:
    if override:
        return int(override)
    _, role_budgets = load_user_overrides()
    try:
        preset = get_preset(role, role_budgets=role_budgets)
        return int(preset["budget"])
    except ValueError:
        return int(get_preset("generic")["budget"])


def _extract_text_from_events(events: list[dict]) -> str | None:
    """Walk the JSONL events and return the last assistant message text.

    Opencode's exact event schema isn't fully stable; we try a few common
    shapes and return None if nothing assistant-shaped is found so the caller
    can fall back to the whole stdout.
    """
    last_text: str | None = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        # Shape A: {"role": "assistant", "content": "..."} or list of parts.
        role = ev.get("role")
        if role == "assistant":
            content = ev.get("content")
            text = _flatten_content(content)
            if text:
                last_text = text
                continue
        # Shape B: {"type": "message", "message": {"role":"assistant",...}}
        msg = ev.get("message")
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            text = _flatten_content(msg.get("content"))
            if text:
                last_text = text
                continue
        # Shape C: {"type":"assistant", "text":"..."} or {"text":"..."}
        if ev.get("type") in ("assistant", "assistant_message", "message") and ev.get("text"):
            last_text = ev["text"]
            continue
        if "text" in ev and isinstance(ev["text"], str) and role in (None, "assistant"):
            last_text = ev["text"]
    return last_text


def _flatten_content(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for piece in content:
            if isinstance(piece, str):
                parts.append(piece)
            elif isinstance(piece, dict):
                t = piece.get("text") or piece.get("content")
                if isinstance(t, str):
                    parts.append(t)
        if parts:
            return "".join(parts)
    return None


def _extract_usage(events: list[dict]) -> dict | None:
    """Return the last `usage` dict found anywhere in the events."""
    last: dict | None = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        u = ev.get("usage")
        if isinstance(u, dict):
            last = u
        msg = ev.get("message")
        if isinstance(msg, dict):
            mu = msg.get("usage")
            if isinstance(mu, dict):
                last = mu
    return last


def _build_cmd(prompt: str, session_started: bool) -> list[str]:
    cmd = ["opencode", "run", "--format", "json"]
    if session_started:
        cmd.append("-c")
    cmd.append(prompt)
    return cmd


def _run_cli_for_task(task, workdir: Path, role_prompt: str | None,
                     budget: int, role: str, hub) -> dict:
    payload = task.get("payload") or {}
    user_prompt = payload.get("prompt") or task.get("task") or ""
    extra = payload.get("system_prompt_extra")
    effective_prompt = role_prompt
    if extra:
        effective_prompt = (effective_prompt or "") + ("\n\n" if effective_prompt else "") + extra

    full_prompt = (
        (effective_prompt + _PROMPT_SEPARATOR + user_prompt)
        if effective_prompt
        else user_prompt
    )

    timeout = int(task.get("timeout") or 300)
    deadline = compute_deadline(timeout)

    marker = workdir / _SESSION_MARKER
    session_started = marker.exists()
    cmd = _build_cmd(full_prompt, session_started)

    t0 = time.monotonic()
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=deadline,
            cwd=str(workdir),
        )
    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - t0, 2)
        raise TaskFailed({
            "error": "cli_timeout",
            "cli": CLI_NAME,
            "role": role,
            "elapsed_s": elapsed,
            "deadline_s": deadline,
        })

    elapsed = round(time.monotonic() - t0, 2)

    if cp.returncode != 0:
        raise TaskFailed({
            "error": "cli_nonzero_exit",
            "exit_code": cp.returncode,
            "stderr": (cp.stderr or "")[-2000:],
            "cli": CLI_NAME,
            "role": role,
            "elapsed_s": elapsed,
        })

    # Parse JSONL events.
    raw_lines = (cp.stdout or "").splitlines()
    events: list[dict] = []
    parse_errors = 0
    for line in raw_lines:
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            parse_errors += 1
            continue
        if isinstance(obj, dict):
            events.append(obj)

    if not events and raw_lines:
        # Try whole stdout as a single JSON document before giving up.
        try:
            obj = json.loads((cp.stdout or "").strip())
            if isinstance(obj, dict):
                events.append(obj)
        except (json.JSONDecodeError, ValueError):
            pass

    if not events and (cp.stdout or "").strip() and parse_errors > 0:
        # Stdout was non-empty but nothing parsed at all -> unparseable.
        raise TaskFailed({
            "error": "cli_unparseable_json",
            "stdout_tail": (cp.stdout or "")[-2000:],
            "cli": CLI_NAME,
            "role": role,
            "elapsed_s": elapsed,
        })

    text = _extract_text_from_events(events) if events else None
    if not text:
        # Fallback to the whole stdout.
        text = (cp.stdout or "").strip()

    usage = _extract_usage(events) if events else None

    # Mark session started after first successful run.
    if not session_started:
        try:
            marker.touch()
        except OSError:
            pass

    result = {
        "text": text,
        "raw_events": events[:50],
        "usage": usage,
        "cli": CLI_NAME,
        "role": role,
        "elapsed_s": elapsed,
    }

    # Budget tracking — warn-only.
    if budget and usage:
        total = (
            usage.get("total_tokens")
            or (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
            or 0
        )
        if total and total > budget:
            warn = (
                f"used tokens {total} exceeded max_budget_tokens {budget}"
            )
            result["payload_schema_warn"] = warn
            try:
                hub.send_activity_log(
                    task.get("task_id"),
                    "payload_schema_warn",
                    {
                        "used_tokens": total,
                        "budget": budget,
                        "cli": CLI_NAME,
                        "role": role,
                    },
                )
            except Exception:
                pass  # advisory only

    return result


def main() -> None:
    args = _build_argparser().parse_args()
    api_key = os.environ.get("AGENT_HUB_API_KEY")
    role_prompt = _resolve_role_prompt(args.role, args.system_prompt_extra)
    budget = _resolve_budget(args.role, args.max_budget_tokens)

    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    pipeline_id = workdir.parent.name or "default"
    hub = AgentHub(
        hub_url=args.hub,
        agent_id=args.id,
        capabilities=_resolve_capabilities(args),
        auth_token=api_key,
        unregister_on_stop=True,
        description=f"cli={CLI_NAME};role={args.role};pipeline={pipeline_id}",
    )

    @hub.task_handler
    def handle(task):
        return _run_cli_for_task(task, workdir, role_prompt, budget, args.role, hub)

    def _shutdown(signum, frame):  # pragma: no cover - signal-driven
        try:
            hub.stop()
        finally:
            sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    hub.start()


if __name__ == "__main__":  # pragma: no cover
    main()
