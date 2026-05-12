"""Cost / token extraction from agent task results.

Pure functions — never raise. See `Agent-comm/design/phase-user-tokens/design_v3.md`
§2.7 for the contract.
"""

from __future__ import annotations

import json
from typing import Optional


_EMPTY = {
    "cost_usd": None,
    "input_tokens": None,
    "output_tokens": None,
    "web_search_requests": None,
}


def cli_from_agent_id(agent_id: Optional[str]) -> Optional[str]:
    """Map an agent_id prefix to a CLI label.

    Mirrors the dashboard's `cliBadge` convention. Returns None for unknown
    prefixes or `None` input.
    """
    if not isinstance(agent_id, str):
        return None
    if agent_id.startswith("claude"):
        return "claude"
    if agent_id.startswith("opencode"):
        return "opencode"
    if agent_id.startswith("codex"):
        return "codex"
    return None


def _coerce_to_dict(result):
    """Best-effort: dict -> dict, JSON str -> dict, anything else -> None."""
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _extract_claude(result_dict: dict) -> dict:
    # Per design §2.7: tolerate both `result["raw"] = {...}` (SDK-wrapped) and
    # flat `result = {total_cost_usd: ...}` shapes. Per reviewer fix:
    # use `result.get("raw") or result` so an empty/falsey `raw` falls back to
    # the top-level Claude payload instead of yielding all-None.
    src = result_dict.get("raw") or result_dict
    if not isinstance(src, dict):
        src = result_dict

    out = dict(_EMPTY)
    try:
        cost = src.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            out["cost_usd"] = float(cost)
    except (AttributeError, TypeError):
        pass

    usage = src.get("usage") if isinstance(src, dict) else None
    if isinstance(usage, dict):
        it = usage.get("input_tokens")
        if isinstance(it, (int, float)):
            out["input_tokens"] = int(it)
        ot = usage.get("output_tokens")
        if isinstance(ot, (int, float)):
            out["output_tokens"] = int(ot)
        stu = usage.get("server_tool_use")
        if isinstance(stu, dict):
            wsr = stu.get("web_search_requests")
            if isinstance(wsr, (int, float)):
                out["web_search_requests"] = int(wsr)
    return out


def _extract_opencode(result_dict: dict) -> dict:
    # Per design §2.7: opencode emits `raw_events` at the TOP LEVEL of result,
    # not nested under `result["raw"]`. The v1 bug was reading `result["raw"]`.
    events = result_dict.get("raw_events")
    if not isinstance(events, list):
        return dict(_EMPTY)

    cost_total = 0.0
    cost_seen = False
    in_total = 0
    out_total = 0
    input_seen = False
    output_seen = False

    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "step_finish":
            continue
        c = ev.get("cost")
        if isinstance(c, (int, float)):
            cost_total += float(c)
            cost_seen = True
        part = ev.get("part")
        if isinstance(part, dict):
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                it = tokens.get("input")
                if isinstance(it, (int, float)):
                    in_total += int(it)
                    input_seen = True
                ot = tokens.get("output")
                if isinstance(ot, (int, float)):
                    out_total += int(ot)
                    output_seen = True

    return {
        "cost_usd": cost_total if cost_seen else None,
        "input_tokens": in_total if input_seen else None,
        "output_tokens": out_total if output_seen else None,
        "web_search_requests": None,
    }


def cost_extract(result, cli: Optional[str]) -> dict:
    """Extract cost + tokens from an agent's reported result dict.

    Returns a dict with keys `cost_usd`, `input_tokens`, `output_tokens`,
    `web_search_requests`. Any value may be None when unavailable.

    Never raises — malformed input yields the all-None dict.
    """
    try:
        if cli not in ("claude", "opencode", "codex"):
            return dict(_EMPTY)
        if cli == "codex":
            # codex doesn't emit cost / token info via the result.
            return dict(_EMPTY)

        result_dict = _coerce_to_dict(result)
        if result_dict is None:
            return dict(_EMPTY)

        if cli == "claude":
            return _extract_claude(result_dict)
        if cli == "opencode":
            return _extract_opencode(result_dict)
    except Exception:
        return dict(_EMPTY)

    return dict(_EMPTY)
