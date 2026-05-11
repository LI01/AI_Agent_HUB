#!/usr/bin/env bash
# Install 3 systemd --user services (claude / codex / opencode adapters) that
# auto-register with an Agent Hub and restart on failure / boot.
#
# Designed for a Linux box where the hub repo is already cloned and a venv is
# set up. Run AS the user that owns the install (the services run as that user).
# Requires: passwordless sudo (for `loginctl enable-linger`).
#
# Required env:
#   AGENT_HUB_API_KEY   API key the agents register with (must have can_register).
#
# Optional env overrides (with defaults):
#   AGENT_HUB_URL       Hub URL.                                   default: http://10.9.0.10:8300
#   AGENT_HUB_REPO      Path to the AI_Agent_HUB checkout.         default: $HOME/work/AI_Agent_HUB
#   AGENT_HUB_VENV      Path to the venv that has hub deps.        default: $AGENT_HUB_REPO/.venv
#   AGENT_HUB_WORKDIRS  Where per-agent workdirs/logs live.        default: $HOME/work/agent-workdirs
#   AGENT_ID_SUFFIX     Tail appended to each agent_id.            default: $(hostname -s)
#   ROLE_CLAUDE         Role passed to claude adapter.             default: reviewer
#   ROLE_CODEX          Role passed to codex adapter.              default: designer
#   ROLE_OPENCODE       Role passed to opencode adapter.           default: tester
#   EXTRA_PATH          Extra dirs prepended to the service PATH.  default: $HOME/.local/bin:$HOME/.opencode/bin
#
# After install, manage with:
#   systemctl --user status {claude,codex,opencode}-agent
#   journalctl --user -u <name>-agent -f
#   systemctl --user restart <name>-agent
set -euo pipefail

: "${AGENT_HUB_API_KEY:?Set AGENT_HUB_API_KEY=... before running this script.}"

HUB=${AGENT_HUB_URL:-http://10.9.0.10:8300}
REPO=${AGENT_HUB_REPO:-$HOME/work/AI_Agent_HUB}
VENV=${AGENT_HUB_VENV:-$REPO/.venv}
WORKDIRS=${AGENT_HUB_WORKDIRS:-$HOME/work/agent-workdirs}
ID_SUFFIX=${AGENT_ID_SUFFIX:-$(hostname -s)}
ROLE_CLAUDE=${ROLE_CLAUDE:-reviewer}
ROLE_CODEX=${ROLE_CODEX:-designer}
ROLE_OPENCODE=${ROLE_OPENCODE:-tester}
EXTRA_PATH=${EXTRA_PATH:-$HOME/.local/bin:$HOME/.opencode/bin}

# Sanity: repo + venv + adapters present.
[[ -d "$REPO" ]] || { echo "AGENT_HUB_REPO not found: $REPO" >&2; exit 1; }
[[ -x "$VENV/bin/python" ]] || { echo "venv python not found: $VENV/bin/python" >&2; exit 1; }
"$VENV/bin/python" -c "
import sys; sys.path.insert(0, '$REPO')
from clients.claude_code import llm_agent
from clients.codex import llm_agent as _c
from clients.opencode import llm_agent as _o
" || { echo "venv missing adapter deps" >&2; exit 1; }

UNIT_DIR=$HOME/.config/systemd/user
ENV_FILE=$UNIT_DIR/agent-hub.env
mkdir -p "$UNIT_DIR" "$WORKDIRS"

# Per-agent workdirs.
CLAUDE_ID=claude-$ROLE_CLAUDE-$ID_SUFFIX
CODEX_ID=codex-$ROLE_CODEX-$ID_SUFFIX
OPENCODE_ID=opencode-$ROLE_OPENCODE-$ID_SUFFIX
for id in "$CLAUDE_ID" "$CODEX_ID" "$OPENCODE_ID"; do
    mkdir -p "$WORKDIRS/$id"
done

# Shared env file, mode 0600. The API key never enters argv.
umask 077
cat > "$ENV_FILE" <<EOF
AGENT_HUB_API_KEY=$AGENT_HUB_API_KEY
AGENT_HUB_URL=$HUB
EOF
chmod 0600 "$ENV_FILE"

COMMON_PATH=$EXTRA_PATH:/usr/local/bin:/usr/bin:/bin

write_unit() {
    local cli=$1 module=$2 role=$3 id=$4
    cat > "$UNIT_DIR/$cli-agent.service" <<UNIT
[Unit]
Description=Agent Hub $cli agent ($role) — $id
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO
EnvironmentFile=$ENV_FILE
Environment=PYTHONPATH=$REPO
Environment=PATH=$COMMON_PATH
ExecStart=$VENV/bin/python -m clients.$module.llm_agent --hub $HUB --id $id --role $role --workdir $WORKDIRS/$id
Restart=on-failure
RestartSec=10s
StandardOutput=append:$WORKDIRS/$id/stdout.log
StandardError=append:$WORKDIRS/$id/stderr.log

[Install]
WantedBy=default.target
UNIT
}

write_unit claude   claude_code "$ROLE_CLAUDE"   "$CLAUDE_ID"
write_unit codex    codex       "$ROLE_CODEX"    "$CODEX_ID"
write_unit opencode opencode    "$ROLE_OPENCODE" "$OPENCODE_ID"

echo "Files written to $UNIT_DIR:"
ls -la "$UNIT_DIR/" | grep -E "(agent-hub\.env|claude-agent|codex-agent|opencode-agent)"

# Enable lingering so the user manager starts at boot (no login required).
sudo loginctl enable-linger "$(whoami)"
echo "lingering: $(loginctl show-user "$(whoami)" --property=Linger --value)"

systemctl --user daemon-reload
for cli in claude codex opencode; do
    systemctl --user enable --now "$cli-agent.service"
done

sleep 3
echo ""
echo "=== Service status ==="
for cli in claude codex opencode; do
    echo "--- $cli-agent ---"
    systemctl --user status "$cli-agent.service" --no-pager -n 5 | head -12
    echo ""
done
