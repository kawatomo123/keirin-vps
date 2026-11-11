#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""競輪オッズ収集（Kドリームス公開ページ → アプリ表示用 odds_live.json）。

目的: アプリの競輪詳細で、モデルの買い目（7券種）に現在オッズと
赤オッズ（10倍以下）の印を出すための表示専用データを作る。
予測・精算・判定には一切使わない。

取得元: Rakuten Kドリームス公開ページ（ログイン不要）
  - 日次インデックス: /racecard/YYYY/MM/DD/（レースキー→場スラッグの対応取得）
  - オッズ: /<slug>/racedetail/<race_id>/?pageType=odds&kakeshikiType=<type>
    ページ内の <span class="num">..</span><span class="odds">..</span> を読む。
  - predictions.json の race_id がそのまま kdreams のレースキーになっている。

リクエスト予算（サーキットブレーカー規律のため明記）:
  - 対象は発走120分前〜発走直前のレースだけ（通常時で同時に3〜6レース、
    多い日でも12レース程度）。オッズは1レース1ページ（全券種入り）。
  - 30分以上先のレースは10分間隔、30分以内は5分間隔（cron は5分毎）。
  - 上限の目安: 1 tick 最大 約15リクエスト（対象レース＋日次インデックス）、開催時間帯のみ。
  - 単一スレッド、リクエスト間隔1.5秒（既存 collector.py と同じ作法）。
  - ブロック形状（403/429/極小ボディ）が3連続 → 回路を開く（1時間、
    再開はインデックス1件のプローブのみ、失敗で倍増・最大4時間）。

出力:
  - <keiba-data clone>/keirin/odds_live.json（変わった時だけ commit/push）
  - 発走を過ぎたレースの最終値は ~/keirin/data/odds_prepost_<date>.json に保存
    （将来のオッズ込み評価のための時点記録。アプリには出さない）

JSON 契約:
{
  "schema": 1,
  "generated_at": "<JST ISO>",
  "source": "kdreams",
  "races": {
    "YYYY-MM-DD|<場名>|<R>": {
      "start": "HH:MM",
      "fetched_at": "<JST ISO>",
      "bets": {"3連単": {"combo": "5-1-4", "odds": 23.4}, ...}  // odds は null 可
    }
  }
}
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
BASE = "https://keirin.kdreams.jp"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
KEIRIN_DIR = Path(os.environ.get("ODDS_KEIRIN_DIR", str(Path.home() / "keirin")))
# 予測の読み取り元（既存の共有 clone。読み取り専用で使う）
REPO = Path(os.environ.get("ODDS_REPO", str(Path.home() / "workspace" / "keiba-data")))
# 発行専用の独立 clone。共有 clone の作業ツリーは刻み発行者が使うため、
# そちらで commit すると rebase の取り合いになる。ここは自分専用に保つ。
PUB_REPO = Path(os.environ.get("ODDS_PUB_REPO", str(KEIRIN_DIR / "keiba-data-pub")))
PUB_REMOTE = "git@github.com:kawatomo123/keiba-data.git"
DATA_DIR = KEIRIN_DIR / "data"
WINDOW_MIN = int(os.environ.get("ODDS_WINDOW_MIN", "120"))
SLEEP_SEC = 1.5

# predictions のフィールド → (順序ありか, アプリ表示ラベル, kdreams にオッズ節があるか)
# kdreams のオッズページは1ページに全券種の節（<!-- 券種 Start -->〜End）を含む。
# 2枠単・2枠複の節は kdreams に無い（掲載なし）ため、常に odds=null になる。
BET_TYPES = [
    ("sanren_tan", True, "3連単", True),
    ("sanren_fuku", False, "3連複", True),
    ("nisya_tan", True, "2車単", True),
    ("nisya_fuku", False, "2車複", True),
    ("wide", False, "ワイド", True),
    ("waku_tan", True, "2枠単", False),
    ("waku_fuku", False, "2枠複", False),
]

# ワイドだけオッズが「1.3～20.1」のような幅表示になる
PAIR_RE = re.compile(r'<span class="num">([0-9\-=]+)</span><span class="odds">([0-9.]+)(?:～([0-9.]+))?</span>')
HREF_RE = re.compile(r'href="https://keirin\.kdreams\.jp/([a-z0-9]+)/racedetail/(\d{16})/"')


def now_jst():
    return datetime.now(JST)


def log(msg):
    print(f"[{now_jst().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- サーキットブレーカー ----------------

def circuit_path():
    return DATA_DIR / "kdreams_circuit.json"


def load_circuit():
    try:
        return json.loads(circuit_path().read_text(encoding="utf-8"))
    except Exception:
        return {"open_until": 0, "cooldown_h": 1, "consec_fails": 0,
                "fails_today": 0, "date": now_jst().strftime("%Y-%m-%d")}


def save_circuit(c):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    circuit_path().write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def circuit_note_failure(c, block_shaped):
    today = now_jst().strftime("%Y-%m-%d")
    if c.get("date") != today:
        c["date"] = today
        c["fails_today"] = 0
    if block_shaped:
        c["consec_fails"] = c.get("consec_fails", 0) + 1
        c["fails_today"] = c.get("fails_today", 0) + 1
        if c["consec_fails"] >= 3:
            c["open_until"] = time.time() + c.get("cooldown_h", 1) * 3600
            log(f"CIRCUIT OPEN until {datetime.fromtimestamp(c['open_until'], JST)} "
                f"(cooldown {c.get('cooldown_h', 1)}h, fails_today={c['fails_today']})")
            c["cooldown_h"] = min(c.get("cooldown_h", 1) * 2, 4)
            c["consec_fails"] = 0
    save_circuit(c)


def circuit_note_success(c):
    if c.get("consec_fails") or c.get("open_until"):
        c["consec_fails"] = 0
        c["open_until"] = 0
        c["cooldown_h"] = 1
        save_circuit(c)


# ---------------- HTTP ----------------

class FetchResult:
    def __init__(self, ok, body="", block=False):
        self.ok = ok
        self.body = body
        self.block = block


def fetch(url, circuit):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", errors="replace")
            status = r.status
    except urllib.error.HTTPError as e:
        circuit_note_failure(circuit, e.code in (403, 429))
        return FetchResult(False, block=e.code in (403, 429))
    except Exception as e:
        log(f"fetch error {url}: {e}")
        return FetchResult(False)
    if status in (403, 429) or len(body) < 1000:
        circuit_note_failure(circuit, True)
        return FetchResult(False, block=True)
    circuit_note_success(circuit)
    return FetchResult(True, body=body)


# ---------------- 対象レース ----------------

def load_predictions():
    # 作業ツリーには触らず、fetch した origin/main の中身を読む（他発行者の未コミット変更と競合しない）
    try:
        git_run(REPO, "fetch", "origin", "main", timeout=60)
        out = git_run(REPO, "show", "origin/main:keirin/predictions.json", timeout=60)
        if out.returncode == 0 and out.stdout:
            return json.loads(out.stdout)
    except Exception as e:
        log(f"predictions via git show failed: {e}")
    p = REPO / "keirin" / "predictions.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"predictions unreadable: {e}")
        return None


def race_start_candidates(date_str, start_time):
    """発走日時の候補。深夜レースは翌暦日に実際に走る場合があるため両方返す。"""
    out = []
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        t = datetime.strptime(start_time, "%H:%M").time()
    except Exception:
        return out
    out.append(datetime.combine(d, t, tzinfo=JST))
    if t.hour < 6:
        out.append(datetime.combine(d + timedelta(days=1), t, tzinfo=JST))
    return out


def pick_targets(preds, now):
    targets = []
    for day in preds.get("days", []):
        date_str = day.get("date", "")
        for r in day.get("races", []):
            st = r.get("start_time") or ""
            if not st:
                continue
            cands = race_start_candidates(date_str, st)
            start = None
            for c in cands:
                if now - timedelta(minutes=3) <= c <= now + timedelta(minutes=WINDOW_MIN):
                    start = c
                    break
            if start is None:
                continue
            targets.append({
                "date": date_str, "venue": r.get("venue", ""), "race_no": r.get("race_no"),
                "race_id": str(r.get("race_id", "")), "start_time": st, "start_dt": start,
                "combos": {f: r.get(f) for f, _, _, _ in BET_TYPES},
            })
    targets.sort(key=lambda x: x["start_dt"])
    return targets


def build_slug_map(calendar_dates, circuit):
    """日次インデックスから race_id → slug の対応を作る。"""
    slug_by_key = {}
    for d in sorted(calendar_dates):
        url = f"{BASE}/racecard/{d.strftime('%Y/%m/%d')}/"
        res = fetch(url, circuit)
        if not res.ok:
            log(f"index fetch failed {d}: block={res.block}")
            continue
        for slug, key in HREF_RE.findall(res.body):
            slug_by_key[key] = slug
        time.sleep(SLEEP_SEC)
    return slug_by_key


# ---------------- オッズ解析 ----------------

def canon(combo, ordered):
    parts = [p for p in re.split(r"[-=]", combo) if p != ""]
    if not ordered:
        parts = sorted(parts, key=lambda x: int(x))
    return tuple(parts)


def parse_sections(body):
    """オッズページを券種の節ごとに分け、{ラベル: {canon(combo): odds}} を返す。

    ページ全体を混ぜて読むと 3連単の値が 3連複に紛れる（順序なし正規化が一致するため）
    ので、必ず節の中でだけ照合する。
    """
    sections = {}
    for _, ordered, label, covered in BET_TYPES:
        if not covered:
            continue
        m = re.search(rf"<!-- {re.escape(label)} Start -->(.*?)<!-- {re.escape(label)} End -->",
                      body, re.S)
        if not m:
            sections[label] = {}
            continue
        d = {}
        for num, val, val2 in PAIR_RE.findall(m.group(1)):
            try:
                v = float(val)
                v2 = float(val2) if val2 else v
            except ValueError:
                continue
            if v >= 9000:  # 9999.9 は発売なし等の番人値
                continue
            d.setdefault(canon(num, ordered), (v, v2))
        sections[label] = d
    return sections


# ---------------- git 発行 ----------------

def git_run(repo, *args, timeout=120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, env=env, timeout=timeout)


def pub_ssh_command():
    # keiba-data への VPS からのアクセスは専用デプロイ鍵（共有 clone の core.sshCommand と同じ）
    key = Path.home() / ".ssh" / "keiba_data_ed25519"
    return f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"


def ensure_pub_repo():
    """発行専用 clone が無ければ作る（浅い clone で十分）。"""
    if (PUB_REPO / ".git").exists():
        # 既存 clone にも鍵と ident を冪等に効かせる
        git_run(PUB_REPO, "config", "core.sshCommand", pub_ssh_command())
        git_run(PUB_REPO, "config", "user.name", "sakura-vps")
        git_run(PUB_REPO, "config", "user.email", "vps@keiba")
        return True
    PUB_REPO.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_SSH_COMMAND=pub_ssh_command())
    r = subprocess.run(["git", "clone", "--depth", "50", PUB_REMOTE, str(PUB_REPO)],
                       capture_output=True, text=True, env=env, timeout=600)
    if r.returncode != 0:
        log(f"pub repo clone failed: {r.stderr[-200:]}")
        return False
    git_run(PUB_REPO, "config", "core.sshCommand", pub_ssh_command())
    git_run(PUB_REPO, "config", "user.name", "sakura-vps")
    git_run(PUB_REPO, "config", "user.email", "vps@keiba")
    log(f"pub repo cloned: {PUB_REPO}")
    return True


def publish(payload_text):
    """発行専用 clone に keirin/odds_live.json を書き、変わっていれば commit/push する。"""
    rel = "keirin/odds_live.json"
    if not ensure_pub_repo():
        return False
    # 発行専用 clone は odds_live.json のためだけにある。ローカル状態は毎回
    # origin/main に合わせて捨てる（reset --hard）ことで、途中失敗で出来た
    # 未コミット変更や未pushコミットが rebase を詰まらせる事故を根絶する。
    # ペイロードは毎回メモリから作り直すので、捨てても何も失われない。
    try:
        git_run(PUB_REPO, "fetch", "origin", "main")
        rs = git_run(PUB_REPO, "reset", "--hard", "origin/main")
        if rs.returncode != 0:
            log(f"publish skipped: reset failed: {rs.stderr[-150:]}")
            return False
    except Exception as e:
        log(f"publish sync failed: {e}")
        return False
    target = PUB_REPO / rel
    # generated_at だけの変化では commit しない（races の中身が変わった時だけ発行する）
    try:
        old_races = json.loads(target.read_text(encoding="utf-8")).get("races") if target.exists() else None
    except Exception:
        old_races = None
    if old_races is not None and old_races == json.loads(payload_text).get("races"):
        return True  # 実質の変化なし
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload_text, encoding="utf-8")
    git_run(PUB_REPO, "add", "--", rel)
    cm = git_run(PUB_REPO, "commit", "-m", "keirin odds_live update", "--", rel)
    if cm.returncode != 0:
        log(f"commit failed: {cm.stderr[-200:]}")
        return False
    for attempt in range(3):
        ps = git_run(PUB_REPO, "push", "origin", "main")
        if ps.returncode == 0:
            return True
        log(f"push failed (attempt {attempt + 1}): {ps.stderr[-150:]}")
        # origin が進んでいたら、自分のコミットを作り直して再試行する
        git_run(PUB_REPO, "fetch", "origin", "main")
        if git_run(PUB_REPO, "reset", "--hard", "origin/main").returncode != 0:
            break
        target.write_text(payload_text, encoding="utf-8")
        git_run(PUB_REPO, "add", "--", rel)
        if git_run(PUB_REPO, "commit", "-m", "keirin odds_live update", "--", rel).returncode != 0:
            break
    return False


# ---------------- 状態 ----------------

def state_path():
    return DATA_DIR / "odds_state.json"


def load_state():
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {"last_fetch": {}, "live": {}}


def save_state(s):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def status_path():
    return DATA_DIR / "odds_status.json"


def write_status(state_label, circuit, targets=None, live_races=None):
    """観測用ハートビート（2026-11-12 Fable P0）。毎回 odds_status.json を書く。

    発行（odds_live.json）は内容が変わった時だけだが、こちらは実行のたびに
    更新するので「変化なし」と「コレクタ死亡」を区別できる。サーキットの
    開閉遷移もここに残す（従来はログのみで無通知だった）。
    """
    try:
        prev = {}
        if status_path().exists():
            prev = json.loads(status_path().read_text(encoding="utf-8"))
        open_now = circuit.get("open_until", 0) > time.time()
        transition = prev.get("transition")
        prev_open = (prev.get("circuit") or {}).get("open")
        if prev_open is not None and bool(prev_open) != open_now:
            transition = {"at": now_jst().isoformat(timespec="seconds"),
                          "kind": "opened" if open_now else "closed"}
        doc = {
            "updated_at": now_jst().isoformat(timespec="seconds"),
            "state": state_label,
            "targets": targets,
            "live_races": live_races,
            "circuit": {
                "open": open_now,
                "open_until": (datetime.fromtimestamp(circuit["open_until"], JST)
                               .isoformat(timespec="seconds")
                               if circuit.get("open_until") else None),
                "consec_fails": circuit.get("consec_fails", 0),
                "fails_today": circuit.get("fails_today", 0),
                "cooldown_h": circuit.get("cooldown_h", 1),
            },
            "transition": transition,
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        status_path().write_text(json.dumps(doc, ensure_ascii=False),
                                 encoding="utf-8")
    except Exception:
        pass


def archive_prepost(entry):
    date = entry["key_date"]
    p = DATA_DIR / f"odds_prepost_{date.replace('-', '')}.json"
    try:
        doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"races": {}}
    except Exception:
        doc = {"races": {}}
    doc["races"][entry["key"]] = entry["payload"]
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------- 本体 ----------------

def main():
    circuit = load_circuit()
    now = now_jst()
    ts = now.timestamp()

    # 回路が開いている間は静かに終わる。再開はインデックス1件のプローブから
    probing = False
    if circuit.get("open_until", 0) > ts:
        log("circuit open; skipping")
        write_status("circuit_open", circuit)
        return 0
    if circuit.get("open_until", 0) > 0:
        probing = True
        circuit["open_until"] = 0
        save_circuit(circuit)

    preds = load_predictions()
    if preds is None:
        write_status("no_predictions", circuit)
        return 0
    targets = pick_targets(preds, now)
    state = load_state()
    last_fetch = state.get("last_fetch", {})
    live = state.get("live", {})

    # 発走を過ぎたレースは最終値をアーカイブして live から落とす
    changed_live = False
    for key in list(live.keys()):
        try:
            date_str, _, _ = key.split("|")
            ent = live[key]
            cands = race_start_candidates(date_str, ent.get("start", ""))
            if cands and all(c < now - timedelta(minutes=3) for c in cands):
                archive_prepost({"key": key, "key_date": date_str, "payload": ent})
                del live[key]
                changed_live = True
        except Exception:
            pass

    if not targets and not changed_live:
        save_state(state)
        log("no target races in window")
        write_status("idle", circuit, targets=0, live_races=len(live))
        return 0

    if targets:
        cal_dates = set()
        for t in targets:
            for c in race_start_candidates(t["date"], t["start_time"]):
                cal_dates.add(c.date())
        slug_map = build_slug_map(cal_dates, circuit)
        if probing and not slug_map:
            # プローブ失敗 = まだブロック形状。回路を開き直す
            circuit_note_failure(circuit, True)
            circuit["open_until"] = time.time() + circuit.get("cooldown_h", 2) * 3600
            save_circuit(circuit)
            log("probe failed; circuit reopened")
            write_status("circuit_open", circuit, targets=len(targets),
                         live_races=len(live))
            return 0

        for t in targets:
            slug = slug_map.get(t["race_id"])
            if not slug:
                log(f"no slug for race {t['race_id']} ({t['venue']} {t['race_no']}R)")
                continue
            key = f"{t['date']}|{t['venue']}|{t['race_no']}"
            mins_to_start = (t["start_dt"] - now).total_seconds() / 60.0
            interval = 5 * 60 if mins_to_start <= 30 else 10 * 60
            last = last_fetch.get(key, 0)
            if ts - last < interval and key in live:
                continue
            bets = {}
            ok_any = False
            # オッズは1レース1ページ（全券種の節を含む）を1回だけ取る
            url = (f"{BASE}/{slug}/racedetail/{t['race_id']}/"
                   f"?pageType=odds&kakeshikiType=3rentan")
            res = fetch(url, circuit)
            if circuit.get("open_until", 0) > time.time():
                log("circuit opened mid-run; aborting")
                save_state(state)
                write_status("circuit_open", circuit, targets=len(targets),
                             live_races=len(live))
                return 0
            sections = parse_sections(res.body) if res.ok else {}
            for field, ordered, label, covered in BET_TYPES:
                combo = t["combos"].get(field)
                odds = None
                odds_max = None
                if combo and covered and res.ok:
                    got = sections.get(label, {}).get(canon(combo, ordered))
                    if got is not None:
                        odds, odds_max = got
                        if odds_max == odds:
                            odds_max = None
                bets[label] = {"combo": combo, "odds": odds, "odds_max": odds_max}
                if odds is not None:
                    ok_any = True
            time.sleep(SLEEP_SEC)
            if ok_any:
                live[key] = {"start": t["start_time"],
                             "fetched_at": now.isoformat(timespec="seconds"),
                             "bets": bets}
                last_fetch[key] = ts
                changed_live = True
                log(f"updated {key} ({mins_to_start:.0f} min to start)")

    state["last_fetch"] = last_fetch
    state["live"] = live
    save_state(state)

    if changed_live or live:
        payload = {
            "schema": 1,
            "generated_at": now_jst().isoformat(timespec="seconds"),
            "source": "kdreams",
            "races": live,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
        if publish(text):
            log(f"published odds_live.json ({len(live)} races)")
        else:
            log("publish deferred (will retry next run)")
    write_status("ok", circuit, targets=len(targets), live_races=len(live))
    return 0


if __name__ == "__main__":
    sys.exit(main())
