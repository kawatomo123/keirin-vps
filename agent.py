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
CODE_FILES = ["collector.py", "backfill_driver.py", "daily_collect.py", "sync_status.sh"]


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
        for name in ["collector.py", "backfill_driver.py", "daily_collect.py"]:
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
    (DATA / "windows.json").write_text(json.dumps({"windows": wins}))
    return f"windows set to {wins}"


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
           "status": lambda c: do_status()}


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
        cid = cmd.get("id") or f.stem
        if cid in st["done_ids"]:
            continue
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
    rclone("copy", str(outbox), f"{rem}keirin/control_results", "--log-level", "ERROR")


if __name__ == "__main__":
    main()
