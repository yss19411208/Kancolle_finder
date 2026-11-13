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
STATE_VERSION = 4
HISTORY_MONTHS = 12       # 常設の判定に使う期間 (か月)。admin_config.json の history_months で変更できる
ARCHIVE_PER_RUN = 4       # 1 回の実行で取り込む過去月の数 (毎朝 5 回起動するので 1 日で約 20 か月進む)
DAY = 86400
WEEK = 7 * DAY
GAP_EXPECT = 8            # その期間に「8 回以上は出るはず」なのに 0 回なら、本当に出ていないとみなす (偶然 0 回の確率は約 0.03%)
NEW_DAYS = 60             # 出始めてからこの日数より短いものは「最近出始めた」(限定の可能性) とする
RATE_MODES = ("node_s", "node_all", "map_all")
SOURCES = [
    {"name": "poi-db ドロップ記録・建造記録 (poi-server)", "url": "https://github.com/poooi/poi-server/wiki",
     "use": "ドロップ率・常設/限定の判定・建造レシピ"},
    {"name": "艦これ攻略Wiki 期間限定ドロップイベント", "url": "https://wikiwiki.jp/kancolle/%E6%9C%9F%E9%96%93%E9%99%90%E5%AE%9A%E3%83%89%E3%83%AD%E3%83%83%E3%83%97%E3%82%A4%E3%83%99%E3%83%B3%E3%83%88",
     "use": "限定ドロップの確認"},
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
HISTORY_DETAIL_MAX = 20
LOG_KEEP = 40             # 管理画面に出す「データ更新の記録」の保持件数
LOG_DETAIL_MAX = 15
SINGLE_MIN_BATTLES = 2000    # 報告 1 件だけで、そのマスの戦闘数がこれ以上なら誤報告とみなして掲載しない  # 更新履歴に個別に書く件数の上限 (超えたら件数だけ)


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


def http_head(url, timeout=30):
    """ダンプの更新を調べるため、Last-Modified と Content-Length だけを取る。失敗したら None。"""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": "Kancolle_finder-updater (+https://github.com/yss19411208/Kancolle_finder)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            lm, cl = r.headers.get("Last-Modified"), r.headers.get("Content-Length")
            return f"{lm}|{cl}" if (lm or cl) else None
    except Exception as e:  # noqa: BLE001
        log(f"  更新の確認に失敗 ({url}): {e}")
        return None


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
def day_bucket(ts, cuts):
    """日本時間の 1 日ごとの区間の開始時刻。登録済みのアップデート日時 (cuts) でも区切る。"""
    b = ts - (ts + 9 * 3600) % DAY
    if cuts:
        i = bisect.bisect_right(cuts, ts)
        if i and cuts[i - 1] > b:
            b = cuts[i - 1]
    return b


def aggregate_drops(path, routes, cuts=()):
    """戻り値: {month: state}。

    state = {
      month, records, first, last, cuts, final,
      tot:  {map: {node: {"b": bossか, "S": [戦闘数, {艦ID: 数}], "O": [..A/B勝利..]}}},
      dden: {map: {node: {区間の開始時刻: [S戦闘数, A/B戦闘数]}}},
      dsid: {map: {node: {艦ID: {区間の開始時刻: [S, A/B]}}}},     # 全艦
      lastdrop: {map: {node: {艦ID: 最後にドロップした時刻}}}
    }
    区間は日本時間の 1 日ごと。さらにアップデート日時でも区切るので、
    登録済みの日時については秒単位で正確に「それ以降」だけを取り出せる。
    """
    cuts = sorted(set(cuts))
    cnt = defaultdict(int)       # (month, mapId, cell, rank, sid) -> n
    dden = defaultdict(int)      # (month, mapId, cell, bucket, rank) -> n
    dsid = defaultdict(int)      # (month, mapId, cell, bucket, rank, sid) -> n
    boss = set()
    lastdrop = {}
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
        bk = day_bucket(ts, cuts)
        used += 1
        cnt[(mo, map_id, cell, rc, sid)] += 1
        dden[(mo, map_id, cell, bk, rc)] += 1
        if sid:
            dsid[(mo, map_id, cell, bk, rc, sid)] += 1
            k = (mo, map_id, cell, sid)
            if ts > lastdrop.get(k, 0):
                lastdrop[k] = ts
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
                   "cuts": cuts, "final": False, "tot": {}, "dden": {}, "dsid": {}, "lastdrop": {}}
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
    for (mo, map_id, cell, bk, rc), n in dden.items():
        m, node = place(map_id, cell)
        h = out[mo]["dden"].setdefault(m, {}).setdefault(node, {}).setdefault(str(bk), [0, 0])
        h[rc] += n
    for (mo, map_id, cell, bk, rc, sid), n in dsid.items():
        m, node = place(map_id, cell)
        h = (out[mo]["dsid"].setdefault(m, {}).setdefault(node, {})
             .setdefault(str(sid), {}).setdefault(str(bk), [0, 0]))
        h[rc] += n
    for (mo, map_id, cell, sid), ts in lastdrop.items():
        m, node = place(map_id, cell)
        d = out[mo]["lastdrop"].setdefault(m, {}).setdefault(node, {})
        if ts > d.get(str(sid), 0):
            d[str(sid)] = ts
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
    cfg.setdefault("limited_keep", [])     # 1 件報告でも掲載する (管理画面の「復元」)
    cfg.setdefault("auto_resident_exclude", [])   # データで常設と判定されても、常設から外す
    cfg.setdefault("resident_force", [])          # データの判定に関係なく、常設にする
    try:
        cfg["history_months"] = max(3, min(48, int(cfg.get("history_months") or HISTORY_MONTHS)))
    except (TypeError, ValueError):
        cfg["history_months"] = HISTORY_MONTHS
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
def history_start(now, months):
    """判定に使う期間の最初の月 (今月を含めて months か月)。"""
    mo = month_of(now)
    for _ in range(months - 1):
        y, m = map(int, mo.split("-"))
        mo = f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"
    return mo


def load_limited_page():
    """期間限定ドロップのページ (data/limited_page.json) から、限定の艦を取り出す。
    戻り値: (継続中 {(海域, マス, 艦)}, 継続中・海域単位 {(海域, 艦)}, 終了済み {(海域, マス|None, 艦): 終了日})"""
    page = load_json(os.path.join(ROOT, "data", "limited_page.json"), {}) or {}
    events = page.get("events", [])
    on_node, on_map, ended = set(), set(), {}
    for m, names in page.get("ongoing", {}).items():
        for n in names:
            on_map.add((m, norm_name(n)))
    for mp, nd, n, ei, was_ended in page.get("entries", []):
        nn = norm_name(n)
        if not was_ended:
            (on_map.add((mp, nn)) if nd is None else on_node.add((mp, nd, nn)))
        else:
            end = (events[ei].get("end") if ei < len(events) else None) or ""
            k = (mp, nd, nn)
            ended[k] = max(ended.get(k, ""), end)
    return on_node, on_map, ended


JUDGE_LABEL = {
    "continuous": "全期間で出ている",
    "added": "途中から出始め、定着",
    "new": "最近出始めた",
    "stopped": "出なくなった",
    "intermittent": "断続的",
    "misreport": "誤報告の疑い",
    "none": "期間内に出ていない",
}


def judge_pairs(states, months, now):
    """データだけで、各マス・各艦の出方を判定する。

    期間を週ごとに区切り、その艦のふだんの率から「その週に何回出るはずか」を求める。
    出るはずの回数の合計が GAP_EXPECT 回以上の区間で 1 回も出ていなければ、その区間は「出ていない」とみなす。
    これで、出にくい艦がたまたま出なかっただけで外れることはない。

    戻り値: {(海域, マス, 艦ID): {"cls", "C", "N", "first", "last", "weeks"}}
    """
    if not months:
        return {}
    w0 = month_start(months[0]) - 9 * 3600          # 日本時間の月初
    nweeks = (now - w0) // WEEK + 1
    den = defaultdict(lambda: [0] * nweeks)
    for mo in months:
        for m, nodes in states[mo]["dden"].items():
            for node, bks in nodes.items():
                if node.startswith("#"):
                    continue
                arr = den[(m, node)]
                for bk, (a, b) in bks.items():
                    w = (int(bk) - w0) // WEEK
                    if 0 <= w < nweeks:
                        arr[w] += a + b
    out = {}
    for mo in months:
        for m, nodes in states[mo]["dsid"].items():
            for node, sids in nodes.items():
                if node.startswith("#"):
                    continue
                for sid, bks in sids.items():
                    k = (m, node, int(sid))
                    rec = out.setdefault(k, {"c": [0] * nweeks})
                    for bk, (a, b) in bks.items():
                        w = (int(bk) - w0) // WEEK
                        if 0 <= w < nweeks:
                            rec["c"][w] += a + b
    for (m, node, sid), rec in out.items():
        c, n = rec.pop("c"), den[(m, node)]
        C, N = sum(c), sum(n)
        act = [w for w in range(nweeks) if c[w]]
        first, last = act[0], act[-1]
        span_n = sum(n[first:last + 1]) or 1
        r = sum(c[first:last + 1]) / span_n
        lead = r * sum(n[:first])
        trail = r * sum(n[last + 1:])
        gap, run = 0.0, 0.0
        for w in range(first, last + 1):
            if c[w]:
                gap, run = max(gap, run), 0.0
            else:
                run += r * n[w]
        gap = max(gap, run)
        if C <= 2 and N >= SINGLE_MIN_BATTLES:
            cls = "misreport"
        elif trail >= GAP_EXPECT:
            cls = "stopped"
        elif gap >= GAP_EXPECT:
            cls = "intermittent"
        elif lead >= GAP_EXPECT:
            cls = "added" if now - (w0 + first * WEEK) >= NEW_DAYS * DAY else "new"
        else:
            cls = "continuous"
        rec.update(cls=cls, C=C, N=N, first=w0 + first * WEEK, last=w0 + (last + 1) * WEEK - 1, weeks=len(act))
    return out


def build_drop_data(states, cfg, ships, now, wiki=None):
    """公開ページ用の Drop_Data.json と、管理画面用の判定一覧 (data/judge.json) を作る。

    常設 = データで「全期間で出ている」か「途中から出始めて定着した」艦。
           ただし期間限定ドロップのページで継続中になっている艦は、データに関係なく限定。
           ページで終了済みの艦が、その後も出続けている場合は常設 (限定から常設になったもの)。
    限定 = それ以外で、有効なアップデート日時以降に出ている艦。
    """
    name_of = {int(k): v["name"] for k, v in ships.items()}
    ti_of = {int(k): v["ti"] for k, v in ships.items()}
    id_by_norm = {}
    for k, v in sorted(ships.items(), key=lambda kv: int(kv[0])):
        id_by_norm.setdefault(norm_name(v["name"]), int(k))

    cutoff = effective_cutoff(cfg, now)
    start = cutoff if cutoff is not None else 0
    norm3 = lambda e: (e.get("map"), e.get("node"), norm_name(e.get("ship", "")))  # noqa: E731
    exclude = {norm3(e) for e in cfg["limited_exclude"]}
    keep = {norm3(e) for e in cfg["limited_keep"]}
    res_off = {norm3(e) for e in cfg["auto_resident_exclude"]}
    res_force = {norm3(e) for e in cfg["resident_force"]}
    on_node, on_map, ended = load_limited_page()
    wiki_set = {(m, node, norm_name(n)) for m, nodes in (wiki or {}).items()
                for node, cols in nodes.items() for col in cols for n in col}

    months = sorted(states)
    hist = [mo for mo in months if mo >= history_start(now, cfg["history_months"])]
    judged = judge_pairs(states, hist, now)
    mode = cfg.get("rate_mode", "node_s")
    single = []

    def rate(ds, do, ns, no, m=None, mden=None):
        """率 [%], ドロップ数, 分母, 基準。
        node_s: そのマスの S勝利のうち (S で出ていなければ A/B も含める) / node_all: S/A/B 勝利のうち /
        map_all: その海域の全マスの S/A/B 勝利のうち、このマスで出た割合"""
        if mode == "map_all":
            d = ds + do
            return (round(d / mden * 100, 2 if d / mden >= 0.0001 else 3), d, mden, "MAP") if d and mden else (None, 0, mden, "")
        if mode == "node_s" and ds and ns:
            return round(ds / ns * 100, 2), ds, ns, "S"
        if (ds + do) and (ns + no):
            return round((ds + do) / (ns + no) * 100, 2), ds + do, ns + no, "SAB"
        return None, 0, ns if mode == "node_s" else ns + no, ""

    # 常設の率: 判定期間の合計 / 限定の率: アップデート日時以降
    tot = defaultdict(lambda: [0, 0, defaultdict(int), defaultdict(int), 0])   # (m,node) -> [nS, nO, dS, dO, boss]
    for mo in months:
        for m, nodes in states[mo]["tot"].items():
            for node, t in nodes.items():
                x = tot[(m, node)]
                x[4] |= t.get("b", 0)
                if mo not in hist:
                    continue
                x[0] += t["S"][0]
                x[1] += t["O"][0]
                for sid, n in t["S"][1].items():
                    x[2][int(sid)] += n
                for sid, n in t["O"][1].items():
                    x[3][int(sid)] += n
    since_den = defaultdict(lambda: [0, 0])
    since_sid = defaultdict(lambda: [0, 0])
    for mo in months:
        if month_start(next_month(mo)) <= start:
            continue
        for m, nodes in states[mo]["dden"].items():
            for node, bks in nodes.items():
                for bk, (a, b) in bks.items():
                    if int(bk) >= start:
                        x = since_den[(m, node)]
                        x[0] += a
                        x[1] += b
        for m, nodes in states[mo]["dsid"].items():
            for node, sids in nodes.items():
                for sid, bks in sids.items():
                    for bk, (a, b) in bks.items():
                        if int(bk) >= start:
                            x = since_sid[(m, node, int(sid))]
                            x[0] += a
                            x[1] += b
    map_den_all = defaultdict(int)
    map_den_since = defaultdict(int)
    for (m, node), x in tot.items():
        map_den_all[m] += x[0] + x[1]
    for (m, node), x in since_den.items():
        map_den_since[m] += x[0] + x[1]

    maps, judge_rows = {}, []
    places = set(tot) | {(e["map"], e["node"]) for e in cfg["limited_manual"] if e.get("map") and e.get("node")}
    for m, node in sorted(places, key=lambda p: (map_key(p[0]), p[1])):
        if node.startswith("#"):
            continue
        nS, nO, dS, dO, boss = tot.get((m, node), [0, 0, {}, {}, 0])
        rows, seen = [], set()
        sids = {sid for (mm, nd, sid) in judged if mm == m and nd == node}
        sids |= {sid for (mm, nd, sid) in since_sid if mm == m and nd == node}
        for e in cfg["resident_force"]:
            if e.get("map") == m and e.get("node") == node and id_by_norm.get(norm_name(e.get("ship", ""))):
                sids.add(id_by_norm[norm_name(e["ship"])])
        for sid in sorted(sids):
            nm = name_of.get(sid, f"ID:{sid}")
            nn = norm_name(nm)
            k3 = (m, node, nn)
            j = judged.get((m, node, sid), {})
            cls = j.get("cls", "")
            page = ("ongoing" if (k3 in on_node or (m, nn) in on_map)
                    else "ended" if (k3 in ended or (m, None, nn) in ended) else "")
            # 公開ページでの扱い
            if k3 in res_force:
                pub, why = "常設", "manual"
            elif page == "ongoing":
                pub, why = "限定", "page"
            elif cls in ("continuous", "added") and k3 not in res_off:
                pub, why = "常設", ("was_limited" if page == "ended" else cls)
            else:
                pub, why = "限定", "auto"
            ds, do = since_sid.get((m, node, sid), (0, 0))
            if pub == "常設":
                r, d, n, basis = rate(dS.get(sid, 0), dO.get(sid, 0), nS, nO, m, map_den_all[m])
                rows.append([nm, ti_of.get(sid, 7), 0, r, d, n, basis, why])
                seen.add(nn)
            elif ds + do and k3 not in exclude:
                ns, no = since_den.get((m, node), (0, 0))
                if ds + do == 1 and ns + no >= SINGLE_MIN_BATTLES and k3 not in keep:
                    single.append([m, node, nm, ns + no])
                    pub = "1隻報告"
                else:
                    r, d, n, basis = rate(ds, do, ns, no, m, map_den_since[m])
                    rows.append([nm, ti_of.get(sid, 7), 1, r, d, n, basis, why])
                    seen.add(nn)
            else:
                pub = "除外" if k3 in exclude else ""
            if j or pub:
                judge_rows.append([m, node, nm, cls, j.get("C", 0), j.get("N", 0),
                                   fmt_dt(j["first"])[:10] if j else "", fmt_dt(min(j["last"], now))[:10] if j else "",
                                   int(k3 in wiki_set), page, pub, why])
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
            if added < start:
                continue
            sid = id_by_norm.get(nn)
            rows.append([name_of.get(sid, e["ship"]), ti_of.get(sid, 7), 1, None, 0, 0, "", "manual"])
            seen.add(nn)
        if rows:
            rows.sort(key=lambda r: (r[1], r[2], r[0]))
            maps.setdefault(m, {})[node] = {"boss": boss, "n": nS, "ships": rows}
    # Wiki にあるのに、判定期間に 1 回も出ていないもの (管理画面の照合用)
    seen_pairs = {(r[0], r[1], norm_name(r[2])) for r in judge_rows}
    for (m, node, nn) in sorted(wiki_set - seen_pairs):
        if (m, node) in tot:
            nm = next((n for col in wiki[m][node] for n in col if norm_name(n) == nn), nn)
            x = tot[(m, node)]
            judge_rows.append([m, node, nm, "none", 0, x[0] + x[1], "", "", 1, "", "", ""])

    first = min((states[mo]["first"] for mo in hist), default=None)
    last = max((states[mo]["last"] for mo in months), default=None)
    data = {
        "generated": fmt_dt(now),
        "data_from": fmt_dt(first) if first else None,
        "data_to": fmt_dt(last) if last else None,
        "cutoff": fmt_dt(cutoff) if cutoff is not None else None,
        "records": sum(states[mo]["records"] for mo in hist),
        "months": hist,
        "limited_months": [mo for mo in months if cutoff is not None and mo >= month_of(cutoff)] or hist,
        "rate_mode": mode,
        "sources": SOURCES,
        "single_hidden": single,
        "types": TYPE_LABELS,
        "columns": ["name", "type", "limited", "rate", "drops", "battles", "basis", "source"],
        "maps": maps,
    }
    judge = {
        "generated": fmt_dt(now),
        "months": hist,
        "labels": JUDGE_LABEL,
        "columns": ["map", "node", "ship", "class", "drops", "battles", "first", "last", "wiki", "page", "published", "why"],
        "rows": judge_rows,
    }
    return data, judge


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


def build_last_drop(states, ships, now):
    """各海域・各マスで、各艦が最後にドロップした日時と、保存している全期間のドロップ数 (管理画面用)。
    maps[海域][マス][艦名] = [最終ドロップ日時, ドロップ数]"""
    name_of = {int(k): v["name"] for k, v in ships.items()}
    best, count = {}, defaultdict(int)
    for mo in states:
        for m, nodes in states[mo].get("lastdrop", {}).items():
            for node, d in nodes.items():
                if node.startswith("#"):
                    continue
                for sid, ts in d.items():
                    k = (m, node, name_of.get(int(sid), f"ID:{sid}"))
                    if ts > best.get(k, 0):
                        best[k] = ts
        for m, nodes in states[mo]["tot"].items():
            for node, t in nodes.items():
                for rc in ("S", "O"):
                    for sid, n in t[rc][1].items():
                        count[(m, node, name_of.get(int(sid), f"ID:{sid}"))] += n
    maps = {}
    for (m, node, name), ts in sorted(best.items(), key=lambda kv: (map_key(kv[0][0]), kv[0][1], kv[0][2])):
        maps.setdefault(m, {}).setdefault(node, {})[name] = [fmt_dt(ts)[:16], count.get((m, node, name), 0)]
    return {"months": sorted(states), "maps": maps}


def write_update_log(old, new, build, old_build, now, notes):
    """1 回の集計で何が変わったかを data/update_log.json に残す (管理画面の「データ更新の記録」)。"""
    on, ol = drop_sets(old)
    nn, nl = drop_sets(new)
    label = lambda s: [f"{m}-{node} {name}" for m, node, name in  # noqa: E731
                       sorted(s, key=lambda x: (map_key(x[0]), x[1], x[2]))][:LOG_DETAIL_MAX]
    has_old = bool(old and old.get("maps"))
    entry = {
        "at": fmt_dt(now),
        "trigger": os.environ.get("GITHUB_EVENT_NAME", "manual"),
        "drop_records": new.get("records", 0),
        "drop_added": new.get("records", 0) - (old or {}).get("records", 0) if has_old else None,
        "build_records": build.get("records", 0),
        "build_added": build.get("records", 0) - (old_build or {}).get("records", 0) if old_build else None,
        "data_to": new.get("data_to"),
        "months": new.get("months"),
        "cutoff": new.get("cutoff"),
        "limited": len(nl),
        "resident": len(nn),
        "limited_added_n": len(nl - ol) if has_old else None,
        "limited_removed_n": len(ol - nl) if has_old else None,
        "resident_added_n": len(nn - on) if has_old else None,
        "resident_removed_n": len(on - nn) if has_old else None,
        "limited_added": label(nl - ol) if has_old else [],
        "limited_removed": label(ol - nl) if has_old else [],
        "resident_added": label(nn - on) if has_old else [],
        "resident_removed": label(on - nn) if has_old else [],
        "notes": notes,
    }
    path = os.path.join(ROOT, "data", "update_log.json")
    log_list = load_json(path, [])
    if not isinstance(log_list, list):
        log_list = []
    log_list.append(entry)
    save_json(path, log_list[-LOG_KEEP:])


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
    wiki = load_json(os.path.join(ROOT, "Normal_Drop_Ship.json"), {}) or {}   # 管理画面の照合にだけ使う
    cfg = load_config()

    drop_states = load_state(args.state_dir, "drop")
    build_states = load_state(args.state_dir, "build")
    failed = []
    notes = []
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
    sig = hashlib.sha1(json.dumps([sorted(cuts), STATE_VERSION], sort_keys=True).encode()).hexdigest()
    skip_dump = (args.smart and meta.get("sig") == sig and now - meta.get("fetched_at", 0) < 20 * 3600
                 and month_of(now) in drop_states)
    # 毎朝 5〜9 時に何度か起動するので、ダンプが前回から更新されていなければ取り直さない
    dump_ver = {}
    if not skip_dump and not args.no_download and not args.drop_file and month_of(now) in drop_states:
        for fname in ("dropshiprecords.gz", "createshiprecords.gz"):
            dump_ver[fname] = http_head(DUMP_BASE + fname)
        same_dump = all(v and v == (meta.get("dump_ver") or {}).get(k) for k, v in dump_ver.items())
        if same_dump and meta.get("sig") == sig:
            skip_dump = True
            log("ダンプは前回の取得から更新されていないため、取り直しません")
    if skip_dump:
        log(f"当月ダンプは {fmt_dt(meta['fetched_at'])} に取得済みのため再取得しません")
        notes.append(f"当月ダンプは取得済みのものを使用 ({fmt_dt(meta['fetched_at'])} 取得)")
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
                    notes.append(f"{fname} を取得できず、保存済みデータで集計")
                    failed.append(fname)
                    path = None
            if not path:
                continue
            try:
                if kind == "drop":
                    new = aggregate_drops(path, routes, cuts)
                    drop_states = merge_state(args.state_dir, "drop", drop_states, new)
                    if not given:
                        os.makedirs(args.state_dir, exist_ok=True)
                        if not dump_ver:
                            dump_ver = {f: http_head(DUMP_BASE + f) for f in ("dropshiprecords.gz", "createshiprecords.gz")}
                        save_json(meta_path, {"sig": sig, "fetched_at": now, "dump_ver": dump_ver})
                else:
                    new = aggregate_builds(path)
                    build_states = merge_state(args.state_dir, "build", build_states, new)
            except Exception as e:  # noqa: BLE001
                warn(f"{fname} の解析に失敗しました。保存済みデータで続行します: {e}")
                notes.append(f"{fname} の解析に失敗")
                failed.append(fname)
            if not given:
                try:
                    os.remove(path)   # 解析が済んだダンプは残さない
                except OSError:
                    pass

        # 過去の月はアーカイブから取り込む (一度取り込んだ月は再取得しない)。
        #  - 常設の判定用: history_months か月前から前月まで
        #  - 限定ドロップ用: 有効なアップデート日時の月から前月まで (例: 10/22 なら 10 月分)
        # 1 回の実行では新しい月から ARCHIVE_PER_RUN か月ずつ。残りは次の実行で取り込む
        if not args.no_download:
            cur = month_of(now)
            months = []
            mo = history_start(now, cfg["history_months"])
            if cutoff is not None:
                mo = min(mo, month_of(cutoff))
            while mo < cur:
                months.append(mo)
                mo = next_month(mo)
            todo = []
            for mo in reversed(months):
                st = drop_states.get(mo)
                in_month = cutoff is not None and month_start(mo) <= cutoff < month_start(next_month(mo))
                if st and st.get("final") and (not in_month or cutoff in st.get("cuts", [])):
                    continue   # 取り込み済み
                todo.append(mo)
            if len(todo) > ARCHIVE_PER_RUN:
                notes.append(f"過去月は残り {len(todo) - ARCHIVE_PER_RUN} か月を次回以降に取り込みます")
                log(f"過去月: 今回 {ARCHIVE_PER_RUN} か月、残り {len(todo) - ARCHIVE_PER_RUN} か月は次回以降")
                todo = todo[:ARCHIVE_PER_RUN]
            for mo in todo:
                url = archive_url("dropshiprecords", mo)
                path = os.path.join(tmp, f"archive_{mo}.gz")
                try:
                    log(f"過去月アーカイブ {mo}: {url}")
                    size = http_get(url, dest=path, timeout=600)
                    log(f"  {size / 1e6:.1f} MB")
                    new = aggregate_drops(path, routes, cuts)
                    if mo not in new:
                        raise RuntimeError(f"{mo} の記録が含まれていません")
                    new[mo]["final"] = True
                    drop_states = merge_state(args.state_dir, "drop", drop_states, {mo: new[mo]}, force=True)
                    notes.append(f"{mo} の過去月アーカイブを取り込み ({new[mo]['records']:,} 件)")
                except Exception as e:  # noqa: BLE001
                    warn(f"{mo} の過去月アーカイブを取り込めませんでした (月初 1〜2 日は未公開のことがあります): {e}")
                    notes.append(f"{mo} の過去月アーカイブを取り込めず")
                finally:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    old_drop = load_json(os.path.join(ROOT, "Drop_Data.json"))
    # 判定期間より古い月は使わないので消す (容量を抑えるため)
    keep_from = history_start(now, cfg["history_months"])
    if cutoff is not None:
        keep_from = min(keep_from, month_of(cutoff))
    for mo in [mo for mo in drop_states if mo < keep_from]:
        drop_states.pop(mo)
        try:
            os.remove(state_path(args.state_dir, "drop", mo))
        except OSError:
            pass
    drop, judge = build_drop_data(drop_states, cfg, ships, now, wiki)
    save_json(os.path.join(ROOT, "data", "judge.json"), judge, compact=True)
    build = build_build_data(build_states, ships, now)
    append_history(old_drop, drop, now)
    old_build = load_json(os.path.join(ROOT, "Build_Data.json"))
    write_update_log(old_drop, drop, build, old_build, now, notes)
    last = build_last_drop(drop_states, ships, now)
    if load_json(os.path.join(ROOT, "data", "last_drop.json")) != last:
        save_json(os.path.join(ROOT, "data", "last_drop.json"), last, compact=True)

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
