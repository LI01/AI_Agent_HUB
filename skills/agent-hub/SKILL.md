# Agent Hub

Configure an MCP-aware client to use Agent Hub tools.

This skill is a convenience layer on top of the Agent Hub MCP server. It does
not add tool operations beyond the MCP tools exposed by the hub.

## Usage

```bash
/agent-hub setup --hub http://localhost:8080 --key <api-key>
```

## Behavior

1. Register the Agent Hub MCP server in the host client's MCP configuration.
2. Persist the hub URL and API key using the host client's config mechanism.
3. Run a smoke `list-agents` check.

The raw API key must not be printed or logged.

## MCP Server

Local stdio:

```bash
AGENT_HUB_URL=http://localhost:8080 AGENT_HUB_API_KEY=<api-key> python -m hub.mcp
```

Remote Streamable HTTP endpoint:

```text
POST http://localhost:8080/mcp
Authorization: Bearer <api-key>
```

## Tools

The skill should expose only the MCP tools implemented by the hub, including
`list-agents`, `submit-task`, `get-task`, `list-tasks`, `stats`, and `health`.
