# keirin-vps

Keirin race data collector for the Sakura VPS (personal research).
Source: Rakuten K-Dreams public pages. Single thread, 1.5s interval.

- `collector.py` — collects entries/lineup/results/payouts per day into CSVs (resumable, raw HTML cached)
- `backfill_driver.py` — collects 2022-01..2026-10 month by month, only inside windows 21:05–02:55 / 04:05–05:40 JST
- `daily_collect.py` — daily 01:10 JST run for yesterday + missed days
- `sync_status.sh` — copies status/CSVs to Drive via the VPS's rclone remote (if configured)
- `install.sh` — one-time setup (venv, systemd timers `keirin-*`)

Install on the VPS as user `ubuntu`:

```sh
curl -fsSL https://raw.githubusercontent.com/kawatomo123/keirin-vps/main/install.sh | bash
```
