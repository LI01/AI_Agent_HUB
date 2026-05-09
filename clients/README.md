# Agent Clients

This directory contains client implementations for different AI coding tools to connect to the Agent Hub.

## Supported Agents

| Agent | Protocol | Setup |
|-------|----------|-------|
| Claude Code | MCP | `claude mcp add ...` |
| OpenCode | MCP | `opencode mcp add ...` |
| Codex | HTTP | `python codex/agent.py ...` |
| Hermes | HTTP | `python hermes/agent.py ...` |
| OpenClaw | HTTP | `python openclaw/agent.py ...` |

## Quick Start

### 1. Start the Hub

```bash
cd /root/agent-hub
PYTHONPATH=/root/agent-hub python -m uvicorn hub.main:app --port 8080
```

### 2. Start an Agent

Pick your agent:

```bash
# Codex
python clients/codex/agent.py --hub http://localhost:8080 --id my-codex --key YOUR-KEY

# Hermes
python clients/hermes/agent.py --hub http://localhost:8080 --id my-hermes --key YOUR-KEY

# OpenClaw
python clients/openclaw/agent.py --hub http://localhost:8080 --id my-openclaw --key YOUR-KEY
```

### 3. Submit a Task

```bash
# Via MCP (Claude Code / OpenCode)
claude mcp get agent-hub submit-task --task "hello from codex"

# Via REST
curl -X POST http://localhost:8080/tasks \
  -H "Authorization: Bearer YOUR-KEY" \
  -d '{"task": "echo hello", "target_agent": "my-codex"}'
```

## Client Architecture

```
Agent Client
    │
    ├── HTTPAgentClient (base)
    │   ├── register() → POST /register
    │   ├── heartbeat() → POST /heartbeat
    │   └── poll() → GET /tasks
    │
    └── WebSocketAgentClient (future)
        ├── connect() → WS /ws
        └── receive tasks in real-time
```

## Adding a New Agent

1. Create a new directory under `clients/`
2. Copy `base.py` or extend `HTTPAgentClient`
3. Implement `task_handler(task) → result`
4. Define capabilities

```python
from base import HTTPAgentClient

def my_handler(task):
    # Your agent logic
    return {"result": "done"}

client = HTTPAgentClient(
    hub_url="http://hub:8080",
    agent_id="my-agent",
    capabilities=["code", "search"],
    auth_token="YOUR-KEY",
    task_handler=my_handler
)
client.start()
```

## Auth Token

Get an API key from the hub:

```bash
# Use seed key
curl -X POST http://localhost:8080/admin/keys \
  -H "Authorization: Bearer agent-hub-seed-admin-key-12345" \
  -d '{"name": "my-agent-key", "can_register": true, "can_assign_tasks": true}'
```

Then use the returned key for your agent.

## Capabilities

Define what your agent can do:

| Capability | Description |
|------------|-------------|
| `code` | Code generation/editing |
| `read` | Read files |
| `write` | Write files |
| `edit` | Edit files |
| `glob` | Find files |
| `grep` | Search content |
| `bash` | Run commands |
| `search` | Web search |
| `test` | Run tests |
| `deploy` | Deploy code |

Add custom capabilities as needed.