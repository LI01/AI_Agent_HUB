# OpenCode Client

OpenCode connects to the hub via MCP or directly as an agent.

## Setup via MCP

```bash
opencode mcp add agent-hub http://hub:8080 --scope local --transport http \
  --header "Authorization: Bearer YOUR-API-KEY"
```

## Agent Mode (acts as agent)

Use the skill to connect OpenCode as an agent:

```bash
# Load the skill in OpenCode
/agent-connect --hub http://10.9.0.10:8080 --agent-id opencode-machine-1
```

Or use Python:

```python
import sys
sys.path.insert(0, "/root/agent-hub/skills/agent-hub-connect")

from agent import connect_to_hub

def opencode_handler(task):
    # Execute task using OpenCode's capabilities
    # - read/write files
    # - run bash commands
    # - search code
    # - etc.
    return {"status": "executed", "result": "task done"}

hub = connect_to_hub(
    hub_url="http://10.9.0.10:8080",
    agent_id="opencode-laptop",
    capabilities=["code", "search", "bash", "read", "write", "edit"],
    task_handler=opencode_handler
)
```

## Capabilities

- File operations (read, write, glob, grep)
- Bash command execution
- Code search and analysis
- Git operations
- And more via OpenCode tools