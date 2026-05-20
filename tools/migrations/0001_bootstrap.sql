-- Migration 0001: bootstrap. typed schema seed.
-- docs/typed-schema-design.md §「全体スキーマ → 旧設計から効いてくる細部 → bootstrap」参照。

-- __human__ sentinel document (id=1)。人手 verdict は confidence=1.0 の通常 claim
-- としてこの document を参照する (§5)。body='' は doc_fts に索引されるが無害。
INSERT OR IGNORE INTO documents(id, slug, title, path, body, ingested_at)
VALUES (1, '__human__', 'Human edits sentinel', NULL, '', '2026-05-20T00:00:00Z');

-- conflict_kinds starter set
INSERT OR IGNORE INTO conflict_kinds(kind, description, created_at) VALUES
  ('person_canonical_name',       'person.canonical_name の矛盾',  '2026-05-20T00:00:00Z'),
  ('person_birth_date',           'person.birth_date の矛盾',      '2026-05-20T00:00:00Z'),
  ('person_nationality',          'person.nationality の矛盾',     '2026-05-20T00:00:00Z'),
  ('organization_canonical_name', 'organization.canonical_name の矛盾', '2026-05-20T00:00:00Z'),
  ('organization_org_type',       'organization.org_type の矛盾',  '2026-05-20T00:00:00Z'),
  ('organization_founded_year',   'organization.founded_year の矛盾', '2026-05-20T00:00:00Z'),
  ('organization_headquarters',   'organization.headquarters の矛盾', '2026-05-20T00:00:00Z'),
  ('product_canonical_name',      'product.canonical_name の矛盾', '2026-05-20T00:00:00Z'),
  ('product_release_date',        'product.release_date の矛盾',   '2026-05-20T00:00:00Z'),
  ('product_category',            'product.category の矛盾',       '2026-05-20T00:00:00Z'),
  ('project_canonical_name',      'project.canonical_name の矛盾', '2026-05-20T00:00:00Z'),
  ('project_started_at',          'project.started_at の矛盾',     '2026-05-20T00:00:00Z'),
  ('project_ended_at',            'project.ended_at の矛盾',       '2026-05-20T00:00:00Z'),
  ('employment_existence',        'employment 存在主張の矛盾',     '2026-05-20T00:00:00Z'),
  ('employment_role',             'employment.role の矛盾',        '2026-05-20T00:00:00Z'),
  ('employment_end_date',         'employment.end_date の矛盾',    '2026-05-20T00:00:00Z'),
  ('manufacturing_existence',     'manufacturing 存在主張の矛盾',  '2026-05-20T00:00:00Z'),
  ('org_hierarchy_existence',     'org_hierarchy 存在主張の矛盾',  '2026-05-20T00:00:00Z');
