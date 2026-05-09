# Codex Client

Connect Codex CLI to the Agent Hub.

## Setup

```bash
# Install Codex if needed
# npm install -g @openai/codex

# Run the agent
cd /root/agent-hub/clients/codex
python agent.py --hub http://10.9.0.10:8080 --id codex-dev --key YOUR-KEY
```

## Usage

```python
from agent import codex_handler

client = HTTPAgentClient(
    hub_url="http://10.9.0.10:8080",
    agent_id="codex-laptop",
    capabilities=["code", "review", "refactor", "test"],
    auth_token="YOUR-KEY",
    task_handler=codex_handler
)
client.start()
```

## Capabilities

Codex can:
- Execute code in sandboxes
- Run terminal commands
- Read/write files
- Search code
- And more via its CLI

## Task Handler Example

```python
def codex_handler(task):
    import subprocess

    task_text = task.get("payload", {}).get("task", "")

    # Execute with Codex
    result = subprocess.run(
        ["codex", "exec", f"--prompt={task_text}", "--quiet"],
        capture_output=True,
        text=True
    )

    return {
        "output": result.stdout,
        "error": result.stderr,
        "returncode": result.returncode
    }
```