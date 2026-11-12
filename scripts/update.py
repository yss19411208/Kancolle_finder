#!/usr/bin/env python3
"""poi-server のダンプから ドロップ / 建造データを集計して JSON を出力する。

GitHub Actions から毎日実行される想定。手元で試す場合:
    pip install pymongo
    python scripts/update.py --state-dir state
    python scripts/update.py --state-dir state --drop-file dropshiprecords.gz --create-file createshiprecords.gz

入力 (リポジトリ直下):
    Normal_Drop_Ship.json   常駐ドロップ (管理画面で編集)
    admin_config.json       アップデート日時 / 終了日 / 手動限定 / 除外 (管理画面で編集)
出力:
    Drop_Data.json  Build_Data.json  Update.json  data/ship_master.json  data/map_routes.json
状態 (state ブランチに保存。月ごとの集計結果):
    state/drop_YYYY-MM.json.gz  state/build_YYYY-MM.json.gz
"""
import argparse
import bisect
import calendar
import gzip
import hashlib
import json
import os
import struct
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JST = timezone(timedelta(hours=9))
DT_FMT = "%Y/%m/%d %H:%M:%S"

DUMP_BASE = os.environ.get("POI_DUMP_BASE", "https://api.poi.moe/dump/")
# 過去月のアーカイブ。ファイル名は <種別>-<翌月1日0時(UTC)のUNIX時刻を16進>.gz
ARCHIVE_BASE = os.environ.get("POI_ARCHIVE_BASE", "https://db.poi.moe/")
STATE_VERSION = 2
MAX_ARCHIVE_MONTHS = 12  # さかのぼって取得する過去月の上限
DATA_START_MONTH = "2026-11"   # 常駐ドロップの率はこの月から毎月積み上げる
RATE_MODES = ("node_s", "node_all", "map_all")
SOURCES = [
    {"name": "poi-db ドロップ記録・建造記録 (poi-server)", "url": "https://github.com/poooi/poi-server/wiki",
     "use": "ドロップ率・限定ドロップ・建造レシピ"},
    {"name": "艦これ攻略Wiki 出撃ドロップ", "url": "https://wikiwiki.jp/kancolle/%E5%87%BA%E6%92%83%E3%83%89%E3%83%AD%E3%83%83%E3%83%97",
     "use": "常駐ドロップの一覧"},
    {"name": "api_start2 (Tibowl)", "url": "https://github.com/Tibowl/api_start2", "use": "艦ID→艦名・艦種"},
    {"name": "poi 海域データ (fcd/map.json)", "url": "https://github.com/poooi/poi", "use": "セルID→マス名"},
    {"name": "KC3改 edges.json", "url": "https://github.com/KC3Kai/KC3Kai", "use": "セルID→マス名の補完"},
]
START2_URLS = [
    "https://raw.githubusercontent.com/Tibowl/api_start2/master/start2.json",
]
POI_MAP_URL = "https://raw.githubusercontent.com/poooi/poi/master/assets/data/fcd/map.json"
KC3_EDGES_URL = "https://raw.githubusercontent.com/KC3Kai/KC3Kai/master/src/data/edges.json"

TYPE_LABELS = ["戦艦級", "航空母艦", "重巡級", "軽巡級", "駆逐艦", "海防艦", "潜水艦", "補助艦艇"]
# api_stype -> 8 分類の添字 (艦これWikiの出撃ドロップ表と同じ並び)
STYPE_TI = {8: 0, 9: 0, 10: 0, 12: 0, 7: 1, 11: 1, 18: 1, 5: 2, 6: 2,
            3: 3, 4: 3, 21: 3, 2: 4, 1: 5, 13: 6, 14: 6}
MAX_WORLD = 7            # 通常海域のみ (イベント海域は対象外)
BUILD_TOP_RECIPES = 60   # 1 艦あたりに出力するレシピ数の上限
BUILD_TOP_SECRETARY = 12
HISTORY_DETAIL_MAX = 20  # 更新履歴に個別に書く件数の上限 (超えたら件数だけ)


def log(*a):
    print(*a, flush=True)


def warn(msg):
    print(f"::warning::{msg}", flush=True)


# ---------------------------------------------------------------- 入出力
def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def http_get(url, dest=None, timeout=120, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Kancolle_finder-updater (+https://github.com/yss19411208/Kancolle_finder)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if dest is None:
                    return r.read()
                size = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        size += len(chunk)
                return size
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  取得失敗 ({i + 1}/{tries}) {url}: {e}")
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"取得できませんでした: {url}: {last}")


def parse_dt(s):
    """'YYYY/MM/DD HH:MM:SS' (日本時間) -> epoch 秒。秒や時刻は省略可。"""
    s = (s or "").strip().replace("-", "/").replace("T", " ")
    for fmt in (DT_FMT, "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=JST).timestamp())
        except ValueError:
            pass
    raise ValueError(f"日時の形式が不正です: {s!r}")


def fmt_dt(epoch):
    return datetime.fromtimestamp(epoch, JST).strftime(DT_FMT)


# ---------------------------------------------------------------- マスターデータ
def norm_name(s):
    return "".join(str(s).split()).replace("　", "").lower()


def refresh_masters(offline=False):
    """艦ID->艦名、海域のセルID->マス名 を取得。失敗時はリポジトリ内のキャッシュを使う。"""
    ship_path = os.path.join(ROOT, "data", "ship_master.json")
    route_path = os.path.join(ROOT, "data", "map_routes.json")
    ships = load_json(ship_path, {})
    routes = load_json(route_path, {})
    if not offline:
        for url in START2_URLS:
            try:
                s2 = json.loads(http_get(url, timeout=60))
                new = {}
                for s in s2["api_mst_ship"]:
                    if s["api_id"] >= 1500 or not s.get("api_name"):
                        continue  # 1500 以上は深海棲艦
                    new[str(s["api_id"])] = {
                        "name": s["api_name"],
                        "ti": STYPE_TI.get(s.get("api_stype"), 7),
                        "base": 1 if s.get("api_sortno") or s.get("api_sort_id") else 0,
                    }
                if len(new) > 300:
                    if new != ships:
                        save_json(ship_path, new, compact=True)
                    ships = new
                    break
            except Exception as e:  # noqa: BLE001
                warn(f"艦マスターの更新に失敗 (キャッシュを使用): {e}")
        try:
            new = {}
            data = json.loads(http_get(POI_MAP_URL, timeout=60))["data"]
            for m, v in data.items():
                a = m.split("-")
                if len(a) == 2 and a[0].isdigit() and int(a[0]) <= MAX_WORLD:
                    new[m] = {e: r[1] for e, r in v.get("route", {}).items() if r and r[1]}
            try:  # poi に無い辺は KC3改 のデータで補う
                kc3 = json.loads(http_get(KC3_EDGES_URL, timeout=60))
                for k, v in kc3.items():
                    m = k.replace("World ", "")
                    a = m.split("-")
                    if len(a) == 2 and a[0].isdigit() and int(a[0]) <= MAX_WORLD:
                        d = new.setdefault(m, {})
                        for e, r in v.items():
                            d.setdefault(e, r[1])
            except Exception as e:  # noqa: BLE001
                log(f"  KC3 edges は取得できず (poi のみ使用): {e}")
            if len(new) >= 30:
                if new != routes:
                    save_json(route_path, new, compact=True)
                routes = new
        except Exception as e:  # noqa: BLE001
            warn(f"海域ルートの更新に失敗 (キャッシュを使用): {e}")
    if not ships or not routes:
        raise SystemExit("マスターデータがありません (data/ship_master.json, data/map_routes.json)")
    return ships, routes


# ---------------------------------------------------------------- mongodump アーカイブ
def iter_archive_docs(path):
    """mongodump --archive (gzip) を流し読みして BSON ドキュメントを順に返す。

    アーカイブは [magic][header][collection metadata...][0xFFFFFFFF] の後に
    [namespace header][doc...][0xFFFFFFFF] が繰り返される。データ以外の
    ドキュメント (_id を持たない) は呼び出し側で読み飛ばす。
    """
    import bson  # pymongo 同梱

    opener = gzip.open
    with open(path, "rb") as probe:
        if probe.read(2) != b"\x1f\x8b":
            opener = open
    with opener(path, "rb") as f:
        head = f.read(4)
        if head != b"\x6d\xe2\x99\x81":      # magic が無ければ素の BSON 連結として扱う
            buf = head
        else:
            buf = b""
        read = f.read
        while True:
            sz = buf + read(4 - len(buf))
            buf = b""
            if len(sz) < 4:
                return
            if sz == b"\xff\xff\xff\xff":
                continue
            n = struct.unpack("<i", sz)[0]
            if n < 5 or n > 16 * 1024 * 1024:
                raise ValueError(f"BSON のサイズが不正です ({n})。ダンプ形式が変わった可能性があります")
            body = read(n - 4)
            if len(body) < n - 4:
                warn("ダンプが途中で切れています")
                return
            yield bson.decode(sz + body)


def oid_ts(doc):
    oid = doc.get("_id")
    try:
        return int.from_bytes(oid.binary[:4], "big")
    except AttributeError:
        return None


def month_of(ts):
    return time.strftime("%Y-%m", time.gmtime(ts))  # ダンプは UTC の月初でリセットされる


def month_start(month):
    y, m = map(int, month.split("-"))
    return calendar.timegm((y, m, 1, 0, 0, 0))


def next_month(month):
    y, m = map(int, month.split("-"))
    return f"{y + (m == 12)}-{m % 12 + 1:02d}"


def archive_url(kind_file, month):
    """month (UTC) の全記録が入った過去月アーカイブの URL。"""
    return f"{ARCHIVE_BASE}{kind_file}-{month_start(next_month(month)):x}.gz"


# ---------------------------------------------------------------- ドロップ集計
def aggregate_drops(path, routes, ships, resident_ids, cuts=()):
    """戻り値: {month: state}。

    state = {
      month, records, first, last,
      tot:  {map: {node: {"b": bossか, "S": [戦闘数, {艦ID: 数}], "O": [..A/B勝利..]}}},
      hden: {map: {node: {区間の開始時刻(epoch秒): [S戦闘数, A/B戦闘数]}}},
      hsid: {map: {node: {艦ID: {区間の開始時刻: [S, A/B]}}}}   # 常駐リストに無い艦のみ
    }
    区間は 1 時間ごと。さらに cuts (登録済みのアップデート日時) でも区切るので、
    登録済みの日時については秒単位で正確に「それ以降」だけを取り出せる。
    """
    cuts = sorted(set(cuts))
    cnt = defaultdict(int)       # (month, mapId, cell, rank, sid) -> n
    hden = defaultdict(int)      # (month, mapId, cell, hour, rank) -> n
    hsid = defaultdict(int)      # (month, mapId, cell, hour, rank, sid) -> n
    boss = set()
    meta = {}
    total = used = 0
    for d in iter_archive_docs(path):
        if "mapId" not in d or "_id" not in d:
            continue
        total += 1
        try:
            map_id = int(d["mapId"])
            if map_id // 10 > MAX_WORLD or map_id < 11 or (d.get("mapLv") or 0) != 0:
                continue
            rank = d.get("rank")
            if rank == "S":
                rc = 0
            elif rank in ("A", "B"):
                rc = 1
            else:
                continue
            ts = oid_ts(d)
            if ts is None:
                continue
            cell = int(d.get("cellId", -1))
            sid = int(d.get("shipId") or 0)
            if sid < 0:
                sid = 0
        except (TypeError, ValueError):
            continue
        mo = month_of(ts)
        hour = ts - ts % 3600
        if cuts:
            i = bisect.bisect_right(cuts, ts)
            if i and cuts[i - 1] > hour:
                hour = cuts[i - 1]
        used += 1
        cnt[(mo, map_id, cell, rc, sid)] += 1
        hden[(mo, map_id, cell, hour, rc)] += 1
        if sid and (map_id, cell, sid) not in resident_ids:
            hsid[(mo, map_id, cell, hour, rc, sid)] += 1
        if d.get("isBoss"):
            boss.add((map_id, cell))
        m = meta.setdefault(mo, [0, ts, ts])
        m[0] += 1
        if ts < m[1]:
            m[1] = ts
        if ts > m[2]:
            m[2] = ts
    log(f"  ドロップ: {total:,} 件中 {used:,} 件を集計 (通常海域・S/A/B勝利)")

    def place(map_id, cell):
        m = f"{map_id // 10}-{map_id % 10}"
        return m, routes.get(m, {}).get(str(cell), f"#{cell}")

    out = {}
    for mo, (n, first, last) in meta.items():
        out[mo] = {"v": STATE_VERSION, "month": mo, "records": n, "first": first, "last": last,
                   "cuts": cuts, "final": False, "tot": {}, "hden": {}, "hsid": {}}
    unknown = set()
    for (mo, map_id, cell, rc, sid), n in cnt.items():
        m, node = place(map_id, cell)
        if node.startswith("#"):
            unknown.add(f"{m}{node}")
        t = out[mo]["tot"].setdefault(m, {}).setdefault(node, {"b": 0, "S": [0, {}], "O": [0, {}]})
        if (map_id, cell) in boss:
            t["b"] = 1
        r = t["S" if rc == 0 else "O"]
        r[0] += n
        if sid:
            r[1][str(sid)] = r[1].get(str(sid), 0) + n
    for (mo, map_id, cell, hour, rc), n in hden.items():
        m, node = place(map_id, cell)
        h = out[mo]["hden"].setdefault(m, {}).setdefault(node, {}).setdefault(str(hour), [0, 0])
        h[rc] += n
    for (mo, map_id, cell, hour, rc, sid), n in hsid.items():
        m, node = place(map_id, cell)
        h = (out[mo]["hsid"].setdefault(m, {}).setdefault(node, {})
             .setdefault(str(sid), {}).setdefault(str(hour), [0, 0]))
        h[rc] += n
    if unknown:
        warn("マス名が不明なセルがあります (#番号で表示): " + ", ".join(sorted(unknown)[:30]))
    return out


# ---------------------------------------------------------------- 建造集計
def aggregate_builds(path):
    """戻り値: {month: {month, records, recipes: {"燃/弾/鋼/ボ/資材": {n, large, ships: {艦ID: [数, {秘書艦ID: 数}]}}}}}"""
    out = {}
    total = 0
    for d in iter_archive_docs(path):
        if "items" not in d or "_id" not in d:
            continue
        try:
            items = [int(x) for x in d["items"]][:5]
            sid = int(d.get("shipId") or 0)
            sec = int(d.get("secretary") or 0)
            ts = oid_ts(d)
        except (TypeError, ValueError):
            continue
        if len(items) < 5 or sid <= 0 or ts is None:
            continue
        total += 1
        st = out.setdefault(month_of(ts), {"month": month_of(ts), "records": 0, "recipes": {}})
        st["records"] += 1
        r = st["recipes"].setdefault("/".join(map(str, items)), {"n": 0, "large": 0, "ships": {}})
        r["n"] += 1
        if d.get("largeFlag"):
            r["large"] = 1
        s = r["ships"].setdefault(str(sid), [0, {}])
        s[0] += 1
        s[1][str(sec)] = s[1].get(str(sec), 0) + 1
    log(f"  建造: {total:,} 件を集計")
    return out


# ---------------------------------------------------------------- 状態 (月ごと)
def state_path(state_dir, kind, month):
    return os.path.join(state_dir, f"{kind}_{month}.json.gz")


def load_state(state_dir, kind):
    res = {}
    if not os.path.isdir(state_dir):
        return res
    for fn in sorted(os.listdir(state_dir)):
        if fn.startswith(kind + "_") and fn.endswith(".json.gz"):
            try:
                with gzip.open(os.path.join(state_dir, fn), "rt", encoding="utf-8") as f:
                    st = json.load(f)
                if kind == "drop" and st.get("v") != STATE_VERSION:
                    log(f"  {fn}: 旧形式のため作り直します")
                    continue
                res[st["month"]] = st
            except (OSError, ValueError, KeyError) as e:
                warn(f"状態ファイルを読めません: {fn}: {e}")
    return res


def merge_state(state_dir, kind, old, new, force=False):
    """ダンプは当月分の全件なので、月ごとに丸ごと置き換える。
    月初リセット直後など件数が減っている場合は、保存済みの方を残す。
    force=True (過去月アーカイブ由来) は常に置き換える。"""
    os.makedirs(state_dir, exist_ok=True)
    for mo, st in new.items():
        prev = old.get(mo)
        if not force and prev and (prev.get("final") or prev.get("records", 0) > st["records"]):
            log(f"  {kind} {mo}: 保存済み {prev['records']:,} 件 > 今回 {st['records']:,} 件 のため据え置き")
            continue
        old[mo] = st
        with gzip.open(state_path(state_dir, kind, mo), "wt", encoding="utf-8", compresslevel=6) as f:
            json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
        log(f"  {kind} {mo}: {st['records']:,} 件を保存")
    return old


# ---------------------------------------------------------------- 設定
def load_config():
    cfg = load_json(os.path.join(ROOT, "admin_config.json"), {}) or {}
    cfg.setdefault("updates", [])
    cfg.setdefault("limited_manual", [])
    cfg.setdefault("limited_exclude", [])
    if cfg.get("rate_mode") not in RATE_MODES:
        cfg["rate_mode"] = "node_s"
    return cfg


def effective_cutoff(cfg, now):
    """現在時刻以前で最も新しいアップデート日時。未来の日時はその時刻になるまで無視。"""
    best = None
    for u in cfg["updates"]:
        try:
            t = parse_dt(u.get("at"))
        except ValueError as e:
            warn(str(e))
            continue
        if t <= now and (best is None or t > best):
            best = t
    return best


# ---------------------------------------------------------------- 出力の計算
def build_drop_data(states, resident, cfg, ships, now):
    name_of = {int(k): v["name"] for k, v in ships.items()}
    ti_of = {int(k): v["ti"] for k, v in ships.items()}
    id_by_norm = {}
    for k, v in sorted(ships.items(), key=lambda kv: int(kv[0])):
        id_by_norm.setdefault(norm_name(v["name"]), int(k))

    cutoff = effective_cutoff(cfg, now)
    base_start = cutoff if cutoff is not None else 0
    exclude = {(e.get("map"), e.get("node"), norm_name(e.get("ship", ""))) for e in cfg["limited_exclude"]}

    def start_for(m, node, name):
        return base_start

    months = sorted(states)
    res_months = [mo for mo in months if mo >= DATA_START_MONTH]   # 常駐の率に使う月
    mode = cfg.get("rate_mode", "node_s")
    den_cache = {}

    def den_since(m, node, start):
        key = (m, node, start)
        if key not in den_cache:
            a = b = 0
            for mo in months:
                for h, (s, o) in states[mo]["hden"].get(m, {}).get(node, {}).items():
                    if int(h) >= start:
                        a += s
                        b += o
            den_cache[key] = (a, b)
        return den_cache[key]

    def rate(ds, do, ns, no, m=None, start=None):
        """率 [%], ドロップ数, 分母, 基準 を返す。
        node_s  : そのマスの S勝利のうち何回出たか (S勝利で出ていなければ A/B も含める)
        node_all: そのマスの S/A/B 勝利のうち何回出たか
        map_all : その海域の全マスの S/A/B 勝利のうち、このマスで何回出たか
        """
        if mode == "map_all":
            n = map_den(m, start)
            d = ds + do
            return (round(d / n * 100, 2 if d / n >= 0.0001 else 3), d, n, "MAP") if d and n else (None, 0, n, "")
        if mode == "node_s" and ds and ns:
            return round(ds / ns * 100, 2), ds, ns, "S"
        if (ds + do) and (ns + no):
            return round((ds + do) / (ns + no) * 100, 2), ds + do, ns + no, "SAB"
        return None, 0, ns if mode == "node_s" else ns + no, ""

    map_cache = {}

    def map_den(m, start):
        """海域全体の S/A/B 勝利数。start=None は常駐用 (積み上げ全期間)、それ以外は start 以降。"""
        key = (m, start)
        if key not in map_cache:
            n = 0
            if start is None:
                for mo in res_months:
                    for t in states[mo]["tot"].get(m, {}).values():
                        n += t["S"][0] + t["O"][0]
            else:
                for mo in months:
                    for hours in states[mo]["hden"].get(m, {}).values():
                        for h, (a, b) in hours.items():
                            if int(h) >= start:
                                n += a + b
            map_cache[key] = n
        return map_cache[key]

    maps = {}
    all_places = set()
    for m, nodes in resident.items():
        for node in nodes:
            all_places.add((m, node))
    for mo in months:
        for m, nodes in states[mo]["tot"].items():
            for node in nodes:
                all_places.add((m, node))
    for e in cfg["limited_manual"]:
        if e.get("map") and e.get("node"):
            all_places.add((e["map"], e["node"]))

    for m, node in sorted(all_places):
        if node.startswith("#"):
            continue  # マス名が不明なセルは掲載しない (ログに警告を出す)
        res_cells = resident.get(m, {}).get(node, [])
        res_names = {}
        for ti, cell in enumerate(res_cells):
            for nm in cell:
                res_names[norm_name(nm)] = (nm, ti)
        # 全期間の合計
        nS = nO = 0
        dS, dO = defaultdict(int), defaultdict(int)
        boss = 0
        for mo in months:
            t = states[mo]["tot"].get(m, {}).get(node)
            if not t:
                continue
            boss |= t.get("b", 0)
            if mo not in res_months:
                continue
            nS += t["S"][0]
            nO += t["O"][0]
            for sid, n in t["S"][1].items():
                dS[int(sid)] += n
            for sid, n in t["O"][1].items():
                dO[int(sid)] += n
        rows = []
        seen = set()
        # 常駐
        for nn, (nm, ti) in res_names.items():
            sid = id_by_norm.get(nn)
            r, d, n, basis = rate(dS.get(sid, 0), dO.get(sid, 0), nS, nO, m) if sid else (None, 0, nS, "")
            rows.append([nm, ti, 0, r, d, n, basis, ""])
            seen.add(nn)
        # 限定 (poi で観測され、常駐リストに無い艦)
        cand = set()
        for mo in months:
            t = states[mo]["tot"].get(m, {}).get(node)
            if t:
                cand.update(int(x) for x in t["S"][1])
                cand.update(int(x) for x in t["O"][1])
        for sid in cand:
            nm = name_of.get(sid, f"ID:{sid}")
            nn = norm_name(nm)
            if nn in seen or (m, node, nn) in exclude:
                continue
            start = start_for(m, node, nm)
            ds = do = 0
            for mo in months:
                st = states[mo]
                hs = st["hsid"].get(m, {}).get(node, {}).get(str(sid))
                if hs is not None:
                    for h, (s, o) in hs.items():
                        if int(h) >= start:
                            ds += s
                            do += o
                else:
                    # 集計時点では常駐扱いだった艦: 月全体が基準日時以降の場合のみ合算
                    t = st["tot"].get(m, {}).get(node)
                    if t and st["first"] >= start:
                        ds += t["S"][1].get(str(sid), 0)
                        do += t["O"][1].get(str(sid), 0)
            if not (ds + do):
                continue
            ns, no = den_since(m, node, start)
            r, d, n, basis = rate(ds, do, ns, no, m, start)
            rows.append([nm, ti_of.get(sid, 7), 1, r, d, n, basis, "auto"])
            seen.add(nn)
        # 手動で追加した限定 (追加日時が基準日時より前のものは自動的に外れる)
        for e in cfg["limited_manual"]:
            if e.get("map") != m or e.get("node") != node or not e.get("ship"):
                continue
            nn = norm_name(e["ship"])
            if nn in seen:
                continue
            try:
                added = parse_dt(e.get("added")) if e.get("added") else now
            except ValueError:
                added = now
            if added < start_for(m, node, e["ship"]):
                continue
            sid = id_by_norm.get(nn)
            rows.append([name_of.get(sid, e["ship"]), ti_of.get(sid, 7), 1, None, 0, 0, "", "manual"])
            seen.add(nn)
        if not rows:
            continue
        rows.sort(key=lambda r: (r[1], r[2], r[0]))
        maps.setdefault(m, {})[node] = {"boss": boss, "n": nS, "ships": rows}

    first = min((states[mo]["first"] for mo in res_months), default=None)
    last = max((states[mo]["last"] for mo in months), default=None)
    return {
        "generated": fmt_dt(now),
        "data_from": fmt_dt(first) if first else None,
        "data_to": fmt_dt(last) if last else None,
        "cutoff": fmt_dt(cutoff) if cutoff is not None else None,
        "records": sum(states[mo]["records"] for mo in res_months),
        "months": res_months,
        "limited_months": [mo for mo in months if cutoff is not None and mo >= month_of(cutoff)] or res_months,
        "rate_mode": mode,
        "sources": SOURCES,
        "types": TYPE_LABELS,
        "columns": ["name", "type", "limited", "rate", "drops", "battles", "basis", "source"],
        "maps": maps,
    }


BUILD_SCORE_K = 100   # スコアで「十分な試行数」とみなす目安


def build_score(hit, n):
    """建造レシピのスコア = 確率[%] × 試行数 / (試行数 + 100)。
    試行数が少ないレシピは大きく割り引かれ、試行数が多く確率も高いレシピが上位になる。
    (例: 1/1 回 = 100% は 0.99 点、3/26 回 = 11.5% は 2.38 点、127/4013 回 = 3.16% は 3.09 点)"""
    if not n:
        return 0.0
    return round(hit / n * 100 * n / (n + BUILD_SCORE_K), 2)


def build_build_data(states, ships, now):
    name_of = {int(k): v["name"] for k, v in ships.items()}
    ti_by_name = {v["name"]: v["ti"] for v in ships.values()}
    recipes = {}
    for mo in sorted(states):
        for key, r in states[mo]["recipes"].items():
            a = recipes.setdefault(key, {"n": 0, "large": 0, "ships": {}})
            a["n"] += r["n"]
            a["large"] |= r.get("large", 0)
            for sid, (n, secs) in r["ships"].items():
                s = a["ships"].setdefault(sid, [0, {}])
                s[0] += n
                for sec, c in secs.items():
                    s[1][sec] = s[1].get(sec, 0) + c
    by_ship = defaultdict(list)
    for key, r in recipes.items():
        recipe = [int(x) for x in key.split("/")]
        for sid, (n, secs) in r["ships"].items():
            top = sorted(secs.items(), key=lambda kv: -kv[1])[:BUILD_TOP_SECRETARY]
            by_ship[name_of.get(int(sid), f"ID:{sid}")].append({
                "recipe": recipe,
                "Count": r["n"],
                "hit": n,
                "rate": round(n / r["n"] * 100, 2),
                "score": build_score(n, r["n"]),
                "big": bool(r["large"]),
                "FirstShip": [name_of.get(int(s), f"ID:{s}") for s, _ in top],
            })
    ships_out = {}
    for name, lst in by_ship.items():
        lst.sort(key=lambda e: (-e["score"], -e["Count"]))
        ships_out[name] = lst[:BUILD_TOP_RECIPES]
    # 掲載するレシピごとに、出やすい艦種と出やすい艦 TOP5
    used = {"/".join(map(str, e["recipe"])) for lst in ships_out.values() for e in lst}
    recipe_out = {}
    for key in used:
        r = recipes[key]
        by_type = defaultdict(int)
        by_ship = []
        for sid, (n, _) in r["ships"].items():
            nm = name_of.get(int(sid), f"ID:{sid}")
            by_type[ti_by_name.get(nm, 7)] += n
            by_ship.append((n, nm))
        by_ship.sort(key=lambda x: (-x[0], x[1]))
        recipe_out[key] = {
            "n": r["n"],
            "types": [[ti, round(c / r["n"] * 100, 1)] for ti, c in sorted(by_type.items(), key=lambda kv: -kv[1])],
            "top": [[nm, round(c / r["n"] * 100, 1)] for c, nm in by_ship[:5]],
        }
    return {
        "generated": fmt_dt(now),
        "records": sum(states[mo]["records"] for mo in states),
        "months": sorted(states),
        "types": {n: ti_by_name.get(n, 7) for n in sorted(ships_out)},
        "type_labels": TYPE_LABELS,
        "recipes": recipe_out,
        "ships": dict(sorted(ships_out.items())),
    }


# ---------------------------------------------------------------- 更新履歴
def drop_sets(data):
    normal, limited = set(), set()
    for m, nodes in (data or {}).get("maps", {}).items():
        for node, v in nodes.items():
            for row in v["ships"]:
                (limited if row[2] else normal).add((m, node, row[0]))
    return normal, limited


def map_key(m):
    a = m.split("-")
    return tuple(int(x) if x.isdigit() else 99 for x in a)


def append_history(old, new, now):
    if not old or not old.get("maps"):
        return False
    on, ol = drop_sets(old)
    nn, nl = drop_sets(new)
    items = []
    for label, added, removed in (("常駐ドロップ", nn - on, on - nn), ("限定ドロップ", nl - ol, ol - nl)):
        for verb, s in (("追加", added), ("終了" if label == "限定ドロップ" else "削除", removed)):
            if not s:
                continue
            if len(s) > HISTORY_DETAIL_MAX:
                items.append(f"{label}{verb}: {len(s)}件")
                continue
            grouped = defaultdict(list)
            for m, node, name in s:
                grouped[(m, node)].append(name)
            for (m, node), names in sorted(grouped.items(), key=lambda kv: (map_key(kv[0][0]), kv[0][1])):
                items.append(f"{label}{verb}: {m}-{node} {'、'.join(sorted(names))}")
    if old.get("cutoff") != new.get("cutoff") and new.get("cutoff"):
        items.insert(0, f"アップデート({new['cutoff']})以降のデータで限定ドロップを再集計")
    if not items:
        return False
    path = os.path.join(ROOT, "Update.json")
    hist = load_json(path, [])
    if not isinstance(hist, list):
        hist = []
    today = datetime.fromtimestamp(now, JST).strftime("%Y/%m/%d")
    if hist and hist[-1].get("auto") and hist[-1].get("更新日時") == today:
        for it in items:
            if it not in hist[-1]["更新内容"]:
                hist[-1]["更新内容"].append(it)
    else:
        hist.append({"更新日時": today, "更新内容": items, "auto": True})
    save_json(path, hist)
    log("  更新履歴に追記: " + " / ".join(items[:5]) + (" ..." if len(items) > 5 else ""))
    return True


# ---------------------------------------------------------------- main
def resident_id_set(resident, ships, routes):
    id_by_norm = {}
    for k, v in sorted(ships.items(), key=lambda kv: int(kv[0])):
        id_by_norm.setdefault(norm_name(v["name"]), int(k))
    res = set()
    unknown = set()
    for m, nodes in resident.items():
        a = m.split("-")
        map_id = int(a[0]) * 10 + int(a[1])
        cells = defaultdict(list)
        for e, n in routes.get(m, {}).items():
            cells[n].append(int(e))
        for node, cols in nodes.items():
            for col in cols:
                for nm in col:
                    sid = id_by_norm.get(norm_name(nm))
                    if sid is None:
                        unknown.add(nm)
                        continue
                    for c in cells.get(node, []):
                        res.add((map_id, c, sid))
    if unknown:
        warn("常駐リストに艦マスターと一致しない艦名があります: " + ", ".join(sorted(unknown)[:30]))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=os.path.join(ROOT, "state"))
    ap.add_argument("--drop-file", help="ダウンロード済みの dropshiprecords.gz を使う")
    ap.add_argument("--create-file", help="ダウンロード済みの createshiprecords.gz を使う")
    ap.add_argument("--no-download", action="store_true", help="ダンプを取得せず、保存済みの状態だけで再出力")
    ap.add_argument("--offline", action="store_true", help="マスターデータも取得しない")
    ap.add_argument("--smart", action="store_true",
                    help="当日すでにダンプを取得済みで、アップデート日時と常駐リストが同じなら再取得しない")
    ap.add_argument("--now", help="現在時刻を上書き (テスト用, 'YYYY/MM/DD HH:MM:SS' 日本時間)")
    args = ap.parse_args()

    now = parse_dt(args.now) if args.now else int(time.time())
    log(f"実行時刻: {fmt_dt(now)} (日本時間)")
    ships, routes = refresh_masters(offline=args.offline)
    log(f"艦マスター {len(ships)} 件 / 海域ルート {len(routes)} 海域")
    resident = load_json(os.path.join(ROOT, "Normal_Drop_Ship.json"), {}) or {}
    cfg = load_config()

    drop_states = load_state(args.state_dir, "drop")
    build_states = load_state(args.state_dir, "build")
    failed = []
    res_ids = resident_id_set(resident, ships, routes)
    cuts = []
    for u in cfg["updates"]:
        try:
            cuts.append(parse_dt(u.get("at")))
        except ValueError:
            pass
    cutoff = effective_cutoff(cfg, now)
    # ダンプは 24 時間に 1 回しか更新されない。除外や手動追加だけの変更なら取得し直す必要はない
    meta_path = os.path.join(args.state_dir, "meta.json")
    meta = load_json(meta_path, {}) or {}
    sig = hashlib.sha1(json.dumps([sorted(cuts), resident], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    skip_dump = (args.smart and meta.get("sig") == sig and now - meta.get("fetched_at", 0) < 20 * 3600
                 and month_of(now) in drop_states)
    if skip_dump:
        log(f"当月ダンプは {fmt_dt(meta['fetched_at'])} に取得済みのため再取得しません")
    with tempfile.TemporaryDirectory() as tmp:
        jobs = [("drop", args.drop_file, "dropshiprecords.gz"),
                ("build", args.create_file, "createshiprecords.gz")]
        for kind, given, fname in jobs:
            path = given
            if not path and not args.no_download and not skip_dump:
                path = os.path.join(tmp, fname)
                try:
                    log(f"ダウンロード: {DUMP_BASE}{fname}")
                    size = http_get(DUMP_BASE + fname, dest=path, timeout=300)
                    log(f"  {size / 1e6:.1f} MB")
                except Exception as e:  # noqa: BLE001
                    warn(f"{fname} を取得できませんでした。保存済みデータで続行します: {e}")
                    failed.append(fname)
                    path = None
            if not path:
                continue
            try:
                if kind == "drop":
                    new = aggregate_drops(path, routes, ships, res_ids, cuts)
                    drop_states = merge_state(args.state_dir, "drop", drop_states, new)
                    if not given:
                        os.makedirs(args.state_dir, exist_ok=True)
                        save_json(meta_path, {"sig": sig, "fetched_at": now})
                else:
                    new = aggregate_builds(path)
                    build_states = merge_state(args.state_dir, "build", build_states, new)
            except Exception as e:  # noqa: BLE001
                warn(f"{fname} の解析に失敗しました。保存済みデータで続行します: {e}")
                failed.append(fname)
            if not given:
                try:
                    os.remove(path)   # 解析が済んだダンプは残さない
                except OSError:
                    pass

        # 有効なアップデート日時が過去の月にある場合、その月から前月までのアーカイブを取り込む。
        # (例: 10/22 のアップデートなら 10 月分。11 月分は上の当月ダンプ)
        if cutoff is not None and not args.no_download:
            cur = month_of(now)
            months = []
            mo = month_of(cutoff)
            while mo < cur:
                months.append(mo)
                mo = next_month(mo)
            if len(months) > MAX_ARCHIVE_MONTHS:
                warn(f"アップデート日時が古いため、直近 {MAX_ARCHIVE_MONTHS} か月分だけ取り込みます")
                months = months[-MAX_ARCHIVE_MONTHS:]
            for mo in months:
                st = drop_states.get(mo)
                in_month = month_start(mo) <= cutoff < month_start(next_month(mo))
                if st and st.get("final") and (not in_month or cutoff in st.get("cuts", [])):
                    continue   # 取り込み済み
                url = archive_url("dropshiprecords", mo)
                path = os.path.join(tmp, f"archive_{mo}.gz")
                try:
                    log(f"過去月アーカイブ {mo}: {url}")
                    size = http_get(url, dest=path, timeout=600)
                    log(f"  {size / 1e6:.1f} MB")
                    new = aggregate_drops(path, routes, ships, res_ids, cuts)
                    if mo not in new:
                        raise RuntimeError(f"{mo} の記録が含まれていません")
                    new[mo]["final"] = True
                    drop_states = merge_state(args.state_dir, "drop", drop_states, {mo: new[mo]}, force=True)
                except Exception as e:  # noqa: BLE001
                    warn(f"{mo} の過去月アーカイブを取り込めませんでした (月初 1〜2 日は未公開のことがあります): {e}")
                finally:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    old_drop = load_json(os.path.join(ROOT, "Drop_Data.json"))
    drop = build_drop_data(drop_states, resident, cfg, ships, now)
    build = build_build_data(build_states, ships, now)
    append_history(old_drop, drop, now)

    # 中身が変わらない日はファイルを書き換えない (無駄なコミットを避ける)
    def same(a, b):
        strip = lambda d: {k: v for k, v in (d or {}).items() if k != "generated"}  # noqa: E731
        return strip(a) == strip(b)

    if not same(old_drop, drop):
        save_json(os.path.join(ROOT, "Drop_Data.json"), drop, compact=True)
    if not same(load_json(os.path.join(ROOT, "Build_Data.json")), build):
        save_json(os.path.join(ROOT, "Build_Data.json"), build, compact=True)
    n_lim = sum(1 for m in drop["maps"].values() for v in m.values() for r in v["ships"] if r[2])
    log(f"出力: {len(drop['maps'])} 海域 / 限定 {n_lim} 件 / 建造 {len(build['ships'])} 艦 "
        f"/ 基準日時 {drop['cutoff'] or 'なし'}")
    if failed and len(failed) == 2 and not drop_states:
        sys.exit("ダンプを取得できず、保存済みデータもありません")


if __name__ == "__main__":
    main()
