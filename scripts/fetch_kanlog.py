#!/usr/bin/env python3
"""艦ろぐ (https://kanlog.info/) のページを取得して保存する。

艦ろぐの運営者 (kurimoto 様) から、2 時間に 1 回程度・更新が終わった時間帯 (奇数時の 10 分過ぎなど)
の取得の許可をいただいている。サイトに負担をかけないよう:
  - 1 ページずつ、間を数秒あけて取得する
  - まず「期間限定ドロップ」のページを取り、サイトの更新日時が前回と同じなら、海域のページは取りに行かない
  - User-Agent にこのサイトのリポジトリを書く

保存先 (--out、ふつうは kanlog ブランチの作業ディレクトリ):
  raw/limited.html.gz      期間限定ドロップ一覧
  raw/map_11.html.gz …    海域ごとのページ (data/map_routes.json にある海域)
  meta.json                取得日時・サイトの更新日時・ページごとの結果
"""
import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("KANLOG_BASE", "https://kanlog.info")
UA = "Kancolle_finder-updater (+https://github.com/yss19411208/Kancolle_finder)"
WAIT = float(os.environ.get("KANLOG_WAIT", "3.0"))         # ページとページの間 (秒)


def log(*a):
    print(*a, flush=True)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", errors="replace")


def site_updated(html):
    """ページに書かれている「更新日時：11/15 13:00」を取り出す。"""
    m = re.search(r"更新日時[：:]\s*([0-9]{1,2}/[0-9]{1,2}\s+[0-9]{1,2}:[0-9]{2})", html)
    return m.group(1) if m else ""


def save(out, name, html):
    os.makedirs(os.path.join(out, "raw"), exist_ok=True)
    with gzip.open(os.path.join(out, "raw", name + ".html.gz"), "wt", encoding="utf-8") as f:
        f.write(html)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="kanlog")
    ap.add_argument("--force", action="store_true", help="サイトの更新日時が前回と同じでも、海域のページを取りに行く")
    args = ap.parse_args()

    meta_path = os.path.join(args.out, "meta.json")
    try:
        meta = json.load(open(meta_path, encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    now = int(time.time())
    routes = json.load(open(os.path.join(ROOT, "data", "map_routes.json"), encoding="utf-8"))
    maps = sorted(routes, key=lambda m: [int(x) for x in m.split("-")])

    try:
        html = get(BASE + "/limited/")
    except Exception as e:  # noqa: BLE001
        log(f"::warning::艦ろぐの期間限定ドロップのページを取得できませんでした: {e}")
        meta.update(checked_at=now, ok=False, message=f"取得できませんでした: {e}")
        os.makedirs(args.out, exist_ok=True)
        json.dump(meta, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return
    upd = site_updated(html)
    log(f"艦ろぐ: サイトの更新日時 {upd or '(読み取れず)'}（前回 {meta.get('site_updated') or 'なし'}）")
    meta["checked_at"] = now
    if upd and upd == meta.get("site_updated") and not args.force:
        log("前回から更新されていないので、海域のページは取得しません")
        meta.update(ok=True, message="更新なし")
        json.dump(meta, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return
    save(args.out, "limited", html)
    pages = {"limited": "ok"}
    for m in maps:
        w, n = m.split("-")
        mid = int(w) * 10 + int(n)
        time.sleep(WAIT)
        try:
            save(args.out, f"map_{mid}", get(f"{BASE}/map/{mid}/"))
            pages[m] = "ok"
        except Exception as e:  # noqa: BLE001
            pages[m] = f"失敗: {e}"
            log(f"  {m}: {e}")
    ok = sum(1 for v in pages.values() if v == "ok")
    meta.update(fetched_at=now, site_updated=upd, ok=ok == len(pages), pages=pages,
                message=f"{ok}/{len(pages)} ページを取得")
    os.makedirs(args.out, exist_ok=True)
    json.dump(meta, open(meta_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"艦ろぐ: {ok}/{len(pages)} ページを取得しました")


if __name__ == "__main__":
    sys.exit(main())
