#!/bin/sh
# One-time installer for the keirin collector on the Sakura VPS.
# No sudo required: user-level venv (without ensurepip) + user crontab.
# Run as user ubuntu:  curl -fsSL https://raw.githubusercontent.com/kawatomo123/keirin-vps/main/install.sh | bash
set -eu
REPO_TARBALL="https://codeload.github.com/kawatomo123/keirin-vps/tar.gz/refs/heads/main"
BASE="$HOME/keirin"
mkdir -p "$BASE/data"

TMP="$(mktemp -d)"
curl -fsSL "$REPO_TARBALL" | tar -xz -C "$TMP" --strip-components=1
cp "$TMP/collector.py" "$TMP/backfill_driver.py" "$TMP/daily_collect.py" "$TMP/sync_status.sh" "$TMP/agent.py" "$BASE/"
chmod +x "$BASE/sync_status.sh"
cp -r "$TMP/systemd" "$BASE/systemd" 2>/dev/null || true

USE_UV=0
# a venv counts as usable only with BOTH python and pip; a skeleton left by a
# failed ensurepip run (python only) must be recreated from scratch
if [ ! -x "$BASE/venv/bin/python" ] || [ ! -x "$BASE/venv/bin/pip" ]; then
  rm -rf "$BASE/venv"
  if python3 -m venv --without-pip "$BASE/venv" 2>/dev/null && [ -x "$BASE/venv/bin/python" ]; then
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$BASE/venv/bin/python" - -q
  else
    echo "venv --without-pip failed; falling back to uv"
    rm -rf "$BASE/venv"
    curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz" | tar -xz -C "$TMP" --strip-components=1
    "$TMP/uv" venv "$BASE/venv" --python python3 -q
    USE_UV=1
  fi
fi
if [ "$USE_UV" = "1" ] || [ ! -x "$BASE/venv/bin/pip" ]; then
  if [ -x "$TMP/uv" ]; then
    "$TMP/uv" pip install --python "$BASE/venv/bin/python" -q requests beautifulsoup4
  else
    curl -fsSL "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz" | tar -xz -C "$TMP" --strip-components=1
    "$TMP/uv" pip install --python "$BASE/venv/bin/python" -q requests beautifulsoup4
  fi
else
  "$BASE/venv/bin/pip" install -q requests beautifulsoup4
fi
"$BASE/venv/bin/python" -m py_compile "$BASE/collector.py" "$BASE/backfill_driver.py" "$BASE/daily_collect.py" "$BASE/agent.py"
echo "python env OK"

# user crontab (idempotent: replace previous keirin block)
CRON_TMP="$(mktemp)"
( crontab -l 2>/dev/null | grep -v '# keirin' || true ) > "$CRON_TMP"
cat >> "$CRON_TMP" <<EOF
5 * * * * /usr/bin/flock -n $BASE/data/backfill.lock $BASE/venv/bin/python $BASE/backfill_driver.py >> $BASE/data/backfill.log 2>&1 # keirin
10 1 * * * /usr/bin/flock -n $BASE/data/daily.lock $BASE/venv/bin/python $BASE/daily_collect.py >> $BASE/data/daily.log 2>&1 # keirin
*/30 * * * * /bin/sh $BASE/sync_status.sh >> $BASE/data/sync.log 2>&1 # keirin
*/5 * * * * /usr/bin/flock -n $BASE/data/agent.lock $BASE/venv/bin/python $BASE/agent.py >> $BASE/data/agent.log 2>&1 # keirin
EOF
crontab "$CRON_TMP"
rm -f "$CRON_TMP"
echo "crontab installed:"
crontab -l | grep '# keirin'
echo "DONE (no sudo). Backfill windows 21:05-02:55 / 04:05-05:40; daily 01:10; agent every 5 min."
echo "Status file: $BASE/data/backfill_status.json (appears at first window run)"
