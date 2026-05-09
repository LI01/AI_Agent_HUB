#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM_DIR="$ROOT/Agent-comm"
STATE_DIR="$ROOT/.agent-comm-state"
SEEN_FILE="$STATE_DIR/seen_files.txt"
LOG_FILE="$STATE_DIR/check.log"

mkdir -p "$STATE_DIR"
touch "$SEEN_FILE" "$LOG_FILE"

if [ ! -d "$COMM_DIR" ]; then
  printf '[%s] Agent-comm directory not found: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$COMM_DIR" >> "$LOG_FILE"
  exit 0
fi

tmp_seen="$(mktemp)"
find "$COMM_DIR" -maxdepth 1 -type f -name '*.md' -print | sort > "$tmp_seen"

new_count=0
while IFS= read -r file; do
  if ! grep -Fxq "$file" "$SEEN_FILE"; then
    new_count=$((new_count + 1))
    {
      printf '\n[%s] New Agent-comm file: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$file"
      sed -n '1,240p' "$file"
      printf '\n'
    } >> "$LOG_FILE"
  fi
done < "$tmp_seen"

mv "$tmp_seen" "$SEEN_FILE"

if [ "$new_count" -eq 0 ]; then
  printf '[%s] Agent-comm check: no new markdown files\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG_FILE"
fi
