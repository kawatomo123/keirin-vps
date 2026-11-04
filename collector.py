#!/usr/bin/env python3
"""Keirin collector v1 (K-Dreams public pages, no login). Resumable by day."""
import argparse
import csv
import gzip
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://keirin.kdreams.jp"
MAINTENANCE_MARK = "システムメンテナンス中"


class DayUnavailable(Exception):
    """The day's racecard could not be obtained (site maintenance, fetch
    failure, or an index page with no race links). Such a day must never
    be recorded as collected: K-Dreams serves a maintenance page for all
    URLs during 01:00-07:00, and treating that page as 'no races today'
    silently zero-filled 2024-03-28..2025-05 on 2026-11-04."""
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
SLEEP = 1.0
SESSION = requests.Session()
SESSION.headers.update(UA)


def fetch(url, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            text = r.text
            if MAINTENANCE_MARK in text:
                raise RuntimeError("site maintenance")
            return text
        except Exception as e:
            print(f"  ! fetch fail ({attempt + 1}/{retries}) {url}: {e}", flush=True)
            time.sleep(5 * (attempt + 1))
    return None


def save_raw(kind, key, html):
    p = RAW / kind / f"{key}.html.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write(html)
    return p


def load_raw(kind, key):
    p = RAW / kind / f"{key}.html.gz"
    if p.exists():
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return f.read()
    return None


ROLE_NORM = {"まくり": "捲", "逃げ": "逃", "追込": "追", "追上": "追", "押え先": "押先", "押さえ先": "押先"}


def parse_lineup(text):
    m = re.search(r"並び予想\s*←\s*(.+?)\s*(?:レース評|投票|出走表)", text)
    if not m:
        return ""
    raw = m.group(1)
    pairs = re.findall(r"(\d)\s*(先行|追込|追上|押え先|押さえ先|自在|まくり|捲|逃げ|逃|競り|追)", raw)
    return " ".join(f"{car}:{ROLE_NORM.get(role, role)}" for car, role in pairs)


def _num(x):
    try:
        return float(x)
    except ValueError:
        return ""


PREFS = ["北海道", "青森", "岩手", "宮城", "秋田", "山形", "福島", "茨城", "栃木", "群馬",
         "埼玉", "千葉", "東京", "神奈川", "新潟", "富山", "石川", "福井", "山梨", "長野",
         "岐阜", "静岡", "愛知", "三重", "滋賀", "京都", "大阪", "兵庫", "奈良", "和歌山",
         "鳥取", "島根", "岡山", "広島", "山口", "徳島", "香川", "愛媛", "高知", "福岡",
         "佐賀", "長崎", "熊本", "大分", "宮崎", "鹿児島", "沖縄"]
PREF_RE = re.compile(r"^(.+?)(" + "|".join(sorted(PREFS, key=len, reverse=True)) + r")/(\d+)/(\d+)$")


def parse_rider_cell(cell):
    """'合地 登汰 岡 山/26/121' -> (name, pref, age, term, scratched). Whitespace-insensitive."""
    scratched = 1 if "欠車" in cell else 0
    compact = re.sub(r"\s+", "", cell).replace("　", "")
    compact = compact.replace("（欠車）", "").replace("(欠車)", "")
    m = PREF_RE.match(compact)
    if m:
        return m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), scratched
    m = re.match(r"^(.+)/(\d+)/(\d+)$", compact)
    if m:
        return m.group(1), "", int(m.group(2)), int(m.group(3)), scratched
    return None


def parse_detail(html, race_id, cal_date):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    m = re.match(r"(.+?)競輪\s*レース詳細\s*\|\s*(.*?)\s+(\d+)R\s*(.*?)\s*\|\s*(\d{4})年(\d{2})月(\d{2})日", title)
    venue_name = m.group(1).strip() if m else ""
    meeting = m.group(2).strip() if m else ""
    race_no = int(m.group(3)) if m else int(race_id[-2:])
    race_name = m.group(4).strip() if m else ""
    text = soup.get_text(" ", strip=True)
    segment = "girls" if ("ガールズ" in race_name or "ガールズ" in meeting) else "men"
    race = {
        "race_id": race_id, "date": cal_date, "venue_name": venue_name,
        "meeting": meeting, "race_no": race_no, "race_name": race_name,
        "segment": segment, "lineup": parse_lineup(text),
    }
    entries = []
    for t in soup.find_all("table"):
        ttext = t.get_text(" ", strip=True)
        if "競走得点" in ttext and "選手名" in ttext and "脚 質" in ttext:
            for tr in t.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                name_idx = next((i for i, c in enumerate(cells) if re.search(r"/\d+/\d+", c)), None)
                if name_idx is None or name_idx < 1:
                    continue
                try:
                    car_no = int(cells[name_idx - 1])
                except ValueError:
                    continue
                frame = cells[name_idx - 2] if name_idx >= 2 else ""
                nm = parse_rider_cell(cells[name_idx])
                rest = cells[name_idx + 1:]
                if not nm or len(rest) < 17:
                    continue
                entries.append({
                    "race_id": race_id, "date": cal_date, "segment": segment,
                    "frame_no": frame, "car_no": car_no,
                    "mark": cells[0] if cells else "",
                    "rider_name": nm[0], "pref": nm[1],
                    "age": nm[2], "term": nm[3], "scratched": nm[4],
                    "rider_class": rest[0], "style": rest[1], "gear": _num(rest[2]),
                    "race_score": _num(rest[3]), "s_count": _num(rest[4]), "b_count": _num(rest[5]),
                    "nige": _num(rest[6]), "makuri": _num(rest[7]), "sashi": _num(rest[8]),
                    "mark_cnt": _num(rest[9]), "w1": _num(rest[10]), "w2": _num(rest[11]),
                    "w3": _num(rest[12]), "out_cnt": _num(rest[13]), "win_rate": _num(rest[14]),
                    "rate2": _num(rest[15]), "rate3": _num(rest[16]),
                })
            break
    return race, entries


COMBO_RE = re.compile(r"(\d(?:[=-]\d)+)([\d,]+)円\((\d+)\)")


def parse_result_page(html):
    """Return {race_no_str: {'finish': [(pos, car)], 'payouts': [(bet, combo, payout, pop)]}}"""
    soup = BeautifulSoup(html, "html.parser")
    order_tables = soup.find_all("table", class_="order_table")
    refund_tables = soup.find_all("table", class_="refund_table")
    race_nos = []
    for el in soup.find_all(True):
        if el.name not in ("span", "div", "p", "h3", "h4", "td"):
            continue
        t = el.get_text(" ", strip=True)
        mm = re.fullmatch(r"(\d{1,2})R\s+\S+", t)
        if mm:
            n = int(mm.group(1))
            if not race_nos or race_nos[-1] != n:
                race_nos.append(n)
    out = {}
    for i, ot in enumerate(order_tables):
        if i >= len(race_nos):
            break
        race_no = race_nos[i]
        finish = []
        rows = ot.find_all("tr")
        if len(rows) >= 2:
            cells = [c.get_text("", strip=True) for c in rows[1].find_all(["th", "td"])]
            pos = 0
            for cell in cells:
                # a cell may hold two riders ('3岸澤4成松'); split every (digit)(name) pair
                for mm in re.finditer(r"(\d)([^\d]+)", cell):
                    pos += 1
                    finish.append((pos, int(mm.group(1))))
        payouts = []
        if i < len(refund_tables):
            payouts = parse_refund_table(refund_tables[i])
        out[str(race_no)] = {"finish": finish, "payouts": payouts}
    return out


ROW1_LABELS = {"2枠連": "waku_fuku", "2車連": "nisya_fuku", "3連勝": "sanren_fuku", "ワイド": "wide"}
ROW2_LABELS = ["waku_tan", "nisya_tan", "sanren_tan"]


def parse_refund_table(rt):
    """Parse one refund_table by label positions (dead-heat safe: a value cell
    may contain several winning combos for the same bet type)."""
    payouts = []
    trs = rt.find_all("tr")
    if trs:
        current = None
        for cell in [c.get_text("", strip=True) for c in trs[0].find_all(["th", "td"])]:
            if cell in ROW1_LABELS:
                current = ROW1_LABELS[cell]
                continue
            if cell in ("複", "連", ""):
                continue
            if current:
                for cm in COMBO_RE.finditer(cell):
                    payouts.append((current, cm.group(1), int(cm.group(2).replace(",", "")), int(cm.group(3))))
    if len(trs) >= 2:
        idx = -1
        current = None
        for cell in [c.get_text("", strip=True) for c in trs[1].find_all(["th", "td"])]:
            if cell == "単":
                idx += 1
                current = ROW2_LABELS[idx] if idx < len(ROW2_LABELS) else None
                continue
            if current:
                for cm in COMBO_RE.finditer(cell):
                    payouts.append((current, cm.group(1), int(cm.group(2).replace(",", "")), int(cm.group(3))))
    return payouts


ENTRY_FIELDS = ["race_id", "date", "segment", "frame_no", "car_no", "mark", "rider_name", "pref", "age", "term", "scratched",
                "rider_class", "style", "gear", "race_score", "s_count", "b_count", "nige", "makuri", "sashi",
                "mark_cnt", "w1", "w2", "w3", "out_cnt", "win_rate", "rate2", "rate3"]
RACE_FIELDS = ["race_id", "date", "venue_name", "meeting", "race_no", "race_name", "segment", "lineup"]
RESULT_FIELDS = ["race_id", "date", "finish_pos", "car_no"]
PAYOUT_FIELDS = ["race_id", "date", "bet_type", "combo", "payout", "popularity"]


def append_csv(path, rows, fields):
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def collect_day(d, tag, stats):
    ds = d.strftime("%Y/%m/%d")
    cal = d.strftime("%Y-%m-%d")
    idx_html = fetch(f"{BASE}/racecard/{ds}/")
    time.sleep(SLEEP)
    if idx_html is None:
        raise DayUnavailable(f"{cal}: no index")
    links = sorted(set(re.findall(r'href="(https://keirin\.kdreams\.jp/[a-z]+/racedetail/(\d{16})/)"', idx_html)))
    if not links:
        raise DayUnavailable(f"{cal}: no race links on index page")
    print(f"{cal}: {len(links)} races", flush=True)
    races, entries, results, payouts = [], [], [], []
    daycodes = {}
    for url, race_id in links:
        html = load_raw("racedetail", race_id)
        if html is None:
            html = fetch(url)
            time.sleep(SLEEP)
            if html is None:
                stats["detail_missing"] += 1
                continue
            save_raw("racedetail", race_id, html)
        race, ents = parse_detail(html, race_id, cal)
        races.append(race)
        entries.extend(ents)
        if not ents:
            stats["entry_empty"] += 1
            print(f"  ! no entries: {race_id}", flush=True)
        daycodes.setdefault(race_id[:12] + "00", url.split("/")[3])
    for daycode, slug in daycodes.items():
        html = load_raw("raceresult", daycode)
        if html is None:
            html = fetch(f"{BASE}/{slug}/raceresult/{daycode}/")
            time.sleep(SLEEP)
            if html is None:
                stats["result_missing"] += 1
                continue
            save_raw("raceresult", daycode, html)
        parsed = parse_result_page(html)
        by_no = {r["race_no"]: r["race_id"] for r in races if r["race_id"][:12] + "00" == daycode}
        for race_no_s, blk in parsed.items():
            rid = by_no.get(int(race_no_s))
            if not rid:
                continue
            for pos, car in blk["finish"]:
                results.append({"race_id": rid, "date": cal, "finish_pos": pos, "car_no": car})
            for bet, combo, pay, pop in blk["payouts"]:
                payouts.append({"race_id": rid, "date": cal, "bet_type": bet, "combo": combo, "payout": pay, "popularity": pop})
    outdir = ROOT / "data"
    append_csv(outdir / f"races_{tag}.csv", races, RACE_FIELDS)
    append_csv(outdir / f"entries_{tag}.csv", entries, ENTRY_FIELDS)
    append_csv(outdir / f"results_{tag}.csv", results, RESULT_FIELDS)
    append_csv(outdir / f"payouts_{tag}.csv", payouts, PAYOUT_FIELDS)
    stats["races"] += len(races)
    stats["entries"] += len(entries)
    stats["results"] += len(results)
    stats["payouts"] += len(payouts)
    print(f"{cal}: done races={len(races)} entries={len(entries)} results={len(results)} payouts={len(payouts)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    prog_path = ROOT / "data" / f"progress_{args.tag}.json"
    done = set()
    if prog_path.exists():
        done = set(json.loads(prog_path.read_text()).get("done", []))
    stats = {"races": 0, "entries": 0, "results": 0, "payouts": 0,
             "detail_missing": 0, "entry_empty": 0, "result_missing": 0}
    d = start
    failed = []
    while d <= end:
        key = d.isoformat()
        if key in done:
            print(f"{key}: skip (done)", flush=True)
        else:
            try:
                collect_day(d, args.tag, stats)
            except DayUnavailable as e:
                print(f"{key}: UNAVAILABLE {e}", flush=True)
                failed.append(key)
                d += timedelta(days=1)
                continue
            done.add(key)
            prog_path.write_text(json.dumps({"done": sorted(done), "stats": stats}, ensure_ascii=False, indent=1))
        d += timedelta(days=1)
    print("ALL DONE", stats, flush=True)
    if failed:
        print("INCOMPLETE", failed, flush=True)
        sys.exit(2)


if __name__ == "__main__":
    sys.exit(main())
