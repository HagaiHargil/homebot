"""All SQL lives here. Plain sqlite3, parameterised queries, no ORM.

Conventions:
  * timestamps (added_at, created_at, sent_at) are UTC ISO-8601 with a 'Z' suffix
  * cooks.cooked_at and nudges.local_date are calendar dates ('YYYY-MM-DD') in LOCAL_TZ
  * connections run in autocommit mode; multi-statement writes use `with tx(conn):`

Run `python -m db --selftest` to exercise the schema against a throwaway database.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema.sql"

VERDICTS = ("good", "ok", "bad")
RECIPE_COLUMNS = {
    "title", "url", "url_canonical", "source", "image_path", "snapshot_path",
    "ingredients", "notes", "fetch_error", "added_at", "archived",
}


# --------------------------------------------------------------------------- helpers

def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or HERE / "data")


def db_path() -> Path:
    return data_dir() / "recipes.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fold(text):
    """Strip combining marks (Hebrew niqqud/cantillation, Latin accents).

    Registered as the SQL function fold() used by the FTS triggers. unicode61's
    remove_diacritics does not cover Hebrew points, so we fold them ourselves.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return text
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize(
        "NFC", "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    )


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = Path(path) if path else db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.create_function("fold", 1, fold, deterministic=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


@contextmanager
def session(path=None):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def reindex_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS index from `recipes` (folded; FTS5 'rebuild' would not fold)."""
    with tx(conn):
        conn.execute("INSERT INTO recipes_fts(recipes_fts) VALUES('delete-all')")
        conn.execute(
            "INSERT INTO recipes_fts(rowid, title, notes, ingredients) "
            "SELECT id, fold(title), fold(notes), fold(ingredients) FROM recipes"
        )


# --------------------------------------------------------------------------- config

def get_config(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO config(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, None if value is None else str(value)),
    )


# --------------------------------------------------------------------------- recipes

def insert_recipe(conn, *, title: str, added_at: str | None = None, **fields) -> int:
    unknown = set(fields) - RECIPE_COLUMNS
    if unknown:
        raise ValueError(f"unknown recipe columns: {unknown}")
    if isinstance(fields.get("ingredients"), list):
        fields["ingredients"] = json.dumps(fields["ingredients"], ensure_ascii=False)
    fields["title"] = title
    fields["added_at"] = added_at or utc_now()
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO recipes ({cols}) VALUES ({marks})", tuple(fields.values()))
    return cur.lastrowid


def update_recipe(conn, recipe_id: int, **fields) -> None:
    if not fields:
        return
    unknown = set(fields) - RECIPE_COLUMNS
    if unknown:
        raise ValueError(f"unknown recipe columns: {unknown}")
    if isinstance(fields.get("ingredients"), list):
        fields["ingredients"] = json.dumps(fields["ingredients"], ensure_ascii=False)
    sets = ", ".join(f"{c} = ?" for c in fields)
    conn.execute(f"UPDATE recipes SET {sets} WHERE id = ?", (*fields.values(), recipe_id))


def get_recipe(conn, recipe_id: int):
    return conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()


def get_recipe_by_canonical(conn, url_canonical: str):
    return conn.execute(
        "SELECT * FROM recipes WHERE url_canonical = ?", (url_canonical,)
    ).fetchone()


def delete_recipe(conn, recipe_id: int) -> dict | None:
    """Delete a recipe (cooks, tags, tg_messages cascade). Returns file paths to clean up."""
    row = get_recipe(conn, recipe_id)
    if not row:
        return None
    photos = [
        r["photo_path"]
        for r in conn.execute(
            "SELECT photo_path FROM cooks WHERE recipe_id = ? AND photo_path IS NOT NULL",
            (recipe_id,),
        )
    ]
    conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    return {
        "title": row["title"],
        "files": [p for p in (row["image_path"], row["snapshot_path"], *photos) if p],
    }


def append_recipe_note(conn, recipe_id: int, text: str, local_date: str) -> None:
    line = f"[{local_date}] {text.strip()}"
    conn.execute(
        "UPDATE recipes SET notes = CASE WHEN notes IS NULL OR notes = '' THEN ? "
        "ELSE notes || char(10) || ? END WHERE id = ?",
        (line, line, recipe_id),
    )


def list_recent(conn, limit: int = 10):
    return conn.execute(
        "SELECT * FROM recipes WHERE archived = 0 ORDER BY added_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def list_untried(conn, limit: int = 20):
    return conn.execute(
        "SELECT r.* FROM recipes r WHERE r.archived = 0 "
        "AND NOT EXISTS (SELECT 1 FROM cooks c WHERE c.recipe_id = r.id) "
        "ORDER BY r.added_at ASC, r.id ASC LIMIT ?",
        (limit,),
    ).fetchall()


def count_untried(conn) -> int:
    return conn.execute(
        "SELECT count(*) FROM recipes r WHERE r.archived = 0 "
        "AND NOT EXISTS (SELECT 1 FROM cooks c WHERE c.recipe_id = r.id)"
    ).fetchone()[0]


def recent_uncooked(conn, limit: int = 3):
    return conn.execute(
        "SELECT r.* FROM recipes r WHERE r.archived = 0 "
        "AND NOT EXISTS (SELECT 1 FROM cooks c WHERE c.recipe_id = r.id) "
        "ORDER BY r.added_at DESC, r.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def fts_query(text: str) -> str | None:
    """User text -> safe FTS5 query: every word must match, as a prefix."""
    tokens = _TOKEN_RE.findall(fold(text) or "")
    if not tokens:
        return None
    return " ".join('"' + t.replace('"', '""') + '"*' for t in tokens)


def search(conn, text: str, limit: int = 8):
    q = fts_query(text)
    if not q:
        return []
    return conn.execute(
        "SELECT r.* FROM recipes_fts f JOIN recipes r ON r.id = f.rowid "
        "WHERE recipes_fts MATCH ? AND r.archived = 0 ORDER BY f.rank LIMIT ?",
        (q, limit),
    ).fetchall()


def pick_what(conn, today: str):
    """Random recipe with >=1 good cook and none in the last 30 days; else a random untried one."""
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT r.* FROM recipes r WHERE r.archived = 0 "
        "AND EXISTS (SELECT 1 FROM cooks c WHERE c.recipe_id = r.id AND c.verdict = 'good') "
        "AND NOT EXISTS (SELECT 1 FROM cooks c WHERE c.recipe_id = r.id AND c.cooked_at > ?) "
        "ORDER BY random() LIMIT 1",
        (cutoff,),
    ).fetchone()
    if row:
        return row, "favourite"
    row = conn.execute(
        "SELECT r.* FROM recipes r WHERE r.archived = 0 "
        "AND NOT EXISTS (SELECT 1 FROM cooks c WHERE c.recipe_id = r.id) "
        "ORDER BY random() LIMIT 1"
    ).fetchone()
    return (row, "untried") if row else (None, None)


def all_recipes(conn):
    return conn.execute("SELECT * FROM recipes ORDER BY added_at, id").fetchall()


# --------------------------------------------------------------------------- cooks

def insert_cook(conn, recipe_id: int, verdict: str, cooked_at: str, notes: str | None = None,
                photo_path: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO cooks (recipe_id, cooked_at, verdict, notes, photo_path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (recipe_id, cooked_at, verdict, notes, photo_path, utc_now()),
    )
    return cur.lastrowid


def get_cook(conn, cook_id: int):
    return conn.execute("SELECT * FROM cooks WHERE id = ?", (cook_id,)).fetchone()


def append_cook_note(conn, cook_id: int, text: str) -> None:
    conn.execute(
        "UPDATE cooks SET notes = CASE WHEN notes IS NULL OR notes = '' THEN ? "
        "ELSE notes || char(10) || ? END WHERE id = ?",
        (text.strip(), text.strip(), cook_id),
    )


def set_cook_photo(conn, cook_id: int, photo_path: str) -> None:
    conn.execute("UPDATE cooks SET photo_path = ? WHERE id = ?", (photo_path, cook_id))


def delete_cook(conn, cook_id: int) -> dict | None:
    row = get_cook(conn, cook_id)
    if not row:
        return None
    conn.execute("DELETE FROM cooks WHERE id = ?", (cook_id,))
    return {"recipe_id": row["recipe_id"], "files": [row["photo_path"]] if row["photo_path"] else []}


def cooks_for(conn, recipe_id: int):
    return conn.execute(
        "SELECT * FROM cooks WHERE recipe_id = ? ORDER BY cooked_at DESC, id DESC",
        (recipe_id,),
    ).fetchall()


def cook_summary(conn, recipe_id: int):
    """(count, last_verdict, last_cooked_at) or (0, None, None)."""
    row = conn.execute(
        "SELECT count(*) AS n, "
        "(SELECT verdict FROM cooks WHERE recipe_id = ? ORDER BY cooked_at DESC, id DESC LIMIT 1) AS v, "
        "max(cooked_at) AS d FROM cooks WHERE recipe_id = ?",
        (recipe_id, recipe_id),
    ).fetchone()
    return row["n"], row["v"], row["d"]


def has_cook_on(conn, local_date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM cooks WHERE cooked_at = ? LIMIT 1", (local_date,)
    ).fetchone() is not None


# --------------------------------------------------------------------------- tags

def add_tags(conn, recipe_id: int, names: list[str]) -> list[str]:
    added = []
    with tx(conn):
        for name in names:
            name = name.strip().lstrip("#").strip()
            if not name:
                continue
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
            tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO recipe_tags(recipe_id, tag_id) VALUES (?, ?)",
                (recipe_id, tag_id),
            )
            added.append(name)
    return added


def tags_for(conn, recipe_id: int) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT t.name FROM tags t JOIN recipe_tags rt ON rt.tag_id = t.id "
            "WHERE rt.recipe_id = ? ORDER BY t.name",
            (recipe_id,),
        )
    ]


# --------------------------------------------------------------------------- telegram mapping

def map_message(conn, chat_id: int, message_id: int, recipe_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tg_messages(chat_id, message_id, recipe_id) VALUES (?, ?, ?)",
        (chat_id, message_id, recipe_id),
    )


def recipe_for_message(conn, chat_id: int, message_id: int):
    row = conn.execute(
        "SELECT recipe_id FROM tg_messages WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------- nudges

def nudge_sent(conn, local_date: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM nudges WHERE local_date = ?", (local_date,)
    ).fetchone() is not None


def claim_nudge(conn, local_date: str) -> bool:
    """Atomically record today's nudge. False if one was already recorded."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO nudges(local_date, sent_at) VALUES (?, ?)",
        (local_date, utc_now()),
    )
    return cur.rowcount == 1


def recent_save_since(conn, since_utc: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM recipes WHERE archived = 0 AND added_at >= ? LIMIT 1", (since_utc,)
    ).fetchone() is not None


# --------------------------------------------------------------------------- undo / stats

def latest_created(conn):
    """The newest recipe or cook as ('recipe'|'cook', id, created_utc), or None."""
    return conn.execute(
        "SELECT kind, id, ts FROM ("
        "  SELECT 'recipe' AS kind, id, added_at AS ts FROM recipes"
        "  UNION ALL SELECT 'cook', id, created_at FROM cooks"
        ") ORDER BY ts DESC, kind = 'recipe' ASC, id DESC LIMIT 1"
    ).fetchone()


def stats(conn, month_prefix: str) -> dict:
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    return {
        "recipes": one("SELECT count(*) FROM recipes WHERE archived = 0"),
        "cooks": one("SELECT count(*) FROM cooks"),
        "cooks_month": one("SELECT count(*) FROM cooks WHERE cooked_at LIKE ?", month_prefix + "%"),
        "never_cooked": count_untried(conn),
        "good": one("SELECT count(*) FROM cooks WHERE verdict = 'good'"),
        "fetch_errors": one("SELECT count(*) FROM recipes WHERE fetch_error IS NOT NULL"),
    }


# --------------------------------------------------------------------------- selftest

def _selftest() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "t.db")
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

        pointed = "עוֹף בַּתַּנּוּר עִם תַּפּוּחֵי אֲדָמָה"
        r1 = insert_recipe(conn, title=pointed, url="https://x.co.il/a",
                           url_canonical="https://x.co.il/a", source="x.co.il",
                           ingredients=["שׁוּם", "Crème fraîche"])
        r2 = insert_recipe(conn, title="Shakshuka", source="manual", notes="spicy, with feta")
        assert get_recipe(conn, r1)["title"] == pointed  # stored as-is

        def ids(q):
            return [r["id"] for r in search(conn, q)]

        assert ids("עוף") == [r1], ids("עוף")                 # unpointed query, pointed title
        assert ids("עוֹף") == [r1]                            # pointed query
        assert ids("תפוחי אדמה") == [r1]                      # multi-word AND
        assert ids("שום") == [r1]                             # ingredients indexed
        assert ids("creme") == [r1]                           # latin diacritics
        assert ids("feta") == [r2]                            # notes indexed
        assert ids("shak") == [r2]                            # prefix
        assert ids('"; DROP TABLE recipes; --') == []         # hostile input is inert
        update_recipe(conn, r2, title="Green shakshuka")
        assert ids("green") == [r2] and ids("shakshuka") == [r2]

        # dedupe key
        try:
            insert_recipe(conn, title="dup", url_canonical="https://x.co.il/a")
            raise AssertionError("UNIQUE not enforced")
        except sqlite3.IntegrityError:
            pass

        # cooks + CHECK
        c1 = insert_cook(conn, r1, "good", "2026-09-01")
        insert_cook(conn, r1, "ok", "2026-09-10")
        try:
            insert_cook(conn, r1, "meh", "2026-09-10")
            raise AssertionError("CHECK not enforced")
        except sqlite3.IntegrityError:
            pass
        assert cook_summary(conn, r1) == (2, "ok", "2026-09-10")
        append_cook_note(conn, c1, "more salt")
        assert get_cook(conn, c1)["notes"] == "more salt"
        assert has_cook_on(conn, "2026-09-10")

        # tags + tg mapping
        assert add_tags(conn, r1, ["dinner", "#Chicken", " "]) == ["dinner", "Chicken"]
        add_tags(conn, r1, ["CHICKEN"])  # NOCASE unique
        assert tags_for(conn, r1) == ["Chicken", "dinner"]
        map_message(conn, 1, 100, r1)
        assert recipe_for_message(conn, 1, 100) == r1

        # nudge idempotency
        assert claim_nudge(conn, "2026-09-20") is True
        assert claim_nudge(conn, "2026-09-20") is False

        # cascade delete
        info = delete_recipe(conn, r1)
        assert info["title"] == pointed
        for table in ("cooks", "tg_messages", "recipe_tags"):
            n = conn.execute(f"SELECT count(*) FROM {table} WHERE recipe_id = ?", (r1,)).fetchone()[0]
            assert n == 0, table
        assert ids("עוף") == []

        reindex_fts(conn)
        assert ids("green") == [r2]
        conn.close()
    print("db selftest: OK")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: python -m db --selftest")
