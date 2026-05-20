-- Typed-schema KG (Greenfield). docs/typed-schema-design.md §全体スキーマ参照。
-- 接続側 `db.py.connect()` でも毎回 `PRAGMA foreign_keys=ON` を発行する規約。
PRAGMA foreign_keys = ON;

-- ============================================================
-- 1. 共通メタ（他から参照されるので先頭で定義）
-- ============================================================
CREATE TABLE IF NOT EXISTS documents (
  id          INTEGER PRIMARY KEY,
  slug        TEXT NOT NULL UNIQUE,
  title       TEXT,
  path        TEXT,
  body        TEXT NOT NULL,
  ingested_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
  title, body,
  content=documents, content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO doc_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO doc_fts(doc_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
  INSERT INTO doc_fts(doc_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO doc_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflict_kinds (
  kind        TEXT PRIMARY KEY,
  description TEXT,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflict_groups (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL REFERENCES conflict_kinds(kind),
  resolved_at TEXT,
  resolver    TEXT,
  note        TEXT,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staging_extractions (
  id             INTEGER PRIMARY KEY,
  document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  raw_payload    TEXT NOT NULL,
  proposed_table TEXT,
  match_summary  TEXT,
  status         TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','assigned','applied','rejected')),
  decided_table  TEXT,
  decided_at     TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_staging_status ON staging_extractions(status, created_at);

CREATE TABLE IF NOT EXISTS schema_proposals (
  id                INTEGER PRIMARY KEY,
  kind              TEXT NOT NULL
                    CHECK (kind IN ('new_table','new_column','rename','split','merge')),
  target_table      TEXT,
  proposed_ddl      TEXT NOT NULL,
  rationale         TEXT,
  evidence_docs     TEXT,
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','superseded')),
  decided_at        TEXT,
  decided_by        TEXT,
  applied_migration INTEGER REFERENCES schema_migrations(version),
  requires_manual_dry_run INTEGER NOT NULL DEFAULT 0
                    CHECK (requires_manual_dry_run IN (0,1)),
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_proposals_status ON schema_proposals(status, created_at);

-- ============================================================
-- 2. エンティティ表 (starter: person / organization / product / project)
--    canonical = 最高 confidence active claim を反映 (§3 状態遷移)
-- ============================================================

-- person ----------------------------------------------------
CREATE TABLE IF NOT EXISTS person (
  id             INTEGER PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  norm_key       TEXT NOT NULL UNIQUE,
  birth_date     TEXT,
  nationality    TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_claims (
  id             INTEGER PRIMARY KEY,
  person_id      INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  column_name    TEXT NOT NULL
                 CHECK (column_name IN ('canonical_name','birth_date','nationality')),
  value          TEXT,
  document_id    INTEGER NOT NULL REFERENCES documents(id),
  evidence       TEXT,
  confidence     REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status         TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','conflicted','superseded')),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at     TEXT NOT NULL,
  UNIQUE (person_id, column_name, document_id)
);
CREATE TABLE IF NOT EXISTS person_aliases (
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  alias     TEXT NOT NULL,
  norm_key  TEXT NOT NULL,
  PRIMARY KEY (person_id, alias)
);
CREATE INDEX IF NOT EXISTS ix_person_claims_lookup ON person_claims(person_id, column_name, status);
CREATE INDEX IF NOT EXISTS ix_person_aliases_nk    ON person_aliases(norm_key);

-- organization ----------------------------------------------
CREATE TABLE IF NOT EXISTS organization (
  id             INTEGER PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  norm_key       TEXT NOT NULL UNIQUE,
  org_type       TEXT CHECK (org_type IS NULL OR org_type IN
                   ('company','lab','team','university','government','nonprofit','other')),
  founded_year   INTEGER,
  headquarters   TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS organization_claims (
  id              INTEGER PRIMARY KEY,
  organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  column_name     TEXT NOT NULL
                  CHECK (column_name IN ('canonical_name','org_type','founded_year','headquarters')),
  value           TEXT,
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','conflicted','superseded')),
  conflict_group  INTEGER REFERENCES conflict_groups(id),
  created_at      TEXT NOT NULL,
  UNIQUE (organization_id, column_name, document_id)
);
CREATE TABLE IF NOT EXISTS organization_aliases (
  organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  alias           TEXT NOT NULL,
  norm_key        TEXT NOT NULL,
  PRIMARY KEY (organization_id, alias)
);
CREATE INDEX IF NOT EXISTS ix_organization_claims_lookup ON organization_claims(organization_id, column_name, status);
CREATE INDEX IF NOT EXISTS ix_organization_aliases_nk    ON organization_aliases(norm_key);

-- product ---------------------------------------------------
CREATE TABLE IF NOT EXISTS product (
  id             INTEGER PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  norm_key       TEXT NOT NULL UNIQUE,
  release_date   TEXT,
  category       TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_claims (
  id          INTEGER PRIMARY KEY,
  product_id  INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  column_name TEXT NOT NULL
              CHECK (column_name IN ('canonical_name','release_date','category')),
  value       TEXT,
  document_id INTEGER NOT NULL REFERENCES documents(id),
  evidence    TEXT,
  confidence  REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status      TEXT NOT NULL DEFAULT 'active'
              CHECK (status IN ('active','conflicted','superseded')),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at  TEXT NOT NULL,
  UNIQUE (product_id, column_name, document_id)
);
CREATE TABLE IF NOT EXISTS product_aliases (
  product_id INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  alias      TEXT NOT NULL,
  norm_key   TEXT NOT NULL,
  PRIMARY KEY (product_id, alias)
);
CREATE INDEX IF NOT EXISTS ix_product_claims_lookup ON product_claims(product_id, column_name, status);
CREATE INDEX IF NOT EXISTS ix_product_aliases_nk    ON product_aliases(norm_key);

-- project ---------------------------------------------------
CREATE TABLE IF NOT EXISTS project (
  id             INTEGER PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  norm_key       TEXT NOT NULL UNIQUE,
  started_at     TEXT,
  ended_at       TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_claims (
  id          INTEGER PRIMARY KEY,
  project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  column_name TEXT NOT NULL
              CHECK (column_name IN ('canonical_name','started_at','ended_at')),
  value       TEXT,
  document_id INTEGER NOT NULL REFERENCES documents(id),
  evidence    TEXT,
  confidence  REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status      TEXT NOT NULL DEFAULT 'active'
              CHECK (status IN ('active','conflicted','superseded')),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at  TEXT NOT NULL,
  UNIQUE (project_id, column_name, document_id)
);
CREATE TABLE IF NOT EXISTS project_aliases (
  project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  alias      TEXT NOT NULL,
  norm_key   TEXT NOT NULL,
  PRIMARY KEY (project_id, alias)
);
CREATE INDEX IF NOT EXISTS ix_project_claims_lookup ON project_claims(project_id, column_name, status);
CREATE INDEX IF NOT EXISTS ix_project_aliases_nk    ON project_aliases(norm_key);

-- ============================================================
-- 3. 関係 (junction tables) + existence_claims + 属性 claims
-- ============================================================

-- employment ------------------------------------------------
CREATE TABLE IF NOT EXISTS employment (
  id              INTEGER PRIMARY KEY,
  person_id       INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  role            TEXT,
  start_date      TEXT,
  end_date        TEXT,
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group  INTEGER REFERENCES conflict_groups(id),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (person_id, organization_id, start_date)
);
CREATE TABLE IF NOT EXISTS employment_existence_claims (
  id             INTEGER PRIMARY KEY,
  employment_id  INTEGER NOT NULL REFERENCES employment(id) ON DELETE CASCADE,
  document_id    INTEGER NOT NULL REFERENCES documents(id),
  evidence       TEXT,
  confidence     REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status         TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','conflicted','superseded')),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at     TEXT NOT NULL,
  UNIQUE (employment_id, document_id)
);
CREATE TABLE IF NOT EXISTS employment_claims (
  id             INTEGER PRIMARY KEY,
  employment_id  INTEGER NOT NULL REFERENCES employment(id) ON DELETE CASCADE,
  column_name    TEXT NOT NULL
                 CHECK (column_name IN ('role','end_date')),
  value          TEXT,
  document_id    INTEGER NOT NULL REFERENCES documents(id),
  evidence       TEXT,
  confidence     REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status         TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','conflicted','superseded')),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at     TEXT NOT NULL,
  UNIQUE (employment_id, column_name, document_id)
);
CREATE INDEX IF NOT EXISTS ix_employment_person  ON employment(person_id);
CREATE INDEX IF NOT EXISTS ix_employment_org     ON employment(organization_id);
CREATE INDEX IF NOT EXISTS ix_employment_ex_look ON employment_existence_claims(employment_id, status);
CREATE INDEX IF NOT EXISTS ix_employment_cl_look ON employment_claims(employment_id, column_name, status);

-- manufacturing ---------------------------------------------
CREATE TABLE IF NOT EXISTS manufacturing (
  id              INTEGER PRIMARY KEY,
  product_id      INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group  INTEGER REFERENCES conflict_groups(id),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (product_id)
);
CREATE TABLE IF NOT EXISTS manufacturing_existence_claims (
  id               INTEGER PRIMARY KEY,
  manufacturing_id INTEGER NOT NULL REFERENCES manufacturing(id) ON DELETE CASCADE,
  document_id      INTEGER NOT NULL REFERENCES documents(id),
  evidence         TEXT,
  confidence       REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status           TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','conflicted','superseded')),
  conflict_group   INTEGER REFERENCES conflict_groups(id),
  created_at       TEXT NOT NULL,
  UNIQUE (manufacturing_id, document_id)
);
CREATE INDEX IF NOT EXISTS ix_manufacturing_ex_look ON manufacturing_existence_claims(manufacturing_id, status);

-- org_hierarchy ---------------------------------------------
CREATE TABLE IF NOT EXISTS org_hierarchy (
  id            INTEGER PRIMARY KEY,
  parent_org_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  child_org_id  INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  evidence      TEXT,
  confidence    REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (child_org_id)
);
CREATE TABLE IF NOT EXISTS org_hierarchy_existence_claims (
  id               INTEGER PRIMARY KEY,
  org_hierarchy_id INTEGER NOT NULL REFERENCES org_hierarchy(id) ON DELETE CASCADE,
  document_id      INTEGER NOT NULL REFERENCES documents(id),
  evidence         TEXT,
  confidence       REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status           TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','conflicted','superseded')),
  conflict_group   INTEGER REFERENCES conflict_groups(id),
  created_at       TEXT NOT NULL,
  UNIQUE (org_hierarchy_id, document_id)
);
CREATE INDEX IF NOT EXISTS ix_org_hierarchy_ex_look ON org_hierarchy_existence_claims(org_hierarchy_id, status);

-- entity_mentions (per-document mention。existence_claims を持たない例外)
CREATE TABLE IF NOT EXISTS entity_mentions (
  id           INTEGER PRIMARY KEY,
  document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  entity_table TEXT NOT NULL
               CHECK (entity_table IN ('person','organization','product','project')),
  entity_id    INTEGER NOT NULL,
  surface_form TEXT,
  span_start   INTEGER,
  span_end     INTEGER
);
CREATE INDEX IF NOT EXISTS ix_em_doc    ON entity_mentions(document_id);
CREATE INDEX IF NOT EXISTS ix_em_entity ON entity_mentions(entity_table, entity_id);

-- ============================================================
-- 4. 長尾関係の受け皿 (weak_relations)
-- ============================================================
CREATE TABLE IF NOT EXISTS weak_relations (
  id              INTEGER PRIMARY KEY,
  subject_table   TEXT NOT NULL
                  CHECK (subject_table IN ('person','organization','product','project')),
  subject_id      INTEGER NOT NULL,
  predicate       TEXT NOT NULL,
  object_table    TEXT
                  CHECK (object_table IS NULL OR object_table IN ('person','organization','product','project')),
  object_id       INTEGER,
  object_text     TEXT,
  document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.3 CHECK (confidence BETWEEN 0 AND 1),
  promoted_to     TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_weak_subject   ON weak_relations(subject_table, subject_id);
CREATE INDEX IF NOT EXISTS ix_weak_object    ON weak_relations(object_table, object_id);
CREATE INDEX IF NOT EXISTS ix_weak_predicate ON weak_relations(predicate);
