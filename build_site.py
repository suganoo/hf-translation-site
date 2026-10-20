"""data/weekly/*.json から GitHub Pages 公開用の静的HTMLを site/ に生成する。

出力:
    site/index.html              最新週
    site/weeks/{week_start}.html 各週
    site/papers/{paper_id}.html  論文ごとの解説ページ
    site/static/                 CSS など
"""

import argparse
import hashlib
import json
import shutil
import sys
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).parent
MAX_AUTHORS = 3


def jp_date(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def range_label(start: date, end: date) -> str:
    if start.year != end.year:
        return f"{jp_date(start)} – {jp_date(end)}"
    return f"{jp_date(start)} – {end.month}月{end.day}日"


def short_authors(authors: list[str]) -> list[str]:
    if len(authors) <= MAX_AUTHORS + 1:  # 「他1名」になるなら全員表示
        return authors
    return authors[:MAX_AUTHORS] + [f"他{len(authors) - MAX_AUTHORS}名"]


def load_weeks(data_dir: Path) -> list[dict]:
    weeks = []
    for path in sorted((data_dir / "weekly").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for p in data["papers"]:
            p["all_authors"] = p["authors"]  # 解説ページでは全員を表示する
            p["authors"] = short_authors(p["authors"])
        start = date.fromisoformat(data["week_start"])
        end = date.fromisoformat(data["week_end"])
        weeks.append({
            **data,
            "start": start,
            "range_label": range_label(start, end),
            "confirmed_label": jp_date(date.fromisoformat(data["confirmed_at"])),
            "nav_label": f"{start.month}/{start.day} – {end.month}/{end.day}",
        })
    weeks.reverse()  # 新しい週が先頭
    return weeks


def nav_months(weeks: list[dict], current: dict, link_prefix: str) -> list[dict]:
    months = []
    for w in weeks:
        label = f"{w['start'].year}年{w['start'].month}月"
        if not months or months[-1]["label"] != label:
            months.append({"label": label, "weeks": []})
        months[-1]["weeks"].append({
            "label": w["nav_label"],
            "url": f"{link_prefix}{w['week_start']}.html",
            "is_current": w is current,
        })
    return months


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--out", type=Path, default=ROOT / "site")
    args = parser.parse_args()
    out_dir = args.out

    weeks = load_weeks(args.data_dir)
    if not weeks:
        print(f"No snapshots found in {args.data_dir / 'weekly'}", file=sys.stderr)
        return 1

    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
    template = env.get_template("week.html")
    paper_template = env.get_template("paper.html")
    # CSS 更新時にブラウザのキャッシュが残らないよう、内容のハッシュを URL に付ける
    env.globals["css_version"] = hashlib.sha256((ROOT / "static/css/style.css").read_bytes()).hexdigest()[:8]

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "weeks").mkdir(parents=True)
    (out_dir / "papers").mkdir()
    shutil.copytree(ROOT / "static", out_dir / "static")
    (out_dir / ".nojekyll").touch()

    # GitHub Pages はリポジトリ名のサブパスで公開されるため、リンクはすべて相対パスにする
    for w in weeks:
        html = template.render(week=w, nav_months=nav_months(weeks, w, ""), static_root="../static", paper_root="../papers")
        (out_dir / "weeks" / f"{w['week_start']}.html").write_text(html, encoding="utf-8")
        for rank, paper in enumerate(w["papers"], 1):
            html = paper_template.render(week=w, paper=paper, rank=rank, static_root="../static")
            (out_dir / "papers" / f"{paper['id']}.html").write_text(html, encoding="utf-8")

    latest = weeks[0]
    html = template.render(week=latest, nav_months=nav_months(weeks, latest, "weeks/"), static_root="static", paper_root="papers")
    (out_dir / "index.html").write_text(html, encoding="utf-8")

    print(f"Built {len(weeks)} week(s) into {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
