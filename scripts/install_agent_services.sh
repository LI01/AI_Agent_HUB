#!/usr/bin/env bash
# Install systemd --user services for one Agent Hub adapter per (CLI × role)
# pair. Default: 3 CLIs × 7 built-in roles = 21 services. Each auto-registers
# with the hub, restarts on failure, and survives reboot via user lingering.
#
# Designed for a Linux box where the hub repo is already cloned and a venv is
# set up. Run AS the user that owns the install (the services run as that user).
# Requires: passwordless sudo (for `loginctl enable-linger`), bash 4+.
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
#   CLIS                Space-separated CLIs to install adapters for.
#                       default: "claude codex opencode"
#   ROLES               Space-separated roles to install per CLI.
#                       default: "pm architect designer coder reviewer tester generic"
#   EXTRA_PATH          Extra dirs prepended to the service PATH.  default: $HOME/.local/bin:$HOME/.opencode/bin
#
# Unit naming:    <cli>-<role>-agent.service
# Agent IDs:      <cli>-<role>-<AGENT_ID_SUFFIX>
# Workdirs:       $AGENT_HUB_WORKDIRS/<agent_id>/
#
# Day-2 ops:
#   systemctl --user status <cli>-<role>-agent
#   journalctl --user -u <cli>-<role>-agent -f
#   systemctl --user restart <cli>-<role>-agent
#
# Re-running this script is idempotent: existing units are overwritten and
# legacy single-CLI units (claude-agent / codex-agent / opencode-agent from
# the prior naming scheme) are bootout + removed.
set -euo pipefail

: "${AGENT_HUB_API_KEY:?Set AGENT_HUB_API_KEY=... before running this script.}"

HUB=${AGENT_HUB_URL:-http://10.9.0.10:8300}
REPO=${AGENT_HUB_REPO:-$HOME/work/AI_Agent_HUB}
VENV=${AGENT_HUB_VENV:-$REPO/.venv}
WORKDIRS=${AGENT_HUB_WORKDIRS:-$HOME/work/agent-workdirs}
ID_SUFFIX=${AGENT_ID_SUFFIX:-$(hostname -s)}
CLIS=${CLIS:-claude codex opencode}
ROLES=${ROLES:-pm architect designer coder reviewer tester generic}
EXTRA_PATH=${EXTRA_PATH:-$HOME/.local/bin:$HOME/.opencode/bin}

# CLI -> python module under clients/<module>/llm_agent.py
declare -A CLI_MODULE=(
    [claude]=claude_code
    [codex]=codex
    [opencode]=opencode
)

# Sanity: repo + venv + adapters present.
[[ -d "$REPO" ]] || { echo "AGENT_HUB_REPO not found: $REPO" >&2; exit 1; }
[[ -x "$VENV/bin/python" ]] || { echo "venv python not found: $VENV/bin/python" >&2; exit 1; }
"$VENV/bin/python" -c "
import sys; sys.path.insert(0, '$REPO')
from clients.claude_code import llm_agent
from clients.codex import llm_agent as _c
from clients.opencode import llm_agent as _o
" || { echo "venv missing adapter deps" >&2; exit 1; }

# Sanity: every requested CLI maps to a known module + the binary is on PATH.
for cli in $CLIS; do
    [[ -n "${CLI_MODULE[$cli]:-}" ]] || { echo "unknown CLI: $cli (no module mapping)" >&2; exit 1; }
    PATH="$EXTRA_PATH:$PATH" command -v "$cli" >/dev/null || {
        echo "warn: $cli binary not found on PATH ($EXTRA_PATH:\$PATH); adapter will fail on first task" >&2
    }
done

UNIT_DIR=$HOME/.config/systemd/user
ENV_FILE=$UNIT_DIR/agent-hub.env
mkdir -p "$UNIT_DIR" "$WORKDIRS"

# Shared env file, mode 0600. The API key never enters argv.
umask 077
cat > "$ENV_FILE" <<EOF
AGENT_HUB_API_KEY=$AGENT_HUB_API_KEY
AGENT_HUB_URL=$HUB
EOF
chmod 0600 "$ENV_FILE"

COMMON_PATH=$EXTRA_PATH:/usr/local/bin:/usr/bin:/bin

# Migration: bootout + remove legacy single-CLI units from the prior naming
# scheme (claude-agent / codex-agent / opencode-agent), if present. The new
# scheme always includes the role.
for legacy in claude-agent codex-agent opencode-agent; do
    legacy_unit="$UNIT_DIR/$legacy.service"
    if [[ -f "$legacy_unit" ]]; then
        echo "[migrate] removing legacy unit: $legacy.service"
        systemctl --user disable --now "$legacy.service" 2>/dev/null || true
        rm -f "$legacy_unit"
    fi
done

# Build (cli, role) matrix and write one unit per pair.
UNITS=()
for cli in $CLIS; do
    module=${CLI_MODULE[$cli]}
    for role in $ROLES; do
        id="$cli-$role-$ID_SUFFIX"
        unit="$cli-$role-agent.service"
        mkdir -p "$WORKDIRS/$id"
        cat > "$UNIT_DIR/$unit" <<UNIT
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
        UNITS+=("$unit")
    done
done

echo "Wrote ${#UNITS[@]} unit(s) to $UNIT_DIR."

# Enable lingering so the user manager starts at boot (no login required).
sudo loginctl enable-linger "$(whoami)"
echo "lingering: $(loginctl show-user "$(whoami)" --property=Linger --value)"

systemctl --user daemon-reload
# `enable --now` is idempotent and won't restart already-running units, so a
# re-run with changed ExecStart args (e.g. new --id) wouldn't take effect.
# Enable + explicitly restart so unit edits always apply.
for unit in "${UNITS[@]}"; do
    systemctl --user enable "$unit" >/dev/null
    systemctl --user restart "$unit"
done

sleep 3
ACTIVE=0
for unit in "${UNITS[@]}"; do
    state=$(systemctl --user is-active "$unit" 2>/dev/null || true)
    if [[ "$state" == "active" ]]; then
        ACTIVE=$((ACTIVE + 1))
    else
        echo "  [!] $unit is $state"
    fi
done
echo ""
echo "=== $ACTIVE/${#UNITS[@]} units active ==="
