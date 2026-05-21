-- Migration 0004: product に課金単位列 (billing_period / included_quota_units /
-- quota_unit_name / trial_period_days) を追加。subscription SaaS の月間/年間 quota や
-- 無料評価版の期間を range クエリ可能にするための typed 列。
-- (1) product に列追加 (CHECK は ALTER で付けられないので、product 表は recreate)
-- (2) product_claims の column_name CHECK enum 拡張 (table recreate)
-- (3) conflict_kinds に 4 列分 seed
--
-- 注意: product_claims.product_id は ON DELETE CASCADE なので、DROP TABLE product を
-- foreign_keys=ON 状態で実行すると product_claims が空になる。recreate 中だけ
-- foreign_keys を OFF にしてから、再度 ON に戻す (connect() でも毎回 ON にしているが
-- migration の executescript 内では現セッション末まで OFF のままになるため明示で戻す)。
PRAGMA foreign_keys = OFF;

-- (1) product 表を recreate して新 4 列 + CHECK を入れる
CREATE TABLE product_new (
  id                   INTEGER PRIMARY KEY,
  canonical_name       TEXT NOT NULL,
  norm_key             TEXT NOT NULL UNIQUE,
  release_date         TEXT,
  category             TEXT,
  billing_period       TEXT CHECK (billing_period IS NULL OR billing_period IN ('monthly','annual','one_time')),
  included_quota_units REAL CHECK (included_quota_units IS NULL OR included_quota_units >= 0),
  quota_unit_name      TEXT,
  trial_period_days    INTEGER CHECK (trial_period_days IS NULL OR trial_period_days >= 0),
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);
INSERT INTO product_new(id, canonical_name, norm_key, release_date, category, created_at, updated_at)
  SELECT id, canonical_name, norm_key, release_date, category, created_at, updated_at FROM product;
DROP TABLE product;
ALTER TABLE product_new RENAME TO product;

-- (2) product_claims を recreate して column_name CHECK を拡張
CREATE TABLE product_claims_new (
  id          INTEGER PRIMARY KEY,
  product_id  INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  column_name TEXT NOT NULL
              CHECK (column_name IN (
                'canonical_name','release_date','category',
                'billing_period','included_quota_units','quota_unit_name','trial_period_days')),
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
INSERT INTO product_claims_new(id, product_id, column_name, value, document_id, evidence, confidence, status, conflict_group, created_at)
  SELECT id, product_id, column_name, value, document_id, evidence, confidence, status, conflict_group, created_at FROM product_claims;
DROP TABLE product_claims;
ALTER TABLE product_claims_new RENAME TO product_claims;
CREATE INDEX IF NOT EXISTS ix_product_claims_lookup ON product_claims(product_id, column_name, status);

-- (3) conflict_kinds seed
INSERT OR IGNORE INTO conflict_kinds(kind, description, created_at) VALUES
  ('product_billing_period',       'product.billing_period の矛盾',       '2026-05-21T00:00:00Z'),
  ('product_included_quota_units', 'product.included_quota_units の矛盾', '2026-05-21T00:00:00Z'),
  ('product_quota_unit_name',      'product.quota_unit_name の矛盾',      '2026-05-21T00:00:00Z'),
  ('product_trial_period_days',    'product.trial_period_days の矛盾',    '2026-05-21T00:00:00Z');

PRAGMA foreign_keys = ON;
