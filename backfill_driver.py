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
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / "venv" / "bin" / "python"
STATUS = ROOT / "data" / "backfill_status.json"
STATUS_LOCK = threading.RLock()
SHARED_STATUS = None

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
    with STATUS_LOCK:
        st["updated_at"] = datetime.now().isoformat(timespec="seconds")
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def tag_progress(tag):
    p = ROOT / "data" / f"progress_{tag}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"done": [], "stats": {}}


def acquire_lock():
    """Single-instance guard inside the driver itself, so ANY start
    (cron wrapper or a manual console start) is safe. The cron line's
    flock alone was bypassed by manual starts on 2026-11-02/03, which
    ran two drivers side by side and duplicated every collected day."""
    import fcntl
    handle = open(ROOT / "data" / ".backfill.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another backfill_driver is running; exit")
        sys.exit(0)
    return handle


class MonthClaims:
    """Hands out each queued month to exactly one worker. The descending
    worker takes the newest remaining month and the ascending worker the
    oldest remaining month, so they never share a tag (and therefore never
    share the CSV/progress files for that month)."""

    def __init__(self, queue, done_months):
        self.queue = list(queue)
        self.done = set(done_months)
        self.claimed = set()
        self.front = 0
        self.back = len(self.queue) - 1
        self.lock = threading.Lock()

    def claim(self, side):
        with self.lock:
            while self.front <= self.back:
                if side == "desc":
                    tag = self.queue[self.front]
                    self.front += 1
                else:
                    tag = self.queue[self.back]
                    self.back -= 1
                if tag not in self.done and tag not in self.claimed:
                    self.claimed.add(tag)
                    return tag
            return None

    def finish(self, tag):
        with self.lock:
            self.done.add(tag)
            self.claimed.discard(tag)

    def all_done(self):
        with self.lock:
            return all(tag in self.done for tag in self.queue)


def update_status(**updates):
    with STATUS_LOCK:
        SHARED_STATUS.update(updates)
        SHARED_STATUS["current"] = (SHARED_STATUS.get("current_desc")
                                    or SHARED_STATUS.get("current_asc"))
        save_status(SHARED_STATUS)


def mark_month_done(tag, claims):
    claims.finish(tag)
    with STATUS_LOCK:
        if tag not in SHARED_STATUS["done_months"]:
            SHARED_STATUS["done_months"].append(tag)
        save_status(SHARED_STATUS)


def run_side(side, claims):
    current_key = f"current_{side}"
    while True:
        tag = claims.claim(side)
        if tag is None:
            update_status(**{current_key: None})
            return
        days = month_days(tag)
        done_days = set(tag_progress(tag)["done"])
        remaining = [d for d in days if d.isoformat() not in done_days]
        if not remaining:
            mark_month_done(tag, claims)
            update_status(**{current_key: None})
            continue
        update_status(**{current_key: tag})
        finished = True
        for d in remaining:
            if not in_window():
                print(f"window ended; {side} stops at {tag} {d}", flush=True)
                finished = False
                break
            print(f"collect {d} tag {tag} ({side})", flush=True)
            r = subprocess.run([str(PY), str(ROOT / "collector.py"),
                                "--start", d.isoformat(), "--end", d.isoformat(), "--tag", tag],
                               cwd=ROOT, capture_output=True, text=True)
            tail = (r.stdout.strip().splitlines() or [""])[-1]
            print(" ->", tail, flush=True)
            update_status(last_day=d.isoformat(), last_result=tail,
                          **{f"last_day_{side}": d.isoformat()})
        update_status(**{current_key: None})
        if finished:
            mark_month_done(tag, claims)
        else:
            return


def main():
    global SHARED_STATUS
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        print(json.dumps(load_status(), ensure_ascii=False))
        return
    _lock = acquire_lock()  # noqa: F841 (must stay referenced)
    if not in_window():
        print("outside window; exit")
        return
    SHARED_STATUS = load_status()
    SHARED_STATUS.setdefault("done_months", [])
    SHARED_STATUS.setdefault("current_desc", None)
    SHARED_STATUS.setdefault("current_asc", None)
    save_status(SHARED_STATUS)
    claims = MonthClaims(month_range(), SHARED_STATUS["done_months"])
    threads = [threading.Thread(target=run_side, args=("desc", claims)),
               threading.Thread(target=run_side, args=("asc", claims))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if claims.all_done():
        print("ALL MONTHS DONE")
    else:
        print("window ended; work remains")


if __name__ == "__main__":
    main()
