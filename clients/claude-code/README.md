# Claude Code Client

Claude Code connects to the hub via MCP (Model Context Protocol).

## Setup

1. Add hub as MCP server:
```bash
claude mcp add agent-hub http://hub:8080 --scope local --transport http \
  --header "Authorization: Bearer YOUR-API-KEY"
```

2. Use MCP tools to interact:
```bash
claude mcp get agent-hub list-agents
claude mcp get agent-hub submit-task --task "your task"
```

## Alternative: Direct Python Client

```python
import sys
sys.path.insert(0, "/root/agent-hub/clients")

from base import HTTPAgentClient

def claude_handler(task):
    # This would call Claude Code CLI
    return {"status": "executed", "agent": "claude-code"}

client = HTTPAgentClient(
    hub_url="http://10.9.0.10:8080",
    agent_id="claude-code-desktop",
    capabilities=["code", "search", "bash", "read", "write"],
    auth_token="YOUR-KEY",
    task_handler=claude_handler
)
client.start()
```

## Capabilities

Claude Code can execute:
- File operations (read, write, edit)
- Shell commands
- Code search and analysis
- Git operations
- And more via available tools