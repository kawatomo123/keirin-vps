#!/bin/sh
# Sync keirin status + CSVs (not raw HTML) to the Drive folder via the VPS's
# existing rclone remote, if one is configured. No-op otherwise.
set -u
REMOTE="$(rclone listremotes 2>/dev/null | head -1)"
[ -z "$REMOTE" ] && exit 0
mkdir -p "$HOME/keirin/data"
rclone copy "$HOME/keirin/data" "${REMOTE}keirin/data" \
  --include "*.csv" --include "*status*.json" --include "progress_*.json" \
  --log-level ERROR
