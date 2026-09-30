# Hugging Face Papers 週次ダイジェスト

Hugging Face Papers のいいね数トップ10を週ごとに集計し、日本語の見どころコメントと要約を付けて公開する静的サイト。
仕様の詳細は [docs/requirements.md](docs/requirements.md) を参照。

## 構成

| ファイル | 役割 |
|---|---|
| `generate_snapshot.py` | HF API から対象週の論文を取得し、Claude API で日本語化して `data/weekly/{週}.json` に保存 |
| `build_site.py` | `data/weekly/*.json` から `site/` に静的HTMLを生成 |
| `templates/week.html`, `static/css/style.css` | ページのテンプレートとデザイン |
| `.github/workflows/weekly-snapshot.yml` | 毎週月曜に上記2つを実行し GitHub Pages に公開 |

## ローカルで動かす

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
python generate_snapshot.py                    # 確定可能な最新週を生成
python generate_snapshot.py --week 2026-09-07  # 過去の週を埋め戻す(月曜日を指定)
python build_site.py                           # site/ にHTMLを出力
python -m http.server -d site 8000             # http://localhost:8000 で確認
```

API キーなしで動作確認だけしたい場合は `--skip-llm --data-dir /tmp/test` を付けると、要約をダミー文にして別ディレクトリに出力する。

## GitHub での公開手順

1. GitHub にリポジトリを作成して push する
2. Settings → Secrets and variables → Actions で `ANTHROPIC_API_KEY` を登録
3. Settings → Pages → Source を **GitHub Actions** に設定
4. Actions タブから「Weekly Paper Snapshot」を手動実行(Run workflow)して初回公開

以降は毎週月曜 00:00 UTC(日本時間 9:00)に自動更新される。テンプレートやCSSを変更して main に push すると、データ生成なしでサイトだけ再ビルドされる。
