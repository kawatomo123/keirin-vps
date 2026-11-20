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
CODE_FILES = ["collector.py", "backfill_driver.py", "daily_collect.py", "sync_status.sh", "agent.py", "odds_collect.py"]
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


GIT_REPO_DIR = ROOT / "data_repo"
GIT_KEY = Path.home() / ".ssh" / "keirin_data_ed25519"
GIT_URL = "git@github.com:kawatomo123/keirin-data.git"


def _git_env():
    import os
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = (f"ssh -i {GIT_KEY} -o IdentitiesOnly=yes "
                              "-o StrictHostKeyChecking=accept-new")
    return env


def do_git_setup():
    """One-time setup for the GitHub data channel (replaces the Drive
    data return): generate a dedicated SSH keypair (public key is
    reported back so it can be registered as a deploy key) and install
    a */30 crontab line that runs `agent.py --sync-data`."""
    out = {}
    r = subprocess.run(["git", "--version"], capture_output=True, text=True)
    out["git_version"] = (r.stdout or r.stderr).strip()
    GIT_KEY.parent.mkdir(parents=True, exist_ok=True)
    if not GIT_KEY.exists():
        r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "",
                            "-f", str(GIT_KEY)], capture_output=True, text=True)
        if r.returncode != 0:
            return {"error": f"ssh-keygen failed: {r.stderr[-200:]}", **out}
    out["pubkey"] = GIT_KEY.with_suffix(".pub").read_text().strip()
    line = (f"*/30 * * * * {ROOT}/venv/bin/python {ROOT}/agent.py "
            f"--sync-data >> {DATA}/git_sync.log 2>&1")
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if "--sync-data" in cur:
        out["cron"] = "already installed"
    else:
        new = (cur.rstrip("\n") + "\n" if cur.strip() else "") + line + "\n"
        r = subprocess.run(["crontab", "-"], input=new,
                           capture_output=True, text=True)
        out["cron"] = "installed" if r.returncode == 0 else f"failed: {r.stderr[-200:]}"
    return out


def do_git_sync():
    """Copy the collection CSVs/progress into the keirin-data repo
    checkout and push if anything changed."""
    env = _git_env()
    if not (GIT_REPO_DIR / ".git").exists():
        r = subprocess.run(["git", "clone", GIT_URL, str(GIT_REPO_DIR)],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            return f"clone failed: {r.stderr[-300:]}"
    else:
        r = subprocess.run(["git", "-C", str(GIT_REPO_DIR), "pull", "-q"],
                           capture_output=True, text=True, env=env)
        if r.returncode != 0:
            return f"pull failed: {r.stderr[-300:]}"
    dest = GIT_REPO_DIR / "data"
    dest.mkdir(exist_ok=True)
    import glob as _glob
    names = [Path(p).name for p in _glob.glob(str(DATA / "*.csv"))]
    names += [Path(p).name for p in _glob.glob(str(DATA / "progress_*.json"))]
    if (DATA / "backfill_status.json").exists():
        names.append("backfill_status.json")
    for name in names:
        shutil.copy2(DATA / name, dest / name)
    ev_src = ROOT / "ev_shadow"
    if ev_src.exists():
        ev_dest = GIT_REPO_DIR / "ev_shadow"
        for src in sorted(ev_src.rglob("*")):
            if src.is_file() and src.suffix in (".csv", ".gz"):
                rel = src.relative_to(ev_src)
                tgt = ev_dest / rel
                tgt.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, tgt)
    subprocess.run(["git", "-C", str(GIT_REPO_DIR), "add", "-A"],
                   capture_output=True, text=True, env=env)
    r = subprocess.run(["git", "-C", str(GIT_REPO_DIR), "status", "--porcelain"],
                       capture_output=True, text=True, env=env)
    if not r.stdout.strip():
        return "no changes"
    subprocess.run(["git", "-C", str(GIT_REPO_DIR), "-c", "user.email=vps@keirin",
                    "-c", "user.name=keirin-vps", "commit", "-qm",
                    f"data sync {datetime.now().isoformat(timespec='minutes')}"],
                   capture_output=True, text=True, env=env)
    r = subprocess.run(["git", "-C", str(GIT_REPO_DIR), "push", "-q"],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return f"push failed: {r.stderr[-300:]}"
    return f"pushed ({len(names)} files tracked)"


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


_EXTRACT_SCRIPT = r'''
import csv, gzip, json, re, sys
from pathlib import Path
ROOT = Path("__ROOT__")
DATA = ROOT / "data"
dates = {}
for p in sorted(DATA.glob("races_*.csv")):
    with p.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dates[row["race_id"]] = row["date"]
rawdir = DATA / "raw" / "racedetail"
block_re = re.compile(r'class="line_position">(.*?)</div>', re.S)
span_re = re.compile(r'<span class="p\d+">([^<]*)</span>')
rows = {}
files = sorted(rawdir.glob("*.html.gz"))
n_ok = n_missing = 0
for i, p in enumerate(files):
    rid = p.name[:-8]
    try:
        html = gzip.open(p, "rt", encoding="utf-8", errors="ignore").read()
    except Exception:
        n_missing += 1
        continue
    m = block_re.search(html)
    if not m:
        n_missing += 1
        continue
    groups, roles = [], []
    for seg in m.group(1).split('class="icon_p space"'):
        vals = [v.strip() for v in span_re.findall(seg)]
        cars = [v for v in vals if v.isdigit()]
        rls = [v for v in vals if v and not v.isdigit() and v != "\u2190"]
        if cars:
            groups.append(",".join(cars))
            roles.append(",".join(rls))
    if not groups:
        n_missing += 1
        continue
    d = dates.get(rid, "")
    rows.setdefault(d[:7].replace("-", ""), []).append(
        [rid, d, "|".join(groups), "|".join(roles)])
    n_ok += 1
    if (i + 1) % 10000 == 0:
        print(f"progress {i + 1}/{len(files)}", flush=True)
for ym, rs in sorted(rows.items()):
    out = DATA / f"lines_{ym}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["race_id", "date", "formation", "roles"])
        w.writerows(sorted(rs))
print(f"DONE files={len(files)} parsed={n_ok} missing={n_missing}", flush=True)
'''


def do_extract_lines():
    """Parse the published line formations (narabi yosou) out of the cached
    racedetail HTML into data/lines_YYYYMM.csv (shipped by git sync). Runs
    detached; completion shows up as lines_*.csv in the data repo."""
    script = DATA / "extract_lines_run.py"
    DATA.mkdir(parents=True, exist_ok=True)
    script.write_text(_EXTRACT_SCRIPT.replace("__ROOT__", str(ROOT)))
    py = ROOT / "venv" / "bin" / "python"
    log = open(DATA / "lines_extract.log", "a", encoding="utf-8")
    p = subprocess.Popen([str(py), str(script)], stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    return f"extract_lines started pid={p.pid}"


EV_DIR = ROOT / "ev_shadow"
EV_SNAPSHOT = ("https://raw.githubusercontent.com/kawatomo123/keirin-vps/"
               "main/predictions_snapshot.json")
EV_BLOCK_MARKERS = ["システムメンテナンス中", "緊急メンテナンス中",
                    "すべてのサービスを停止"]
EV_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def _ev_fetch(url, timeout=30):
    import urllib.request as _u
    req = _u.Request(url, headers=EV_UA)
    with _u.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def _ev_text(html_text):
    import html as _h
    import re as _re
    t = _re.sub(r"(?s)<(script|style).*?</\1>", " ", html_text)
    t = _re.sub(r"<[^>]+>", " ", t)
    return _re.sub(r"\s+", " ", _h.unescape(t))


def _ev_slug_map(d):
    import re as _re
    import time as _time
    cache = EV_DIR / f"slugmap_{d.isoformat()}.json"
    if cache.exists() and _time.time() - cache.stat().st_mtime < 3600:
        return json.loads(cache.read_text())
    html_text = _ev_fetch(f"https://keirin.kdreams.jp/racecard/{d:%Y/%m/%d}/")
    pairs = _re.findall(r"/([a-z0-9-]+)/racedetail/(\d{16})/", html_text)
    m = {rid: slug for slug, rid in pairs}
    EV_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(m))
    return m


def do_ev_capture():
    """EV shadow capture on the VPS (stdlib only). Same contract as the
    VM's ev_capture.py: for today's races starting in (now+4, now+25]
    minutes, fetch the K-Dreams odds page once and store the page text
    plus the model's picks/probabilities from the predictions snapshot
    published in this repo. Captures land in ev_shadow/ and return to
    the VM through the git data sync."""
    import csv as _csv
    import gzip as _gz
    import re as _re
    import time as _time
    from datetime import timedelta
    now = datetime.now()
    today = now.date()
    try:
        doc = json.loads(_ev_fetch(EV_SNAPSHOT))
    except Exception as e:
        return f"snapshot fetch failed: {e!r}"
    days = {x["date"]: x for x in doc.get("days", [])}
    races = days.get(today.isoformat(), {}).get("races", [])
    if not races:
        return "no races for today in snapshot; skip"
    EV_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = EV_DIR / "manifest.csv"
    done = set()
    if manifest_path.exists():
        with open(manifest_path, newline="") as f:
            done = {row["race_id"] for row in _csv.DictReader(f)}
    lo, hi = now + timedelta(minutes=4), now + timedelta(minutes=25)
    targets = []
    for r in races:
        st = r.get("start_time") or ""
        if not _re.fullmatch(r"\d{2}:\d{2}", st):
            continue
        start = datetime.combine(today, datetime.strptime(st, "%H:%M").time())
        if lo < start <= hi and r["race_id"] not in done:
            targets.append((start, r))
    if not targets:
        return "no races in window"
    smap = _ev_slug_map(today)
    raw_dir = EV_DIR / "raw" / today.isoformat()
    raw_dir.mkdir(parents=True, exist_ok=True)
    new_file = not manifest_path.exists()
    fails = captured = 0
    with open(manifest_path, "a", newline="") as f:
        w = _csv.writer(f)
        if new_file:
            w.writerow(["date", "race_id", "venue", "race_no", "start_time",
                        "captured_at", "mins_before", "segment",
                        "sanren_tan", "sanren_fuku", "nisya_tan", "riders_json"])
        for start, r in sorted(targets):
            slug = smap.get(r["race_id"])
            if not slug:
                continue
            url = (f"https://keirin.kdreams.jp/{slug}/racedetail/"
                   f"{r['race_id']}/?pageType=odds&kakeshikiType=3rentan")
            try:
                text = _ev_text(_ev_fetch(url))
            except Exception:
                fails += 1
                if fails >= 3:
                    return f"circuit breaker: fetch failures (captured {captured})"
                continue
            if any(m in text for m in EV_BLOCK_MARKERS):
                return f"circuit breaker: maintenance page (captured {captured})"
            if "人気順" not in text:
                continue
            fails = 0
            with _gz.open(raw_dir / f"{r['race_id']}.txt.gz", "wt",
                          encoding="utf-8") as gz:
                gz.write(text)
            riders = [{"car_no": x["car_no"], "p_win": x["p_win"],
                       "p_top3": x["p_top3"]} for x in r.get("riders", [])]
            w.writerow([today.isoformat(), r["race_id"], r["venue"],
                        r["race_no"], r["start_time"],
                        now.strftime("%Y-%m-%d %H:%M:%S"),
                        round((start - now).total_seconds() / 60, 1),
                        r.get("segment", ""), r.get("sanren_tan", ""),
                        r.get("sanren_fuku", ""), r.get("nisya_tan", ""),
                        json.dumps(riders, ensure_ascii=False)])
            f.flush()
            captured += 1
            _time.sleep(1)
    return f"captured {captured}"


def do_ev_cron_setup():
    """Install a */5 crontab line running `agent.py --ev-capture`
    (idempotent, mirrors the --sync-data line from git_setup)."""
    line = (f"*/5 * * * * {ROOT}/venv/bin/python {ROOT}/agent.py "
            f"--ev-capture >> {DATA}/ev_capture.log 2>&1")
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    if "--ev-capture" in cur:
        return "ev cron already installed"
    new = (cur.rstrip("\n") + "\n" if cur.strip() else "") + line + "\n"
    r = subprocess.run(["crontab", "-"], input=new,
                       capture_output=True, text=True)
    return "ev cron installed" if r.returncode == 0 else f"failed: {r.stderr[-200:]}"


ACTIONS = {"update_code": lambda c: do_update_code(),
           "extract_lines": lambda c: do_extract_lines(),
           "ev_cron_setup": lambda c: do_ev_cron_setup(),
           "collect_day": do_collect_day,
           "set_windows": do_set_windows,
           "dedup_data": lambda c: do_dedup_data(),
           "reset_zero_days": lambda c: do_reset_zero_days(),
           "git_setup": lambda c: do_git_setup(),
           "git_sync_data": lambda c: do_git_sync(),
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
    if "--sync-data" in sys.argv:
        try:
            print(do_git_sync())
        except Exception as e:
            print(f"sync-data error: {e!r}")
        return
    if "--ev-capture" in sys.argv:
        try:
            print(do_ev_capture())
        except Exception as e:
            print(f"ev-capture error: {e!r}")
        return
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
