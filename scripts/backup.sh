#!/usr/bin/env bash
# Nightly online backup of the SQLite DB; keeps 14 days.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
DATA_DIR="${DATA_DIR:-$PWD/data}"
mkdir -p "$DATA_DIR/backup"
sqlite3 "$DATA_DIR/recipes.db" ".backup '$DATA_DIR/backup/recipes-$(date +%F).db'"
find "$DATA_DIR/backup" -name 'recipes-*.db' -mtime +13 -delete
