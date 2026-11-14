#!/usr/bin/env python3
"""艦これ攻略Wiki「期間限定ドロップイベント」を取得し、data/limited_page.json を作り直す。

GitHub Actions のデータ更新の最初に実行する (失敗しても集計は止めない)。

  1. Wiki から今のページを取得する (1 日 1 回まで。管理画面からの再集計や HTML の送信では毎回)
  2. 読み取った結果が、保存済みの HTML (data/limited_src/current.html) より明らかに少なければ
     (取得の失敗・ページの一時的な崩れ) 使わずに、保存済みの HTML のままにする
  3. data/limited_src/ の HTML (今のページ + アーカイブ) から data/limited_page.json を作り直す
  4. 結果を data/limited_src/status.json に残す (管理画面に表示する)

アーカイブ (終了済ドロップ) のページは折りたたみを開くのにブラウザが必要なので自動では取得しない。
管理画面から保存した HTML を送ると data/limited_src/archive.html が更新される。
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_limited_page as P  # noqa: E402

URL = "https://wikiwiki.jp/kancolle/" + urllib.parse.quote("期間限定ドロップイベント")
UA = "Kancolle_finder-updater (+https://github.com/yss19411208/Kancolle_finder)"
CUR = os.path.join(P.SRC_DIR, "current.html")
STATUS = os.path.join(P.SRC_DIR, "status.json")
MIN_INTERVAL = 20 * 3600     # 定時実行では 20 時間に 1 回だけ Wiki に取りに行く


def load_status():
    try:
        with open(STATUS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def jst(ts):
    return time.strftime("%Y/%m/%d %H:%M", time.gmtime(ts + 9 * 3600))


def fetch():
    req = urllib.request.Request(URL, headers={"User-Agent": UA, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", errors="replace")


def summary(page):
    return {"events": len(page.get("events", [])), "entries": len(page.get("entries", [])),
            "ongoing": len(page.get("ongoing", {}))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="前回の取得から時間がたっていなくても取りに行く")
    ap.add_argument("--no-fetch", action="store_true", help="取得せず、保存済みの HTML から作り直すだけ")
    args = ap.parse_args()

    st = load_status()
    old_st = json.loads(json.dumps(st))
    now = int(time.time())
    msg = ""
    changed_src = False
    if not args.no_fetch and (args.force or now - st.get("fetched_at", 0) >= MIN_INTERVAL):
        st["fetched_at"] = now
        try:
            html = fetch()
            if "ドロップ対象" not in html or "期間限定ドロップ" not in html:
                raise ValueError("ページの中身が想定と違います（ログインやアクセス制限の画面の可能性）")
            # 保存済みの HTML と比べて、明らかに少なければ使わない
            old_ev = {"events": 0, "ongoing": 0}
            if os.path.exists(CUR):
                _, info = P.build_page([(CUR, None)])
                old_ev = info[0]
            _, info = P.build_page([("Wiki から取得したページ", html)])
            new_ev = info[0]
            if new_ev["events"] < max(1, old_ev["events"] * 0.7) or (old_ev["ongoing"] and not new_ev["ongoing"]):
                raise ValueError(f"読み取れたイベントが少なすぎます（取得 {new_ev['events']} 件 / 保存済み "
                                 f"{old_ev['events']} 件、継続中一覧 {new_ev['ongoing']} 海域）")
            old_html = open(CUR, encoding="utf-8", errors="replace").read() if os.path.exists(CUR) else ""
            if html != old_html:
                os.makedirs(P.SRC_DIR, exist_ok=True)
                with open(CUR, "w", encoding="utf-8") as f:
                    f.write(html)
                changed_src = True
            st.update(ok=True, source="wiki", got_at=now)
            msg = f"Wiki から取得しました（イベント {new_ev['events']} 件・継続中一覧 {new_ev['ongoing']} 海域）"
        except Exception as e:  # noqa: BLE001
            st["ok"] = False
            msg = f"Wiki から取得できませんでした。保存済みのページを使います: {e}"
            print("::warning::" + msg)

    srcs = P.default_sources()
    if not srcs:
        print("data/limited_src/ に HTML が無いので、data/limited_page.json はそのままにします")
        return
    before = summary(json.load(open(os.path.join(P.ROOT, "data", "limited_page.json"), encoding="utf-8"))) \
        if os.path.exists(os.path.join(P.ROOT, "data", "limited_page.json")) else {}
    page, info = P.build_page(srcs)
    if not page.get("events"):
        print("::warning::限定ページを読み取れませんでした。data/limited_page.json はそのままにします")
        return
    P.save_page(page)
    after = summary(page)
    st.update(page=after, files={os.path.basename(p): i for (p, _), i in zip(srcs, info)},
              message=msg or st.get("message", ""))
    if msg:
        st["message_at"] = now
    if before != after or changed_src:
        st["changed_at"] = now
    if st != old_st:          # 変わらなければ書き換えない (無駄なコミットを避ける)
        os.makedirs(P.SRC_DIR, exist_ok=True)
        with open(STATUS, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
            f.write("\n")
    print(f"限定ページ: {msg or '保存済みの HTML から作り直しました'} → イベント {after['events']} / "
          f"艦 {after['entries']} / 継続中一覧 {after['ongoing']} 海域（取得 {jst(st.get('got_at', 0)) if st.get('got_at') else 'なし'}）")


if __name__ == "__main__":
    main()
