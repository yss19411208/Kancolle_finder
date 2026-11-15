#!/usr/bin/env python3
"""艦ろぐ (https://kanlog.info/) の保存済みページ (scripts/fetch_kanlog.py が kanlog ブランチに置いたもの) から、
マス・艦・勝利ランクごとのドロップ数を読み取り、集計 (scripts/update.py --kanlog) で使う JSON にする。

艦ろぐの海域ページには、表の元になるデータが JSON で埋め込まれている:
  drop-rate-row-variant-data        "{セル番号}-{艦ID}-{難易度}" -> 勝利ランクの組み合わせごとの {s, a, b} (艦ID -1 = ドロップなし)
  drop-rate-rank-variant-total-data "0:s" / "0:a" / "0:b" -> {セル番号: 戦闘数}
  drop-history-data                 {"dates": [...], "rows": {"{セル}-{艦ID}-{難易度}": {日の番号: [S, A, B]}}}  (直近 30 日)
  drop-rate-latest-data             "{セル}-{艦ID}-{難易度}" -> {s/a/b: 最終ドロップ日}
各行 (<tr data-row-key=… data-m=…>) には、期間限定の印 (「期間限定 【H'ween2026】」など) が付いている。
セル番号とマス名は、セルのボタン (data-cell-no / data-cell-label) で対応づける。

出力 (--out):
{
  "site_updated": "2026/11/15 13:00", "fetched_at": 秒,
  "maps": {"1-1": {"start": "2026-10-15", "updated": "2026/11/15 13:00:28", "dates": ["2026-10-17", …],
           "nodes": {"C": {"tot": [S戦闘, A/B戦闘], "ships": {"艦ID": [S, A/B]}, "lim": {"艦ID": "【…】"},
                           "last": {"艦ID": "2026-11-15"},
                           "days": {"tot": {"日の番号": [S, A/B]}, "ships": {"艦ID": {"日の番号": [S, A/B]}}}}}}}
}
"""
import argparse
import glob
import gzip
import html as H
import json
import os
import re
import sys


def scripts(h):
    out = {}
    for m in re.finditer(r'<script id="([^"]+)" type="application/json"[^>]*>(.*?)</script>', h, re.S):
        try:
            out[m.group(1)] = json.loads(m.group(2))
        except ValueError:
            pass
    return out


def split_key(k):
    """"12-424-0" -> (12, 424, 0)、"1--1-0" -> (1, -1, 0)"""
    m = re.fullmatch(r"(\d+)-(-?\d+)-(\d+)", k)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_map(h):
    js = scripts(h)
    cells = {int(no): lab for no, lab in re.findall(r'data-cell-no="(\d+)" data-cell-label="([^"]*)"', h)}
    if not cells or "drop-rate-row-variant-data" not in js:
        return None
    start = re.search(r'集計: <time datetime="([^"]+)"', h)
    upd = re.search(r'更新: <time datetime="([^"]+)"', h)
    nodes = {}

    def node(no):
        lab = cells.get(no)
        if not lab:
            return None
        return nodes.setdefault(lab, {"tot": [0, 0], "ships": {}, "lim": {}, "last": {},
                                       "days": {"tot": {}, "ships": {}}})

    # 戦闘数 (ドロップなしを含む全部)
    tot = js.get("drop-rate-rank-variant-total-data", {})
    for rk, i in (("0:s", 0), ("0:a", 1), ("0:b", 1)):
        for no, n in (tot.get(rk) or {}).items():
            nd = node(int(no))
            if nd is not None:
                nd["tot"][i] += int(n)
    # 艦ごとのドロップ数 (S/A/B)
    for k, v in js["drop-rate-row-variant-data"].items():
        p = split_key(k)
        if not p or p[2] != 0 or p[1] <= 0:      # 難易度つき (イベント) とドロップなしは除く
            continue
        nd = node(p[0])
        sab = v.get("sab") or {}
        if nd is not None and sab:
            nd["ships"][str(p[1])] = [int(sab.get("s", 0)), int(sab.get("a", 0)) + int(sab.get("b", 0))]
    # 期間限定の印
    for m in re.finditer(r'<tr class="js-drop-rate-row"[^>]*data-row-key="([^"]+)"[^>]*>(.*?)</tr>', h, re.S):
        p = split_key(m.group(1))
        if not p or p[2] != 0:
            continue
        b = re.search(r'title="期間限定ドロップ">\s*期間限定\s*(.*?)</span>', m.group(2), re.S)
        nd = node(p[0])
        if b and nd is not None:
            nd["lim"][str(p[1])] = H.unescape(re.sub(r"<[^>]+>", "", b.group(1))).strip() or "期間限定"
    # 最終ドロップ日
    for k, v in (js.get("drop-rate-latest-data") or {}).items():
        p = split_key(k)
        if not p or p[2] != 0 or p[1] <= 0:
            continue
        nd = node(p[0])
        if nd is not None and v:
            nd["last"][str(p[1])] = max(v.values())
    # 日ごとの数 (直近 30 日)
    hist = js.get("drop-history-data") or {}
    for k, days in (hist.get("rows") or {}).items():
        p = split_key(k)
        if not p or p[2] != 0:
            continue
        nd = node(p[0])
        if nd is None:
            continue
        for di, x in days.items():
            s, ab = int(x[0]), int(x[1]) + int(x[2])
            t = nd["days"]["tot"].setdefault(di, [0, 0])
            t[0] += s
            t[1] += ab
            if p[1] > 0 and s + ab:
                nd["days"]["ships"].setdefault(str(p[1]), {})[di] = [s, ab]
    return {"start": start.group(1) if start else None, "updated": upd.group(1) if upd else None,
            "dates": hist.get("dates") or [], "nodes": nodes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="kanlog", help="fetch_kanlog.py の保存先 (raw/ と meta.json がある所)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    try:
        meta = json.load(open(os.path.join(args.raw, "meta.json"), encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    out = {"site_updated": meta.get("site_updated", ""), "fetched_at": meta.get("fetched_at", 0), "maps": {}}
    n_ships = n_lim = 0
    for f in sorted(glob.glob(os.path.join(args.raw, "raw", "map_*.html.gz"))):
        mid = int(re.search(r"map_(\d+)", f).group(1))
        m = f"{mid // 10}-{mid % 10}"
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                r = parse_map(fh.read())
        except (OSError, ValueError) as e:
            print(f"::warning::艦ろぐ {m} を読み取れませんでした: {e}")
            continue
        if not r:
            print(f"::warning::艦ろぐ {m}: ページの形が想定と違います（サイトが変わった可能性）")
            continue
        out["maps"][m] = r
        n_ships += sum(len(nd["ships"]) for nd in r["nodes"].values())
        n_lim += sum(len(nd["lim"]) for nd in r["nodes"].values())
    if not out["site_updated"]:
        # 古い fetch_kanlog.py は更新日時を読み取れていなかったので、ページから読む
        for f in glob.glob(os.path.join(args.raw, "raw", "limited.html.gz")):
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                m = re.search(r"更新日時[：:]\s*(?:<time[^>]*>)?\s*([0-9]{1,2}/[0-9]{1,2}\s+[0-9]{1,2}:[0-9]{2})", fh.read())
            if m:
                out["site_updated"] = m.group(1)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"艦ろぐ: {len(out['maps'])} 海域・艦 {n_ships} 組（うち期間限定 {n_lim} 組）を読み取りました"
          f"（サイトの更新 {out['site_updated'] or '不明'}）")
    return 0 if out["maps"] else 1


if __name__ == "__main__":
    sys.exit(main())
