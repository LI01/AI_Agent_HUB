# OpenClaw Client

Connect OpenClaw to the Agent Hub.

## Setup

```bash
# Run the agent
cd /root/agent-hub/clients/openclaw
python agent.py --hub http://10.9.0.10:8080 --id openclaw-dev --key YOUR-KEY
```

## Usage

```python
from agent import OpenClawAgent, openclaw_handler

client = OpenClawAgent(
    hub_url="http://10.9.0.10:8080",
    agent_id="openclaw-laptop",
    auth_token="YOUR-KEY"
)
client.start()
```

## Capabilities

OpenClaw can:
- `glob` — Find files by pattern
- `grep` — Search file contents
- `read` — Read files
- `write` — Write files
- `edit` — Edit files
- `bash` — Run shell commands
- `code` — Code execution

## Task Handler Example

```python
from openclaw import tools

def openclaw_handler(task: dict) -> dict:
    task_text = task.get("payload", {}).get("task", "")

    # Use OpenClaw tools
    files = tools.glob.glob("**/*.py")

    # Run commands
    result = tools.bash.run_command("echo hello")

    return {
        "files_found": len(files),
        "command_output": result
    }
```