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

-- contract ----------------------------------------------------
-- サービス記述書/SLA/利用規約/セキュリティ補遺/DPA 等の法的文書を表す entity。
-- 1 contract が 1 つの版 + 1 つの URL + 1 つの jurisdiction を持つ。版が変われば
-- 別 contract 行を作って supersedes (Phase B) で繋ぐ運用を想定。
CREATE TABLE IF NOT EXISTS contract (
  id                 INTEGER PRIMARY KEY,
  canonical_name     TEXT NOT NULL,
  norm_key           TEXT NOT NULL UNIQUE,
  contract_type      TEXT CHECK (contract_type IS NULL OR contract_type IN
                       ('service_description','terms_of_service','sla',
                        'security_addendum','data_processing','other')),
  effective_date     TEXT,                       -- YYYY-MM-DD
  valid_until        TEXT,                       -- YYYY-MM-DD or NULL
  jurisdiction       TEXT,                       -- '日本' / 'New York, US' 等の自由文字列
  url                TEXT,                       -- canonical URL
  version            TEXT,                       -- 'v2024.1' 等
  -- SLA 数値 (range クエリの主目的なので typed)
  sla_uptime_percent          REAL CHECK (sla_uptime_percent IS NULL OR (sla_uptime_percent >= 0 AND sla_uptime_percent <= 100)),
  sla_response_time_minutes   REAL CHECK (sla_response_time_minutes IS NULL OR sla_response_time_minutes >= 0),
  -- BCP 数値
  rto_hours          REAL CHECK (rto_hours IS NULL OR rto_hours >= 0),
  rpo_hours          REAL CHECK (rpo_hours IS NULL OR rpo_hours >= 0),
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contract_claims (
  id          INTEGER PRIMARY KEY,
  contract_id INTEGER NOT NULL REFERENCES contract(id) ON DELETE CASCADE,
  column_name TEXT NOT NULL
              CHECK (column_name IN (
                'canonical_name','contract_type','effective_date','valid_until',
                'jurisdiction','url','version',
                'sla_uptime_percent','sla_response_time_minutes',
                'rto_hours','rpo_hours')),
  value       TEXT,
  document_id INTEGER NOT NULL REFERENCES documents(id),
  evidence    TEXT,
  confidence  REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status      TEXT NOT NULL DEFAULT 'active'
              CHECK (status IN ('active','conflicted','superseded')),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at  TEXT NOT NULL,
  UNIQUE (contract_id, column_name, document_id)
);
CREATE TABLE IF NOT EXISTS contract_aliases (
  contract_id INTEGER NOT NULL REFERENCES contract(id) ON DELETE CASCADE,
  alias       TEXT NOT NULL,
  norm_key    TEXT NOT NULL,
  PRIMARY KEY (contract_id, alias)
);
CREATE INDEX IF NOT EXISTS ix_contract_claims_lookup ON contract_claims(contract_id, column_name, status);
CREATE INDEX IF NOT EXISTS ix_contract_aliases_nk    ON contract_aliases(norm_key);

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

-- product_variant -------------------------------------------
-- product → product のバリアント関係 (Pro/Plus/Ultra/Enterprise/Trial 等)。
-- parent: ファミリ代表、variant: バリアント。UNIQUE(variant_product_id) で
-- 1 variant ≤ 1 parent を強制 (org_hierarchy と同じ単一親モデル)。
CREATE TABLE IF NOT EXISTS product_variant (
  id                 INTEGER PRIMARY KEY,
  parent_product_id  INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  variant_product_id INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  document_id        INTEGER NOT NULL REFERENCES documents(id),
  evidence           TEXT,
  confidence         REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group     INTEGER REFERENCES conflict_groups(id),
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (variant_product_id)
);
CREATE TABLE IF NOT EXISTS product_variant_existence_claims (
  id                 INTEGER PRIMARY KEY,
  product_variant_id INTEGER NOT NULL REFERENCES product_variant(id) ON DELETE CASCADE,
  document_id        INTEGER NOT NULL REFERENCES documents(id),
  evidence           TEXT,
  confidence         REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status             TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','conflicted','superseded')),
  conflict_group     INTEGER REFERENCES conflict_groups(id),
  created_at         TEXT NOT NULL,
  UNIQUE (product_variant_id, document_id)
);
CREATE INDEX IF NOT EXISTS ix_product_variant_ex_look ON product_variant_existence_claims(product_variant_id, status);

-- governance --------------------------------------------------
-- 製品 ↔ 契約 (多対多)。1 製品が複数契約に従う / 1 契約が複数製品を governs する。
CREATE TABLE IF NOT EXISTS governance (
  id              INTEGER PRIMARY KEY,
  product_id      INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  contract_id     INTEGER NOT NULL REFERENCES contract(id) ON DELETE CASCADE,
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group  INTEGER REFERENCES conflict_groups(id),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (product_id, contract_id)
);
CREATE TABLE IF NOT EXISTS governance_existence_claims (
  id             INTEGER PRIMARY KEY,
  governance_id  INTEGER NOT NULL REFERENCES governance(id) ON DELETE CASCADE,
  document_id    INTEGER NOT NULL REFERENCES documents(id),
  evidence       TEXT,
  confidence     REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status         TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','conflicted','superseded')),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at     TEXT NOT NULL,
  UNIQUE (governance_id, document_id)
);
CREATE INDEX IF NOT EXISTS ix_governance_ex_look ON governance_existence_claims(governance_id, status);

-- compliance --------------------------------------------------
-- 契約 ↔ 準拠基準名 (テキスト)。standard を typed entity 化するのは後段 (Phase B)。
-- standard_name は自由テキスト ('ISO 27001', 'SOC 2 Type II', 'GDPR' 等)。
CREATE TABLE IF NOT EXISTS compliance (
  id              INTEGER PRIMARY KEY,
  contract_id     INTEGER NOT NULL REFERENCES contract(id) ON DELETE CASCADE,
  standard_name   TEXT NOT NULL,
  certified_until TEXT,                  -- YYYY-MM-DD or NULL (期限なし)
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group  INTEGER REFERENCES conflict_groups(id),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (contract_id, standard_name)
);
CREATE TABLE IF NOT EXISTS compliance_existence_claims (
  id             INTEGER PRIMARY KEY,
  compliance_id  INTEGER NOT NULL REFERENCES compliance(id) ON DELETE CASCADE,
  document_id    INTEGER NOT NULL REFERENCES documents(id),
  evidence       TEXT,
  confidence     REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  status         TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','conflicted','superseded')),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at     TEXT NOT NULL,
  UNIQUE (compliance_id, document_id)
);
CREATE INDEX IF NOT EXISTS ix_compliance_ex_look ON compliance_existence_claims(compliance_id, status);
CREATE INDEX IF NOT EXISTS ix_compliance_standard ON compliance(standard_name);

-- entity_mentions (per-document mention。existence_claims を持たない例外)
CREATE TABLE IF NOT EXISTS entity_mentions (
  id           INTEGER PRIMARY KEY,
  document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  entity_table TEXT NOT NULL
               CHECK (entity_table IN ('person','organization','product','project','contract')),
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
                  CHECK (subject_table IN ('person','organization','product','project','contract')),
  subject_id      INTEGER NOT NULL,
  predicate       TEXT NOT NULL,
  object_table    TEXT
                  CHECK (object_table IS NULL OR object_table IN ('person','organization','product','project','contract')),
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
