"""Hugging Face Papers の週次トップ10スナップショットを生成する。

対象週(今週月曜の2週間前の月曜〜日曜)の daily_papers を取得し、
upvotes 上位10件に日本語の見どころコメント・要約を付けて
data/weekly/{week_start}.json に保存する。

使い方:
    python generate_snapshot.py                    # 通常実行(確定可能な週を自動算出)
    python generate_snapshot.py --week 2026-08-03  # 指定週を生成(過去分の埋め戻し用)
    python generate_snapshot.py --skip-llm         # LLM を呼ばずに動作確認(要約はダミー、キャッシュ保存なし)
    python generate_snapshot.py --backfill-explainers  # 既存の全週に解説ページ用データ(概要図・基礎知識)を追加
"""

import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

HF_API = "https://huggingface.co/api/daily_papers?date={}"
HF_PAPER_API = "https://huggingface.co/api/papers/{}"
MODEL = "claude-sonnet-5-5"
TOP_N = 10

HIGHLIGHT_PROMPT = """\
以下の論文について、「ここがすごい」を凝縮した日本語の見どころコメントを書いてください。

条件:
- 1〜2文、合計60〜100字。100字を超えないこと。だ・である調
- 1文に数値や手法名を詰め込みすぎない。数値は最も印象的なものを1つに絞る
- 1文目で具体的な新規性を書く: 数値、比較対象、覆した通説、新しくできるようになったこと など
- 2文目を書く場合は、それがなぜ重要か(何が変わるか・誰に効くか)を書く。要約の繰り返しにはしない
- 「画期的」「革新的」「驚異的」「大幅に向上」のような、具体性のない誇張表現は使わない
- アブストラクトに書かれている事実の範囲で書く

良い例: 7Bモデルで70B級の数学推論性能を達成した。大規模モデルを動かせない環境でも高度な推論が使えるようになる。
良い例: 学習データを増やすほど性能が伸びるというスケーリング則の前提が、この設定では成り立たないことを示した。
悪い例: 画期的な研究成果であり、今後の発展が期待される。(何がすごいのか分からない)
悪い例: AIの性能を大幅に向上させる革新的手法を提案した。(空虚な形容詞だけ)

見どころコメントの本文だけを出力してください(前置き・引用符・説明・字数などの注記は不要)。

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


EXPLAINER_PROMPT = """\
以下の論文について、AIの基礎知識がまだ浅い読者(情報系の大学1〜2年生程度)向けの解説ページの素材を作ってください。
出力は指定のJSONスキーマに従ってください。

## 1. 概要図 (diagram)
「課題 → アプローチ → 成果」の3つの箱と、従来との対比1行で構成する図の文言。
- problem: 従来の何が困っていたか / approach: この論文がどう解いたか / outcome: 何ができるようになったか・何が分かったか
- 各箱の head は20字以内の見出し、body は60〜80字の補足
- before / after は「従来」と「この論文」の違いを対比で書く。各40字以内
- アブストラクトに書かれている事実の範囲で書く。数値を入れるなら図全体で1つまで

## 2. 基礎知識 (concepts) をちょうど2〜3個
- この論文の要約を読むうえで読者が最もつまずきそうな前提概念を選び、基礎的なものから順に並べる
- 論文固有の手法名は選ばず、他の論文でも出てくる一般的な概念を選ぶ。「機械学習」「ニューラルネットワーク」のように易しすぎる概念も避ける
- term: 日本語の用語名 / term_en: 英語表記
- body: 一般的な説明を200〜280字で。何のためのものか、どう動くか、何が難しいかを書く
- analogy: 身近なたとえを1〜2文、60字程度で
- in_paper: この論文ではその概念がどう使われる・関係するかを1〜2文、80字以内で
- body と analogy は論文に依存しない一般的な知識として書き、in_paper はアブストラクトの事実の範囲で書く

## 文体
- だ・である調。専門用語は初出で簡単に言い換える
- 「画期的」「革新的」のような誇張表現は使わない

基礎知識の良い例:
term: エージェントハーネス / term_en: Agent Harness
body: 大規模言語モデル(LLM)そのものは、文章を受け取って文章を返すことしかできない。ファイルを読む、Web を検索する、コードを実行して結果を確かめる、うまくいくまで繰り返す、といった「仕事をこなす動き」は、モデルの外側にあるプログラムが担っている。この外側の制御プログラムをハーネスと呼ぶ。同じモデルでも、ハーネスの出来によってタスクの成功率は大きく変わる。
analogy: たとえるなら、モデルは馬で、ハーネスは馬具と手綱。力の強い馬でも、馬具が合っていなければ荷車はうまく引けない。
in_paper: ハーネスを人が設計するのをやめ、モデルと分野に合わせて自動で作り、使いながら改善していく対象として扱っている。

タイトル: {title}

アブストラクト:
{abstract}
"""

_BOX = {
    "type": "object",
    "properties": {"head": {"type": "string"}, "body": {"type": "string"}},
    "required": ["head", "body"],
    "additionalProperties": False,
}
EXPLAINER_SCHEMA = {
    "type": "object",
    "properties": {
        "diagram": {
            "type": "object",
            "properties": {
                "problem": _BOX,
                "approach": _BOX,
                "outcome": _BOX,
                "before": {"type": "string"},
                "after": {"type": "string"},
            },
            "required": ["problem", "approach", "outcome", "before", "after"],
            "additionalProperties": False,
        },
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "term_en": {"type": "string"},
                    "body": {"type": "string"},
                    "analogy": {"type": "string"},
                    "in_paper": {"type": "string"},
                },
                "required": ["term", "term_en", "body", "analogy", "in_paper"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["diagram", "concepts"],
    "additionalProperties": False,
}

DUMMY_EXPLAINER = {
    "diagram": {
        "problem": {"head": "(課題 未生成)", "body": "--skip-llm で実行"},
        "approach": {"head": "(アプローチ 未生成)", "body": "--skip-llm で実行"},
        "outcome": {"head": "(成果 未生成)", "body": "--skip-llm で実行"},
        "before": "(従来 未生成)",
        "after": "(この論文 未生成)",
    },
    "concepts": [
        {"term": "(用語 未生成)", "term_en": "Dummy", "body": "(説明 未生成)", "analogy": "(たとえ 未生成)", "in_paper": "(未生成)"},
    ],
}


PROMPT_HASHES = {
    name: hashlib.sha256(prompt.encode()).hexdigest()[:8]
    for name, prompt in [("highlight", HIGHLIGHT_PROMPT), ("summary", SUMMARY_PROMPT), ("explainer", EXPLAINER_PROMPT)]
}

_langfuse = None


def make_client():
    """Claude API クライアントを作る。LANGFUSE_* が設定されていれば呼び出しを Langfuse に送る。"""
    global _langfuse
    import anthropic
    if os.environ.get("LANGFUSE_PUBLIC_KEY"):
        from langfuse import get_client
        # OTel の TracerProvider を登録するので、Anthropic クライアントより先に作る
        _langfuse = get_client()
    # SDK が出す OpenTelemetry スパンにプロンプトと応答の本文も含める(Langfuse 未設定時は何も送られない)
    return anthropic.Anthropic(open_telemetry={"traces": {"content_mode": "content"}})  # ANTHROPIC_API_KEY を環境変数から読む


def trace_week(week: str, mode: str):
    """この中で作られるトレースを週ごとの Langfuse セッションにまとめる。"""
    if _langfuse is None:
        return contextlib.nullcontext()
    from langfuse import propagate_attributes
    return propagate_attributes(session_id=f"week-{week}", tags=[mode], metadata={"model": MODEL})


def trace_span(name: str, **kwargs):
    if _langfuse is None:
        return contextlib.nullcontext()
    return _langfuse.start_as_current_observation(as_type="span", name=name, **kwargs)


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


def ask_claude(client, prompt: str, schema: dict | None = None) -> str:
    output_config = {"effort": "medium"}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config=output_config,
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
    with trace_span("highlight", metadata={"prompt_hash": PROMPT_HASHES["highlight"]}):
        highlight = ask_claude(client, HIGHLIGHT_PROMPT.format(title=title, abstract=abstract))
    with trace_span("summary", metadata={"prompt_hash": PROMPT_HASHES["summary"]}):
        summary = ask_claude(client, SUMMARY_PROMPT.format(title=title, abstract=abstract))
    return {"highlight_ja": highlight, "summary_ja": summary}


def generate_explainer(client, title: str, abstract: str) -> dict:
    with trace_span("explainer", metadata={"prompt_hash": PROMPT_HASHES["explainer"]}):
        text = ask_claude(client, EXPLAINER_PROMPT.format(title=title, abstract=abstract), EXPLAINER_SCHEMA)
    explainer = json.loads(text)
    explainer["concepts"] = explainer["concepts"][:3]
    return explainer


def fetch_abstract(paper_id: str) -> str:
    return fetch_json(HF_PAPER_API.format(paper_id)).get("summary", "")


def backfill_explainers(client, data_dir: Path, cache: dict, cache_path: Path) -> None:
    """既存の週次スナップショットのうち explainer が無い論文に解説データを追加する。"""
    for snapshot_path in sorted((data_dir / "weekly").glob("*.json")):
        snapshot = load_json(snapshot_path, None)
        changed = False
        for p in snapshot["papers"]:
            if "explainer" in p:
                continue
            entry = cache.setdefault(p["id"], {})
            if "explainer" not in entry:
                print(f"  [{snapshot_path.stem}] generating explainer: {p['title'][:60]}")
                with trace_week(snapshot_path.stem, "backfill-explainers"), \
                        trace_span(f"paper {p['id']}", input={"title": p["title"]}):
                    entry["explainer"] = generate_explainer(client, p["title"], fetch_abstract(p["id"]))
                save_json(cache_path, cache)
            p["explainer"] = entry["explainer"]
            changed = True
        if changed:
            save_json(snapshot_path, snapshot)
            print(f"Updated {snapshot_path}")


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
    parser.add_argument("--backfill-explainers", action="store_true",
                        help="既存の全週に解説ページ用データ(概要図・基礎知識)を追加する")
    args = parser.parse_args()

    if args.backfill_explainers:
        cache_path = args.data_dir / "summary_cache.json"
        backfill_explainers(make_client(), args.data_dir, load_json(cache_path, {}), cache_path)
        return 0

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
    client = None if args.skip_llm else make_client()

    for rank, p in enumerate(top, 1):
        if args.skip_llm:
            cache_entry = cache.get(p["id"]) or {"highlight_ja": "(見どころ未生成)", "summary_ja": "(要約未生成: --skip-llm で実行)"}
            cache_entry = {"explainer": DUMMY_EXPLAINER, **cache_entry}
        else:
            cache_entry = cache.setdefault(p["id"], {})
            # キャッシュ済みで LLM を呼ばない論文は空のトレースを作らない
            needs_llm = "summary_ja" not in cache_entry or "explainer" not in cache_entry
            paper_span = (trace_span(f"paper {p['id']}", input={"title": p["title"], "rank": rank})
                          if needs_llm else contextlib.nullcontext())
            with trace_week(week_start.isoformat(), "weekly"), paper_span:
                if "summary_ja" not in cache_entry:
                    print(f"  [{rank}] generating: {p['title'][:60]}")
                    cache_entry.update(generate_highlight_and_summary(client, p["title"], p["summary_en"]))
                    save_json(cache_path, cache)  # 途中で失敗しても生成済み分は残す
                if "explainer" not in cache_entry:
                    print(f"  [{rank}] generating explainer: {p['title'][:60]}")
                    cache_entry["explainer"] = generate_explainer(client, p["title"], p["summary_en"])
                    save_json(cache_path, cache)
        p["highlight_ja"] = cache_entry["highlight_ja"]
        p["summary_ja"] = cache_entry["summary_ja"]
        p["explainer"] = cache_entry["explainer"]
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
