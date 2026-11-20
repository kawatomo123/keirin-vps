#!/usr/bin/env python3
"""Daily collection for the Sakura VPS: collect any uncollected days in
the past week (normally just yesterday), into monthly tags.
Runs at 01:10 JST, after the last midnight races are settled.

Audit fix 2026-11-21 (A-1/A-3): the collector's exit code is checked.
A day the collector fails (rc != 0, e.g. K-Dreams maintenance
01:00-07:00) is retried every 45 minutes until 08:00 the same morning
instead of silently waiting for the next day's run, and up to 7 missed
days are worked per run (was 3). Every run leaves its per-day outcome
in data/daily_collect_status.json, which the git sync carries to the
VM (agent.do_git_sync copies it explicitly), so a stall is visible
off-VPS. Partial days (fetch misses inside an otherwise successful
day) are flagged "partial" in the status file; they stay marked done
(un-marking them is unsafe: the collector appends CSV rows with no
dedupe, so a re-collect would duplicate the day's rows)."""
import json
import subprocess
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / "venv" / "bin" / "python"
STATUS_PATH = ROOT / "data" / "daily_collect_status.json"
RETRY_MINUTES = 45
DEADLINE = dtime(8, 0)  # same-morning retry cutoff (maintenance ends 07:00)
MAX_DAYS_PER_RUN = 7
PARTIAL_KEYS = ("detail_missing", "entry_empty", "result_missing")


def tag_progress(tag):
    p = ROOT / "data" / f"progress_{tag}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"done": [], "stats": {}}


def write_status(payload):
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    tmp.replace(STATUS_PATH)


def attempt(d, tag):
    """Run the collector for one day; return an outcome dict."""
    r = subprocess.run([str(PY), str(ROOT / "collector.py"),
                        "--start", d.isoformat(), "--end", d.isoformat(),
                        "--tag", tag],
                       cwd=ROOT, capture_output=True, text=True)
    lines = (r.stdout.strip().splitlines() or [""])
    out = {"rc": r.returncode, "last_line": lines[-1]}
    if r.returncode != 0:
        out["status"] = "failed"
        err = (r.stderr.strip().splitlines() or [""])
        if err[-1]:
            out["error"] = err[-1][:300]
        return out
    prog = tag_progress(tag)
    if d.isoformat() not in prog.get("done", []):
        out["status"] = "failed"
        out["error"] = "collector exited 0 but day not in progress done"
        return out
    stats = prog.get("stats", {})
    out["stats"] = stats
    out["status"] = ("partial" if any(stats.get(k, 0) for k in PARTIAL_KEYS)
                     else "ok")
    return out


def main():
    today = date.today()
    targets = []
    for back in range(1, 8):  # look back up to a week for missed days
        d = today - timedelta(days=back)
        tag = d.strftime("%Y%m")
        if d.isoformat() not in tag_progress(tag).get("done", []):
            targets.append((d, tag))
    targets.sort()
    targets = targets[:MAX_DAYS_PER_RUN]
    payload = {"run_date": today.isoformat(),
               "started_at": datetime.now().isoformat(timespec="seconds"),
               "days": {}, "pending": [], "finished": False}
    write_status(payload)
    if not targets:
        payload["finished"] = True
        payload["note"] = "no uncollected days in the past week"
        write_status(payload)
        print("daily collect: nothing to do", flush=True)
        return 0
    pending = list(targets)
    attempts = {}
    while pending:
        for d, tag in list(pending):
            attempts[d.isoformat()] = attempts.get(d.isoformat(), 0) + 1
            print(f"daily collect {d} tag {tag} "
                  f"(attempt {attempts[d.isoformat()]})", flush=True)
            out = attempt(d, tag)
            out["attempts"] = attempts[d.isoformat()]
            out["updated_at"] = datetime.now().isoformat(timespec="seconds")
            payload["days"][d.isoformat()] = out
            print(f"  -> {out['status']} rc={out['rc']}: {out['last_line']}",
                  flush=True)
            if out["status"] in ("ok", "partial"):
                pending.remove((d, tag))
            payload["pending"] = [d.isoformat() for d, _ in pending]
            write_status(payload)
        if not pending:
            break
        now = datetime.now()
        deadline = datetime.combine(today, DEADLINE)
        if now >= deadline:
            print(f"daily collect: deadline {DEADLINE} reached, "
                  f"{len(pending)} day(s) still pending", flush=True)
            break
        sleep_s = min(RETRY_MINUTES * 60,
                      max(60, (deadline - now).total_seconds()))
        print(f"daily collect: retrying {len(pending)} day(s) in "
              f"{sleep_s / 60:.0f} min", flush=True)
        time.sleep(sleep_s)
    payload["pending"] = [d.isoformat() for d, _ in pending]
    payload["finished"] = True
    payload["finished_at"] = datetime.now().isoformat(timespec="seconds")
    write_status(payload)
    failed = [d for d, o in payload["days"].items()
              if o["status"] == "failed"]
    if failed:
        print(f"daily collect: FAILED days: {failed}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
