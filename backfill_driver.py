#!/usr/bin/env python3
"""Backfill driver for the Sakura VPS.

Collects months 2022-01 .. 2026-10 (excluding 2025-01, already collected on the
VM) one day at a time, only inside the allowed windows, so it never competes
with the keiba production jobs on the same VPS. Resumable: the collector's
per-tag progress files skip finished days, and status.json tracks months.
"""
import json
import subprocess
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / "venv" / "bin" / "python"
STATUS = ROOT / "data" / "backfill_status.json"

# (start, end) local-time windows, every day; overridable via data/windows.json
# written by the control agent: {"windows": [["21:05","02:55"], ["04:05","05:40"]]}
WINDOWS = [(dtime(21, 5), dtime(23, 59)), (dtime(0, 0), dtime(2, 55)), (dtime(4, 5), dtime(5, 40))]


def _parse_windows(raw):
    out = []
    for a, b in raw:
        ah, am = map(int, a.split(":"))
        bh, bm = map(int, b.split(":"))
        if (ah, am) <= (bh, bm):
            out.append((dtime(ah, am), dtime(bh, bm)))
        else:  # crosses midnight -> split
            out.append((dtime(ah, am), dtime(23, 59)))
            out.append((dtime(0, 0), dtime(bh, bm)))
    return out


def load_windows(now=None):
    p = ROOT / "data" / "windows.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            # Optional per-weekday override (Mon=0 .. Sun=6), used to run
            # all day on weekdays while keeping weekend windows narrow so
            # keiba production on the same VPS is never disturbed.
            bw = d.get("by_weekday") or {}
            key = str((now or datetime.now()).weekday())
            if key in bw:
                out = _parse_windows(bw[key])
                if out:
                    return out
            out = _parse_windows(d["windows"])
            if out:
                return out
        except Exception:
            pass
    return WINDOWS


def in_window(now=None):
    dt = now or datetime.now()
    t = dt.time()
    return any(a <= t <= b for a, b in load_windows(dt))


def month_range():
    """Backfill queue: most recent 3 years, newest month first
    (2026-10 down to 2023-11), excluding 2025-01 (collected on the VM).
    Decided by the user 2026-11-02."""
    out = []
    y, m = 2026, 10
    while (y, m) >= (2023, 11):
        if not (y == 2025 and m == 1):
            out.append(f"{y:04d}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def month_days(tag):
    y, m = int(tag[:4]), int(tag[4:])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    days, d = [], first
    while d < nxt:
        days.append(d)
        d += timedelta(days=1)
    return days


def load_status():
    if STATUS.exists():
        return json.loads(STATUS.read_text())
    return {"done_months": [], "current": None}


def save_status(st):
    st["updated_at"] = datetime.now().isoformat(timespec="seconds")
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def tag_progress(tag):
    p = ROOT / "data" / f"progress_{tag}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"done": [], "stats": {}}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        print(json.dumps(load_status(), ensure_ascii=False))
        return
    if not in_window():
        print("outside window; exit")
        return
    st = load_status()
    for tag in month_range():
        if tag in st["done_months"]:
            continue
        days = month_days(tag)
        done_days = set(tag_progress(tag)["done"])
        remaining = [d for d in days if d.isoformat() not in done_days]
        if not remaining:
            st["done_months"].append(tag)
            st["current"] = None
            save_status(st)
            continue
        st["current"] = tag
        save_status(st)
        for d in remaining:
            if not in_window():
                print(f"window ended; stop at {tag} {d}")
                save_status(st)
                return
            print(f"collect {d} tag {tag}", flush=True)
            r = subprocess.run([str(PY), str(ROOT / "collector.py"),
                                "--start", d.isoformat(), "--end", d.isoformat(), "--tag", tag],
                               cwd=ROOT, capture_output=True, text=True)
            tail = (r.stdout.strip().splitlines() or [""])[-1]
            print(" ->", tail, flush=True)
            st["last_day"] = d.isoformat()
            st["last_result"] = tail
            save_status(st)
        st["done_months"].append(tag)
        st["current"] = None
        save_status(st)
    print("ALL MONTHS DONE")


if __name__ == "__main__":
    main()
