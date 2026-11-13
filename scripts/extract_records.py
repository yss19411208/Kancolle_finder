#!/usr/bin/env python3
"""poi のドロップ記録から、指定した艦 (または誤報告の疑いがある記録) を生のまま取り出す調査用スクリプト。

使い方:
  python scripts/extract_records.py --month 2026-11 --targets "1-2-D 卯月, 1-1-A 神通"
  python scripts/extract_records.py --month 2026-04 --targets auto

  --month   空なら当月のダンプ、YYYY-MM なら db.poi.moe の過去月アーカイブ
  --targets 「海域-マス 艦名」をカンマ区切り。auto なら、その月に 2 件以下しか出ていない
            珍しい艦 (そのマスのドロップの 0.01% 未満) を自動で選ぶ

出力 (out/ 以下):
  records.json   取り出した記録そのもの (日時・同じマスでの敵編成の出現割合を付ける)
  normal.json    比較用に、同じマスの普通の記録を数件
  summary.json   マスごとの敵編成・ツール名・司令部Lvの内訳
"""
import argparse
import json
import os
import random
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update as U  # noqa: E402

JST = timezone(timedelta(hours=9))


def jsonable(v):
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    if type(v).__name__ == "ObjectId":
        return {"$oid": str(v)}
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def place_of(routes, map_id, cell):
    m = f"{map_id // 10}-{map_id % 10}"
    return m, routes.get(m, {}).get(str(cell), f"#{cell}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="")
    ap.add_argument("--targets", default="auto")
    ap.add_argument("--max", type=int, default=50, help="1 つの対象あたりに出す記録の上限")
    ap.add_argument("--file", help="ダウンロード済みのダンプを使う (テスト用)")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    U.refresh_masters(offline=True)
    ships = U.load_json(os.path.join(U.ROOT, "data", "ship_master.json"))
    routes = U.load_json(os.path.join(U.ROOT, "data", "map_routes.json"))
    name_of = {int(k): v["name"] for k, v in ships.items()}
    ids_of = defaultdict(set)
    for k, v in ships.items():
        ids_of[U.norm_name(v["name"])].add(int(k))

    auto = args.targets.strip().lower() == "auto"
    want = set()   # (map, node, sid)
    if not auto:
        for t in args.targets.replace("、", ",").split(","):
            t = t.strip()
            if not t:
                continue
            place, _, ship = t.partition(" ")
            parts = place.split("-")
            if len(parts) != 3 or not ship.strip():
                sys.exit(f"対象の書き方が違います: {t}（例: 1-2-D 卯月）")
            ids = ids_of.get(U.norm_name(ship.strip()))
            if not ids:
                sys.exit(f"艦名が見つかりません: {ship}")
            for sid in ids:
                want.add((f"{parts[0]}-{parts[1]}", parts[2], sid))

    with tempfile.TemporaryDirectory() as tmp:
        path = args.file
        if not path:
            url = (U.archive_url("dropshiprecords", args.month) if args.month
                   else U.DUMP_BASE + "dropshiprecords.gz")
            path = os.path.join(tmp, "dump.gz")
            U.log(f"ダウンロード: {url}")
            U.log(f"  {U.http_get(url, dest=path, timeout=600) / 1e6:.1f} MB")

        # 1 周目: マスごとの敵編成・艦の数 (auto のときは珍しい艦を選ぶため)
        fleet = defaultdict(Counter)    # (map,node) -> Counter(敵編成)
        origin = defaultdict(Counter)
        ship_n = defaultdict(Counter)   # (map,node) -> Counter(sid)
        battles = Counter()
        keep = defaultdict(list)        # (map,node,sid) -> [doc]
        normal = defaultdict(list)      # (map,node) -> 普通の記録 (無作為に数件)
        rnd = random.Random(1)
        total = 0
        for d in U.iter_archive_docs(path):
            if "mapId" not in d or "_id" not in d:
                continue
            try:
                map_id = int(d["mapId"])
                if map_id // 10 > U.MAX_WORLD or map_id < 11 or (d.get("mapLv") or 0) != 0:
                    continue
                if d.get("rank") not in ("S", "A", "B"):
                    continue
                m, node = place_of(routes, map_id, int(d["cellId"]))
                sid = int(d.get("shipId") or 0)
            except (TypeError, ValueError, KeyError):
                continue
            total += 1
            key = (m, node)
            fl = tuple(d.get("enemyShips1") or [])
            fleet[key][fl] += 1
            origin[key][str(d.get("origin"))] += 1
            battles[key] += 1
            if sid > 0:
                ship_n[key][sid] += 1
            k3 = (m, node, sid)
            if auto or k3 in want:
                lst = keep[k3]
                if len(lst) < args.max:
                    lst.append(d)
            # 比較用: 各マスで無作為に 3 件 (リザーバー抽出)
            lst = normal[key]
            if len(lst) < 3:
                lst.append(d)
            elif rnd.random() < 3 / battles[key]:
                lst[rnd.randrange(3)] = d
        U.log(f"記録 {total:,} 件を読みました")

        if auto:
            chosen = set()
            for (m, node), cnt in ship_n.items():
                tot = sum(cnt.values())
                if tot < 5000:
                    continue
                for sid, c in cnt.items():
                    if c <= 2 and c / tot < 0.0001:
                        chosen.add((m, node, sid))
            keep = {k: v for k, v in keep.items() if k in chosen}
            U.log(f"自動で選んだ珍しい艦: {len(keep)} 組")

        def describe(d):
            m, node = place_of(routes, int(d["mapId"]), int(d["cellId"]))
            ts = U.oid_ts(d)
            fl = tuple(d.get("enemyShips1") or [])
            fc = fleet[(m, node)]
            return {
                "_日時(日本時間)": datetime.fromtimestamp(ts, JST).strftime("%Y/%m/%d %H:%M:%S") if ts else None,
                "_海域マス": f"{m}-{node}",
                "_艦名": name_of.get(int(d.get("shipId") or 0), "ドロップなし" if not d.get("shipId") else "?"),
                "_この敵編成がこのマスで出た割合": round(fc[fl] / max(1, sum(fc.values())) * 100, 4),
                "_このマスで最も多い敵編成": list(fc.most_common(1)[0][0]) if fc else None,
                **jsonable(d),
            }

        records = []
        for (m, node, sid), docs in sorted(keep.items()):
            cnt = ship_n[(m, node)]
            for d in docs:
                r = describe(d)
                r["_この艦のこのマスでの件数(この月)"] = cnt[sid]
                r["_このマスのドロップ数(この月)"] = sum(cnt.values())
                records.append(r)
        places = {(r["_海域マス"].rsplit("-", 1)[0], r["_海域マス"].rsplit("-", 1)[1]) for r in records}
        if not auto:
            places |= {(m, node) for m, node, _ in want}
        out_normal = [describe(d) for p in sorted(places) for d in normal.get(p, [])]
        summary = {
            f"{m}-{node}": {
                "戦闘数": battles[(m, node)],
                "敵編成(上位10)": [[list(k), v] for k, v in fleet[(m, node)].most_common(10)],
                "敵編成の種類数": len(fleet[(m, node)]),
                "ツール名": origin[(m, node)].most_common(),
            } for m, node in sorted(places)
        }

    os.makedirs(args.out, exist_ok=True)
    for fn, obj in (("records.json", records), ("normal.json", out_normal), ("summary.json", summary)):
        with open(os.path.join(args.out, fn), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    U.log(f"出力: 記録 {len(records)} 件 / 比較用 {len(out_normal)} 件 / マス {len(summary)} か所")
    if not records:
        U.warn("対象の記録は見つかりませんでした（その月には出ていない可能性があります）")


if __name__ == "__main__":
    main()
