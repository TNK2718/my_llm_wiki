-- ナレッジグラフ用スキーマ（SQLite）。DB が正本。
-- すべて出典付き・非破壊（上書きせず status で管理）。

CREATE TABLE IF NOT EXISTS documents (
  id          INTEGER PRIMARY KEY,
  slug        TEXT UNIQUE NOT NULL,
  title       TEXT,
  path        TEXT,
  summary     TEXT,
  ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
  id             INTEGER PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  type           TEXT NOT NULL,           -- person/org/product/project/concept...
  norm_key       TEXT NOT NULL,           -- 正規化キー（ブロッキング・突合用）
  attributes     TEXT,                    -- JSON
  status         TEXT NOT NULL DEFAULT 'active',  -- active/merged/conflicted
  merged_into    INTEGER REFERENCES entities(id), -- merged のとき統合先
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  UNIQUE(type, norm_key)
);

CREATE TABLE IF NOT EXISTS entity_aliases (
  entity_id INTEGER NOT NULL REFERENCES entities(id),
  alias     TEXT NOT NULL,
  norm_key  TEXT NOT NULL,
  PRIMARY KEY (entity_id, alias)
);

CREATE TABLE IF NOT EXISTS relations (
  id             INTEGER PRIMARY KEY,
  subject_id     INTEGER NOT NULL REFERENCES entities(id),
  predicate      TEXT NOT NULL,
  object_id      INTEGER NOT NULL REFERENCES entities(id),
  document_id    INTEGER NOT NULL REFERENCES documents(id),
  evidence       TEXT,
  confidence     REAL NOT NULL DEFAULT 0.5,
  conflict_group INTEGER,                 -- 同一の矛盾集合に付与
  status         TEXT NOT NULL DEFAULT 'active',  -- active/superseded/conflicted
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mentions (
  id           INTEGER PRIMARY KEY,
  entity_id    INTEGER NOT NULL REFERENCES entities(id),
  document_id  INTEGER NOT NULL REFERENCES documents(id),
  surface_form TEXT,
  context      TEXT
);

CREATE TABLE IF NOT EXISTS facts (
  id             INTEGER PRIMARY KEY,
  entity_id      INTEGER NOT NULL REFERENCES entities(id),
  attribute      TEXT NOT NULL,
  value          TEXT,
  document_id    INTEGER NOT NULL REFERENCES documents(id),
  conflict_group INTEGER,
  status         TEXT NOT NULL DEFAULT 'active',  -- active/conflicted
  created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ent_normkey  ON entities(type, norm_key);
CREATE INDEX IF NOT EXISTS ix_rel_subject  ON relations(subject_id, predicate);
CREATE INDEX IF NOT EXISTS ix_fact_entity  ON facts(entity_id, attribute);
CREATE INDEX IF NOT EXISTS ix_men_entity   ON mentions(entity_id);

-- 文書本文の全文検索（ハイブリッド検索の片側）
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(slug, title, body);

-- 統合を解決して active な実体だけ見るビュー
CREATE VIEW IF NOT EXISTS v_active_entities AS
  SELECT * FROM entities WHERE status != 'merged';
