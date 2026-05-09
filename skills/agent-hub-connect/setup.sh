#!/bin/bash
# Setup script for agent-hub-connect skill

echo "Setting up Agent Hub Connect skill..."

# Install required dependencies
pip install websocket-client -q

echo "Done. Usage:"
echo "  python agent.py --hub http://10.9.0.10:8080 --id my-agent"