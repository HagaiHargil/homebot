PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS recipes (
  id            INTEGER PRIMARY KEY,
  title         TEXT NOT NULL,
  url           TEXT,                  -- final URL after redirects, NULL for photo/manual
  url_canonical TEXT UNIQUE,           -- dedupe key, NULL for photo/manual
  source        TEXT,                  -- domain, or 'photo' / 'manual'
  image_path    TEXT,
  snapshot_path TEXT,
  ingredients   TEXT,                  -- JSON array of strings, or NULL
  notes         TEXT,
  fetch_error   TEXT,                  -- non-NULL means parsing degraded
  added_at      TEXT NOT NULL,
  archived      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cooks (
  id         INTEGER PRIMARY KEY,
  recipe_id  INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  cooked_at  TEXT NOT NULL,            -- date of cooking (may be backdated)
  verdict    TEXT NOT NULL CHECK (verdict IN ('good','ok','bad')),
  notes      TEXT,
  photo_path TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cooks_recipe ON cooks(recipe_id);
CREATE INDEX IF NOT EXISTS idx_cooks_date   ON cooks(cooked_at);

CREATE TABLE IF NOT EXISTS tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS recipe_tags (
  recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (recipe_id, tag_id)
);

-- maps a Telegram message to a recipe, so replying to a card logs against it
CREATE TABLE IF NOT EXISTS tg_messages (
  chat_id    INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  recipe_id  INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  PRIMARY KEY (chat_id, message_id)
);

-- idempotency for the daily nudge
CREATE TABLE IF NOT EXISTS nudges (
  local_date TEXT PRIMARY KEY,         -- 'YYYY-MM-DD' in LOCAL_TZ
  sent_at    TEXT NOT NULL
);

-- small key/value store (e.g. the owner's chat_id, set by /start)
CREATE TABLE IF NOT EXISTS config (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(
  title, notes, ingredients,
  content='recipes', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);

-- FTS sync triggers.
-- unicode61's remove_diacritics only folds Latin combining marks; Hebrew niqqud
-- survives it. fold() is registered by db.connect() and strips niqqud/cantillation,
-- so the index holds unpointed text and queries are folded the same way.
-- Consequence: writes to `recipes` must go through db.connect() (datasette is
-- read-only, and `sqlite3 .backup` fires no triggers, so both are fine).
CREATE TRIGGER IF NOT EXISTS recipes_ai AFTER INSERT ON recipes BEGIN
  INSERT INTO recipes_fts(rowid, title, notes, ingredients)
  VALUES (new.id, fold(new.title), fold(new.notes), fold(new.ingredients));
END;

CREATE TRIGGER IF NOT EXISTS recipes_ad AFTER DELETE ON recipes BEGIN
  INSERT INTO recipes_fts(recipes_fts, rowid, title, notes, ingredients)
  VALUES ('delete', old.id, fold(old.title), fold(old.notes), fold(old.ingredients));
END;

CREATE TRIGGER IF NOT EXISTS recipes_au AFTER UPDATE ON recipes BEGIN
  INSERT INTO recipes_fts(recipes_fts, rowid, title, notes, ingredients)
  VALUES ('delete', old.id, fold(old.title), fold(old.notes), fold(old.ingredients));
  INSERT INTO recipes_fts(rowid, title, notes, ingredients)
  VALUES (new.id, fold(new.title), fold(new.notes), fold(new.ingredients));
END;
