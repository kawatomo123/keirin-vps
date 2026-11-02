#!/bin/sh
# One-time installer for the keirin collector on the Sakura VPS.
# Run as user ubuntu:  curl -fsSL https://raw.githubusercontent.com/kawatomo123/keirin-vps/main/install.sh | bash
set -eu
REPO_TARBALL="https://codeload.github.com/kawatomo123/keirin-vps/tar.gz/refs/heads/main"
BASE="$HOME/keirin"
mkdir -p "$BASE/data"

TMP="$(mktemp -d)"
curl -fsSL "$REPO_TARBALL" | tar -xz -C "$TMP" --strip-components=1
cp "$TMP/collector.py" "$TMP/backfill_driver.py" "$TMP/daily_collect.py" "$TMP/sync_status.sh" "$TMP/agent.py" "$BASE/"
chmod +x "$BASE/sync_status.sh"
cp -r "$TMP/systemd" "$BASE/systemd"

if [ ! -x "$BASE/venv/bin/python" ]; then
  if ! python3 -m venv "$BASE/venv" 2>/dev/null; then
    echo "python3-venv missing; installing (sudo)..."
    sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv python3-pip
    python3 -m venv "$BASE/venv"
  fi
fi
"$BASE/venv/bin/pip" install -q --upgrade pip
"$BASE/venv/bin/pip" install -q requests beautifulsoup4
"$BASE/venv/bin/python" -m py_compile "$BASE/collector.py" "$BASE/backfill_driver.py" "$BASE/daily_collect.py" "$BASE/agent.py"
echo "python env OK"

sudo cp "$BASE/systemd/keirin-backfill.service" "$BASE/systemd/keirin-backfill.timer" \
        "$BASE/systemd/keirin-daily.service" "$BASE/systemd/keirin-daily.timer" \
        "$BASE/systemd/keirin-sync.service" "$BASE/systemd/keirin-sync.timer" \
        "$BASE/systemd/keirin-agent.service" "$BASE/systemd/keirin-agent.timer" \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now keirin-backfill.timer keirin-daily.timer keirin-sync.timer keirin-agent.timer
echo "timers enabled:"
systemctl list-timers 'keirin-*' --no-pager || true
echo "DONE. Backfill runs inside windows 21:05-02:55 / 04:05-05:40; daily at 01:10."
echo "Status file: $BASE/data/backfill_status.json"
