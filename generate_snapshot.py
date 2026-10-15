"""Hugging Face Papers の週次トップ10スナップショットを生成する。

対象週(今週月曜の2週間前の月曜〜日曜)の daily_papers を取得し、
upvotes 上位10件に日本語の見どころコメント・要約を付けて
data/weekly/{week_start}.json に保存する。

使い方:
    python generate_snapshot.py                    # 通常実行(確定可能な週を自動算出)
    python generate_snapshot.py --week 2026-08-03  # 指定週を生成(過去分の埋め戻し用)
    python generate_snapshot.py --skip-llm         # LLM を呼ばずに動作確認(要約はダミー、キャッシュ保存なし)
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

HF_API = "https://huggingface.co/api/daily_papers?date={}"
MODEL = "claude-sonnet-5-5"
TOP_N = 10

HIGHLIGHT_PROMPT = """\
以下の論文について、「何がすごいか」を一言で表す日本語の見どころコメントを書いてください。

条件:
- 15〜25字の1文。句点(。)は付けない
- 具体的な新規性を書く: 数値、比較対象、覆した通説、新しくできるようになったこと など
- 「画期的」「革新的」「驚異的」「大幅に向上」のような、具体性のない誇張表現は使わない

良い例: 既存のスケーリング則の常識を覆す結果
良い例: 7Bモデルで70B級の数学推論性能を達成
悪い例: 画期的な研究成果(何がすごいのか分からない)
悪い例: AIの性能を大幅に向上させる革新的手法(空虚な形容詞だけ)

見どころコメントの本文だけを出力してください(前置き・引用符・説明は不要)。

タイトル: {title}

アブストラクト:
{abstract}
"""

SUMMARY_PROMPT = """\
以下の論文の内容を、日本語で3〜4文・350字程度に要約してください。

条件:
- 手法(何をしたか)・実験(何で検証したか)・結果(何が分かったか)を含める。実験は対象データやベンチマーク名を挙げる程度に簡潔にする
- 背景説明や前置きは省き、1文に情報を詰め込みすぎて読みにくくならないようにする
- 研究者でない技術者にも伝わる平易な文体(です・ます調ではなく、だ・である調)
- 専門用語は必要なら英語表記を括弧で併記してよい
- 誇張表現は避け、アブストラクトに書かれている事実だけを書く

要約の本文だけを出力してください(前置き・見出し・箇条書きは不要)。

タイトル: {title}

アブストラクト:
{abstract}
"""


def get_confirmable_week_start(today: date) -> date:
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(weeks=2)


def fetch_json(url: str, retries: int = 3):
    req = urllib.request.Request(url, headers={"User-Agent": "hf-translation-site/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt + 1}/{retries - 1}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))


def collect_top_papers(week_start: date) -> list[dict]:
    papers = {}
    for i in range(7):
        target_date = week_start + timedelta(days=i)
        items = fetch_json(HF_API.format(target_date.isoformat()))
        print(f"  {target_date}: {len(items)} papers")
        for item in items:
            p = item["paper"]
            pid = p["id"]
            upvotes = p.get("upvotes", 0)
            # upvotes は累積値。同一論文が複数日に出現するので最大値を採用する(合算は厳禁)
            if pid not in papers or upvotes > papers[pid]["upvotes"]:
                papers[pid] = {
                    "id": pid,
                    "title": p["title"],
                    "summary_en": p.get("summary", ""),
                    "upvotes": upvotes,
                    "authors": [a["name"] for a in p.get("authors", [])],
                    "url": f"https://huggingface.co/papers/{pid}",
                }
    return sorted(papers.values(), key=lambda x: x["upvotes"], reverse=True)[:TOP_N]


def ask_claude(client, prompt: str) -> str:
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        # 安全性分類器で拒否された場合はサーバー側で自動的に別モデルへフォールバックする
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request: {response.stop_details}")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"Empty response (stop_reason={response.stop_reason})")
    return text


def generate_highlight_and_summary(client, title: str, abstract: str) -> dict:
    return {
        "highlight_ja": ask_claude(client, HIGHLIGHT_PROMPT.format(title=title, abstract=abstract)),
        "summary_ja": ask_claude(client, SUMMARY_PROMPT.format(title=title, abstract=abstract)),
    }


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--week", type=date.fromisoformat, help="対象週の月曜日 (YYYY-MM-DD)")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--skip-llm", action="store_true", help="LLM を呼ばずダミー文を入れる(動作確認用)")
    args = parser.parse_args()

    today = date.today()
    week_start = args.week or get_confirmable_week_start(today)
    if week_start.weekday() != 0:
        parser.error(f"--week は月曜日を指定してください: {week_start}")
    if week_start + timedelta(weeks=2) > today:
        parser.error(f"{week_start} の週はまだ確定できません(確定日: {week_start + timedelta(weeks=2)})")

    snapshot_path = args.data_dir / "weekly" / f"{week_start.isoformat()}.json"
    cache_path = args.data_dir / "summary_cache.json"

    if snapshot_path.exists():
        print(f"Snapshot already exists, skipping: {snapshot_path}")
        return 0

    print(f"Collecting papers for week of {week_start}")
    top = collect_top_papers(week_start)
    if not top:
        print("No papers found for this week; nothing written.", file=sys.stderr)
        return 1

    cache = load_json(cache_path, {})
    client = None
    if not args.skip_llm:
        import anthropic
        client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を環境変数から読む

    for rank, p in enumerate(top, 1):
        if p["id"] not in cache:
            if args.skip_llm:
                cache_entry = {"highlight_ja": "(見どころ未生成)", "summary_ja": "(要約未生成: --skip-llm で実行)"}
            else:
                print(f"  [{rank}] generating: {p['title'][:60]}")
                cache_entry = generate_highlight_and_summary(client, p["title"], p["summary_en"])
                cache[p["id"]] = cache_entry
                save_json(cache_path, cache)  # 途中で失敗しても生成済み分は残す
        else:
            cache_entry = cache[p["id"]]
            print(f"  [{rank}] cached: {p['title'][:60]}")
        p["highlight_ja"] = cache_entry["highlight_ja"]
        p["summary_ja"] = cache_entry["summary_ja"]
        del p["summary_en"]

    save_json(snapshot_path, {
        "week_start": week_start.isoformat(),
        "week_end": (week_start + timedelta(days=6)).isoformat(),
        "confirmed_at": today.isoformat(),
        "papers": top,
    })
    print(f"Wrote {snapshot_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
