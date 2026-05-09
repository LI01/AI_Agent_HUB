"""Standalone stdio MCP server for Agent Hub.

Usage:
    AGENT_HUB_URL=http://127.0.0.1:8080 AGENT_HUB_API_KEY=... python -m hub.mcp
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .mcp_protocol import HttpHubBackend, handle_jsonrpc


async def _stdio_loop(backend: HttpHubBackend) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            response = await handle_jsonrpc(payload, backend)
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent Hub MCP stdio server")
    parser.add_argument("--hub", default=os.getenv("AGENT_HUB_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--key", default=os.getenv("AGENT_HUB_API_KEY"))
    args = parser.parse_args()

    if not args.key:
        print("AGENT_HUB_API_KEY or --key is required", file=sys.stderr)
        return 2

    asyncio.run(_stdio_loop(HttpHubBackend(args.hub, args.key)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
