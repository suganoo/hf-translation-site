# hf-translation-site 要件定義書 (Draft v0.2)

最終更新: 2026-09-20
ステータス: ドラフト(議論・更新中)

## 1. 概要

Hugging Face Papers のいいね数上位論文を週次(固定週)で集計し、日本語要約と見どころコメントを付けて表示する静的サイト。インフラコストゼロで運用する。

## 2. 集計ルール

- **週の区切り**: 固定週(月曜始まり〜日曜終わり)
- **確定タイミング**: 対象週の翌々週の月曜(=週末から7日後)に確定させる
  - 理由: 週の最終日に発表された論文はいいねを集める時間が短く、週初めの論文と不公平な比較になる。全論文が発表から均等に7日間のいいね蓄積期間を持てるようにするため、測定を1週間遅らせる
  - トレードオフ: 常に2週間遅れの表示になる(速報性より公平な比較を優先する判断)
- **実行トリガー**: GitHub Actions cron、毎週月曜 00:00 UTC(+ 手動実行用の `workflow_dispatch`)
- **対象週の算出方法**: 今週月曜から2週間前の月曜〜日曜
- **冪等性**: 該当週のスナップショットが既に存在する場合はスキップ

## 3. データ取得

- **API**: `https://huggingface.co/api/daily_papers?date=YYYY-MM-DD`(非公式・無認証、title/authors/upvotes/summary(英語アブストラクト)/publishedAt を返す)
- 対象週の7日分を日毎に取得
- **重複排除**: upvotes は累積値であり、同一論文が複数日にまたがって出現する。`paper_id` をキーに、最大値の upvotes を採用する(単純合算すると水増しになるため厳禁)
- 集計後、upvotes 降順で上位10件を抽出

## 4. LLM生成(3種類、別プロンプトで生成)

- 使用LLM: Claude API(従量課金。インフラはゼロコストだがLLM APIは別途コストが発生し得る)

### highlight_ja(見どころコメント、1〜2文・60〜100字)

「ここがすごい」を凝縮して1〜2文で。1文目に具体的な新規性、2文目にその意義(何が変わるか・誰に効くか)。誇張表現(「画期的」「革新的」等の空虚な形容詞)を避け、具体的新規性(数値・比較対象・覆した通説など)を書かせる。

- 良い例: 「7Bモデルで70B級の数学推論性能を達成した。大規模モデルを動かせない環境でも高度な推論が使えるようになる。」
- 悪い例をプロンプトに明示すること(「画期的な研究成果」のような具体性のない表現を避けさせるため)

### summary_ja(内容要約、3〜4文・350字程度)

手法・実験・結果を含む要約。

### explainer(論文解説ページ用データ、JSONスキーマ指定の構造化出力)

AIの基礎知識がまだ浅い読者(情報系の大学1〜2年生程度)向けの解説ページの素材。

- **diagram(概要図)**: 課題(problem)→ アプローチ(approach)→ 成果(outcome)の3つの箱。各箱は見出し(head、20字以内)と補足(body、60〜80字)。加えて従来(before)とこの論文(after)の対比を各40字以内で
- **concepts(基礎知識)**: 要約を読むうえでつまずきそうな一般的な前提概念を2〜3個。用語名(term)、英語表記(term_en)、一般的な説明(body、200〜280字)、たとえ話(analogy)、この論文での使われ方(in_paper、80字以内)
- 概要図の箱や矢印はテンプレート側で描画し、LLM には文言だけを出力させる(LLM に SVG 等を生成させない)
- 用語の逆引き(「この用語が出てくる論文」一覧)は作らない

### キャッシュ

論文IDをキーに要約をキャッシュし、既出論文は再生成しない(新規トップ10入りした論文のみLLM呼び出しを発生させる)。

## 5. データスキーマ

```json
{
  "week_start": "2026-08-03",
  "week_end": "2026-08-09",
  "confirmed_at": "2026-08-24",
  "papers": [
    {
      "id": "2509.xxxxx",
      "title": "Scaling laws for multilingual reasoning in small language models",
      "authors": ["Chen, ...", "Okafor, ...", "Lindqvist, ..."],
      "url": "https://huggingface.co/papers/2509.xxxxx",
      "upvotes": 342,
      "highlight_ja": "既存のスケーリング則の常識を覆す結果",
      "summary_ja": "小規模言語モデルでも多言語間の推論能力が...",
      "explainer": {
        "diagram": {
          "problem": {"head": "...", "body": "..."},
          "approach": {"head": "...", "body": "..."},
          "outcome": {"head": "...", "body": "..."},
          "before": "...",
          "after": "..."
        },
        "concepts": [
          {"term": "...", "term_en": "...", "body": "...", "analogy": "...", "in_paper": "..."}
        ]
      }
    }
  ]
}
```

- **保存先**: `data/weekly/{week_start}.json`(1週間1ファイル、リポジトリにコミット)
- **要約キャッシュ**: `data/summary_cache.json`(論文ID → highlight_ja / summary_ja / explainer)
- **既存週への explainer 追加**: `python generate_snapshot.py --backfill-explainers`(ワークフローの手動実行では `backfill_explainers` にチェック)

## 6. バッチ処理ロジック(疑似コード)

```python
from datetime import date, timedelta

def get_confirmable_week_start(today: date) -> date:
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(weeks=2)

def generate_weekly_snapshot():
    today = date.today()
    week_start = get_confirmable_week_start(today)

    if snapshot_exists(week_start):
        return  # 冪等性の担保

    papers = {}
    for i in range(7):
        target_date = week_start + timedelta(days=i)
        resp = fetch(f"https://huggingface.co/api/daily_papers?date={target_date}")
        for item in resp:
            pid = item["paper"]["id"]
            upvotes = item["paper"]["upvotes"]
            if pid not in papers or upvotes > papers[pid]["upvotes"]:
                papers[pid] = {
                    "id": pid,
                    "title": item["paper"]["title"],
                    "summary_en": item["paper"]["summary"],
                    "upvotes": upvotes,
                    "authors": [a["name"] for a in item["paper"]["authors"]],
                    "url": f"https://huggingface.co/papers/{pid}",
                }

    top10 = sorted(papers.values(), key=lambda x: x["upvotes"], reverse=True)[:10]

    cache = load_summary_cache()
    for p in top10:
        if p["id"] not in cache:
            cache[p["id"]] = generate_highlight_and_summary(p["title"], p["summary_en"])
        p["highlight_ja"] = cache[p["id"]]["highlight_ja"]
        p["summary_ja"] = cache[p["id"]]["summary_ja"]

    save_summary_cache(cache)
    save_snapshot(week_start, top10)
```

## 7. インフラ

- **実行環境**: GitHub Actions(cron、毎週月曜0:00 UTC + `workflow_dispatch`)
- **ホスティング**: GitHub Pages(静的サイト)
- **APIキー管理**: GitHub Secrets 経由で環境変数として渡す(`ANTHROPIC_API_KEY` をコードに直接埋め込まない)
- **DBサーバー不要**: JSONファイル + Gitコミットでデータストアを兼ねる

```yaml
# .github/workflows/weekly-snapshot.yml
name: Weekly Paper Snapshot
on:
  schedule:
    - cron: '0 0 * * 1'
  workflow_dispatch:

jobs:
  generate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r requirements.txt
      - run: python generate_snapshot.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      - name: Commit and push
        run: |
          git config user.name "github-actions"
          git config user.email "actions@github.com"
          git add data/
          git commit -m "Add weekly snapshot $(date +%F)" || echo "No changes"
          git push
```

## 8. デザイン方針

- **トーン**: 学術誌・新聞縮刷版寄りの落ち着いたトーン。ダッシュボード調(カード羅列、派手な色使い)は避ける
- **レイアウト**: 左に週ナビゲーション(年月別リスト)、右に選択した週のトップ10を縦一列表示
- 角丸カードは使わず、罫線一本(hairline border)で論文ごとに区切る
- **タイポグラフィ**: タイトルはセリフ体(`var(--font-voice)` 相当)、本文・メタ情報はサンセリフ体
- **各論文ブロックの構成順(上から下へ)**:
  1. 順位番号(セリフ体、大きめ、色は控えめ)
  2. 論文タイトル
  3. 見どころコメント(highlight_ja) — 電球アイコン + アクセントカラーで要約と視覚的に区別
  4. 著者名・upvotes数(小さめ、ミュートカラー)
  5. 日本語要約(summary_ja)
- **論文タイトルのリンク先**: 論文ごとの解説ページ `papers/{id}.html`(新しいタブで開く)。HF Papers・arXiv へのリンクは解説ページ側に置く
- **解説ページの構成順(上から下へ)**: 週ページへの戻りリンク → 順位・タイトル・著者・upvotes・HF/arXiv リンク・見どころコメント → 概要図(課題 → アプローチ → 成果、従来との対比)→ 基礎知識2〜3個(一般的な説明・たとえ話・この論文では)→ 日本語要約
- **透明性の担保**: 各週ページに「確定日」を明記し、2週間遅延の設計であることを利用者に示す
- **カラー**: オフホワイト系ベース(`#FAFAF8`)、アクセントは落ち着いた青緑系(`#3B5A6B`寄り)。upvotes数値は煽らない温かみのある色(茶系)を使用

## 9. インフラ・ホスティング決定事項

- ホスティング: GitHub Pages
- データ保存: リポジトリ内 JSON ファイルとして commit(外部DB不要)
- 要約生成 LLM: Claude API
- **フロントエンド**: Python + Jinja2 によるビルド時静的HTML/CSS生成
  - 理由: データ集計バッチ(`generate_snapshot.py`)と同じPythonで完結でき、Node.js/React等の新規ツールチェーンを学ぶ必要がない。サイトの挙動は「週を選んで一覧を見る」程度で複雑なインタラクションが不要なため、素のHTML/CSSで十分
  - 出力: `data/weekly/*.json` を読み込み、GitHub Actions内でビルドしてGitHub Pages公開用の静的HTMLを生成する
- **キャッシュ再利用方針**: 論文がトップ10から陥落後、再びトップ10入りした場合は `data/summary_cache.json` のキャッシュ済み要約(highlight_ja / summary_ja)をそのまま再利用する(論文内容自体は変わらないため再生成は不要)

## 10. 未確定・要検討事項

- 週ナビゲーションUI(年月リストの折りたたみ方など)の詳細実装
