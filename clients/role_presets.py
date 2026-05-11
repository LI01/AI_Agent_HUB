"""Phase 2.3 — built-in role presets and user-override loading.

One source of truth for role -> {capabilities, prompt, budget}. Skill helpers
and adapters import from here so the table is never duplicated.

Prompt text lives in `clients/role_prompts/*.md` (one file per role) so
non-Python contributors can edit prompts without touching this module.
`_common.md` is prepended to every non-None role prompt as a shared baseline.

Design ref: design/phase2.3/design_v3.md §4 + §6.

Stdlib only. Prompts are read once at import time.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any


_PROMPTS_DIR = pathlib.Path(__file__).parent / "role_prompts"


def _load_prompt(role: str) -> str:
    """Return `_common.md` + `<role>.md`, joined by a blank line.

    Both files are required for any role with a prompt. Missing files raise
    `FileNotFoundError` at import time so a broken install fails loudly.
    """
    common = (_PROMPTS_DIR / "_common.md").read_text(encoding="utf-8").strip()
    role_text = (_PROMPTS_DIR / f"{role}.md").read_text(encoding="utf-8").strip()
    return f"{common}\n\n{role_text}"


BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "pm": {
        "capabilities": ["pm", "plan", "coordinate"],
        "prompt": _load_prompt("pm"),
        "budget": 30000,
    },
    "architect": {
        "capabilities": ["architect", "design"],
        "prompt": _load_prompt("architect"),
        "budget": 50000,
    },
    "designer": {
        "capabilities": ["design", "spec"],
        "prompt": _load_prompt("designer"),
        "budget": 40000,
    },
    "coder": {
        "capabilities": ["code"],
        "prompt": _load_prompt("coder"),
        "budget": 30000,
    },
    "reviewer": {
        "capabilities": ["review", "code-review"],
        "prompt": _load_prompt("reviewer"),
        "budget": 20000,
    },
    "tester": {
        "capabilities": ["test"],
        "prompt": _load_prompt("tester"),
        "budget": 20000,
    },
    "generic": {
        "capabilities": ["chat"],
        "prompt": None,
        "budget": 20000,
    },
}


def list_roles() -> list[str]:
    """Return the sorted list of built-in role names."""
    return sorted(BUILTIN_PRESETS.keys())


def get_preset(
    role: str,
    *,
    role_prompts: dict | None = None,
    role_budgets: dict | None = None,
) -> dict:
    """Return `{"capabilities", "prompt", "budget"}` for `role`.

    `role_prompts` / `role_budgets` (typically obtained from
    `load_user_overrides()`) override the built-in defaults per-key. A `None`
    value in the override mapping means "fall back to built-in" (so users can
    explicitly null out a key in their config without removing it).

    Unknown role -> ValueError.
    """
    if role not in BUILTIN_PRESETS:
        raise ValueError(
            f"unknown role {role!r}; known roles: {list_roles()}"
        )
    base = BUILTIN_PRESETS[role]
    # shallow copy of the per-role dict, copy the capabilities list so callers
    # can't mutate the module-level table.
    out: dict[str, Any] = {
        "capabilities": list(base["capabilities"]),
        "prompt": base["prompt"],
        "budget": base["budget"],
    }
    if role_prompts:
        v = role_prompts.get(role)
        if v is not None:
            out["prompt"] = v
    if role_budgets:
        v = role_budgets.get(role)
        if v is not None:
            out["budget"] = v
    return out


def _read_json_safe(path: pathlib.Path) -> dict:
    """Return the parsed JSON object at `path`, or `{}` if missing/unreadable.

    Never raises. Top-level non-dict -> `{}` (we only use object configs).
    """
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


def _deep_merge(base: dict, override: dict) -> dict:
    """Return base deep-merged with override (override wins per-key)."""
    out = dict(base)
    for k, v in override.items():
        if (
            k in out
            and isinstance(out[k], dict)
            and isinstance(v, dict)
        ):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_user_overrides() -> tuple[dict, dict]:
    """Load `(role_prompts, role_budgets)` from global + local config.

    Reads `~/.agent-hub/config.json` (global) and `<cwd>/.agent-hub.local.json`
    (local) and deep-merges them, with local taking precedence per-key.

    Missing files, unreadable files, and parse errors all degrade to empty
    dicts — never raises.
    """
    global_path = pathlib.Path.home() / ".agent-hub" / "config.json"
    local_path = pathlib.Path.cwd() / ".agent-hub.local.json"
    merged = _deep_merge(_read_json_safe(global_path), _read_json_safe(local_path))
    role_prompts = merged.get("role_prompts") or {}
    role_budgets = merged.get("role_budgets") or {}
    if not isinstance(role_prompts, dict):
        role_prompts = {}
    if not isinstance(role_budgets, dict):
        role_budgets = {}
    return role_prompts, role_budgets
