"""Minimal MCP JSON-RPC tool surface for Agent Hub.

The implementation is intentionally small and dependency-free. It follows the
MCP JSON-RPC method shape for initialize, tools/list, and tools/call while
keeping one tool registry usable by stdio and embedded HTTP transports.

This is a deliberate hand-rolled MCP subset for Phase 1.5: `initialize`,
`tools/list`, `tools/call`, and `ping`. If the MCP surface grows beyond basic
hub tool exposure, replacing this module with a Python MCP SDK should be treated
as the next design step.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx
from fastapi import HTTPException


JSON = dict[str, Any]


class McpToolError(Exception):
    def __init__(self, message: str, code: int = -32000, data: Optional[JSON] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


def _object_schema(properties: JSON, required: list[str] | None = None) -> JSON:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}
OBJECT = {"type": "object", "additionalProperties": True}
STRING_LIST = {"type": "array", "items": STRING}

# Phase 2.2 (§3.4 + §5 row 8): capabilities accept bare strings OR object
# capability descriptors. Backward-compatible with Phase 1 string-only form.
CAPABILITY_ITEM = {
    "oneOf": [
        {"type": "string"},
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "version": {"type": "integer", "minimum": 1},
                "payload_schema": {"type": "object"},
                "result_schema": {"type": "object"},
            },
            "required": ["name"],
        },
    ]
}
CAPABILITY_LIST = {"type": "array", "items": CAPABILITY_ITEM}

# Phase 2.2: list-capabilities output row schema. Documented in design §3.1.
CAPABILITY_ROW = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "version": {"type": "integer", "minimum": 1},
        "payload_schema": {"type": ["object", "null"]},
        "result_schema": {"type": ["object", "null"]},
        "agent_count": {"type": "integer", "minimum": 0},
        "aggregated_schema_conflict": {"type": "boolean"},
    },
    "required": ["name", "version", "agent_count"],
    "additionalProperties": True,
}
CAPABILITY_ROWS = {"type": "array", "items": CAPABILITY_ROW}


TOOLS: dict[str, JSON] = {
    "list-agents": {
        "description": "List registered agents visible to the current key.",
        "inputSchema": _object_schema({}),
    },
    "get-agent": {
        "description": "Get one registered agent by id.",
        "inputSchema": _object_schema({"agent_id": STRING}, ["agent_id"]),
    },
    "register-agent": {
        "description": "Register or update an agent.",
        "inputSchema": _object_schema({
            "agent_id": STRING,
            "capabilities": CAPABILITY_LIST,
            "name": STRING,
            "description": STRING,
            "owner": STRING,
            "machine_info": OBJECT,
        }, ["agent_id"]),
    },
    "unregister-agent": {
        "description": "Unregister an agent by id.",
        "inputSchema": _object_schema({"agent_id": STRING}, ["agent_id"]),
    },
    "submit-task": {
        "description": "Submit a task. target_agent is optional.",
        "inputSchema": _object_schema({
            "task": STRING,
            "task_type": STRING,
            "payload": OBJECT,
            "target_agent": STRING,
            "priority": INTEGER,
            "timeout": INTEGER,
            "parent_task_id": STRING,
            "min_version": {"type": "integer", "minimum": 1, "default": 1},
        }),
    },
    "list-capabilities": {
        "description": "List capabilities aggregated across online agents.",
        "inputSchema": _object_schema({}),
        "outputSchema": CAPABILITY_ROWS,
    },
    "get-task": {
        "description": "Get task status, result, logs, and metadata.",
        "inputSchema": _object_schema({"task_id": STRING}, ["task_id"]),
    },
    "list-tasks": {
        "description": "List tasks, optionally filtered by status or agent_id.",
        "inputSchema": _object_schema({"status": STRING, "agent_id": STRING}),
    },
    "stats": {
        "description": "Return aggregate hub stats.",
        "inputSchema": _object_schema({}),
    },
    "health": {
        "description": "Return hub health.",
        "inputSchema": _object_schema({}),
    },
    "create-api-key": {
        "description": "Create an API key. Requires admin.",
        "inputSchema": _object_schema({
            "name": STRING,
            "can_register": BOOLEAN,
            "can_assign_tasks": BOOLEAN,
            "can_view_agents": BOOLEAN,
            "can_view_tasks": BOOLEAN,
            "allowed_agents": STRING_LIST,
            "is_admin": BOOLEAN,
            "expires_days": INTEGER,
        }, ["name"]),
    },
    "list-api-keys": {
        "description": "List API key metadata. Requires admin.",
        "inputSchema": _object_schema({}),
    },
    "revoke-api-key": {
        "description": "Revoke an API key by name. Requires admin.",
        "inputSchema": _object_schema({"name": STRING}, ["name"]),
    },
}


def list_tool_definitions() -> list[JSON]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
            "outputSchema": spec.get(
                "outputSchema", {"type": "object", "additionalProperties": True}
            ),
        }
        for name, spec in TOOLS.items()
    ]


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


class HubBackend(ABC):
    @abstractmethod
    async def request(self, method: str, path: str, body: Optional[JSON] = None, params: Optional[JSON] = None) -> Any:
        raise NotImplementedError


class HttpHubBackend(HubBackend):
    """Backend used by standalone stdio and compatibility CLI mode."""

    def __init__(self, hub_url: str, api_key: str):
        self.hub_url = hub_url.rstrip("/")
        self.api_key = api_key

    async def request(self, method: str, path: str, body: Optional[JSON] = None, params: Optional[JSON] = None) -> Any:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(
                method,
                f"{self.hub_url}{path}",
                json=body,
                params=params,
                headers=headers,
            )
        if response.status_code >= 400:
            raise McpToolError(_http_error_message(response), code=-32001)
        return response.json()


class InProcessHubBackend(HubBackend):
    """Backend used by embedded /mcp. Calls the FastAPI handlers directly."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    @property
    def _authorization(self) -> str:
        return f"Bearer {self.api_key}"

    async def request(self, method: str, path: str, body: Optional[JSON] = None, params: Optional[JSON] = None) -> Any:
        from . import main
        from .models import RegisterRequest, TaskSubmitRequest

        try:
            if method == "GET" and path == "/agents":
                return main.list_agents(self._authorization)
            if method == "GET" and path == "/capabilities":
                return main.list_capabilities(self._authorization)
            if method == "GET" and path.startswith("/agents/"):
                return main.get_agent(path.rsplit("/", 1)[-1], self._authorization)
            if method == "POST" and path == "/register":
                return await main.register_agent(RegisterRequest(**(body or {})), self._authorization)
            if method == "POST" and path == "/unregister":
                return main.unregister_agent((body or {})["agent_id"], self._authorization)
            if method == "POST" and path == "/tasks":
                return await main.submit_task(TaskSubmitRequest(**(body or {})), self._authorization)
            if method == "GET" and path.startswith("/tasks/"):
                return main.get_task(path.rsplit("/", 1)[-1], self._authorization)
            if method == "GET" and path == "/tasks":
                params = params or {}
                return main.list_tasks(params.get("status"), params.get("agent_id"), self._authorization)
            if method == "GET" and path == "/stats":
                return main.get_stats(self._authorization)
            if method == "GET" and path == "/health":
                return main.health()
            if method == "POST" and path == "/admin/keys":
                return main.create_api_key(main.CreateKeyRequest(**(body or {})), self._authorization)
            if method == "GET" and path == "/admin/keys":
                return main.list_api_keys(self._authorization)
            if method == "DELETE" and path.startswith("/admin/keys/"):
                return main.revoke_api_key(path.rsplit("/", 1)[-1], self._authorization)
        except HTTPException as exc:
            # Phase 2.2 §3.4: payload_schema_violation arrives as a dict detail
            # from /tasks strict-mode preflight. Surface it as Invalid params
            # (-32602) with the dict carried in error.data so MCP clients can
            # see capability/version/message. Other errors stay -32001.
            detail = exc.detail
            if (
                exc.status_code == 400
                and isinstance(detail, dict)
                and detail.get("error") == "payload_schema_violation"
            ):
                raise McpToolError(
                    detail.get("message") or "payload_schema_violation",
                    code=-32602,
                    data=detail,
                ) from exc
            if isinstance(detail, dict):
                # Generic dict detail: preserve structure under data, message
                # falls back to a stable string form.
                raise McpToolError(
                    str(detail.get("message") or detail.get("error") or detail),
                    code=-32001,
                    data=detail,
                ) from exc
            raise McpToolError(str(detail), code=-32001) from exc
        except KeyError as exc:
            raise McpToolError(f"missing required field: {exc.args[0]}", code=-32602) from exc
        raise McpToolError(f"unsupported backend request {method} {path}", code=-32603)


def _http_error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail or f"HTTP {response.status_code}")


async def call_tool(backend: HubBackend, name: str, arguments: Optional[JSON] = None) -> Any:
    if name not in TOOLS:
        raise McpToolError(f"unknown tool: {name}", code=-32602)
    args = arguments or {}

    if name == "list-agents":
        return await backend.request("GET", "/agents")
    if name == "list-capabilities":
        # Phase 2.2 §3.4: returns the top-level array directly (Fix F6).
        return await backend.request("GET", "/capabilities")
    if name == "get-agent":
        return await backend.request("GET", f"/agents/{args['agent_id']}")
    if name == "register-agent":
        body = {
            "agent_id": args["agent_id"],
            "capabilities": args.get("capabilities") or [],
        }
        for key in ("name", "description", "owner", "machine_info"):
            if key in args:
                body[key] = args[key]
        return await backend.request("POST", "/register", body=body)
    if name == "unregister-agent":
        return await backend.request("POST", "/unregister", body={"agent_id": args["agent_id"]})
    if name == "submit-task":
        return await backend.request("POST", "/tasks", body={k: v for k, v in args.items() if v is not None})
    if name == "get-task":
        return await backend.request("GET", f"/tasks/{args['task_id']}")
    if name == "list-tasks":
        return await backend.request("GET", "/tasks", params={k: v for k, v in args.items() if v is not None})
    if name == "stats":
        return await backend.request("GET", "/stats")
    if name == "health":
        return await backend.request("GET", "/health")
    if name == "create-api-key":
        return await backend.request("POST", "/admin/keys", body={k: v for k, v in args.items() if v is not None})
    if name == "list-api-keys":
        return await backend.request("GET", "/admin/keys")
    if name == "revoke-api-key":
        return await backend.request("DELETE", f"/admin/keys/{args['name']}")

    raise McpToolError(f"unimplemented tool: {name}", code=-32603)


async def handle_jsonrpc(payload: Any, backend: HubBackend) -> Any:
    if isinstance(payload, list):
        responses = [await _handle_one(item, backend) for item in payload]
        return [response for response in responses if response is not None]
    return await _handle_one(payload, backend)


async def _handle_one(message: Any, backend: HubBackend) -> Optional[JSON]:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid JSON-RPC request")

    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    is_notification = "id" not in message

    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-hub", "version": "1.5"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": list_tool_definitions()}
        elif method == "tools/call":
            if not isinstance(params, dict):
                raise McpToolError("tools/call params must be an object", code=-32602)
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            if not tool_name:
                raise McpToolError("tools/call requires name", code=-32602)
            value = _to_jsonable(await call_tool(backend, tool_name, arguments))
            result = {
                "content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}],
                "structuredContent": value,
                "isError": False,
            }
        elif method and method.startswith("notifications/"):
            return None
        else:
            raise McpToolError(f"method not found: {method}", code=-32601)
    except McpToolError as exc:
        if is_notification:
            return None
        return _error(request_id, exc.code, exc.message, data=exc.data)
    except Exception as exc:  # noqa: BLE001 - protocol boundary
        if is_notification:
            return None
        return _error(request_id, -32603, str(exc))

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str, data: Optional[JSON] = None) -> JSON:
    error: JSON = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


async def invoke_tool(backend: HubBackend, name: str, arguments: Optional[JSON] = None) -> Any:
    return _to_jsonable(await call_tool(backend, name, arguments or {}))
