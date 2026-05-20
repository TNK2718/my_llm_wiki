-- Migration 0003: contract entity + governance/compliance junctions を starter に追加。
-- schema.sql 側で IF NOT EXISTS してあるので新表は自動で立つ。本 migration では
-- (1) 既存 DB の entity_mentions / weak_relations の CHECK enum を rebuild
-- (2) conflict_kinds の新規 seed
-- のみを行う。

-- (1) entity_mentions の CHECK enum 拡張 (contract 追加)
CREATE TABLE entity_mentions_new (
  id           INTEGER PRIMARY KEY,
  document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  entity_table TEXT NOT NULL
               CHECK (entity_table IN ('person','organization','product','project','contract')),
  entity_id    INTEGER NOT NULL,
  surface_form TEXT,
  span_start   INTEGER,
  span_end     INTEGER
);
INSERT INTO entity_mentions_new(id, document_id, entity_table, entity_id, surface_form, span_start, span_end)
  SELECT id, document_id, entity_table, entity_id, surface_form, span_start, span_end FROM entity_mentions;
DROP TABLE entity_mentions;
ALTER TABLE entity_mentions_new RENAME TO entity_mentions;
CREATE INDEX IF NOT EXISTS ix_em_doc    ON entity_mentions(document_id);
CREATE INDEX IF NOT EXISTS ix_em_entity ON entity_mentions(entity_table, entity_id);

-- (2) weak_relations の CHECK enum 拡張
CREATE TABLE weak_relations_new (
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
INSERT INTO weak_relations_new(id, subject_table, subject_id, predicate, object_table, object_id, object_text, document_id, evidence, confidence, promoted_to, created_at)
  SELECT id, subject_table, subject_id, predicate, object_table, object_id, object_text, document_id, evidence, confidence, promoted_to, created_at FROM weak_relations;
DROP TABLE weak_relations;
ALTER TABLE weak_relations_new RENAME TO weak_relations;
CREATE INDEX IF NOT EXISTS ix_weak_subject   ON weak_relations(subject_table, subject_id);
CREATE INDEX IF NOT EXISTS ix_weak_object    ON weak_relations(object_table, object_id);
CREATE INDEX IF NOT EXISTS ix_weak_predicate ON weak_relations(predicate);

-- (3) conflict_kinds starter seed (contract + governance + compliance)
INSERT OR IGNORE INTO conflict_kinds(kind, description, created_at) VALUES
  ('contract_canonical_name',           'contract.canonical_name の矛盾',          '2026-05-21T00:00:00Z'),
  ('contract_contract_type',            'contract.contract_type の矛盾',           '2026-05-21T00:00:00Z'),
  ('contract_effective_date',           'contract.effective_date の矛盾',          '2026-05-21T00:00:00Z'),
  ('contract_valid_until',              'contract.valid_until の矛盾',             '2026-05-21T00:00:00Z'),
  ('contract_jurisdiction',             'contract.jurisdiction の矛盾',            '2026-05-21T00:00:00Z'),
  ('contract_url',                      'contract.url の矛盾',                     '2026-05-21T00:00:00Z'),
  ('contract_version',                  'contract.version の矛盾',                 '2026-05-21T00:00:00Z'),
  ('contract_sla_uptime_percent',       'contract.sla_uptime_percent の矛盾',      '2026-05-21T00:00:00Z'),
  ('contract_sla_response_time_minutes','contract.sla_response_time_minutes の矛盾','2026-05-21T00:00:00Z'),
  ('contract_rto_hours',                'contract.rto_hours の矛盾',               '2026-05-21T00:00:00Z'),
  ('contract_rpo_hours',                'contract.rpo_hours の矛盾',               '2026-05-21T00:00:00Z'),
  ('governance_existence',              'governance 存在主張の矛盾',                '2026-05-21T00:00:00Z'),
  ('compliance_existence',              'compliance 存在主張の矛盾',                '2026-05-21T00:00:00Z');
