#!/usr/bin/env python3
"""Daily collection for the Sakura VPS: collect any uncollected days in the
current and previous month (normally just yesterday), into monthly tags.
Runs at 01:10 JST, after the last midnight races are settled."""
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / "venv" / "bin" / "python"


def tag_progress_days(tag):
    p = ROOT / "data" / f"progress_{tag}.json"
    if p.exists():
        return set(json.loads(p.read_text())["done"])
    return set()


def main():
    today = date.today()
    targets = []
    for back in range(1, 8):  # look back up to a week for missed days
        d = today - timedelta(days=back)
        tag = d.strftime("%Y%m")
        if d.isoformat() not in tag_progress_days(tag):
            targets.append((d, tag))
    targets.sort()
    for d, tag in targets[:3]:  # at most 3 days per run
        print(f"daily collect {d} tag {tag}", flush=True)
        r = subprocess.run([str(PY), str(ROOT / "collector.py"),
                            "--start", d.isoformat(), "--end", d.isoformat(), "--tag", tag],
                           cwd=ROOT, capture_output=True, text=True)
        print((r.stdout.strip().splitlines() or [""])[-1], flush=True)


if __name__ == "__main__":
    main()
