#!/usr/bin/env python3
"""よそのサイトのページ (艦これ攻略Wiki・艦ろぐ) の原本を、公開リポジトリに読める形で置かないための暗号化。

リポジトリは公開されていて、main ブランチは GitHub Pages でそのまま配信される。原本の HTML を
そのまま置くと、
  - よそのページの丸ごとのコピーを再配布することになる
  - Wiki のページに含まれるスクリプトが、こちらのサイト (github.io) の上で動いてしまう
ので、環境変数 RAW_KEY (GitHub の Secrets と、管理画面の .env に同じ値を入れる) があれば
openssl で暗号化して「ファイル名.enc」として置く。RAW_KEY が無ければ今までどおり平文で置く。

  python scripts/rawstore.py migrate DIR ...   DIR の中の平文の原本を暗号化して、平文を消す
"""
import os
import subprocess
import sys

SUFFIX = ".enc"
TARGET_EXT = (".html", ".html.gz")
_ARGS = ["-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", "env:RAW_KEY"]


def key():
    return os.environ.get("RAW_KEY", "").strip()


def _openssl(args, data):
    r = subprocess.run(["openssl", "enc"] + args, input=data, capture_output=True, check=False)
    if r.returncode != 0:
        raise RuntimeError("openssl: " + r.stderr.decode(errors="replace").strip()[:200])
    return r.stdout


def encrypt(data):
    return _openssl(_ARGS, data)


def decrypt(data):
    return _openssl(["-d"] + _ARGS, data)


def write(path, data):
    """data (bytes) を保存する。RAW_KEY があれば path.enc に暗号化して置き、平文の path は消す。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if key():
        with open(path + SUFFIX, "wb") as f:
            f.write(encrypt(data))
        if os.path.exists(path):
            os.remove(path)
    else:
        with open(path, "wb") as f:
            f.write(data)


def read(path):
    """path (または path.enc) の中身 (bytes)。無ければ None。"""
    if os.path.exists(path + SUFFIX) and key():
        with open(path + SUFFIX, "rb") as f:
            return decrypt(f.read())
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    return None


def exists(path):
    return os.path.exists(path) or (bool(key()) and os.path.exists(path + SUFFIX))


def plain_name(path):
    return path[:-len(SUFFIX)] if path.endswith(SUFFIX) else path


def migrate(dirs):
    """平文の原本を暗号化して、平文を消す。RAW_KEY が無ければ何もしない。"""
    if not key():
        print("RAW_KEY が設定されていないので、原本は平文のままです（公開リポジトリから読めます）")
        return 0
    n = 0
    for d in dirs:
        for root, _, files in os.walk(d):
            for fn in files:
                p = os.path.join(root, fn)
                if fn.endswith(TARGET_EXT):
                    with open(p, "rb") as f:
                        write(p, f.read())
                    n += 1
    print(f"原本 {n} 個を暗号化しました" if n else "暗号化していない原本はありません")
    return n


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "migrate":
        migrate(sys.argv[2:])
    else:
        print(__doc__)
        sys.exit(2)
