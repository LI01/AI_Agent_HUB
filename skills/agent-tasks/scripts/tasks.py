"""Phase 2.3 — `agent-tasks` skill helper.

Picks an agent from the hub, prompts the user for a task payload using the
agent's advertised `payload_schema`, submits via `POST /tasks`, and polls
until terminal.

Design ref: design/phase2.3/design_v3.md §2.2, §7, §10.

No new top-level deps. `httpx` is already pulled in by `agent_sdk`.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from typing import Any, Optional

import httpx


# Keep the codebase import path resilient to direct script invocation.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from clients._state import locked_read_spawned  # noqa: E402
from clients._files import (  # noqa: E402
    InvalidPayloadFiles,
    _validate_relpath,
)


# --- HTTP helpers --------------------------------------------------------


def _auth_headers(api_key: Optional[str]) -> dict:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def list_agents(hub_url: str, api_key: Optional[str]) -> list[dict]:
    """`GET /agents`. Returns the list of registered agents (possibly empty).

    Raises `httpx.HTTPError` on transport failure; raises `RuntimeError`
    on non-2xx status with the response body included.
    """
    url = hub_url.rstrip("/") + "/agents"
    r = httpx.get(url, headers=_auth_headers(api_key), timeout=10.0)
    if r.status_code // 100 != 2:
        raise RuntimeError(f"GET /agents failed: {r.status_code} {r.text}")
    body = r.json()
    if not isinstance(body, list):
        raise RuntimeError(f"GET /agents returned non-list body: {body!r}")
    return body


def get_agent(hub_url: str, api_key: Optional[str], agent_id: str) -> dict:
    url = hub_url.rstrip("/") + f"/agents/{agent_id}"
    r = httpx.get(url, headers=_auth_headers(api_key), timeout=10.0)
    if r.status_code // 100 != 2:
        raise RuntimeError(f"GET /agents/{agent_id} failed: {r.status_code} {r.text}")
    return r.json()


# --- metadata resolution -------------------------------------------------


def _parse_description_metadata(description: Optional[str]) -> dict:
    """Parse `cli=...;role=...` style descriptions. Unknown keys are kept
    but only `cli` and `role` are used by the table renderer."""
    out: dict[str, str] = {}
    if not description or not isinstance(description, str):
        return out
    for part in description.split(";"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        v = v.strip()
        if k:
            out[k] = v
    return out


def _resolve_metadata(agent: dict, spawned_by_id: dict) -> tuple[str, str]:
    """Return `(cli, role)` for an agent.

    Priority per design v3 §2.2 finding 5:
        spawned.json[agent_id].cli/role > description "cli=...;role=..." >
        "unknown"
    """
    agent_id = agent.get("agent_id") or agent.get("id") or ""
    rec = spawned_by_id.get(agent_id) or {}
    cli = rec.get("cli")
    role = rec.get("role")
    if cli and role:
        return cli, role

    parsed = _parse_description_metadata(agent.get("description"))
    if not cli:
        cli = parsed.get("cli") or "unknown"
    if not role:
        role = parsed.get("role") or "unknown"
    return cli, role


def _format_capabilities(caps: Any) -> str:
    """Render Phase 2.2 Capability list as `name:vN, ...`."""
    if not caps:
        return ""
    parts = []
    for c in caps:
        if isinstance(c, dict):
            name = c.get("name") or "?"
            ver = c.get("version") or 1
            parts.append(f"{name}:v{ver}")
        elif isinstance(c, str):
            parts.append(c)
    return ", ".join(parts)


def render_agent_table(agents: list[dict], spawned_records: list[dict]) -> str:
    """Render a numbered, fixed-width table of agents.

    `agents` is the raw `GET /agents` body. `spawned_records` is the local
    `spawned.json` list. Metadata priority follows `_resolve_metadata`.
    """
    spawned_by_id = {
        r.get("agent_id"): r
        for r in (spawned_records or [])
        if isinstance(r, dict) and r.get("agent_id")
    }

    rows = []
    for a in agents or []:
        agent_id = a.get("agent_id") or a.get("id") or ""
        cli, role = _resolve_metadata(a, spawned_by_id)
        status = a.get("status") or "unknown"
        caps = _format_capabilities(a.get("capabilities") or a.get("skills"))
        rows.append((agent_id, cli, role, str(status), caps))

    # Stable sort by (status_rank, agent_id) so idle agents float up.
    status_rank = {"idle": 0, "busy": 1, "offline": 2}
    rows.sort(key=lambda r: (status_rank.get(r[3], 99), r[0]))

    header = ("agent_id", "cli", "role", "status", "capabilities")
    widths = [
        max(len(header[i]), max((len(r[i]) for r in rows), default=0))
        for i in range(5)
    ]
    # Cap absurd capability widths so the table stays readable.
    widths[4] = min(widths[4], 60)

    def _fmt(idx: Optional[int], parts) -> str:
        prefix = f"{idx:>2}  " if idx is not None else "    "
        return prefix + "  ".join(
            (parts[i] if i == 4 else parts[i].ljust(widths[i]))
            for i in range(5)
        )

    lines = [_fmt(None, header)]
    if not rows:
        lines.append("    (no agents registered)")
        return "\n".join(lines)
    for i, r in enumerate(rows, start=1):
        lines.append(_fmt(i, r))
    return "\n".join(lines)


# --- payload prompting ---------------------------------------------------


_UNSUPPORTED_KEYWORDS = ("oneOf", "anyOf", "allOf", "$ref")


def _coerce_scalar(raw: str, jtype: Optional[str]) -> Any:
    """Coerce a raw input string to the JSON type the schema declares.

    Best-effort: bad input passes through as the raw string so the local
    validator can flag it (rather than us raising mid-prompt).
    """
    if jtype == "integer":
        try:
            return int(raw.strip())
        except (TypeError, ValueError):
            return raw
    if jtype == "number":
        try:
            return float(raw.strip())
        except (TypeError, ValueError):
            return raw
    if jtype == "boolean":
        s = raw.strip().lower()
        if s in ("true", "yes", "y", "1"):
            return True
        if s in ("false", "no", "n", "0"):
            return False
        return raw
    # string / unknown -> leave as-is.
    return raw


_MAX_NESTING_DEPTH = 2


def prompt_payload_from_schema(
    schema: Optional[dict],
    capability_name: str,
    *,
    input_fn=None,
    _depth: int = 0,
) -> dict:
    """Prompt for a payload conforming to `schema`.

    Per design v3 §10 g:
      - `type:object` with `properties` -> per-field prompts (with type coercion).
      - Recurse one level into nested `type:object` properties (max depth = 2).
      - Anything else, or any unsupported keyword (oneOf/anyOf/allOf/$ref) at
        any level, falls back to a single raw-JSON prompt.

    `input_fn` is injected for tests; defaults to the builtin `input`.
    """
    if input_fn is None:
        input_fn = input

    schema = schema or {}

    # Detect unsupported keywords at this level -> raw fallback.
    if any(k in schema for k in _UNSUPPORTED_KEYWORDS):
        return _prompt_raw_json(schema, capability_name, input_fn)

    # Beyond the depth limit: anything still object-shaped goes to raw JSON.
    if _depth >= _MAX_NESTING_DEPTH:
        return _prompt_raw_json(schema, capability_name, input_fn)

    if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
        properties: dict = schema["properties"]
        required = set(schema.get("required") or [])
        out: dict[str, Any] = {}
        for field_name, field_schema in properties.items():
            field_schema = field_schema or {}
            jtype = field_schema.get("type")

            # Unsupported keyword on a property -> raw JSON for that subfield.
            if any(k in field_schema for k in _UNSUPPORTED_KEYWORDS):
                sub = _prompt_raw_json(
                    field_schema,
                    f"{capability_name}.{field_name}",
                    input_fn,
                )
                if sub or field_name in required:
                    out[field_name] = sub
                continue

            # Nested object: recurse one level.
            if (
                jtype == "object"
                and isinstance(field_schema.get("properties"), dict)
            ):
                print(f"  {field_name} (object):")
                sub = prompt_payload_from_schema(
                    field_schema,
                    f"{capability_name}.{field_name}",
                    input_fn=input_fn,
                    _depth=_depth + 1,
                )
                if sub or field_name in required:
                    out[field_name] = sub
                continue

            hint_bits = []
            if jtype:
                hint_bits.append(jtype)
            if field_schema.get("enum"):
                hint_bits.append("enum=" + ",".join(map(str, field_schema["enum"])))
            if field_name in required:
                hint_bits.append("required")
            hint = f" [{'; '.join(hint_bits)}]" if hint_bits else ""
            prompt_str = f"  {field_name}{hint}: "
            raw = input_fn(prompt_str)
            if raw == "" and field_name not in required:
                # Allow skipping optional fields entirely.
                continue
            out[field_name] = _coerce_scalar(raw, jtype)
        return out

    # Anything else (no schema, scalar schema, array-only schema, ...) ->
    # raw JSON fallback.
    return _prompt_raw_json(schema, capability_name, input_fn)


def _prompt_raw_json(
    schema: Optional[dict], capability_name: str, input_fn
) -> dict:
    """Single raw-JSON prompt. Schema is printed as a hint."""
    if schema:
        try:
            hint = json.dumps(schema, indent=2, sort_keys=False)
        except (TypeError, ValueError):
            hint = repr(schema)
        prompt_str = (
            f"Payload schema for capability {capability_name!r} (raw JSON mode):\n"
            f"{hint}\nEnter payload as JSON: "
        )
    else:
        prompt_str = (
            f"No schema for capability {capability_name!r}; "
            f"enter payload as JSON: "
        )
    raw = input_fn(prompt_str)
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"payload is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError(
            f"payload must be a JSON object at top level, got {type(parsed).__name__}"
        )
    return parsed


# --- --file / --expect-back parsing (Phase 2.4 design v2 §4.2) ----------


def _parse_file_flags(file_args: Optional[list[str]]) -> list[dict]:
    """Parse repeated `--file <rel>=<local>` values into payload.files entries.

    Each ``spec`` is a single string with one ``=`` separating the in-payload
    relative path from the local source path. The local file is read as
    UTF-8 text and embedded as a string. Hard-fails on:

      - missing ``=`` or empty side
      - relpath that fails ``clients._files._validate_relpath`` (leading
        ``/``, ``\\``, drive letters, ``..``, empty parts)
      - local file does not exist or is unreadable
      - local file is not valid UTF-8 (binary support is Phase 2.5+;
        codex Q-answer #2 — no silent base64)

    Returns a list of ``{"path": rel, "content": text}`` dicts, in the
    order of the input flags.
    """
    if not file_args:
        return []
    out: list[dict] = []
    for spec in file_args:
        if "=" not in spec:
            raise SystemExit(
                f"--file expects <rel>=<local>, got {spec!r}"
            )
        rel, _, local = spec.partition("=")
        rel = rel.strip()
        local = local.strip()
        if not rel or not local:
            raise SystemExit(f"--file empty side: {spec!r}")
        # Validate the in-payload relpath up front so the user gets a
        # clear local error before the task is ever submitted. The
        # adapter would catch this server-side anyway via validate_files.
        try:
            _validate_relpath(rel)
        except InvalidPayloadFiles as e:
            raise SystemExit(
                f"--file {spec!r}: invalid path ({e.reason}: {e.detail})"
            )
        # FileNotFoundError and UnicodeDecodeError propagate so callers
        # (including the skill's pytest unit tests per the Phase 2.4 unit
        # B spec) can distinguish "missing local file" from "binary local
        # file" without re-parsing a SystemExit message. The CLI entry
        # point in main() converts both to SystemExit so user-facing UX
        # stays a clean error line.
        content = pathlib.Path(local).read_text(encoding="utf-8")
        out.append({"path": rel, "content": content})
    return out


# --- task lifecycle ------------------------------------------------------


def submit_task(
    hub_url: str,
    api_key: Optional[str],
    target_agent: Optional[str],
    task_type: Optional[str],
    payload: dict,
    *,
    min_version: int = 1,
    max_budget_tokens: Optional[int] = None,
    task_text: Optional[str] = None,
    timeout: Optional[int] = None,
) -> str:
    """`POST /tasks`. Returns the assigned `task_id`."""
    body: dict = {
        "task_type": task_type,
        "payload": dict(payload or {}),
        "target_agent": target_agent,
        "min_version": int(min_version or 1),
    }
    if max_budget_tokens is not None:
        body["payload"].setdefault("max_budget_tokens", int(max_budget_tokens))
    if task_text is not None:
        body["task"] = task_text
    elif "task" in body["payload"]:
        body["task"] = body["payload"]["task"]
    else:
        body["task"] = f"{task_type or 'task'} for {target_agent or 'any'}"
    if timeout is not None:
        body["timeout"] = int(timeout)

    url = hub_url.rstrip("/") + "/tasks"
    r = httpx.post(url, json=body, headers=_auth_headers(api_key), timeout=15.0)
    if r.status_code // 100 != 2:
        raise RuntimeError(f"POST /tasks failed: {r.status_code} {r.text}")
    data = r.json()
    task_id = data.get("task_id")
    if not task_id:
        raise RuntimeError(f"POST /tasks returned no task_id: {data!r}")
    return task_id


_TERMINAL_STATUSES = ("completed", "failed", "timeout", "cancelled")


def poll_task(
    hub_url: str,
    api_key: Optional[str],
    task_id: str,
    *,
    interval_s: float = 1.5,
    timeout_s: float = 330.0,
    sleep_fn=time.sleep,
    clock=time.monotonic,
) -> dict:
    """Poll `GET /tasks/<id>` until terminal or `timeout_s` exceeded.

    Returns the final task body. If the wall budget is exceeded before a
    terminal status is observed, returns the most recent body with an
    additional `_polling_timeout: True` marker.
    """
    url = hub_url.rstrip("/") + f"/tasks/{task_id}"
    headers = _auth_headers(api_key)
    started = clock()
    delay = max(0.25, float(interval_s))
    last: dict = {}
    while True:
        r = httpx.get(url, headers=headers, timeout=10.0)
        if r.status_code // 100 != 2:
            raise RuntimeError(
                f"GET /tasks/{task_id} failed: {r.status_code} {r.text}"
            )
        last = r.json()
        status = (last or {}).get("status")
        if status in _TERMINAL_STATUSES:
            return last
        if clock() - started > timeout_s:
            last = dict(last or {})
            last["_polling_timeout"] = True
            return last
        sleep_fn(delay)
        # Gentle backoff toward an 8s ceiling, per design §2.2 step 10.
        delay = min(8.0, delay * 1.5)


# --- argparse + main -----------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-tasks",
        description="Submit a task to an Agent-Hub-registered agent.",
    )
    p.add_argument("--hub", default=None, help="Hub URL (e.g. http://localhost:8080).")
    p.add_argument("--key", default=None, help="API key. Env: AGENT_HUB_API_KEY.")
    p.add_argument("--agent", default=None, help="Skip the picker; target this agent_id.")
    p.add_argument("--task-type", default=None, help="Skip the capability picker.")
    p.add_argument(
        "--payload",
        default=None,
        help="Raw JSON payload — skips per-field prompts.",
    )
    p.add_argument("--min-version", type=int, default=1)
    p.add_argument("--max-budget-tokens", type=int, default=None)
    p.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Per-task timeout in seconds (matches the hub's default).",
    )
    p.add_argument(
        "--no-poll",
        action="store_true",
        help="Submit and return task_id immediately, no polling.",
    )
    p.add_argument(
        "--interactive",
        action="store_true",
        help="Force interactive prompting even when other flags are supplied.",
    )
    # Phase 2.4 — file sharing.
    p.add_argument(
        "--file",
        action="append",
        default=None,
        metavar="REL=LOCAL",
        help=(
            "Attach a local UTF-8 text file as a payload.files entry. "
            "REL is the path inside the agent's workdir; LOCAL is the path on "
            "your machine. Repeatable. Merges with any --payload-supplied files."
        ),
    )
    p.add_argument(
        "--expect-back",
        action="append",
        default=None,
        metavar="REL",
        help=(
            "Request a relative path be read back from the agent's workdir "
            "into result.files. Repeatable."
        ),
    )
    return p


def _resolve_hub(args) -> str:
    if args.hub:
        return args.hub
    env = os.environ.get("AGENT_HUB_URL")
    if env:
        return env
    return "http://localhost:8080"


def _resolve_key(args) -> Optional[str]:
    # Per design §2.0: --key > AGENT_HUB_API_KEY > config files (handled by
    # the spawn skill helper) > prompt. We don't write configs here.
    if args.key:
        return args.key
    return os.environ.get("AGENT_HUB_API_KEY")


def _pick_agent_interactive(agents: list[dict], spawned: list[dict]) -> str:
    if not agents:
        raise SystemExit("No agents registered with the hub.")
    print(render_agent_table(agents, spawned))
    raw = input("Pick an agent by # (or paste agent_id): ").strip()
    # Accept either an index or a direct agent_id.
    spawned_by_id = {r.get("agent_id"): r for r in spawned if isinstance(r, dict)}
    rows = sorted(
        agents,
        key=lambda a: (
            {"idle": 0, "busy": 1, "offline": 2}.get(a.get("status", ""), 99),
            a.get("agent_id") or "",
        ),
    )
    if raw.isdigit():
        idx = int(raw)
        if not (1 <= idx <= len(rows)):
            raise SystemExit(f"Selection {idx} out of range.")
        return rows[idx - 1].get("agent_id") or ""
    # Direct agent_id paste.
    known = {a.get("agent_id") for a in agents}
    if raw in known:
        return raw
    raise SystemExit(f"Unknown agent_id: {raw!r}")


def _pick_capability(agent: dict, requested: Optional[str]) -> tuple[str, dict]:
    """Return `(task_type, payload_schema)` for the chosen capability."""
    caps = agent.get("capabilities") or agent.get("skills") or []
    # Normalize capability list to dict shape.
    norm: list[dict] = []
    for c in caps:
        if isinstance(c, dict):
            norm.append(c)
        elif isinstance(c, str):
            norm.append({"name": c, "version": 1, "payload_schema": None})
    if not norm:
        if requested:
            return requested, {}
        raise SystemExit(f"Agent {agent.get('agent_id')!r} has no capabilities.")
    if requested:
        for c in norm:
            if c.get("name") == requested:
                return requested, c.get("payload_schema") or {}
        # Allow it through anyway — the hub will validate dispatch.
        return requested, {}
    if len(norm) == 1:
        c = norm[0]
        return c.get("name") or "general", c.get("payload_schema") or {}
    print("Capabilities:")
    for i, c in enumerate(norm, start=1):
        print(f"  {i}  {c.get('name')}:v{c.get('version', 1)}")
    raw = input("Pick a capability by #: ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(norm)):
        raise SystemExit(f"Bad selection {raw!r}")
    c = norm[int(raw) - 1]
    return c.get("name") or "general", c.get("payload_schema") or {}


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    hub_url = _resolve_hub(args)
    api_key = _resolve_key(args)

    spawned = locked_read_spawned()

    # 1. Pick an agent.
    if args.agent:
        target_agent = args.agent
        try:
            agent = get_agent(hub_url, api_key, target_agent)
        except RuntimeError as e:
            print(f"warning: could not fetch agent details ({e}); proceeding blind",
                  file=sys.stderr)
            agent = {"agent_id": target_agent, "capabilities": []}
    else:
        agents = list_agents(hub_url, api_key)
        target_agent = _pick_agent_interactive(agents, spawned)
        agent = get_agent(hub_url, api_key, target_agent)

    # 2. Pick a capability.
    task_type, payload_schema = _pick_capability(agent, args.task_type)

    # 3. Build the payload.
    if args.payload is not None:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            raise SystemExit(f"--payload is not valid JSON: {e}")
        if not isinstance(payload, dict):
            raise SystemExit("--payload must be a JSON object.")
    else:
        payload = prompt_payload_from_schema(payload_schema, task_type)

    # 3b. Merge --file / --expect-back into the payload (design v2 §4.3).
    # MERGE semantics: extend any list already supplied via --payload; only
    # raise on a type conflict (non-list `files` / `expect_files_back`).
    try:
        extra_files = _parse_file_flags(args.file)
    except FileNotFoundError as e:
        raise SystemExit(f"--file: {e}")
    except UnicodeDecodeError as e:
        raise SystemExit(
            f"--file: local file is not UTF-8 ({e}); binary is Phase 2.5+."
        )
    if extra_files:
        existing = payload.get("files")
        if isinstance(existing, list):
            payload["files"] = existing + extra_files
        elif existing is None:
            payload["files"] = extra_files
        else:
            raise SystemExit(
                "--payload sets files= to a non-list; cannot merge --file."
            )

    if args.expect_back:
        existing_eb = payload.get("expect_files_back")
        if isinstance(existing_eb, list):
            payload["expect_files_back"] = existing_eb + list(args.expect_back)
        elif existing_eb is None:
            payload["expect_files_back"] = list(args.expect_back)
        else:
            raise SystemExit(
                "--payload sets expect_files_back= to a non-list; "
                "cannot merge --expect-back."
            )

    # 4. Submit.
    task_id = submit_task(
        hub_url,
        api_key,
        target_agent,
        task_type,
        payload,
        min_version=args.min_version,
        max_budget_tokens=args.max_budget_tokens,
        timeout=args.timeout,
    )
    print(f"submitted task_id={task_id}")

    if args.no_poll:
        return 0

    # 5. Poll.
    final = poll_task(hub_url, api_key, task_id)
    status = final.get("status")
    print(f"final status: {status}")
    result = final.get("result")
    if isinstance(result, dict):
        text = result.get("text") or result.get("response") or ""
        if text:
            print("---")
            print(text)
    print("---")
    print(json.dumps(final, indent=2, sort_keys=False))
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
