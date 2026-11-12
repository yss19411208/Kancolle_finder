#!/usr/bin/env python3
"""管理画面のパスワードを設定する。 使い方: venv/bin/python set_password.py"""
import getpass
import os
import re
import secrets

from werkzeug.security import generate_password_hash

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
pw = getpass.getpass("新しいパスワード (12文字以上): ")
if len(pw) < 12 or pw != getpass.getpass("もう一度: "):
    raise SystemExit("12文字未満、または一致しません")
text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def put(text, key, value):
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        return re.sub(rf"^{key}=.*$", lambda _: line, text, flags=re.M)
    return text.rstrip("\n") + ("\n" if text else "") + line + "\n"


text = put(text, "ADMIN_PASSWORD_HASH", generate_password_hash(pw))
if not re.search(r"^SECRET_KEY=.+$", text, flags=re.M):
    text = put(text, "SECRET_KEY", secrets.token_hex(32))
with open(path, "w", encoding="utf-8") as f:
    f.write(text)
os.chmod(path, 0o600)
print("保存しました:", path)
