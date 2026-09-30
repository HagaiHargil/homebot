"""Markdown export: one `## ` section per recipe, with URL, tags, notes and a dated cook log.

Standalone: uv run export.py [OUT.md]   (default: $DATA_DIR/export/recipes.md)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import db

EMOJI = {"good": "👍", "ok": "👌", "bad": "👎"}


def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _quote(text: str) -> list[str]:
    return [f"> {line}".rstrip() for line in text.strip().splitlines()] or [">"]


def render(conn) -> tuple[str, int]:
    recipes = db.all_recipes(conn)
    out = ["# Recipes", "", f"_{len(recipes)} recipes · exported {db.utc_now()}_", ""]
    for r in recipes:
        title = _one_line(r["title"]).replace("#", r"\#") or "Untitled"
        out.append(f"## {title}")
        out.append("")
        meta = [f"- **Saved:** {r['added_at'][:10]} · {r['source'] or 'manual'} · id {r['id']}"]
        if r["url"]:
            meta.append(f"- **URL:** <{r['url']}>")
        tags = db.tags_for(conn, r["id"])
        if tags:
            meta.append("- **Tags:** " + ", ".join(tags))
        if r["archived"]:
            meta.append("- **Archived**")
        if r["image_path"]:
            meta.append(f"- **Image:** `{r['image_path']}`")
        out += meta + [""]

        if r["ingredients"]:
            try:
                items = json.loads(r["ingredients"])
            except ValueError:
                items = [r["ingredients"]]
            out.append("**Ingredients**")
            out.append("")
            out += [f"- {_one_line(str(i))}" for i in items]
            out.append("")

        if r["notes"]:
            out.append("**Notes**")
            out.append("")
            out += _quote(r["notes"])
            out.append("")

        cooks = list(reversed(db.cooks_for(conn, r["id"])))  # oldest first
        if cooks:
            out.append("**Cook log**")
            out.append("")
            for c in cooks:
                note = f" — {_one_line(c['notes'])}" if c["notes"] else ""
                out.append(f"- {c['cooked_at']} {EMOJI[c['verdict']]} {c['verdict']}{note}")
            out.append("")
    return "\n".join(out).rstrip() + "\n", len(recipes)


def export_markdown(conn, out_path: Path) -> tuple[Path, int]:
    text, count = render(conn)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out_path)
    return out_path, count


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else db.data_dir() / "export" / "recipes.md"
    with db.session() as conn:
        path, n = export_markdown(conn, target)
    print(f"wrote {n} recipes to {path}")
