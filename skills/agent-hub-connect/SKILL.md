# Agent Hub Connect

Compatibility note: this Phase 1 skill is a WebSocket connector for running an
OpenCode process as an agent. For MCP client onboarding and human task
submission, use the Phase 1.5 `skills/agent-hub/` skill instead.

Connect OpenCode to the Agent Hub as an agent. Enables receiving tasks from the hub and executing them.

## Usage

```
/agent-connect --hub http://10.9.0.10:8080 --agent-id opencode-laptop
```

## Description

This skill connects OpenCode to the Agent Hub via WebSocket. Once connected:

1. Registers with hub using the specified agent_id
2. Maintains persistent WebSocket connection
3. Receives tasks pushed from hub
4. Executes tasks using OpenCode's tools
5. Reports results back to hub

## Parameters

- `--hub` — Hub URL (required)
- `--agent-id` — Unique identifier for this agent (default: hostname)
- `--capabilities` — What this agent can do (comma-separated)

## Connection Flow

1. OpenCode loads this skill
2. Skill establishes WebSocket to hub
3. Sends: `{"type": "register", "agent_id": "...", "capabilities": [...]}`
4. Hub confirms: `{"type": "registered"}`
5. Connection persists — hub pushes tasks anytime
6. On task: execute → report result

## Example

```bash
/agent-connect --hub http://10.9.0.10:8080 --agent-id opencode-machine-1 --capabilities code,search,test
```
