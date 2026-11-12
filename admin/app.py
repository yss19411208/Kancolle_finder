#!/usr/bin/env python3
"""艦これ ドロップ検索 管理画面。

このサーバーは poi のダンプを取得・解析しない。行うのは
  - ログイン
  - GitHub 上の設定ファイル (admin_config.json / Normal_Drop_Ship.json / Update.json) の読み書き
  - GitHub Actions の再集計の起動と状況表示
だけで、集計はすべて GitHub Actions 側で行われる。
"""
import base64
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from functools import wraps

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(os.path.join(HERE, ".env"))

REPO = os.environ.get("GITHUB_REPO", "yss19411208/Kancolle_finder")
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
WORKFLOW = os.environ.get("GITHUB_WORKFLOW", "update.yml")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
SITE_URL = os.environ.get("SITE_URL", f"https://{REPO.split('/')[0]}.github.io/{REPO.split('/')[1]}/")

# 編集できるファイル / 読むだけのファイル
WRITABLE = {"config": "admin_config.json", "resident": "Normal_Drop_Ship.json", "history": "Update.json"}
READABLE = dict(WRITABLE, drop="Drop_Data.json", master="data/ship_master.json", routes="data/map_routes.json")
MAX_BODY = 3 * 1024 * 1024

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    MAX_CONTENT_LENGTH=MAX_BODY,
)

# ---------------------------------------------------------------- ログイン
FAILS = {}          # ip -> [回数, 最終失敗時刻]
LOCK_AFTER = 5
LOCK_SECONDS = 600


def client_ip():
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"


def locked(ip):
    n, t = FAILS.get(ip, (0, 0))
    if n >= LOCK_AFTER and time.time() - t < LOCK_SECONDS:
        return True
    if time.time() - t >= LOCK_SECONDS:
        FAILS.pop(ip, None)
    return False


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify(error="ログインが必要です"), 401
            return redirect(url_for("login"))
        if request.method not in ("GET", "HEAD"):
            sent = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(sent, session.get("csrf", "")):
                return jsonify(error="不正なリクエストです (再読み込みしてください)"), 403
        return f(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = client_ip()
        if locked(ip):
            error = "試行回数が多すぎます。10分後にやり直してください。"
        else:
            user = request.form.get("user", "")
            pw = request.form.get("password", "")
            ok = (ADMIN_HASH and hmac.compare_digest(user, ADMIN_USER)
                  and check_password_hash(ADMIN_HASH, pw))
            if ok:
                FAILS.pop(ip, None)
                session.clear()
                session.permanent = True
                session["user"] = user
                session["csrf"] = secrets.token_urlsafe(32)
                return redirect(url_for("index"))
            n, _ = FAILS.get(ip, (0, 0))
            FAILS[ip] = (n + 1, time.time())
            time.sleep(float(os.environ.get("LOGIN_DELAY", "1")))
            error = "ユーザー名またはパスワードが違います。"
    return render_template("login.html", error=error, configured=bool(ADMIN_HASH and TOKEN))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("admin.html", csrf=session["csrf"], repo=REPO, site_url=SITE_URL,
                           user=session["user"])


@app.after_request
def headers(resp):
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
    return resp


# ---------------------------------------------------------------- GitHub API
class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def gh(method, path, body=None, raw=False):
    url = f"https://api.github.com/repos/{REPO}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "kancolle-finder-admin",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read()
            if raw:
                return payload
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        raise GitHubError(e.code, msg or e.reason) from None
    except urllib.error.URLError as e:
        raise GitHubError(502, f"GitHub に接続できません: {e.reason}") from None


@app.errorhandler(GitHubError)
def on_gh_error(e):
    hint = {401: "GITHUB_TOKEN が無効です", 403: "GITHUB_TOKEN の権限が不足しています",
            404: "ファイルまたはリポジトリが見つかりません",
            409: "他で更新されています。再読み込みしてからやり直してください",
            422: "他で更新されています。再読み込みしてからやり直してください"}.get(e.status, "")
    return jsonify(error=f"GitHub エラー {e.status}: {hint or e}"), 502


def file_sha(path):
    try:
        return gh("GET", f"/contents/{path}?ref={BRANCH}").get("sha")
    except GitHubError as e:
        if e.status == 404:
            return None
        raise


# ---------------------------------------------------------------- 検証
def is_str(x, n=200):
    return isinstance(x, str) and len(x) <= n


def validate(key, c):
    if key == "config":
        if not isinstance(c, dict):
            return "形式が不正です"
        for k in ("updates", "end_dates", "limited_manual", "limited_exclude"):
            if not isinstance(c.get(k), list):
                return f"{k} がありません"
        for u in c["updates"]:
            if not (isinstance(u, dict) and is_str(u.get("at"), 19) and len(u["at"]) == 19):
                return "アップデート日時は YYYY/MM/DD HH:MM:SS で入力してください"
        for e in c["end_dates"]:
            if not (isinstance(e, dict) and is_str(e.get("ship")) and e["ship"]
                    and is_str(e.get("end"), 19) and len(e["end"]) == 19):
                return "終了日の艦名と日時を確認してください"
        for e in c["limited_manual"] + c["limited_exclude"]:
            if not (isinstance(e, dict) and all(is_str(e.get(k)) and e[k] for k in ("map", "node", "ship"))):
                return "限定ドロップの 海域 / マス / 艦名 を確認してください"
    elif key == "resident":
        if not isinstance(c, dict) or not c:
            return "形式が不正です"
        for m, nodes in c.items():
            if not (is_str(m, 8) and isinstance(nodes, dict)):
                return f"{m}: 形式が不正です"
            for node, cols in nodes.items():
                if not (is_str(node, 8) and isinstance(cols, list) and len(cols) == 8
                        and all(isinstance(col, list) and all(is_str(n) for n in col) for col in cols)):
                    return f"{m}-{node}: 8 分類の配列になっていません"
    elif key == "history":
        if not isinstance(c, list):
            return "形式が不正です"
        for e in c:
            if not (isinstance(e, dict) and is_str(e.get("更新日時"), 30)
                    and isinstance(e.get("更新内容"), list) and all(is_str(x, 500) for x in e["更新内容"])):
                return "更新履歴の形式が不正です"
    return None


# ---------------------------------------------------------------- API
@app.route("/api/file/<key>")
@login_required
def get_file(key):
    if key not in READABLE:
        abort(404)
    path = READABLE[key]
    try:
        content = json.loads(gh("GET", f"/contents/{path}?ref={BRANCH}", raw=True))
    except GitHubError as e:
        if e.status == 404:
            return jsonify(content=None, sha=None)
        raise
    return jsonify(content=content, sha=file_sha(path) if key in WRITABLE else None)


@app.route("/api/file/<key>", methods=["POST"])
@login_required
def put_file(key):
    if key not in WRITABLE:
        abort(404)
    body = request.get_json(silent=True) or {}
    content = body.get("content")
    err = validate(key, content)
    if err:
        return jsonify(error=err), 400
    path = WRITABLE[key]
    indent = 1 if key == "resident" else 2
    text = json.dumps(content, ensure_ascii=False, indent=indent) + "\n"
    payload = {
        "message": (body.get("message") or f"管理画面: {path} を更新")[:200],
        "content": base64.b64encode(text.encode("utf-8")).decode(),
        "branch": BRANCH,
    }
    if body.get("sha"):
        payload["sha"] = body["sha"]
    res = gh("PUT", f"/contents/{path}", payload)
    return jsonify(ok=True, sha=res.get("content", {}).get("sha"))


@app.route("/api/rerun", methods=["POST"])
@login_required
def rerun():
    gh("POST", f"/actions/workflows/{WORKFLOW}/dispatches", {"ref": BRANCH})
    return jsonify(ok=True)


@app.route("/api/runs")
@login_required
def runs():
    res = gh("GET", f"/actions/workflows/{WORKFLOW}/runs?per_page=8")
    out = [{
        "id": r["id"], "status": r["status"], "conclusion": r["conclusion"], "event": r["event"],
        "created_at": r["created_at"], "updated_at": r["updated_at"], "url": r["html_url"],
    } for r in res.get("workflow_runs", [])]
    return jsonify(runs=out)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8787")), debug=False)
