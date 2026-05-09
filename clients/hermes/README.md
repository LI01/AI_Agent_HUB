# Hermes Client

Connect Hermes (or any custom AI) to the Agent Hub.

## Usage

```bash
python agent.py --hub http://10.9.0.10:8080 --id hermes-1 --key YOUR-KEY
```

## Python Usage

```python
from agent import HermesAgentClient, hermes_handler

client = HermesAgentClient(
    hub_url="http://10.9.0.10:8080",
    agent_id="hermes-ai",
    capabilities=["code", "reasoning", "analysis", "math"],
    auth_token="YOUR-KEY",
    task_handler=hermes_handler
)
client.start()
```

## Customization

Edit `agent.py` to customize `hermes_handler` with your Hermes-specific logic:

```python
def hermes_handler(task: dict) -> dict:
    # Call Hermes API
    # response = hermes_client.execute(task)

    # Or use Hermes CLI
    # result = subprocess.run(["hermes", "run", task_text], ...)

    return {"result": "your result"}
```

## Capabilities

Define what Hermes can do:
- `code` — Code generation and editing
- `reasoning` — Logical reasoning
- `analysis` — Code analysis
- `math` — Mathematical reasoning
- Custom capabilities as needed