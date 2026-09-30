# Recipe Log

Single-user Telegram bot: share a recipe link → it's saved; tap 👍 👌 👎 when you cook it.
SQLite + FTS5, browsed read-only through Datasette over Tailscale.

| file | role |
|---|---|
| `schema.sql` | tables, FTS5 index + sync triggers |
| `db.py` | all SQL (`uv run python -m db --selftest`) |
| `ingest.py` | URL → saved recipe (`uv run ingest.py URL…` = dry-run parse) |
| `bot.py` | handlers, nudge jobs, entrypoint |
| `export.py` | Markdown export (`uv run export.py [out.md]`) |
| `metadata.yml` | Datasette canned queries |
| `deploy/` | systemd **user** units + timers |
| `scripts/` | `backup.sh` (nightly), `weekly_export.sh` (export + git commit) |

## Setup

```bash
uv sync
cp .env.example .env    # fill in TELEGRAM_TOKEN (@BotFather) and ALLOWED_USER_ID (@userinfobot)
uv run bot.py           # try it in the foreground; send /start from your phone
```

### Run as services (no root needed)

```bash
mkdir -p ~/.config/systemd/user
ln -sf "$PWD"/deploy/recipebot*.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now recipebot recipebot-datasette recipebot-backup.timer recipebot-export.timer
loginctl enable-linger "$USER"      # keep user services running while logged out
journalctl --user -u recipebot -f   # logs
```

### Browse from the iPhone

```bash
tailscale serve --bg 8001           # may first need: sudo tailscale set --operator=$USER
```
Then open `https://<this-machine>.<tailnet>.ts.net/` with Tailscale on.

## Callback data

| data | meaning |
|---|---|
| `c:<rid>:good\|ok\|bad` | log a cook, dated today |
| `cn:<cook_id>` | prompt for notes (text or a photo of the dish) |
| `pick:<rid>` | chosen from `/cooked` or the nudge → message becomes the card |
| `v:<rid>` | view: send the card for a search/list result |
| `t:<rid>` | prompt for tags |
| `title:<rid>` / `ts:<rid>` | prompt for a title / skip it |
| `x:<rid>` → `xy:<rid>` / `xc:<rid>` | delete → confirm / cancel |
| `new` | save the plain text just searched as a manual recipe |
| `more` / `no` | nudge: open the `/cooked` picker / dismiss |

## Deviations from the spec (and why)

- **Hebrew niqqud:** `unicode61 remove_diacritics 2` does *not* strip Hebrew points (only
  Latin-style combining marks; verified). The FTS triggers call `fold()`, a Python function
  registered by `db.connect()` that strips all combining marks, and queries are folded the same
  way. So writes to `recipes` must go through `db.connect()`; a raw `sqlite3` CLI `UPDATE recipes`
  fails with `no such function: fold` rather than silently corrupting the index. After such
  maintenance, `db.reindex_fts(conn)` rebuilds the index.
- **cron → systemd user timers:** this machine has no cron. `Persistent=true` catches up
  after sleep.
- **Nudge catch-up:** besides `run_once` at startup, a 10-minute repeating check catches a
  laptop that *woke* from sleep after the nudge time (the process never restarted). The
  `nudges` table makes all of them idempotent.
- **Extra state:** a `config` table (for the chat id from `/start`); callbacks `v`, `ts`, `xy`,
  `xc`, `new`. 🗑 asks for confirmation.
- `cooks.cooked_at` stores a local calendar date (`YYYY-MM-DD`), not a UTC timestamp. It is a
  date, and the nudge compares it with the local "today".
- If saving a link fails outright (e.g. the DB is unwritable), the URL is appended to
  `data/failed_captures.txt`.
- A photo sent right after tapping "📝 Add notes / photo" is attached to that cook
  (`cook_photos/`). Any other photo becomes a new recipe.
