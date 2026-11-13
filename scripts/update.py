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
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JST = timezone(timedelta(hours=9))
DT_FMT = "%Y/%m/%d %H:%M:%S"

DUMP_BASE = os.environ.get("POI_DUMP_BASE", "https://api.poi.moe/dump/")
# 過去月のアーカイブ。ファイル名は <種別>-<翌月1日0時(UTC)のUNIX時刻を16進>.gz
ARCHIVE_BASE = os.environ.get("POI_ARCHIVE_BASE", "https://db.poi.moe/")
STATE_VERSION = 6
HISTORY_MONTHS = 12       # 常設の判定に使う期間 (か月)。admin_config.json の history_months で変更できる
ARCHIVE_PER_RUN = 6       # 1 回の実行で取り込む過去月の数。残りがあれば GitHub Actions が続けて自分を起動する
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
    {"name": "api_start2 (Tibowl)", "url": "https://github.com/Tibowl/api_start2", "use": "艦ID→艦名・艦種・レア度 (api_backs)"},
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
LOG_DETAIL_MAX = 15       # 更新履歴に個別に書く件数の上限 (超えたら件数だけ)
FLEET_HOLD = 50           # 敵編成の確認: この回数以下しか出ていない編成は、記録を後で取り消せるよう控えておく
FLEET_MIN_SHARE = 0.001  # その月のマスの戦闘の 0.1% 未満しか出ない敵編成 (かつ FLEET_HOLD 回以下) の記録は誤記録として捨てる
REP_KEEP = 30             # 艦ごとに控える報告者の数の上限
MIN_NODE_BATTLES = 100   # 判定期間の戦闘がこれ未満のマスは載せない (戦闘の無いマスの誤った記録など)
SINGLE_MIN_BATTLES = 2000    # 報告 1 件だけで、そのマスの戦闘数がこれ以上なら誤報告とみなして掲載しない


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


def http_get(url, dest=None, timeout=120, tries=8):
    """URL を取得する。dest を渡すとファイルに保存し、途中で切れたら続きから取り直す (Range)。
    Content-Length と受け取ったサイズが合うまで完了とみなさない。"""
    ua = {"User-Agent": "Kancolle_finder-updater (+https://github.com/yss19411208/Kancolle_finder)"}
    last, size, total = None, 0, None
    if dest is not None:
        open(dest, "wb").close()
    for i in range(tries):
        try:
            headers = dict(ua)
            if dest is not None and size:
                headers["Range"] = f"bytes={size}-"
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                if dest is None:
                    return r.read()
                if size and r.status != 206:   # 続きから取れないサーバーなら最初から
                    log("  続きからの取得に対応していないため、最初から取り直します")
                    size = 0
                    open(dest, "wb").close()
                cl = r.headers.get("Content-Length")
                if r.status == 206:
                    cr = r.headers.get("Content-Range", "")
                    if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                        total = int(cr.rsplit("/", 1)[1])
                elif cl and cl.isdigit():
                    total = int(cl)
                with open(dest, "ab") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        size += len(chunk)
            if total is None or size >= total:
                return size
            raise IOError(f"途中で切れました ({size / 1e6:.1f} / {total / 1e6:.1f} MB)")
        except Exception as e:  # noqa: BLE001
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                raise RuntimeError(f"ファイルがありません (404): {url}") from e
            last = e
            more = f"、{size / 1e6:.1f} MB まで受信済み・続きから再開" if dest is not None and size else ""
            log(f"  取得失敗 ({i + 1}/{tries}) {url}: {e}{more}")
            time.sleep(min(60, 5 * (i + 1)))
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
                        "backs": s.get("api_backs") or 0,     # 背景の色 = レア度 (1〜8)
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
      lastdrop: {map: {node: {艦ID: 最後にドロップした時刻}}},
      rep:  {map: {node: {艦ID: [報告者(最大30), {ツール: 数}, 1時間の最大件数]}}},
      orig: {map: {node: {ツール: 数}}},     # マス全体のツールの内訳
      rej:  {map: {node: {理由: 数}}}        # 誤記録として捨てた件数
    }
    誤記録として捨てるもの: 海域名 (quest) が無い / 敵編成が空 / そのマスでほとんど出ない敵編成
    (別のマスの記録が紛れ込んだもの)。
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
    fcnt = defaultdict(int)      # (month, mapId, cell, 敵編成) -> n
    hold = {}                    # 同上 -> [(bk, rc, sid)] (FLEET_HOLD 回まで)
    rej = defaultdict(int)       # (month, mapId, cell, 理由) -> n
    repo = {}                    # (month, mapId, cell, sid) -> [set(報告者), Counter(ツール), Counter(時)]
    orig = defaultdict(int)      # (month, mapId, cell, ツール) -> n
    quest_of = {}                # mapId -> 最初に見た海域名 (後で多数決)
    quests = defaultdict(lambda: defaultdict(int))
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
        q = d.get("quest")
        fleet = tuple(d.get("enemyShips1") or ())
        if not q:
            rej[(mo, map_id, cell, "海域名なし")] += 1
            continue
        if not fleet:
            rej[(mo, map_id, cell, "敵編成なし")] += 1
            continue
        quests[map_id][q] += 1
        bk = day_bucket(ts, cuts)
        used += 1
        fk = (mo, map_id, cell, fleet)
        fcnt[fk] += 1
        if fcnt[fk] <= FLEET_HOLD:
            hold.setdefault(fk, []).append((bk, rc, sid, q))
        elif fk in hold:
            del hold[fk]
        ok = str(d.get("origin") or "?").split("/")[0]
        orig[(mo, map_id, cell, ok)] += 1
        if sid:
            fp = d.get("teitokuId") or f"{d.get('origin')}|{d.get('teitokuLv')}"
            rp = repo.get((mo, map_id, cell, sid))
            if rp is None:
                rp = repo[(mo, map_id, cell, sid)] = [set(), defaultdict(int), defaultdict(int), 0]
            rp[3] += 1
            if len(rp[0]) < REP_KEEP:
                rp[0].add(hashlib.sha1(str(fp).encode()).hexdigest()[:10])
            rp[1][ok] += 1
            if rp[3] <= 500:
                rp[2][ts // 3600] += 1
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
    # 誤記録の取り消し: そのマスでほとんど出ない敵編成 / 海域名が他と違う
    node_n = defaultdict(int)
    for (mo, map_id, cell, rc, sid), n in cnt.items():
        node_n[(mo, map_id, cell)] += n
    main_q = {m: max(qs.items(), key=lambda kv: kv[1])[0] for m, qs in quests.items()}
    dropped = 0
    for (mo, map_id, cell, fleet), lst in hold.items():
        bad_fleet = fcnt[(mo, map_id, cell, fleet)] / max(1, node_n[(mo, map_id, cell)]) < FLEET_MIN_SHARE
        for bk, rc, sid, q in lst:
            reason = ("ほかのマスの敵編成" if bad_fleet else
                      "海域名が違う" if q != main_q.get(map_id) else None)
            if not reason:
                continue
            dropped += 1
            rej[(mo, map_id, cell, reason)] += 1
            cnt[(mo, map_id, cell, rc, sid)] -= 1
            dden[(mo, map_id, cell, bk, rc)] -= 1
            if sid:
                dsid[(mo, map_id, cell, bk, rc, sid)] -= 1
                rp = repo.get((mo, map_id, cell, sid))
                if rp:
                    rp[3] -= 1
    for dct in (cnt, dden, dsid):
        for k in [k for k, v in dct.items() if v <= 0]:
            del dct[k]
    alive = {(k[0], k[1], k[2], k[5]) for k in dsid}
    for k in [k for k in lastdrop if k not in alive]:
        del lastdrop[k]
    log(f"  ドロップ: {total:,} 件中 {used - dropped:,} 件を集計 (通常海域・S/A/B勝利)。"
        f"誤記録として除外 {sum(rej.values()):,} 件")

    def place(map_id, cell):
        m = f"{map_id // 10}-{map_id % 10}"
        return m, routes.get(m, {}).get(str(cell), f"#{cell}")

    out = {}
    for mo, (n, first, last) in meta.items():
        out[mo] = {"v": STATE_VERSION, "month": mo, "records": n, "first": first, "last": last,
                   "cuts": cuts, "final": False, "tot": {}, "dden": {}, "dsid": {}, "lastdrop": {},
                   "rep": {}, "orig": {}, "rej": {}}
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
    for (mo, map_id, cell, sid), (fps, oks, hrs, n) in repo.items():
        if n <= 0 or (mo, map_id, cell, sid) not in alive:
            continue
        m, node = place(map_id, cell)
        # 同じマス名に複数のセル番号がある海域 (ルート違い) は足し合わせる
        cur = out[mo]["rep"].setdefault(m, {}).setdefault(node, {}).get(str(sid))
        if cur is None:
            cur = out[mo]["rep"][m][node][str(sid)] = [[], {}, 0]
        cur[0] = sorted(set(cur[0]) | fps)[:REP_KEEP]
        for ok, v in oks.items():
            cur[1][ok] = cur[1].get(ok, 0) + v
        cur[2] = max(cur[2], max(hrs.values()) if hrs else 0)
    for (mo, map_id, cell, ok), n in orig.items():
        m, node = place(map_id, cell)
        o = out[mo]["orig"].setdefault(m, {}).setdefault(node, {})
        o[ok] = o.get(ok, 0) + n
    for (mo, map_id, cell, reason), n in rej.items():
        if mo not in out:
            continue
        m, node = place(map_id, cell)
        o = out[mo]["rej"].setdefault(m, {}).setdefault(node, {})
        o[reason] = o.get(reason, 0) + n
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
    "intermittent": "途切れた期間あり・今は出ている",
    "misreport": "誤報告の疑い",
    "none": "期間内に出ていない",
    "fewnode": "記録が少ないマス",
}


def classify_series(c, n, now, w0):
    """週ごとのドロップ数 c と戦闘数 n から出方を判定する (1 つの勝利ランク分)。"""
    nweeks = len(c)
    act = [w for w in range(nweeks) if c[w]]
    if not act:
        return None
    first, last = act[0], act[-1]
    C = sum(c)
    span_n = sum(n[first:last + 1]) or 1
    r = sum(c[first:last + 1]) / span_n
    lead = r * sum(n[:first])
    trail = r * sum(n[last + 1:])
    # seg: 今続いている出方が始まった週 (途切れた期間の後、または出始め)。0 なら期間のはじめから
    seg = first if lead >= GAP_EXPECT else 0
    gap, run = 0.0, 0.0
    segs, s0, prev = [], first, first     # 出ていた期間 [(始めの週, 終わりの週)]
    for w in range(first, last + 1):
        if c[w]:
            if run >= GAP_EXPECT:
                seg = w
                segs.append((s0, prev))
                s0 = w
            gap, run = max(gap, run), 0.0
            prev = w
        else:
            run += r * n[w]
    segs.append((s0, last))
    gap = max(gap, run)
    if C <= 2 and sum(n[first:]) >= SINGLE_MIN_BATTLES:
        cls = "misreport"      # 出始めてから今までに戦闘が十分あるのに 2 件以下
    elif trail >= GAP_EXPECT:
        cls = "stopped"
    elif gap >= GAP_EXPECT:
        cls = "intermittent"
    elif lead >= GAP_EXPECT:
        cls = "added" if now - (w0 + first * WEEK) >= NEW_DAYS * DAY else "new"
    else:
        cls = "continuous"
    return {"cls": cls, "C": C, "seg": (w0 + seg * WEEK) if seg else None,
            "segs": [(b - a + 1) for a, b in segs]}


# 勝利ランクごとの判定をまとめるときの優先順 (どれかのランクで今も出ていれば出ている)
CLS_ORDER = ["continuous", "added", "intermittent", "new", "stopped", "misreport"]


def judge_pairs(states, months, now):
    """データだけで、各マス・各艦の出方を判定する。

    期間を週ごとに区切り、その艦のふだんの率から「その週に何回出るはずか」を求める。
    出るはずの回数の合計が GAP_EXPECT 回以上の区間で 1 回も出ていなければ、その区間は「出ていない」とみなす。
    S 勝利と A/B 勝利ではドロップ表が違う (ボスの A/B 勝利でしか出ない艦がある) ので、別々に判定してまとめる。

    戻り値: {(海域, マス, 艦ID): {"cls", "rank", "C", "N", "first", "last", "seg", "rep"}}
    """
    if not months:
        return {}
    w0 = month_start(months[0]) - 9 * 3600          # 日本時間の月初
    nweeks = (now - w0) // WEEK + 1
    den = defaultdict(lambda: ([0] * nweeks, [0] * nweeks))
    for mo in months:
        for m, nodes in states[mo]["dden"].items():
            for node, bks in nodes.items():
                if node.startswith("#"):
                    continue
                arr = den[(m, node)]
                for bk, ab in bks.items():
                    w = (int(bk) - w0) // WEEK
                    if 0 <= w < nweeks:
                        arr[0][w] += ab[0]
                        arr[1][w] += ab[1]
    raw = {}
    for mo in months:
        for m, nodes in states[mo]["dsid"].items():
            for node, sids in nodes.items():
                if node.startswith("#"):
                    continue
                for sid, bks in sids.items():
                    k = (m, node, int(sid))
                    rec = raw.get(k)
                    if rec is None:
                        rec = raw[k] = {"c": ([0] * nweeks, [0] * nweeks), "b0": None, "b1": None,
                                        "fp": set(), "ok": defaultdict(int), "hr": 0}
                    for bk, ab in bks.items():
                        w = (int(bk) - w0) // WEEK
                        if 0 <= w < nweeks and ab[0] + ab[1]:
                            rec["c"][0][w] += ab[0]
                            rec["c"][1][w] += ab[1]
                            bk = int(bk)
                            rec["b0"] = bk if rec["b0"] is None else min(rec["b0"], bk)
                            rec["b1"] = bk if rec["b1"] is None else max(rec["b1"], bk)
        for m, nodes in states[mo].get("rep", {}).items():
            for node, sids in nodes.items():
                for sid, (fps, oks, hr) in sids.items():
                    rec = raw.get((m, node, int(sid)))
                    if rec is None:
                        continue
                    if len(rec["fp"]) < REP_KEEP:
                        rec["fp"].update(fps[:REP_KEEP - len(rec["fp"])])
                    for ok, v in oks.items():
                        rec["ok"][ok] += v
                    rec["hr"] = max(rec["hr"], hr)
    out = {}
    for (m, node, sid), rec in raw.items():
        if rec["b0"] is None:
            continue
        nS, nO = den[(m, node)]
        res = {}
        for ri, (c, n) in enumerate(zip(rec["c"], (nS, nO))):
            x = classify_series(c, n, now, w0)
            if x:
                res["S" if ri == 0 else "AB"] = x
        best = min(res.items(), key=lambda kv: CLS_ORDER.index(kv[1]["cls"]))
        C = sum(x["C"] for x in res.values())
        out[(m, node, sid)] = {
            "cls": best[1]["cls"], "rank": best[0], "C": C, "N": sum(nS) + sum(nO),
            "first": rec["b0"], "last": rec["b1"], "seg": best[1]["seg"], "segs": best[1]["segs"],
            "rep": {"fp": len(rec["fp"]), "ok": dict(rec["ok"]), "hr": rec["hr"]},
        }
    return out


def suspicious(j, node_orig):
    """少ない報告が 1 人・1 時間・1 種類のツールに偏っていないか。偏っていれば理由を返す。"""
    rp = j.get("rep") or {}
    C = j.get("C", 0)
    if C < 3 or not rp:
        return ""
    fp = rp.get("fp", 0)
    if not fp:
        return ""          # 報告者の情報が無い (古い形式のデータ)
    if (C >= 3 and fp <= 1) or (C >= 20 and fp <= 3):
        return f"報告者が {fp} 人だけ ({C} 件)"
    # 1 時間への集中: 報告者も少ないときだけ (新しい海域の公開直後などは、みんなが同じ時間に出撃するため)
    if C >= 5 and rp.get("hr", 0) >= C * 0.5 and fp <= max(2, C // 3):
        return f"{rp['hr']} 件が同じ 1 時間に集中 (報告者 {fp} 人)"
    # ツールの偏り: そのマスでのツールの割合から見て、偶然では 0.1% 未満でしか起きない偏りのときだけ
    ok = rp.get("ok") or {}
    tot = sum(node_orig.values()) or 1
    # (A/B 勝利のドロップは報告するツールが偏る (ほぼ poi だけ) ので、S 勝利のときだけ調べる)
    if len(ok) == 1 and j.get("rank") == "S":
        tool = next(iter(ok))
        share = node_orig.get(tool, 0) / tot
        if share ** C < 0.001:
            return f"{C} 件すべて {tool} からの報告 (ふだんの割合 {share * 100:.0f}%)"
    return ""


def rarity_level(backs):
    """api_backs をレア度の 3 段階にする: 1〜3 → 1 (コモン)、4〜5 → 2 (レア)、6〜8 → 3 (スーパーレア)。"""
    b = int(backs or 0)
    return 1 if 1 <= b <= 3 else 2 if b in (4, 5) else 3 if b >= 6 else 0


def rarity_of_names(ships, maps):
    """公開ページに出る艦名 -> レア度。同じ名前の艦が複数あるときは、改造前 (ID が小さい方) を使う。"""
    by_name = {}
    for k in sorted(ships, key=int):
        v = ships[k]
        by_name.setdefault(v["name"], rarity_level(v.get("backs")))
    names = {r[0] for nodes in maps.values() for v in nodes.values() for r in v["ships"]}
    return {n: by_name[n] for n in sorted(names) if by_name.get(n)}


def reason_text(pub, why, cls, j, nt, sus, since_drops, since_battles, cutoff, node_battles):
    """公開ページでの扱いの理由を、管理画面に出す文章にする (公開していないものは必ず理由を書く)。"""
    C, N = j.get("C", 0), j.get("N", 0)
    cut = fmt_dt(cutoff)[:16] if cutoff is not None else "なし"
    change = nt.get("change", "")
    last = fmt_dt(j["last"])[:10] if j else ""
    if pub == "常設":
        return {
            "manual": "手動で「常設にする」にしている",
            "continuous": "判定期間のはじめから今まで出続けている",
            "added": f"途中から出始め、60 日以上出続けている（{change}）" if change else "途中から出始め、60 日以上出続けている",
            "new": f"出始めて 60 日未満だが、最新のアップデート（{cut}）後にも出ている（{change}）",
            "intermittent": f"途中で出ない期間があったが、今は出ている（{change}）",
            "was_limited": "限定ページでは終了済みだが、その後も出続けている",
        }.get(why, "今も出ている")
    if pub == "限定":
        if why == "manual_lim":
            return f"手動で「限定にする」にしている。最新のアップデート（{cut}）以降に {since_drops} 件"
        return f"限定ページで継続中。最新のアップデート（{cut}）以降に {since_drops} 件出ている"
    if pub == "1隻報告":
        return (f"最新のアップデート（{cut}）以降の報告が 1 件だけで、その間にこのマスの戦闘が "
                f"{since_battles:,} 回ある（{SINGLE_MIN_BATTLES:,} 回以上で 1 件だけのものは載せない）")
    if pub == "除外":
        return "除外リストに入れている"
    # ---- 公開していないもの
    if why == "page":
        return f"限定ページで継続中だが、最新のアップデート（{cut}）以降は 1 回も出ていない"
    if why == "manual_off":
        return "手動で「常設から外す」にしている"
    few = (f"このマスの戦闘が判定期間に {node_battles:,} 回しかない（{MIN_NODE_BATTLES} 回未満）。"
           "戦闘の無いマスに紛れ込んだ記録の可能性が高い")
    if why == "suspect":
        return f"報告が偏っている: {sus}（誤報告・いたずらの疑い）" + (f"。また、{few}" if cls == "fewnode" else "")
    if why == "fewnode" or cls == "fewnode":
        return few
    if why == "hold":
        return "この海域で今まで一度も出たことがない艦が出始めた。「常設にする」か「限定にする」を押すまで載せない"
    if why == "unconfirmed":
        if C <= 2 and cls == "new":
            return f"出始めたばかりで {C} 件しかなく、誤報告と見分けられない（3 件以上になったら判定する）"
        return (f"出始めて（または再開して）60 日未満で、最新のアップデート（{cut}）以降に出ていない。"
                "終わったばかりの限定の可能性があるため、アップデート後に出るのを確かめるまで載せない")
    if cls == "stopped" or why == "stopped":
        return f"最近は出ていない（{change or f'最終ドロップ {last}'}）。常設から外れたか、限定が終わったとみなした"
    if cls == "misreport" or why == "misreport":
        return (f"判定期間にこのマスで戦闘 {N:,} 回あるのに {C} 件しか出ていない"
                f"（出始めてから {SINGLE_MIN_BATTLES:,} 回以上戦闘があって 2 件以下は誤報告の疑い）")
    if cls == "none" or why == "none":
        return "Wiki の出撃ドロップには載っているが、判定期間に 1 回も出ていない"
    return "理由不明（管理者に知らせてください）"


def change_notes(judged, cfg, now, on_node, on_map):
    """出始め・出なくなった日を、メンテナンスの日に合わせて文章にする。限定の可能性も付ける。

    メンテナンスの日 = 管理画面に登録したアップデート日時の日 + 同じ日に 2 海域以上・3 組以上が
    いっせいに出始めた (または出なくなった) 日。率の低い艦はアプデから数日後に初めて出ることがあるので、
    出始めはその日以前で一番近いメンテ日 (14 日以内) に、出なくなったのは最終ドロップ以後で一番近いメンテ日にあてる。
    """
    day = lambda ts: (ts + 9 * 3600) // DAY  # noqa: E731  日本時間の日番号
    ev = defaultdict(set)    # 日 -> 海域
    evn = defaultdict(int)
    for (m, node, sid), j in judged.items():
        if j["cls"] in ("new", "added"):
            ev[day(j["first"])].add(m)
            evn[day(j["first"])] += 1
        elif j["cls"] == "stopped":
            ev[day(j["last"]) + 1].add(m)
            evn[day(j["last"]) + 1] += 1
    maint = {d for d in ev if len(ev[d]) >= 2 and evn[d] >= 3}
    for u in cfg.get("updates", []):
        try:
            maint.add(day(parse_dt(u.get("at") if isinstance(u, dict) else u)))
        except (ValueError, TypeError, AttributeError):
            pass
    ms = sorted(maint)
    fmt = lambda d: fmt_dt(d * DAY - 9 * 3600)[:10]  # noqa: E731

    def before(d):
        c = [x for x in ms if d - 14 <= x <= d]
        return c[-1] if c else None

    def after(d):
        c = [x for x in ms if d < x <= d + 14]
        return c[0] if c else None

    starts = defaultdict(int)   # (海域, メンテ日) -> 出始めた組の数
    res = {}
    for k, j in judged.items():
        m = k[0]
        if j["cls"] in ("new", "added"):
            d = before(day(j["first"]))
            res[k] = {"change": f"{fmt(d)} のメンテから出始め" if d else f"{fmt(day(j['first']))} ごろから出始め",
                      "_d": d if j["cls"] == "new" else None}
            if d:
                starts[(m, d)] += 1
        elif j["cls"] == "stopped":
            d = after(day(j["last"]))
            res[k] = {"change": f"{fmt(d)} のメンテから出ていない" if d else f"{fmt(day(j['last']))} 以降出ていない"}
        elif j["cls"] == "intermittent":
            res[k] = {"change": f"{fmt_dt(j['seg'])[:10]} ごろ再開（途切れた期間あり）" if j.get("seg") else "途切れた期間あり"}
            # 以前の出ていた期間が短く (20 週以内)、今の期間も始まって 120 日以内なら、季節ごとの限定の可能性
            past = (j.get("segs") or [])[:-1]
            if past and min(past) <= 20 and j.get("seg") and now - j["seg"] <= 120 * DAY:
                res[k]["lim"] = f"以前にも {min(past)} 週ほど出て止まったことがある（季節ごとの限定の可能性）"
    for k, x in res.items():
        d = x.pop("_d", None)
        if d and starts[(k[0], d)] >= 3 and not x.get("lim"):
            x["lim"] = f"同じメンテで同じ海域の {starts[(k[0], d)]} 組がいっせいに出始めた（限定の可能性）"
    return res


def build_drop_data(states, cfg, ships, now, wiki=None):
    """公開ページ用の Drop_Data.json と、管理画面用の判定一覧 (data/judge.json) を作る。

    限定 = 期間限定ドロップのページで継続中になっている艦 (アップデート日時以降に出ているもの)。
    常設 = それ以外で、今も出続けている艦 (途中から出始めた艦・新艦・ドロップ表が変わった艦も含む)。
           率は、今の出方が始まってから (途切れた期間の後) の分で出す。
    載せない = 出なくなった艦 (常設から外れた・限定が終わった)、誤報告の疑い。
    データだけで常設を限定にすることはしない。
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

    # 手動で限定にしたもの (追加日時が基準日時より前のものは外れる)
    manual_lim = set()
    for e in cfg["limited_manual"]:
        try:
            added = parse_dt(e.get("added")) if e.get("added") else now
        except ValueError:
            added = now
        if added >= (cutoff if cutoff is not None else 0):
            manual_lim.add(norm3(e))
    # マスごとのツールの内訳 (報告の偏りを調べるため)
    node_orig = defaultdict(lambda: defaultdict(int))
    for mo in hist:
        for m, nodes in states[mo].get("orig", {}).items():
            for node, oks in nodes.items():
                for ok, v in oks.items():
                    node_orig[(m, node)][ok] += v
    # その海域で、その艦が最初に出た時刻 (どのマスでも)
    map_first = {}
    for (m, node, sid), j in judged.items():
        k = (m, sid)
        map_first[k] = min(map_first.get(k, j["first"]), j["first"])
    hist_from = month_start(hist[0]) - 9 * 3600 if hist else now
    notes = change_notes(judged, cfg, now, on_node, on_map)
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

    # 今の出方が始まってから (seg 以降) の合計。途中から出始めた艦の率を薄めないため
    hist_months = [mo for mo in months if mo in hist]

    def seg_sum(m, node, sid, seg):
        a = b = c1 = c2 = 0
        for mo in hist_months:
            if month_start(next_month(mo)) <= seg - DAY:
                continue
            for bk, (x, y) in states[mo]["dden"].get(m, {}).get(node, {}).items():
                if int(bk) >= seg:
                    a += x
                    b += y
            for bk, (x, y) in states[mo]["dsid"].get(m, {}).get(node, {}).get(str(sid), {}).items():
                if int(bk) >= seg:
                    c1 += x
                    c2 += y
        return a, b, c1, c2

    _segmap = {}

    def seg_map(m, seg):
        if (m, seg) not in _segmap:
            t = 0
            for mo in hist_months:
                for node, bks in states[mo]["dden"].get(m, {}).items():
                    t += sum(x + y for bk, (x, y) in bks.items() if int(bk) >= seg)
            _segmap[(m, seg)] = t
        return _segmap[(m, seg)]

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
            if j and nS + nO < MIN_NODE_BATTLES:
                cls = "fewnode"     # 戦闘の無いマス (資源マスなど) に紛れ込んだ記録。率も意味をなさない
            page = ("ongoing" if (k3 in on_node or (m, nn) in on_map)
                    else "ended" if (k3 in ended or (m, None, nn) in ended) else "")
            # 公開ページでの扱い
            if k3 in res_force:
                pub, why = "常設", "manual"
            elif page == "ongoing":
                pub, why = "限定", "page"
            elif k3 in manual_lim:
                pub, why = "限定", "manual_lim"
            elif cls == "fewnode":
                pub, why = "", cls
            elif k3 in res_off:
                pub, why = "", "manual_off"
            elif cls in ("continuous", "added", "new", "intermittent"):
                pub, why = "常設", ("was_limited" if page == "ended" else cls)
            else:
                pub, why = "", cls          # stopped / misreport / 記録なし: 載せない
            ds, do = since_sid.get((m, node, sid), (0, 0))
            # 出始めて (再開して) 60 日未満の艦は、最新のアップデート後にも出ていることを確かめてから常設にする。
            # 限定がメンテナンスで終わった直後は「出なくなった」と言えるだけの戦闘数がまだ無いため
            if pub == "常設" and why != "manual" and cutoff is not None and not (ds + do):
                began = j.get("seg") or (j.get("first") if cls == "new" else None)
                if began and now - began < NEW_DAYS * DAY and began < cutoff:
                    pub, why = "", "unconfirmed"
            # 出始めたばかりで 2 件以下は、まだ誤報告と見分けられないので載せない
            if pub == "常設" and why == "new" and j.get("C", 0) <= 2:
                pub, why = "", "unconfirmed"
            # 報告が 1 人・1 時間・1 種類のツールに偏っているものは載せない (誤報告・いたずら対策)
            sus = suspicious(j, node_orig[(m, node)]) if j else ""
            if sus and why not in ("manual", "page", "manual_lim"):
                pub, why = "", "suspect"
            # その海域で初めて出た艦 (判定期間のはじめから 60 日以上たってから出始めた) は、管理画面で承認するまで載せない
            if (pub == "常設" and why not in ("manual",) and cls == "new"
                    and map_first.get((m, sid)) == j.get("first") and j["first"] - hist_from >= NEW_DAYS * DAY):
                pub, why = "", "hold"
            if pub == "常設":
                seg = j.get("seg")
                if seg:
                    a, b, c1, c2 = seg_sum(m, node, sid, seg)
                    r, d, n, basis = rate(c1, c2, a, b, m, seg_map(m, seg))
                else:
                    r, d, n, basis = rate(dS.get(sid, 0), dO.get(sid, 0), nS, nO, m, map_den_all[m])
                rows.append([nm, ti_of.get(sid, 7), 0, r, d, n, basis, why])
                seen.add(nn)
            elif pub == "":
                pass
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
                nt = notes.get((m, node, sid), {})
                ns, no = since_den.get((m, node), (0, 0))
                reason = reason_text(pub, why, cls, j, nt, sus, ds + do, ns + no, cutoff, nS + nO)
                judge_rows.append([m, node, nm, cls, j.get("C", 0), j.get("N", 0),
                                   fmt_dt(j["first"])[:10] if j else "", fmt_dt(min(j["last"], now))[:10] if j else "",
                                   int(k3 in wiki_set), page, pub, why,
                                   j.get("rank", ""), nt.get("change", ""), nt.get("lim", ""), sus, reason])
        # 手動で追加した限定 (追加日時が基準日時より前のものは自動的に外れる)
        for e in cfg["limited_manual"]:
            if e.get("map") != m or e.get("node") != node or not e.get("ship"):
                continue
            nn = norm_name(e["ship"])
            if nn in seen or (m, node, nn) not in manual_lim:
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
            judge_rows.append([m, node, nm, "none", 0, x[0] + x[1], "", "", 1, "", "", "none", "", "", "", "",
                               f"Wiki の出撃ドロップには載っているが、判定期間（{hist[0]}〜{hist[-1]}）の"
                               f"戦闘 {x[0] + x[1]:,} 回で 1 回も出ていない"])

    rejected = sum(v for mo in hist for nodes in states[mo].get("rej", {}).values()
                   for rs in nodes.values() for v in rs.values())
    first = min((states[mo]["first"] for mo in hist), default=None)
    last = max((states[mo]["last"] for mo in months), default=None)
    data = {
        "rejected": rejected,
        "generated": fmt_dt(now),
        "data_from": fmt_dt(first) if first else None,
        "data_to": fmt_dt(last) if last else None,
        "cutoff": fmt_dt(cutoff) if cutoff is not None else None,
        "records": sum(states[mo]["records"] for mo in hist),
        "months": hist,
        "limited_months": [mo for mo in months if cutoff is not None and mo >= month_of(cutoff)] or hist,
        "rate_mode": mode,
        "sources": SOURCES,
        # レア度 (api_backs): 1 = コモン (1〜3) / 2 = レア (4〜5) / 3 = スーパーレア (6〜8)
        "rarity": rarity_of_names(ships, maps),
        "single_hidden": single,
        "types": TYPE_LABELS,
        "columns": ["name", "type", "limited", "rate", "drops", "battles", "basis", "source"],
        "maps": maps,
    }
    judge = {
        "generated": fmt_dt(now),
        "months": hist,
        "labels": JUDGE_LABEL,
        "columns": ["map", "node", "ship", "class", "drops", "battles", "first", "last", "wiki", "page", "published", "why",
                    "rank", "change", "lim", "suspect", "reason"],
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
                        meta.update(sig=sig, fetched_at=now, dump_ver=dump_ver)
                        save_json(meta_path, meta)
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
            # db.poi.moe に無い月 (2 か月分まとめたファイルに入っている月など) は 7 日間は探し直さない
            missing = {k: v for k, v in (meta.get("archive_missing") or {}).items() if now - v < 7 * DAY}
            todo = []
            for mo in reversed(months):
                if mo in missing:
                    continue
                st = drop_states.get(mo)
                in_month = cutoff is not None and month_start(mo) <= cutoff < month_start(next_month(mo))
                if st and st.get("final") and (not in_month or cutoff in st.get("cuts", [])):
                    continue   # 取り込み済み
                todo.append(mo)
            if len(todo) > ARCHIVE_PER_RUN:
                notes.append(f"過去月は残り {len(todo) - ARCHIVE_PER_RUN} か月を次回以降に取り込みます")
                log(f"過去月: 今回 {ARCHIVE_PER_RUN} か月、残り {len(todo) - ARCHIVE_PER_RUN} か月は次回以降")
                todo = todo[:ARCHIVE_PER_RUN]
                # 続きがあることを workflow に伝える (次の実行を自動で起動する)
                if os.environ.get("GITHUB_OUTPUT"):
                    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                        f.write("pending=1\n")
            def complete(st, mo):
                """ファイルにその月が丸ごと入っているか (前後 2 日の余裕をみる)"""
                return (st["first"] <= month_start(mo) + 2 * DAY
                        and st["last"] >= month_start(next_month(mo)) - 2 * DAY)

            done = set()
            for mo in todo:
                if mo in done:
                    continue   # この実行で、別の月のファイルからすでに取り込んだ
                # その月のファイル。無ければ翌月のファイル (2 か月分まとめて入っていることがある)
                urls = [archive_url("dropshiprecords", mo)]
                if next_month(mo) < cur:
                    urls.append(archive_url("dropshiprecords", next_month(mo)))
                path = os.path.join(tmp, f"archive_{mo}.gz")
                err, not_found = None, False
                for url in urls:
                    try:
                        log(f"過去月アーカイブ {mo}: {url}")
                        size = http_get(url, dest=path, timeout=600)
                        log(f"  {size / 1e6:.1f} MB")
                        new = aggregate_drops(path, routes, cuts)
                        if mo not in new or not complete(new[mo], mo):
                            raise RuntimeError(f"{mo} の記録が丸ごとは含まれていません")
                        # 目的の月と、同じファイルに丸ごと入っていた他の月 (判定期間内のもの) を保存
                        got = {}
                        for k, st in new.items():
                            if k in months and (k == mo or (complete(st, k) and not (drop_states.get(k) or {}).get("final"))):
                                st["final"] = True
                                got[k] = st
                        drop_states = merge_state(args.state_dir, "drop", drop_states, got, force=True)
                        done |= set(got)
                        for k in sorted(got):
                            notes.append(f"{k} の過去月アーカイブを取り込み ({got[k]['records']:,} 件)")
                            missing.pop(k, None)
                        err = None
                        break
                    except Exception as e:  # noqa: BLE001
                        err = e
                        log(f"  {e}")
                        if url == urls[0]:
                            not_found = "404" in str(e)
                            if not not_found:
                                break   # その月のファイルはあるのに失敗した (途中で切れた等): 次回取り直す
                    finally:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                if err is not None:
                    if not_found:
                        missing[mo] = now
                        warn(f"{mo} の過去月アーカイブは db.poi.moe にありません。7 日後にもう一度探します")
                        notes.append(f"{mo} の過去月アーカイブは公開されていないため取り込めず")
                    else:
                        warn(f"{mo} の過去月アーカイブを取り込めませんでした。次回もう一度取り込みます: {err}")
                        notes.append(f"{mo} の過去月アーカイブを取り込めず")
            if missing != (meta.get("archive_missing") or {}):
                meta["archive_missing"] = missing
                os.makedirs(args.state_dir, exist_ok=True)
                save_json(meta_path, meta)

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
