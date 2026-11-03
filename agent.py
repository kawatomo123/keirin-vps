#!/usr/bin/env python3
"""Keirin VPS control agent.

Polls the Drive folder keirin/control/ (via the VPS's rclone remote) for JSON
command files and executes a fixed, allow-listed set of actions. Results are
written to keirin/control_results/. No arbitrary shell execution: file
contents are only ever parsed as JSON and matched against the allow-list.

Actions:
  {"id": "...", "action": "update_code"}                    -> pull latest package from the fixed GitHub repo, replace code files (with backup), compile-check
  {"id": "...", "action": "collect_day", "date": "YYYY-MM-DD", "tag": "YYYYMM"}
  {"id": "...", "action": "set_windows", "windows": [["21:05","02:55"],["04:05","05:40"]]}
  {"id": "...", "action": "status"}                          -> snapshot of progress/status files
"""
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE = DATA / "agent_state.json"
TARBALL = "https://codeload.github.com/kawatomo123/keirin-vps/tar.gz/refs/heads/main"
CODE_FILES = ["collector.py", "backfill_driver.py", "daily_collect.py", "sync_status.sh", "agent.py"]
GH_CONTROL = "https://raw.githubusercontent.com/kawatomo123/keirin-vps/main/control.json"


def remote():
    r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
    remotes = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    return remotes[0] if remotes else None


def rclone(*args):
    return subprocess.run(["rclone", *args], capture_output=True, text=True)


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"done_ids": []}


def save_state(st):
    DATA.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def do_update_code():
    with tempfile.TemporaryDirectory() as td:
        tgz = Path(td) / "pkg.tar.gz"
        urllib.request.urlretrieve(TARBALL, tgz)
        with tarfile.open(tgz) as tf:
            tf.extractall(td, filter="data")
        src = next(Path(td).glob("keirin-vps-*"))
        backup = DATA / "code_backup" / datetime.now().strftime("%Y%m%d%H%M%S")
        backup.mkdir(parents=True, exist_ok=True)
        for name in CODE_FILES:
            new = src / name
            if not new.exists():
                return f"missing {name} in package"
            cur = ROOT / name
            if cur.exists():
                shutil.copy2(cur, backup / name)
            shutil.copy2(new, cur)
        py = ROOT / "venv" / "bin" / "python"
        for name in ["collector.py", "backfill_driver.py", "daily_collect.py", "agent.py"]:
            r = subprocess.run([str(py), "-m", "py_compile", str(ROOT / name)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                # roll back
                for b in backup.iterdir():
                    shutil.copy2(b, ROOT / b.name)
                return f"compile failed for {name}: {r.stderr[-300:]}"
    return "code updated"


def do_collect_day(cmd):
    d, tag = cmd.get("date"), cmd.get("tag")
    if not d or not tag:
        return "date/tag required"
    py = ROOT / "venv" / "bin" / "python"
    r = subprocess.run([str(py), str(ROOT / "collector.py"), "--start", d, "--end", d, "--tag", tag],
                       cwd=ROOT, capture_output=True, text=True)
    tail = (r.stdout.strip().splitlines() or [""])[-1]
    return f"collect_day {d}: rc={r.returncode} {tail}"


def do_set_windows(cmd):
    wins = cmd.get("windows")
    if not wins:
        return "windows required"
    DATA.mkdir(parents=True, exist_ok=True)
    payload = {"windows": wins}
    if cmd.get("by_weekday"):
        payload["by_weekday"] = cmd["by_weekday"]
    (DATA / "windows.json").write_text(json.dumps(payload))
    return f"windows set to {wins} by_weekday={'yes' if 'by_weekday' in payload else 'no'}"


def do_dedup_data():
    """Remove exact duplicate lines from data CSVs (repairs the double
    collection of 2026-11-02/03, when two drivers ran concurrently).
    Every CSV's rows carry a unique key (race_id / race_id+car), so an
    exact duplicate line is always the same row written twice."""
    removed = {}
    for path in sorted(DATA.glob("*.csv")):
        lines = path.read_text(encoding="utf-8").splitlines()
        seen, out = set(), []
        for line in lines:
            if line not in seen:
                seen.add(line)
                out.append(line)
        n = len(lines) - len(out)
        if n:
            path.write_text("\n".join(out) + "\n", encoding="utf-8")
        removed[path.name] = n
    return {"removed": removed}


def do_reset_zero_days():
    """Undo false completions: days recorded as done in a progress file
    but with no race rows in that month's races CSV (the 2026-11-04
    K-Dreams maintenance incident zero-filled them). Un-marks those days
    and removes affected months from backfill done_months so the driver
    recollects them. Days that produced rows are never touched."""
    import csv as _csv
    reset = {}
    for prog_path in sorted(DATA.glob("progress_*.json")):
        tag = prog_path.stem.replace("progress_", "")
        prog = json.loads(prog_path.read_text())
        done_days = prog.get("done", [])
        if not done_days:
            continue
        race_dates = set()
        races_path = DATA / f"races_{tag}.csv"
        if races_path.exists():
            with races_path.open(encoding="utf-8") as f:
                for row in _csv.DictReader(f):
                    race_dates.add(row["date"])
        zero = [d for d in done_days if d not in race_dates]
        if zero:
            prog["done"] = [d for d in done_days if d in race_dates]
            prog_path.write_text(json.dumps(prog, ensure_ascii=False, indent=1))
            reset[tag] = len(zero)
    if reset:
        status_path = DATA / "backfill_status.json"
        if status_path.exists():
            st = json.loads(status_path.read_text())
            st["done_months"] = [t for t in st.get("done_months", [])
                                 if t not in reset]
            status_path.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    return {"reset_days": reset, "total": sum(reset.values())}


def do_status():
    snap = {}
    for name in ["backfill_status.json", "agent_state.json"]:
        p = DATA / name
        if p.exists():
            snap[name] = json.loads(p.read_text())
    prog = {}
    for p in sorted(DATA.glob("progress_*.json")):
        j = json.loads(p.read_text())
        prog[p.stem] = {"days_done": len(j["done"]), "stats": j["stats"]}
    snap["progress"] = prog
    snap["at"] = datetime.now().isoformat(timespec="seconds")
    return snap


ACTIONS = {"update_code": lambda c: do_update_code(),
           "collect_day": do_collect_day,
           "set_windows": do_set_windows,
           "dedup_data": lambda c: do_dedup_data(),
           "reset_zero_days": lambda c: do_reset_zero_days(),
           "status": lambda c: do_status()}


def run_command(cmd, st, outbox):
    cid = cmd.get("id") or ""
    if not cid or cid in st["done_ids"]:
        return
    action = cmd.get("action")
    try:
        if action in ACTIONS:
            result = ACTIONS[action](cmd)
        else:
            result = f"unknown action: {action}"
    except Exception as e:  # keep agent alive
        result = f"error: {e!r}"
    (outbox / f"{cid}.json").write_text(json.dumps(
        {"id": cid, "action": action, "at": datetime.now().isoformat(timespec="seconds"),
         "result": result}, ensure_ascii=False, indent=1, default=str))
    st["done_ids"].append(cid)
    save_state(st)
    print(cid, action, "->", str(result)[:120])


def main():
    rem = remote()
    if not rem:
        print("no rclone remote; exit")
        return
    inbox = DATA / "control_inbox"
    outbox = DATA / "control_outbox"
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    rclone("copy", f"{rem}keirin/control", str(inbox), "--log-level", "ERROR")
    st = load_state()
    for f in sorted(inbox.glob("*.json")):
        try:
            cmd = json.loads(f.read_text())
        except Exception:
            continue
        if isinstance(cmd, dict):
            run_command(cmd, st, outbox)
    # GitHub channel: commands committed to control.json in the package repo.
    # (Files placed in Drive keirin/control from a user account are not
    # visible to this VPS's drive.file-scoped rclone, so GitHub is the
    # working command transport; results still return via Drive.)
    try:
        with urllib.request.urlopen(GH_CONTROL, timeout=20) as r:
            gh = json.loads(r.read().decode("utf-8"))
        for cmd in (gh if isinstance(gh, list) else [gh]):
            if isinstance(cmd, dict):
                run_command(cmd, st, outbox)
    except Exception as e:
        print("github control fetch failed:", e)
    rclone("copy", str(outbox), f"{rem}keirin/control_results", "--log-level", "ERROR")


if __name__ == "__main__":
    main()
