"""Capability primitives for Phase 2.2 (F3).

Versioned, schema-bearing capabilities + a tiny pure-Python JSON-Schema-subset
validator. Pure-Python by design (Fix F1 — no `jsonschema` dependency).

Supported validator keywords: `type`, `required`, `properties`, `items`,
`enum`, `additionalProperties`. Any other JSON Schema keyword is reported
deterministically as `unsupported_keyword: <name>` rather than silently
passing.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Optional, Union

from pydantic import BaseModel, Field

from .models import Agent, AgentStatus, Capability, Task


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PayloadSchemaError(Exception):
    """Raised in strict mode when payload validation fails.

    `detail` is a JSONable dict describing the failure (capability,
    capability_version, path, message).
    """

    def __init__(self, detail: dict):
        super().__init__(detail.get("message", "payload_schema_violation"))
        self.detail = detail


# ---------------------------------------------------------------------------
# Mode (env var, NOT cached at import time so tests can monkeypatch)
# ---------------------------------------------------------------------------

_VALID_MODES = {"off", "warn", "strict"}


def current_mode() -> str:
    """Read AGENT_HUB_VALIDATE_PAYLOAD on every call.

    Returns one of {"off", "warn", "strict"}. Default is "warn".
    Anything outside the valid set falls back to "warn".
    """
    raw = os.getenv("AGENT_HUB_VALIDATE_PAYLOAD", "warn").lower()
    return raw if raw in _VALID_MODES else "warn"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_capability(item: Union[str, dict, Capability]) -> Capability:
    """Coerce wire-shape capability (str | dict | Capability) → Capability."""
    if isinstance(item, Capability):
        return item
    if isinstance(item, str):
        return Capability(name=item, version=1, payload_schema=None, result_schema=None)
    if isinstance(item, dict):
        return Capability(**item)
    raise TypeError(f"capability must be str|dict|Capability, got {type(item)}")


def normalize_capabilities(items: list) -> list[Capability]:
    return [normalize_capability(x) for x in (items or [])]


def capability_to_dict(cap: Union[Capability, dict, str]) -> dict:
    """JSONable dict form of a Capability (or pass-through if already dict)."""
    if isinstance(cap, dict):
        return cap
    if isinstance(cap, Capability):
        return cap.model_dump(mode="json")
    if isinstance(cap, str):
        return {
            "name": cap,
            "version": 1,
            "payload_schema": None,
            "result_schema": None,
        }
    raise TypeError(f"capability must be Capability|dict|str, got {type(cap)}")


# ---------------------------------------------------------------------------
# Capability resolution (for routing + validation)
# ---------------------------------------------------------------------------

def _iter_normalized_caps(agent: Agent):
    """Yield Capability instances regardless of how Agent.capabilities is stored."""
    for cap in (agent.capabilities or []):
        if isinstance(cap, Capability):
            yield cap
        elif isinstance(cap, dict):
            try:
                yield Capability(**cap)
            except Exception:
                continue
        elif isinstance(cap, str):
            yield Capability(name=cap, version=1)
        # silently skip anything else


def _agent_can_handle(agent: Agent, task: Task) -> bool:
    """True iff `agent` advertises a capability matching `task.task_type` at
    version >= task.min_version. Empty capabilities = wildcard."""
    if not agent.capabilities:
        return True
    floor = getattr(task, "min_version", None) or 1
    target = task.task_type
    for cap in _iter_normalized_caps(agent):
        if cap.name == target and cap.version >= floor:
            return True
    return False


def _resolve_cap_version(agent: Agent, task_type: str, min_version: int = 1) -> Optional[int]:
    """Highest version >= min_version that `agent` advertises for `task_type`,
    or None if no match."""
    floor = min_version or 1
    best: Optional[int] = None
    for cap in _iter_normalized_caps(agent):
        if cap.name != task_type:
            continue
        if cap.version < floor:
            continue
        if best is None or cap.version > best:
            best = cap.version
    return best


def _resolve_payload_schema(agent: Agent, task_type: str, version: int) -> Optional[dict]:
    """Return the agent's payload_schema for the given (name, version), or
    None if the agent advertises that cap as bare (schema-less) or not at all."""
    for cap in _iter_normalized_caps(agent):
        if cap.name == task_type and cap.version == version:
            return cap.payload_schema
    return None


# ---------------------------------------------------------------------------
# Aggregation (GET /capabilities)
# ---------------------------------------------------------------------------

def _schema_key(schema: Optional[dict]) -> Optional[str]:
    if schema is None:
        return None
    return json.dumps(schema, sort_keys=True)


def _merge_bucket(name: str, version: int, entries: list) -> dict:
    """`entries` is a list of (agent_id, Capability) tuples. Per design §3.1.1:

    1. Any bare advertiser (payload_schema is None) → aggregate is null + conflict flag.
    2. All schemas byte-identical → that schema, no conflict flag.
    3. Otherwise → first-by-agent-id schema, conflict flag set.
    """
    # Sort by agent_id for deterministic first-by-agent-id selection
    entries_sorted = sorted(entries, key=lambda t: t[0])
    agent_ids = [aid for aid, _ in entries_sorted]
    agent_count = len(entries_sorted)

    def merge(field: str) -> tuple[Optional[dict], bool]:
        schemas = [getattr(cap, field) for _, cap in entries_sorted]
        if any(s is None for s in schemas):
            return None, True
        keys = {_schema_key(s) for s in schemas}
        if len(keys) == 1:
            return schemas[0], False
        return schemas[0], True

    payload, payload_conflict = merge("payload_schema")
    result, result_conflict = merge("result_schema")

    row = {
        "name": name,
        "version": version,
        "agent_count": agent_count,
        "agent_ids": agent_ids,
        "payload_schema": payload,
        "result_schema": result,
    }
    if payload_conflict or result_conflict:
        row["aggregated_schema_conflict"] = True
    return row


def aggregate_capabilities(agents: list[Agent]) -> list[dict]:
    """Group capabilities across non-offline agents. Sort name-asc, version-desc."""
    buckets: dict[tuple[str, int], list] = defaultdict(list)
    for agent in agents:
        if agent.status == AgentStatus.OFFLINE:
            continue
        for cap in _iter_normalized_caps(agent):
            buckets[(cap.name, cap.version)].append((agent.agent_id, cap))
    rows = [_merge_bucket(name, version, entries)
            for (name, version), entries in buckets.items()]
    rows.sort(key=lambda r: (r["name"], -r["version"]))
    return rows


# ---------------------------------------------------------------------------
# Pure-Python JSON Schema subset validator (Fix F1)
# ---------------------------------------------------------------------------

_SUPPORTED = {"type", "required", "properties", "items", "enum", "additionalProperties"}
_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _validate(value, schema, path: str = "") -> Optional[str]:
    """Pure-Python validator. Returns None on success, else short error string.

    Supports exactly: type, required, properties, items, enum, additionalProperties.
    Any other top-level keyword in `schema` returns
    "unsupported_keyword: <name>".
    """
    if not isinstance(schema, dict):
        return None

    # Deterministic unsupported-keyword check FIRST
    for kw in schema.keys():
        if kw not in _SUPPORTED:
            return f"unsupported_keyword: {kw}"

    # type
    if "type" in schema:
        type_name = schema["type"]
        expected = _TYPE_MAP.get(type_name)
        if expected is None:
            return f"unsupported_keyword: type_{type_name}"
        # bool is a subclass of int — distinguish
        if type_name == "integer" and isinstance(value, bool):
            return f"{path or '/'}: expected integer, got boolean"
        if type_name == "number" and isinstance(value, bool):
            return f"{path or '/'}: expected number, got boolean"
        if not isinstance(value, expected):
            return f"{path or '/'}: expected {type_name}"

    # enum
    if "enum" in schema:
        if value not in schema["enum"]:
            return f"{path or '/'}: value not in enum"

    # object-ish
    if isinstance(value, dict):
        for req_name in (schema.get("required") or []):
            if req_name not in value:
                return f"missing required field '{req_name}'"
        props = schema.get("properties") or {}
        addl = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                err = _validate(v, props[k], f"{path}/{k}")
                if err is not None:
                    return err
            else:
                if addl is False:
                    return f"{path}/{k}: additional property not allowed"
                if isinstance(addl, dict):
                    return "unsupported_keyword: additionalProperties_subschema"
                # True or omitted → ok

    # array-ish
    if isinstance(value, list) and "items" in schema:
        items_schema = schema["items"]
        if isinstance(items_schema, list):
            return "unsupported_keyword: items_tuple"
        for i, item in enumerate(value):
            err = _validate(item, items_schema, f"{path}/{i}")
            if err is not None:
                return err

    return None


def validate_payload(payload: dict, schema: Optional[dict]) -> Optional[str]:
    """Top-level helper: returns None on success, else a short error string.

    No-op when `schema` is None.
    """
    if schema is None:
        return None
    return _validate(payload, schema, "")


__all__ = [
    "Capability",
    "PayloadSchemaError",
    "aggregate_capabilities",
    "capability_to_dict",
    "current_mode",
    "normalize_capabilities",
    "normalize_capability",
    "validate_payload",
    "_agent_can_handle",
    "_resolve_cap_version",
    "_resolve_payload_schema",
    "_validate",
]
