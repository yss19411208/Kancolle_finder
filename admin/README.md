# 管理画面

ログイン付きの管理画面です。Ubuntu サーバーで動かします。

このサーバーは poi のダンプを取得・解析しません。GitHub 上の設定ファイルを書き換え、GitHub Actions の再集計を起動するだけです。

## できること

- アップデート日時の登録（未来の日時も可）
- 常駐リストの編集、Wiki の HTML との差分表示
- 限定ドロップの確認・除外・手動追加
- 更新履歴の編集
- 再集計の実行と状況確認

## 1. GitHub のトークンを作る

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token

- Repository access: **Only select repositories** → `Kancolle_finder`
- Permissions: **Contents: Read and write**、**Actions: Read and write**

## 2. サーバーに設置

```sh
sudo apt update && sudo apt install -y python3-venv git
sudo git clone https://github.com/yss19411208/Kancolle_finder.git /opt/Kancolle_finder
sudo chown -R "$USER": /opt/Kancolle_finder
cd /opt/Kancolle_finder/admin
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
nano .env                       # GITHUB_TOKEN= にトークンを貼る。ADMIN_USER も好みで変更
venv/bin/python set_password.py # ログイン用パスワードを設定
```

## 3. 常時起動 (systemd)

```sh
sed "s/CHANGE_ME/$USER/" kancolle-admin.service | sudo tee /etc/systemd/system/kancolle-admin.service
sudo systemctl daemon-reload
sudo systemctl enable --now kancolle-admin
curl -sI http://127.0.0.1:8787/login | head -1    # HTTP/1.1 200 OK が出れば起動している
```

アプリは `127.0.0.1:8787` だけで待ち受けます。外部には次の Cloudflare Tunnel 経由でのみ公開します（ポート開放は不要）。

## 4. Cloudflare Tunnel で公開 (dreamyard.cc)

```sh
curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared.deb
cloudflared tunnel login                       # ブラウザで dreamyard.cc を選ぶ
cloudflared tunnel create kancolle-admin
cloudflared tunnel route dns kancolle-admin kancolle-admin.dreamyard.cc
```

`~/.cloudflared/config.yml` を作成（`<UUID>` は create で表示された ID）:

```yaml
tunnel: <UUID>
credentials-file: /home/<ユーザー名>/.cloudflared/<UUID>.json
ingress:
  - hostname: kancolle-admin.dreamyard.cc
    service: http://127.0.0.1:8787
  - service: http_status:404
```

```sh
sudo cloudflared --config ~/.cloudflared/config.yml service install
sudo systemctl enable --now cloudflared
```

`https://kancolle-admin.dreamyard.cc/` を開くとログイン画面が出ます。

さらに守りたい場合は、Cloudflare Zero Trust → Access → Applications でこのホスト名に自分のメールアドレスだけを許可するポリシーを付けると、ログイン画面の前にもう一段の認証が入ります。

## 更新

```sh
cd /opt/Kancolle_finder && git pull && sudo systemctl restart kancolle-admin
```

## メモ

- パスワードを 5 回間違えると、その接続元は 10 分間ログインできません。
- アップデート日時・限定の除外・手動追加は、操作したその場で保存されます。常駐リストと更新履歴は「保存」ボタンで保存します。
- 保存すると GitHub Actions の再集計が自動で始まります。公開ページへの反映は数分後です。
- 別の端末で変更した内容は、右上の「最新に更新」で読み込めます（画面に戻ったときも自動で読み込みます）。
- `.env` はリポジトリに含めないでください（`.gitignore` 済み）。
