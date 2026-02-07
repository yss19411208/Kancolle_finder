# Kancolle_finder 艦これドロップ検索ツール ⚓

艦隊これくしょんの艦娘ドロップ海域を簡単に検索できるWebアプリケーションです。

![艦これドロップ検索](https://img.shields.io/badge/艦これ-ドロップ検索-blue)

## 特徴

- 🎨 **モダンなUI** - スマートで使いやすいデザイン
- ⚡ **クイック選択** - 艦種をワンクリックで選択
- 🔗 **攻略ページへのリンク** - 海域をクリックすると攻略ページが開きます
- 📱 **レスポンシブ対応** - スマホでも快適に使用可能
- 🚀 **高速動作** - 純粋なHTML/CSS/JavaScriptで動作

## デモ

GitHub Pagesでデモを公開中: [デモページを見る](#)

## 使い方

### オンライン版

1. GitHub Pagesのリンクにアクセス
2. 艦種をクイック選択ボタンから選択
3. 艦船名を選択
4. ドロップ海域が表示されます
5. 海域タグをクリックすると攻略ページが開きます

### ローカルで使用

```bash
# リポジトリをクローン
git clone https://github.com/YOUR_USERNAME/kancolle-drop-search.git

# ディレクトリに移動
cd kancolle-drop-search

# ローカルサーバーを起動（推奨）
python -m http.server 8000

# ブラウザで http://localhost:8000 を開く
```

または、`艦これ検索_final.html`をブラウザで直接開いてください。

## ファイル構成

```
.
├── 艦これ検索_final.html    # メインHTMLファイル
├── index.json                # 艦娘ドロップデータ
└── README.md                 # このファイル
```

## データ形式

`index.json`は以下の形式で艦娘のドロップデータを格納しています：

```json
[
  {
    "榛名": ["2-1", "2-3", "2-4", ...],
    "霧島": ["2-1", "2-2", "2-3", ...]
  },
  ...
]
```

配列のインデックス:
- 0: 戦艦
- 1: 空母
- 2: 重巡洋艦
- 3: 軽巡洋艦
- 4: 駆逐艦
- 5: 海防艦
- 6: 潜水艦
- 7: 補助艦艇

## カスタマイズ

### データの更新

`index.json`を編集することで、ドロップデータを更新できます。

### スタイルの変更

HTMLファイル内の`<style>`タグ内でCSSをカスタマイズできます。

## 技術スタック

- HTML5
- CSS3 (グラデーション、アニメーション)
- Vanilla JavaScript (外部ライブラリ不使用)

## ライセンス

MIT License

## 貢献

プルリクエストを歓迎します！

1. このリポジトリをフォーク
2. 新しいブランチを作成 (`git checkout -b feature/amazing-feature`)
3. 変更をコミット (`git commit -m 'Add some amazing feature'`)
4. ブランチにプッシュ (`git push origin feature/amazing-feature`)
5. プルリクエストを作成

## クレジット

- 海域攻略情報: [ぜかましねっと航海日誌](https://zekamashi.net/)
- データ提供: 艦これ攻略Wiki

## 注意事項

このツールは非公式のファンメイドツールです。
DMM GAMES / C2 Preparat / KADOKAWAとは一切関係ありません。

---

⚓ Happy Hunting, Admiral!
