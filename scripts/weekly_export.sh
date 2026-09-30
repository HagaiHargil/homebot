#!/usr/bin/env bash
# Weekly Markdown export, committed to a local git repo at $DATA_DIR/export.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
DATA_DIR="${DATA_DIR:-$PWD/data}"
OUT="$DATA_DIR/export"
mkdir -p "$OUT"
.venv/bin/python export.py "$OUT/recipes.md"
cd "$OUT"
[ -d .git ] || git init -q
git add -A
git diff --cached --quiet || git -c user.name=recipebot -c user.email=recipebot@localhost \
  commit -q -m "export $(date +%F)"
