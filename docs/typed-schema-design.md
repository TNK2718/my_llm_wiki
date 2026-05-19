# Typed Schema Redesign: 型テーブル + Claims + 人間レビュー付きスキーマ進化

> Status: **未着手**（設計合意、実装前）
> Last updated: 2026-05-20
> 注: 旧 `resolve-layer-design.md` と `future-tasks-design.md` は本ドキュメントに統合済み。前者は「型 schema に内包 / LLM 提案フローに統合」、後者は後段「tasks の扱い」節へ。

## 背景

現行スキーマ（`tools/schema.sql`）は動的 `entities` / `relations` / `facts` 3 テーブル + EAV の構成。柔軟だが SLM 主体の text2sql / 解析と相性が悪い:

- `facts.value` が常に TEXT で型情報が消える → range クエリ不能、silent 0-rows、述語ハルシネーション
- `relations(subject_id, predicate, object_id)` も generic で「職位・期間・文脈」のような関係自体の属性を表現できない
- 自由文字列の `predicate` / `attribute` に対する resolve 層が後付けで複雑化
- スキーマが「制約として機能していない」（CHECK 無し、FK 強制 OFF、enum 列が自由文字列）

この問題群を **「型ごとに固定列を持つテーブル群」＋「全主張を保持する `*_claims` 副テーブル」＋「人間レビュー付きスキーマ進化フロー」** で根治する。data/schema は Greenfield、評価 gold も型に合わせて作り直す。

---

## 設計の柱（合意事項）

1. **canonical + claims 二層、canonical = 最高 confidence active claim の反映**: 型テーブルは「現在の最高 confidence active claim」を反映するマテリアライズド・ビュー的役割。INSERT-once（先着優先）ではない。新 claim 到着時に confidence を比較して canonical を更新する。理由: 弱い主張が先着して canonical を汚染するリスクを排し、claims 別表化の意義（証拠保全・矛盾管理）と canonical の役割を整合させる。詳細は「Claims の運用規約 §3」。
2. **Greenfield**: 既存 `data/kg.sqlite` は捨てる。評価 gold（`data/eval/gold/`）は新スキーマで作り直す。後方互換は維持しない。
3. **Relations は junction + 存在 claims + 属性 claims の三層**: エンティティ表に inter-entity FK 列を持たない。junction 表の `document_id` / `evidence` / `confidence` 列は「最高 confidence active 存在主張のキャッシュ」で、全文書からの存在主張は `<relation>_existence_claims` 副表に積む。属性主張は `<relation>_claims` で扱う（従来通り）。`column_name='*'` sentinel は使わない。属性のない関係（`manufacturing` / `org_hierarchy` 等）でも `<relation>_existence_claims` は持つ — これで設計柱「全主張を保持」がすべての関係に対して一様に適用される。`entity_mentions` は各行が既に per-document mention なので例外（existence_claims を持たない）。
4. **長尾関係は `weak_relations` に逃がす**: LLM が抽出するゆるい関係（言及・並列・比較など）すべてを新 junction proposal で受けると proposals がスパムに埋もれる。confidence 閾値以下や型不明の関係は専用の `weak_relations` 表に積み、人間が「これは typed 化すべき」と判断したものだけ proposal 経由で昇格させる。
5. **スキーマ進化はトリアージ→ステージ→一括レビュー**: ingest は止めない。未知の型 / 列 / マッピングが曖昧な抽出は `staging_extractions` と `schema_proposals` に積み、後でダッシュボードからまとめて承認する。
6. **starter エンティティから `concept` を外す**: catch-all 型は型強制の趣旨と相性が悪く、ガベージダンプ化するリスクが大きい。曖昧概念は staging で別 type 提案として扱う。starter は person / organization / product / project の 4 型のみ。
7. **LLM 提案は必ず「既存テーブルとの類似度 + 新設提案」を両出し**: 最終判断は人間。誤分裂（既存があるのに新設）と過剰新設（似ているのに分けすぎ）を両方抑える。**類似度スコアは LLM ではなく決定論的に算出**（後段 D1 参照）。

---

## なぜ Relations を全部 junction テーブルにするか

候補は (A) 全 junction、(B) 単純1対多は FK 列・属性持ち relation のみ junction、(C) generic relations 残し段階的に typed 化、の 3 案。**Greenfield 前提では (A) + 長尾は `weak_relations` を推奨**。理由:

1. **claims モデルと一様に乗る** — 存在主張は `<relation>_existence_claims`、属性主張は `<relation>_claims` に積めば `<entity>_claims` と同じ confidence-based 戦略で動かせる
2. **「属性を後で増やしたい」変更が schema レベルの破壊にならない** — `product.manufacturer_id` を後で「製造期間も持ちたい」と言われた瞬間に junction 化が必要になる、という移行が要らない
3. **LLM 提案フローが「新 junction テーブル提案」の一形態に統一できる** — 「新列追加」と「新テーブル作成」の 2 種類だけで全パターンを覆える
4. **カーディナリティは junction の PK / UNIQUE で表現できる**:
   - `PRIMARY KEY (a_id, b_id)` → 多対多（同一ペアは 1 行）
   - `UNIQUE (a_id)` 追加 → a は単一の b にしか繋がらない
   - `PRIMARY KEY (a_id, b_id, valid_from)` → 履歴を許す

代償は JOIN が 1 段増えること。text2sql には不利方向だが、**スキーマが定型化されているため SLM が誤る余地はむしろ減る**（語彙が schema に内包される）。長尾関係は `weak_relations` 表で受け、proposals の S/N を保つ。

---

## 全体スキーマ（骨格 DDL）

DDL は「メタ → エンティティ → junction → weak_relations」の順で並べ、forward FK を排する。

```sql
PRAGMA foreign_keys = ON;

-- ============================================================
-- 1. 共通メタ（他から参照されるので先頭で定義）
-- ============================================================
CREATE TABLE documents (
  id          INTEGER PRIMARY KEY,
  slug        TEXT NOT NULL UNIQUE,
  title       TEXT,
  path        TEXT,
  body        TEXT NOT NULL,        -- 本文を正規テーブルに保持（doc_fts は external content）
  ingested_at TEXT NOT NULL          -- ISO 8601 UTC
);

CREATE VIRTUAL TABLE doc_fts USING fts5(
  title, body,
  content=documents, content_rowid=id  -- external-content モード
);

-- external-content モードは自動同期しないので、documents 変更時に doc_fts を同期するトリガを置く
CREATE TRIGGER documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO doc_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO doc_fts(doc_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER documents_au AFTER UPDATE ON documents BEGIN
  INSERT INTO doc_fts(doc_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO doc_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TABLE schema_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

-- conflict 種別の lookup 表（FK で参照、新種は INSERT で追加）
CREATE TABLE conflict_kinds (
  kind        TEXT PRIMARY KEY,       -- 'person_birth_date' | 'employment_role' | ...
  description TEXT,
  created_at  TEXT NOT NULL
);

CREATE TABLE conflict_groups (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL REFERENCES conflict_kinds(kind),
  resolved_at TEXT,
  resolver    TEXT,
  note        TEXT,
  created_at  TEXT NOT NULL
);

-- 未知の型 / 未知の列 / マッピングが曖昧な抽出結果を保留
CREATE TABLE staging_extractions (
  id             INTEGER PRIMARY KEY,
  document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  raw_payload    TEXT NOT NULL,      -- JSON: 抽出 entity / relation の生データ（スキーマは extract_schema.py で固定、ingest 時に pydantic 検証）
  proposed_table TEXT,               -- LLM が推定した行き先
  match_summary  TEXT,               -- JSON: 既存類似 + 新設案
  status         TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','assigned','applied','rejected')),
  decided_table  TEXT,               -- レビュー後の最終行き先
  decided_at     TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX ix_staging_status ON staging_extractions(status, created_at);

-- スキーマ変更そのものの提案（テーブル新設 / 列追加）
CREATE TABLE schema_proposals (
  id                INTEGER PRIMARY KEY,
  kind              TEXT NOT NULL
                    CHECK (kind IN ('new_table','new_column','rename','split','merge')),
  target_table      TEXT,            -- 既存テーブル名（new_table は NULL）
  proposed_ddl      TEXT NOT NULL,   -- 生 DDL
  rationale         TEXT,            -- LLM が出した根拠
  evidence_docs     TEXT,            -- JSON: [doc_id, ...]
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','superseded')),
  decided_at        TEXT,
  decided_by        TEXT,
  applied_migration INTEGER REFERENCES schema_migrations(version),
  created_at        TEXT NOT NULL
);
CREATE INDEX ix_proposals_status ON schema_proposals(status, created_at);

-- ============================================================
-- 2. エンティティ表（starter set: person / organization / product / project）
--   canonical: 最高 confidence active claim を反映するマテリアライズド役（§3 参照）
--   *_claims:  全主張 + 矛盾管理
--   *_aliases: 別名・正規化キー（突合用）
-- ============================================================

-- person ----------------------------------------------------
CREATE TABLE person (
  id             INTEGER PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  norm_key       TEXT NOT NULL UNIQUE,
  birth_date     TEXT,                -- YYYY-MM-DD
  nationality    TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE TABLE person_claims (
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
  -- 同一文書から同一列への重複主張は禁止（同じ証拠は 1 件のみ）
  UNIQUE (person_id, column_name, document_id)
);
CREATE TABLE person_aliases (
  person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  alias     TEXT NOT NULL,
  norm_key  TEXT NOT NULL,
  PRIMARY KEY (person_id, alias)
);
CREATE INDEX ix_person_claims_lookup ON person_claims(person_id, column_name, status);
CREATE INDEX ix_person_aliases_nk    ON person_aliases(norm_key);

-- organization ----------------------------------------------
CREATE TABLE organization (
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
-- organization_claims, organization_aliases: person と同パターン
-- claims.column_name の CHECK enum も canonical 列名のみ（sentinel は無し）

-- product / project も同パターンで起動
--   product(id, canonical_name, norm_key, release_date, ...)
--   project(id, canonical_name, norm_key, started_at, ended_at, ...)
-- それぞれに *_claims, *_aliases を併設

-- ============================================================
-- 3. 関係 (junction tables) + provenance 列 + 必要なら *_claims
--   存在主張は junction 表の行 1 行で表現（sentinel '*' は不要）。
--   provenance は junction 表自身に持つ。属性主張のみ <rel>_claims で扱う。
-- ============================================================

-- 雇用。1 行 = 1 期間（start_date が期間の識別キー）。
-- 同 (person, org) で複数期間（再入社など）は別行で表現する。
CREATE TABLE employment (
  id              INTEGER PRIMARY KEY,
  person_id       INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  role            TEXT,
  start_date      TEXT,                 -- YYYY-MM-DD or NULL（不明）。期間の識別キーなので claims には積まない
  end_date        TEXT,
  -- 最高 confidence active 存在主張のキャッシュ。全主張は employment_existence_claims に
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group  INTEGER REFERENCES conflict_groups(id),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  -- start_date NULL は SQLite の NULL-distinct 規則で複数 NULL 行が共存しうる
  --   → 同一期間と判明した時点で dedup で merge する
  UNIQUE (person_id, organization_id, start_date)
);
-- 全文書からの存在主張（「この (person, org, start_date) 雇用関係は実在する」）
CREATE TABLE employment_existence_claims (
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
-- 属性 (role / end_date) の主張。start_date は employment 行の識別キーなので claims に含めない
CREATE TABLE employment_claims (
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
CREATE INDEX ix_employment_person   ON employment(person_id);
CREATE INDEX ix_employment_org      ON employment(organization_id);
CREATE INDEX ix_employment_ex_look  ON employment_existence_claims(employment_id, status);
CREATE INDEX ix_employment_cl_look  ON employment_claims(employment_id, column_name, status);

-- 製造（属性なし。junction 行は最高 conf active 存在主張のキャッシュ、全主張は manufacturing_existence_claims に）
CREATE TABLE manufacturing (
  id              INTEGER PRIMARY KEY,
  product_id      INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  -- 最高 confidence active 存在主張のキャッシュ
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group  INTEGER REFERENCES conflict_groups(id),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (product_id)                  -- 1 product : 1 manufacturer。違反主張は staging_extractions へ
);
-- 全文書からの存在主張（同 product への異 org 主張は UNIQUE 衝突で staging へ。詳細は Claims §3）
CREATE TABLE manufacturing_existence_claims (
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
CREATE INDEX ix_manufacturing_ex_look ON manufacturing_existence_claims(manufacturing_id, status);

-- 親組織関係（自己参照）
CREATE TABLE org_hierarchy (
  id            INTEGER PRIMARY KEY,
  parent_org_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  child_org_id  INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  -- 最高 confidence active 存在主張のキャッシュ
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  evidence      TEXT,
  confidence    REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (child_org_id)                -- 子は単一の親。違反主張は staging_extractions へ
);
-- 全文書からの存在主張
CREATE TABLE org_hierarchy_existence_claims (
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
CREATE INDEX ix_org_hierarchy_ex_look ON org_hierarchy_existence_claims(org_hierarchy_id, status);

-- 文書言及（旧 mentions 相当、type 解決後の参照）
CREATE TABLE entity_mentions (
  id           INTEGER PRIMARY KEY,
  document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  entity_table TEXT NOT NULL
               CHECK (entity_table IN ('person','organization','product','project')),
  entity_id    INTEGER NOT NULL,
  surface_form TEXT,
  span_start   INTEGER,
  span_end     INTEGER
);
CREATE INDEX ix_em_doc    ON entity_mentions(document_id);
CREATE INDEX ix_em_entity ON entity_mentions(entity_table, entity_id);
-- 注: entity_table/entity_id は polymorphic FK で SQL 上は anti-pattern。
--    mention は「言及した事実」だけを記録する低リスク用途として許容。
--    型表からの DELETE 時の孤児 mention は FK で担保できないので、starter 段階では
--    エンティティ削除を禁止し soft-delete のみ運用。痛んだら型別 mentions に分割する
--    （Open Questions 参照）。

-- ============================================================
-- 4. 長尾関係の受け皿（weak_relations）
--   LLM が抽出したゆるい関係（confidence < 閾値、または型不明）はここに積む。
--   人間レビューで「typed 化すべき」と判断された predicate は schema_proposal を
--   経て junction 表に昇格し、該当行に promoted_to を埋めて移行完了をマークする。
-- ============================================================
CREATE TABLE weak_relations (
  id              INTEGER PRIMARY KEY,
  subject_table   TEXT NOT NULL
                  CHECK (subject_table IN ('person','organization','product','project')),
  subject_id      INTEGER NOT NULL,
  predicate       TEXT NOT NULL,            -- 自由文字列。長尾の受け皿として例外的に許容
  object_table    TEXT
                  CHECK (object_table IS NULL OR object_table IN ('person','organization','product','project')),
  object_id       INTEGER,                  -- object が entity でない場合は NULL（object_text を見る）
  object_text     TEXT,                     -- 目的語が文字列のみのとき（"Q4 売上目標" など）
  document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.3 CHECK (confidence BETWEEN 0 AND 1),
  promoted_to     TEXT,                     -- proposal 承認で junction 化されたら junction 名
  created_at      TEXT NOT NULL
);
CREATE INDEX ix_weak_subject   ON weak_relations(subject_table, subject_id);
CREATE INDEX ix_weak_object    ON weak_relations(object_table, object_id);
CREATE INDEX ix_weak_predicate ON weak_relations(predicate);
```

### 旧設計から効いてくる細部

- `documents.body` を本テーブルに保持し、`doc_fts` を external-content モード化 + 同期トリガで「`INSERT OR REPLACE` で id が切替→ FK 孤児」「`doc_fts` 重複追記」を一掃
- 全 FK に `ON DELETE` を明示
- `PRAGMA foreign_keys = ON` をスキーマ頭で発行し、**接続側 `connect()` でも毎回実行**（schema 頭の PRAGMA は接続には効かない）
- enum 列はすべて `CHECK`
- `conflict_groups` は専用テーブル化し、解決者・日時・備考を保持
- 各テーブルにマイグレーション基盤（`schema_migrations`）を組み込む
- `updated_at` はアプリ層（`db.py` upsert 系）で必ず set。UPDATE トリガで二重管理しない
- 各 `*_claims` に `UNIQUE (entity_id, column_name, document_id)` を、`*_existence_claims` に `UNIQUE (junction_id, document_id)` を初期から課し、同文書からの重複主張を弾く（claims 肥大化対策の最初の一手）
- **bootstrap**: migration 0001 で以下の seed を投入する:
  - `documents` に 1 行（`id=1, slug='__human__', title='Human edits sentinel', body='', ingested_at=<bootstrap-time>`）。人手編集 claim はこの document を参照する（§5 参照）
  - `conflict_kinds` に starter 値を INSERT:
    - エンティティ属性系: `person_canonical_name`, `person_birth_date`, `person_nationality`, `organization_canonical_name`, `organization_founded_year`, `organization_headquarters`, `product_canonical_name`, `product_release_date`, `project_canonical_name`, `project_started_at`, `project_ended_at`
    - 関係存在主張系: `employment_existence`, `manufacturing_existence`, `org_hierarchy_existence`
    - 関係属性系: `employment_role`, `employment_end_date`
  - canonical 表の列追加時は、対応する `conflict_kinds.kind` も同 migration で INSERT する規約（CHECK enum を ALTER せずに増やせるのが lookup 表化の利点）

---

## Claims の運用規約

claims 表は「監査ログ・矛盾履歴・属性主張集約」の三役で、canonical 表とは責務を分ける。

### 1. column_name の意味と enum

- `column_name` は対応 canonical 表の列名集合に限定する（**`CHECK` enum で静的に列挙**）。canonical 表の列を追加するときは migration で claims の CHECK も同期更新。
- 代替案: ランタイムバリデータ（`db.py.record_claim()` で `PRAGMA table_info` と突き合わせ）。CHECK ほど強くないが migration コストが低い。**Greenfield では CHECK を採用**。
- **関係の存在主張は `<relation>_existence_claims` 副表で扱う**: junction 表の `document_id` / `evidence` / `confidence` 列は最高 conf active 存在主張のキャッシュで、過去・他文書の主張は副表に積む。`<relation>_claims` は属性（`role`、`end_date` など）の主張のみを扱う。属性を持たない関係（`manufacturing` / `org_hierarchy`）も `<relation>_existence_claims` は持つ（`_claims` 表は持たない）。`column_name='*'` sentinel は使わない。

### 2. value は TEXT 一律

- 型は **canonical 側で担保**。claims 側は `value TEXT` 一律で構わない。
- range クエリ・型検査は常に canonical 表を引く。**text2sql は claims.value に range / 比較 predicate を当てない**（§8 linter R1 で担保）。claims を SELECT 対象にすること自体は許可（履歴・裏付け数・矛盾一覧などの監査クエリで必要）、ただし `status` フィルタを要求（§8 linter R2）。
- claims は「いつ・どの文書から・どんな主張があったか」を残すだけのログ層。

### 3. canonical 値の確定タイミング（confidence ベース状態遷移）

canonical 表は「現在の最高 confidence active claim」を反映する。新規 claim 到着時は **既存 active 主張との confidence 比較で動的に判定**する。

| 段階 | 新 claim の比較対象 | 操作 |
|---|---|---|
| 1 件目 | （なし） | canonical を新値で set、`claim.status='active'` |
| N 件目で同値 | 既存 active と同値 | claim を `active` で追加（多重 evidence）、canonical 据え置き |
| N 件目で新値、confidence が既存最高より δ 以上高い (例 δ=0.1) | 既存 active を `superseded` に降格、新 claim を `active`、canonical を新値で UPDATE | – |
| N 件目で新値、confidence 差が δ 以内 | 新旧両 claim を `conflicted`、`conflict_groups` に 1 行、canonical は据え置き | – |
| N 件目で新値、confidence が既存より大幅に低い | 新 claim を `superseded` で記録のみ、canonical 据え置き | – |

- 閾値 δ は初期値 0.1。Phase 4 でメトリクスを見ながら調整する。
- 同じ規約を **関係 junction の存在主張**にも適用する: 同一 junction 行への存在主張は `<relation>_existence_claims` に積み、§3 の状態遷移を適用する。junction 表の `document_id` / `evidence` / `confidence` は「最高 conf active 存在主張」のキャッシュとして UPDATE する。属性列（`employment.role` など）は `<relation>_claims` 経由で別管理。
- **cardinality 制約と矛盾する存在主張**: 例えば `manufacturing` は UNIQUE(product_id) を持つので、同 product に異 organization を主張する claim は新規 junction 行として INSERT できない。この場合は `staging_extractions` にエスカレーションし、人手レビューで「既存 manufacturing 行を更新する」「両立しないとして既存を superseded にする」のどちらかを選択する（confidence-based 自動 flip はしない）。
- **人手 review による解決は専用の状態遷移を持たない**: 人手 verdict は「`confidence=1.0` の新 claim を INSERT する」操作として表現する（詳細 §5）。新人手 claim が §3 の通常遷移で active となり、旧 active 主張は superseded、`conflict_groups.resolved_at` も同じコードパスで埋まる。

### 4. 関係の存在主張と列値主張

- **存在主張**: `<relation>_existence_claims` に全文書からの主張を積み、§3 状態遷移で canonical（junction 行の `document_id` / `evidence` / `confidence`）を反映。`column_name='*'` のような sentinel は使わない。
- **列値主張**: junction 表が属性列を持つ場合（`employment.role` など）、それは `<relation>_claims` に column_name='role' などで積み上げ、§3 の状態遷移に従って canonical を反映。
- **属性のない関係**（`manufacturing` / `org_hierarchy`）も `<relation>_existence_claims` を持つ（`_claims` 表は持たない）。これで「全主張を保持」が全関係に対して一様に適用される。
- **junction 行識別キー列への矛盾**（例: `employment.start_date`、`manufacturing.organization_id`）は §3 の confidence-based UPDATE 対象外。識別キー列は claims に含めず、矛盾は `staging_extractions` へ。

### 5. 人手編集と LLM confidence の cap

人手 review の verdict は、専用の "resolved" state machine を持たず、**`confidence=1.0` の新 claim を INSERT する操作** として表現する。

- 人手 verdict を確定したいとき: 当該 (entity, column) に
  - `value = <verdict>`,
  - `confidence = 1.0`,
  - `document_id = 1`（bootstrap で確保した `__human__` document）,
  - `evidence = 'resolver: <name>; verdict over claim_ids=[…]'`,
  - で claim を INSERT する。`<relation>_existence_claims` も同じ。
- §3 の通常状態遷移が走り、新 claim が `active`、既存 active は `superseded` に降格、canonical を UPDATE。`conflict_groups.resolved_at` / `resolver` も同じコードパスで埋まる。
- **再 review の連鎖**: 後で更に異 evidence が来て再 review が必要になっても、再度 `confidence=1.0` の人手 claim を INSERT すれば §3 が処理する。回数制限なし。過去の人手 verdict は `superseded` として claims 表に残り audit 可能。
- **LLM-extracted claim の confidence は extractor で 0.95 に cap**: `confidence = min(raw_extracted_conf, 0.95)` を post-processing で適用する。これで:
  - 人手 conf=1.0 vs 最強 doc conf=0.95、δ=0.1 → `|1.0 - 0.95| = 0.05 ≤ δ` → **必ず conflict をトリガ**（人手 verdict は doc 由来 claim に silent には負けない）
  - 弱い doc (conf 0.5 等) → δ 以上低いので silent superseded（claims 表には残り、dashboard で「弱い反証あり」として可視化可能）
- **CHECK 制約 `confidence BETWEEN 0 AND 1` で 1.0 が上限**: 不正な ingest が conf=1.1 を投げて人手 verdict を奪うことは DB レベルで防がれる。
- **synthetic `__human__` document の中身**: `body=''`、`title='Human edits sentinel'`。doc_fts に索引はされる（空 body は無害）。dashboard の document 一覧では特別扱いして隠す。

### 6. 並行性・再 ingest・戻り値

- **並行性**: `record_claim()` / `record_existence_claim()` は SELECT-then-write を **`BEGIN IMMEDIATE`** で開始する 1 トランザクション内で実行する。WAL モード下でも writer は serialize されるためレースなし。SQLite は single-writer なのでスケーラビリティ上の問題は LLM 抽出側がボトルネックで隠れる。書き込みが詰まる場合は ingest 並列度を下げる（extract は並列、書き込みは serialize）。
- **同 (entity, column, document_id) の再 ingest = UPSERT**: `UNIQUE` 違反時の挙動を明示:

  | 既存 row | 新 claim の挙動 |
  |---|---|
  | 存在しない | 通常の INSERT、§3 状態遷移を適用 |
  | 存在、value 同値 | `evidence` / `confidence` を新値で UPDATE。`created_at` 据え置き。状態遷移不要 |
  | 存在、value 異値 | 全列を UPDATE、`created_at` 据え置き、当該 (entity, column) の §3 状態遷移を全件再計算 |

  `*_existence_claims` も `UNIQUE (junction_id, document_id)` 違反時に同じセマンティクス。`created_at` 据え置きの理由: 「いつこの文書が初めて主張したか」が監査上重要。再抽出は更新であって新事実ではない。
- **`record_claim()` の戻り値 enum**: dashboard・smoke test で「何が起きたか」を判定するため:

  ```python
  RecordClaimResult = Literal[
    'inserted_active',         # 1 件目 or 既存上回り、canonical UPDATE
    'inserted_superseded',     # δ 以上低い conf、記録のみ
    'inserted_conflicted',     # δ 以内、conflict_groups 行を新規生成
    'updated_evidence',        # 同 value 再 ingest、evidence/conf のみ UPDATE
    'upsert_revalue',          # 異 value 再 ingest、§3 再計算
    'rejected_low_confidence', # conf < 0.3、staging_extractions / weak_relations へ転送
  ]
  ```

  `record_existence_claim()` も同 enum（`'rejected_low_confidence'` の転送先は `weak_relations`）。

### 7. 低 confidence claim のルーティング (conf < 0.3)

canonical が著しく弱い主張で初期化されないよう、低 conf claim は claims 表に入れず別経路へ:

- **エンティティ属性 claim (`*_claims`) < 0.3**: `staging_extractions` へ。payload: `{"kind":"attribute_claim", "entity_table":"person", "entity_id":42, "column_name":"birth_date", "value":"1990", "confidence":0.2, ...}`
- **関係の存在主張 (`*_existence_claims`) < 0.3**: `weak_relations` へ。`subject_table` / `subject_id` / `predicate` / `object_table` / `object_id` を埋め、junction 行は作らない
- **関係の属性 claim (`<relation>_claims`) < 0.3**: `staging_extractions` へ。junction 行は既存前提（無ければ存在主張側でブロックされている）

`record_claim()` / `record_existence_claim()` は conf < 0.3 で `'rejected_low_confidence'` を返し、呼び出し元（`tools/ingest.py`）が staging/weak への振り分けを行う。

### 8. text2sql からの claims アクセス（linter 規約）

claims 表は監査・履歴・矛盾管理用途で、通常の事実問い合わせは canonical 表を使う。ただし「履歴」「裏付け数」「矛盾一覧」を求める質問では claims を引く必要があるので、**「claims を SELECT 対象に含めない」全面禁止は採らない**。代わりに以下 2 ルールを linter で強制する:

**R1. `*_claims.value` への range / 比較 predicate を禁止**

- 禁止: `<`, `>`, `<=`, `>=`, `BETWEEN`, `LIKE '%pattern%'`、および `CAST(value AS INTEGER)` / `DATE(value)` / `julianday(value)` 等の型変換関数
- 許可: `=`, `IN (...)`, `IS NULL`, `IS NOT NULL`
- 理由: claims.value は TEXT 一律。型情報は canonical 側のみ。range クエリは canonical 表を使うべき
- 例: `WHERE person.birth_date > '1990-01-01'` は OK、`WHERE person_claims.value > '1990-01-01'` は NG

**R2. `*_claims` / `*_existence_claims` への参照は `status` フィルタ必須**

- `FROM <table>_claims` または `JOIN <table>_claims` のとき、当該別名に対する `status = …` または `status IN (…)` predicate が WHERE / JOIN ON のいずれかに必要
- 理由: claims は active / superseded / conflicted を保持する。フィルタなし aggregation は誤集計（例: 5 文書のうち 4 文書が superseded された主張で COUNT(*)=5 になる）
- 例外: `SELECT status, COUNT(*) FROM person_claims GROUP BY status` は status が SELECT に出現し集計目的が明示的なので許可

**許可される claims クエリ例**:

```sql
-- Alice の role 履歴を時系列で
SELECT c.value, c.created_at, c.document_id
  FROM employment_claims c
  JOIN employment e ON e.id = c.employment_id
 WHERE e.person_id = :alice
   AND c.column_name = 'role'
   AND c.status IN ('active','superseded')
 ORDER BY c.created_at;

-- Alice の birth_date を何文書が裏付けているか
SELECT COUNT(DISTINCT document_id)
  FROM person_claims
 WHERE person_id = :alice
   AND column_name = 'birth_date'
   AND status = 'active';

-- 現在 conflict 状態の主張一覧
SELECT * FROM person_claims WHERE status = 'conflicted';
```

**実装方針**:

- `tools/query.py` の生成後 lint pass で `sqlglot` AST を walk
- R1 / R2 違反を検出したら、エラー内容を含めて prompt を再生成 (1 回 retry)
- retry も通らなければ FTS fallback。Phase 4 で retry 回数とメトリクスを調整

---

## ビルトイン starter set

最初の DB に ship する構成（旧 `entities.type` 5 種のうち `concept` を除く 4 型と、最小の関係集合）:

| 種別 | テーブル |
|---|---|
| エンティティ canonical | `person`, `organization`, `product`, `project` |
| エンティティ claims / aliases | `person_claims`, `person_aliases`（他 3 型も同パターン） |
| 関係 junction | `employment`, `manufacturing`, `org_hierarchy`, `entity_mentions` |
| 関係 existence claims | `employment_existence_claims`, `manufacturing_existence_claims`, `org_hierarchy_existence_claims`（`entity_mentions` を除く全関係に併設） |
| 関係 attribute claims | `employment_claims`（属性を持つ関係のみ。`manufacturing` / `org_hierarchy` は持たない） |
| 長尾受け皿 | `weak_relations` |
| メタ | `documents`, `doc_fts`, `schema_migrations`, `conflict_groups`, `staging_extractions`, `schema_proposals` |

- `concept` は starter に含めない。catch-all 化のリスクが高く、型強制の趣旨と相反する。曖昧概念は `weak_relations.object_text` や `staging_extractions` に積み、典型パターンが見えてから proposal 経由で typed 化する。
- `project_membership` 等は starter に含めず、最初の関連文書を ingest した時点で proposal 経由で追加（Open Q 3 参照）。
- 各 starter テーブルは「最低限の列」で起動する（`canonical_name`, `norm_key`, タイムスタンプ、明らかな 1–2 列）。それ以上は schema proposal 経由で人間が育てる。

---

## tasks の扱い

TODO / アクションアイテム管理は本リポの**主要ユースケース**で、旧 `future-tasks-design.md` で別レイヤとして設計されていた。typed-schema 化に伴い本設計に統合する（旧 doc は廃止）。詳細 DDL は Phase 5 着手時に詰めるが、規約と方針はここで確定させる。

### 規約

- **tasks は typed entity の一種**として扱うが、**claims / 自前の provenance 列を持たない例外**:
  - 理由: ライフサイクル管理ドメインでは「複数文書からの主張の集約」より「人間の直接編集」が主体。同一 task への矛盾主張を confidence で解決する要件は薄い。
  - ingest 経由で抽出された task は `tasks.status='proposed'` で初期化し、人間レビューで `open` に昇格する（confidence-based canonical 戦略は使わない）。出典は `tasks.document_id`（任意 FK）で 1 件のみ保持。
- **assignee は junction に分解**: canonical 表に inter-entity FK を置かない柱に従い、`task_assignments(task_id, person_id, role)` で表現する。`tasks.assignee_id` 列は持たない。
- **task ↔ entity 関連は型別 junction**: 旧 `task_entities` の polymorphic FK は採らず、`task_person` / `task_organization` / `task_product` / `task_project` の 4 junction に分解する（`entity_mentions` 同様の polymorphic は採用しない。tasks は件数が少なく型別に分けても膨らまない）。
- **enum 列はすべて CHECK 強制**: `tasks.status IN ('proposed','open','in_progress','done','cancelled')`、`tasks.priority IN ('high','med','low')`、`task_assignments.role` も starter enum で限定。
- **日付列のフォーマット強制**: `tasks.due_date` / `closed_at` に `CHECK (... GLOB '____-__-__')`。
- **`tasks_fts` 同期トリガ** を `doc_fts` と同パターンで 3 つ設置。
- **`task_deps` の循環防止**: `CHECK (task_id != depends_on)` + INSERT トリガで再帰展開して reach 検査。

### 段階導入

starter には含めない。Phase 5 で取り込む。Phase 1〜4 で claims/proposal フローを固めてから tasks を追加することで、「claims を持たない例外」を確立した規約の上に明示できる。

### スコープ外（明示的に除外）

- **recurring task**（週次レビュー等）: 初期版では非対応。要件が出たら `task_recurrence(task_id, rrule)` を proposal で追加。
- **sub-task の階層**: `task_deps` は依存であり親子ではない。要件が出たら `tasks.parent_id` を proposal で追加。
- **assignee の複数指名**: `task_assignments` で多対多になるので原理上扱えるが、UI の主担当表示などは Phase 5 で詰める。

### Open question

- **ingest 経由 task の品質管理**: LLM が誤抽出した task（存在しない担当者、過去日 due_date など）が `proposed` 状態で大量に積まれた場合の triage UI。**推奨**: ダッシュボードの「弱関係レビュー」キューと同じパターンで `/api/tasks?status=proposed` のレビュー UI を Phase 5 で実装。
- **`closed_at` の自動 set**: `status='done'` 遷移時にアプリ層 (`db.py`) で set。トリガでは管理しない（typed-schema の `updated_at` 規約と同じ）。

---

## Ingest フロー

```
[doc] ─▶ extract_entities (LLM)
            │ 出力: { proposed_type, canonical_name, attributes,
            │         new_table_proposal: { table, columns, rationale } }
            │ ※ existing_matches の類似度スコアは LLM ではなく
            │    決定論的に算出（norm_key 一致 + embedding cos sim）
            ▼
       resolve_or_stage()
            ├─ similarity が閾値以上 & top-1 突出 → 既存表に upsert + claims（§3 状態遷移）
            ├─ 不明な属性列 → staging へ。ingest は止めない
            └─ 候補拮抗 / 新設提案あり → staging_extractions + schema_proposals
            ▼
[doc] ─▶ extract_relations (LLM)
            │ 出力: { proposed_junction, from, to, attributes, confidence,
            │         new_junction_proposal }
            ▼
       resolve_relation()
            ├─ 既存 junction にマップ可能 & confidence >= 0.3
            │     → junction 行を find-or-create（UNIQUE 列で突合）
            │     → <relation>_existence_claims に主張を record + §3 状態遷移で junction キャッシュを UPDATE
            │     → 属性主張は <relation>_claims に record + §3 状態遷移
            ├─ cardinality 制約と矛盾（例: 既存 manufacturing 行と異 org_id を主張）→ staging_extractions
            ├─ confidence < 0.3 または predicate 未知 → weak_relations へ
            └─ 同一 predicate が weak_relations に N 件超 蓄積 → schema_proposal 自動起票（typed 化候補）
            ▼
       矛盾検出（「Claims の運用規約 §3」に従う）
            ├─ canonical 表に既値あり & 主張値が異なる → confidence 差で UPDATE / conflict_group
            └─ 解決は人間が UI/CLI で実施
            ▼
       dashboard に通知（pending 件数）
```

### LLM 提案フォーマット例

```json
{
  "proposed_table": "startup",
  "existing_matches": [
    {"table": "organization",
     "rationale": "業種属性が startup 風だが企業の一形態とも解釈できる"}
  ],
  "new_table_proposal": {
    "table": "startup",
    "extends_from": "organization",
    "additional_columns": [
      {"name": "stage", "type": "TEXT", "example": "seed|series_a|..."},
      {"name": "total_funding_usd", "type": "INTEGER"}
    ],
    "rationale": "..."
  }
}
```

- `existing_matches` と `new_table_proposal` は常に両方出させる。これで「誤分裂（既存があるのに新設）」と「過剰新設（似ているのに分けすぎ）」の双方を人間レビューで抑える。
- **類似度スコアは LLM 出力に含めない**: SLM の幻覚で恣意化されるリスクが大きい。`similarity` はパイプライン側で決定論的に計算（`norm_key` 完全一致 + 名前 embedding の cos sim）し、match_summary にマージしてから staging に保存する。

---

## レビューフロー（triage → stage → batch review）

1. ingest 中の判断は staging / weak_relations に積む。ingest 自体は止めない
2. ダッシュボードに 3 つのレビューキューを設ける:
   - **schema_proposals**: DDL を編集 → 承認 → migration 自動生成 + 適用
   - **staging_extractions**: 既存テーブルへマージ / 新設テーブルへ flush / 棄却
   - **weak_relations**: 同一 predicate のまとまりを見て「typed 化」「破棄」「保留」を選択。typed 化は schema_proposal に変換
3. schema_proposal 承認時の安全運用（4 段階）:
   1. **AST 事前検証**: `sqlglot.parse(proposed_ddl, dialect='sqlite')` で構文木を作り、`kind` ごとの許容形式と一致するか検査。違反したら `exec()` を呼ばずに即 reject。`kind` 別の許容形式:

      | `kind` | 許容する top-level 文（順序自由・複数可） | 禁止 |
      |---|---|---|
      | `new_table` | `CREATE TABLE <target>` ちょうど 1 本 + `CREATE INDEX … ON <target>` 0+ 本 | `IF NOT EXISTS` / `AS SELECT` / それ以外の文 |
      | `new_column` | `ALTER TABLE <target> ADD COLUMN …` ちょうど 1 本 + `CREATE INDEX … ON <target>` 0+ 本 | 他の ALTER variant、DROP COLUMN |
      | `rename` | `ALTER TABLE <src> RENAME TO <dst>` ちょうど 1 本（`target_table` は `<src>`） | 他の文 |
      | `split` / `merge` | Phase 0 では構造的検証を行わず、人手レビュー必須フラグを立てる（Open Q 10） | 自動 apply は不可 |

      共通の禁止リスト（全 kind に適用）: `DROP …` / `PRAGMA …` / `ATTACH …` / `DETACH …` / `VACUUM` / `REINDEX` / `CREATE TRIGGER` / `CREATE VIEW` / `CREATE TABLE … AS SELECT` / `INSERT` / `UPDATE` / `DELETE` / 複文（許容文の組み合わせ以外の `;` 区切り）。

      また、文中の table 名が `schema_proposals.target_table` と一致しないものは reject。`CREATE INDEX` が貼られる table 名も `target_table` と一致を要求。

   2. **dry-run**: AST 事前検証を通った DDL を本 DB のスナップショット（一時 DB）に `exec()` して、構文・既存制約との衝突・FK 整合性を検出
   3. **diff 表示**: 適用前後の `sqlite_schema` 差分を UI に表示し、承認者が最終確認
   4. **apply**: 本 DB に DDL 実行 → `schema_migrations` に 1 行追加 → `applied_migration` をリンク
   - AST 検証失敗 / dry-run 失敗いずれも `applied_migration` を NULL のまま `status='rejected'`、`decided_*` に理由（`'ast_validation_failed: <reason>'` か `'dry_run_failed: <reason>'`）を記録
4. apply 成功後、関連 staging_extractions / weak_relations を再 replay（または `assigned` 状態へ、`weak_relations.promoted_to` を埋める）
5. 監査ログとして `schema_proposals.decided_*` に承認者・日時を残す

`staging_extractions` / `weak_relations` の TTL や差し戻し時の挙動は実装段階で詰める。最初は手動運用で進める。

---

## eval gold への影響

既存 gold（`data/eval/gold/extract/*.yaml`, `dedup/*.yaml`, `query/*.yaml`）は**新スキーマに合わせて作り直し**:

- `extract` gold: 期待 `person` / `organization` / `employment` / `*_claims` 行のセットで記述
- `query` gold: text2sql 期待 SQL を typed table を使う形に書き直す。range / 比較 predicate は canonical 列のみ、claims を引くときは `status` フィルタ付き（§8 linter 規約を gold 側でも遵守）。「履歴系」「裏付け数」「矛盾一覧」を問うクエリは claims を SELECT 対象に含めて構わない
- `dedup` gold: 既存パターン継続（norm_key ベース突合をテーブル別に走らせる）

新メトリクス候補:
- `schema_proposal_precision`: 人間が「新設すべき」と判断したものを LLM が新設提案できた率
- `staging_replay_correctness`: 承認後の replay で canonical 表が正しく埋まる率
- `canonical_stability`: confidence-based UPDATE が同 entity に対して頻繁に flip しないこと（高 churn は δ 設定の問題を示唆）
- `weak_relation_promotion_recall`: 後から typed 化された predicate を、`weak_relations` がきちんと捕捉できていた率

ground truth の出し方は実装段階で詰める。最初は `data/eval/gold/schema_proposals/<doc>.yml` を新設し、各文書につき「期待される proposal kind + target_table + columns」と「期待される staging_extractions 行」をペアで保持する想定。Phase 4 でフォーマットを確定する。

---

## 既存コードへの影響

| ファイル | 影響 |
|---|---|
| `tools/schema.sql` | 完全書き換え（上記 DDL） |
| `tools/db.py` | 大半書き換え。型別 `upsert_<type>()` / `find_or_create_<junction>()` / `record_existence_claim()` / `record_claim()` に分割。`record_existence_claim()` も `record_claim()` も §3 状態遷移を実装（前者は junction 行のキャッシュ列を、後者は canonical 表の属性列を UPDATE）。書き込みは **`BEGIN IMMEDIATE`** で 1 トランザクション化。戻り値は `RecordClaimResult` enum（§6 参照）。dedup ロジック（`norm_key` / aliases）はテーブル単位で再利用（`tools/normalize.py` 系の正規化規則を継承）。`connect()` で毎回 `PRAGMA foreign_keys=ON` |
| `tools/extract_schema.py` (or extractor post-processing) | LLM 抽出の `confidence` フィールドを post-processing で `min(raw, 0.95)` に cap（§5）。`record_claim()` 呼び出し前に適用 |
| `tools/ingest.py` | LLM プロンプトを「typed extract + match-or-propose」へ刷新。staging / weak_relations への積み上げを追加 |
| `tools/query.py` | text2sql の system prompt とスキーマ提示を刷新。`column_hints` は型 schema の中で動くため大半不要に。FTS fallback は残す。生成 SQL に対し §8 の linter (R1: claims.value への range/比較 禁止、R2: claims アクセスは status フィルタ必須) を `sqlglot` AST で検査、違反は 1 回 retry → FTS fallback |
| `tools/server.py` | `/api/entities` 系を型別エンドポイントへ分割、`/api/proposals` / `/api/staging` / `/api/weak_relations` を新設。`POST /api/proposals/:id/approve` は AST 事前検証（`sqlglot` + kind 別許容形式チェック、レビューフロー §3 step 1）→ dry-run → diff → apply のパイプラインを実装 |
| `web/` | ダッシュボードに「スキーマ提案レビュー」「未確定抽出レビュー」「弱関係レビュー」のタブを追加 |
| `prompts/` | `extract_graph` を typed extract + proposal 出力に書き換え（concept 型を提案候補から外す）。dedup プロンプトは継続利用 |
| `data/eval/gold/` | 全 gold を書き直し |
| `docs/future-tasks-design.md` | **廃止**。tasks 規約は本ドキュメント「tasks の扱い」節に統合。本設計を main に取り込むタイミングで削除 |

---

## 段階導入

- **Phase 0**（本ドキュメント）: 設計確定 + starter DDL ファイル化
- **Phase 1**: Greenfield 切替
  - (a) 既存 `data/kg.sqlite` を `data/kg.sqlite.bak.<date>` にリネーム保存
  - (b) 新スキーマで空 DB を作成 + マイグレーション基盤
  - (c) 旧 eval gold は `data/eval/gold.legacy/` にアーカイブ
  - (d) `db.py` の型別 API + smoke test。`record_claim()` の §3 状態遷移をユニットテスト。ingest は `person` だけ手動で
- **Phase 2**: extract プロンプト刷新と ingest 全体の動作確認。staging / weak_relations は write-only、レビューは CLI で
- **Phase 3**: ダッシュボードに proposal レビュー UI + staging replay + weak_relations triage + dry-run UI
- **Phase 4**: 評価 gold 再生成、メトリクス追加、リグレッション基準
  - 目安: typed 新 gold で text2sql スコアが旧 gold スコアの 80% を下回らないこと（実値は Phase 4 着手時に確定）
  - confidence 閾値 δ の調整も Phase 4
- **Phase 5**: `tasks` を本設計に取り込み（「tasks の扱い」節の規約に従って DDL を起こす）。`future-tasks-design.md` は削除

---

## 既知のトレードオフ / Open questions

1. **`entity_mentions` の polymorphic FK** — 純粋には anti-pattern。代替は型別 `person_mentions` / `organization_mentions` への分割。**推奨**: 初期は polymorphic + CHECK enum で許容。1 型あたり mention 件数が 10 万行を超えた時点で型別 mentions への分割を proposal 化する。
2. **confidence-based canonical の閾値 δ** — δ=0.1 は経験則。小さすぎると conflicted が頻発、大きすぎると弱い主張が居座る。**推奨**: 初期 δ=0.1 で出して Phase 4 メトリクス（`canonical_stability`）で調整。型別に δ を変える要件が出たら `canonical_policy(table, delta)` 表を後付け。
3. **starter relation set の妥当性** — `manufacturing` / `org_hierarchy` は初回 ingest を見ないと過不足が分からない。**推奨**: starter は `employment` / `entity_mentions` / `weak_relations` を必須、`manufacturing` / `org_hierarchy` は例示として DDL を準備するが、実 ingest で出てこなければ作らない方針。長尾は `weak_relations` で受けるので proposal キューはコア関係に絞れる。
4. **既存 KG 用語の更新** — `README.md` / `AGENTS.md` / メモリの用語と例を新スキーマに揃える作業が後段で必要。**推奨**: 本設計を main に取り込むタイミングで 1 PR にまとめて書き換え。
5. **claims / existence_claims のスナップショット化** — 行が増え続けるとサイズ問題が出る。**推奨**: 初期から `UNIQUE (entity_id, column_name, document_id)` / `UNIQUE (junction_id, document_id)` で同文書からの重複主張を弾く（DDL 反映済み）。さらに `*_claims` / `*_existence_claims` の合計行数が 100 万を超えたら `*_archive` への moving 運用を導入。閾値と運用は Phase 5 で詰める。
6. **text2sql のスキーマ提示戦略** — typed schema は表数が増えるため system prompt サイズが膨張する。(a) 質問から関連表を retrieve して 3-5 表だけ提示、(b) ER 図 ASCII 圧縮、(c) `query.py` で starter set のみ常駐＋proposal 由来の表は別経路、のいずれを採るかは Phase 4 で評価しながら決定。
7. **`weak_relations` 自動昇格の閾値** — 同一 predicate が N 件超 蓄積したら schema_proposal を自動起票する案を Ingest フローに書いたが、N の値と「同一 predicate 判定」（exact 文字列か embedding 類似度か）は未定。**推奨**: 初期は exact 文字列で N=5、Phase 3 のダッシュボード実装時に embedding クラスタリングへ拡張。
8. **`concept` 廃止に伴う「曖昧概念」の扱い** — 旧 KG では「アルゴリズム」「規格」のような抽象が `concept` に積まれていた。**推奨**: 初期は `weak_relations.object_text` に文字列として残し、頻出するクラスタが見えた段階で `algorithm` / `standard` 等の新 entity 型として proposal で起こす。`concept` を catch-all として復活させない。
9. **`organization.org_type` の CHECK enum の運用** — starter は 7 値 (`company`/`lab`/`team`/`university`/`government`/`nonprofit`/`other`) で開始。`other` 行が一定数（例: 100 行超 or 全体の 10%超）溜まったら新 org_type 候補を proposal で起こし、CHECK enum に追加（migration）。enum が 15 値を超えたら `conflict_kinds` 同様の lookup 表化を proposal で検討。
10. **`split` / `merge` proposal の AST 検証** — 表の分割・統合は (a) 新 table を CREATE、(b) 旧 table から `INSERT … SELECT`、(c) 旧 table の `DROP TABLE` または `ALTER TABLE … DROP COLUMN`、の複数文を伴う。DROP を含むので新規 table 提案と同じ AST 許容リストには載せられない。**推奨**: Phase 0 では構造的検証を諦め、proposal に `requires_manual_dry_run=true` フラグを立て、ダッシュボードで承認者が「2 段階確認」（DDL を読んで明示チェック + 別承認者の co-approve）を経由する経路にする。自動 apply は Phase 5 で migration テンプレート + 専用 lint を整備した上で解禁する。

---

## 受け入れ基準（実装後）

- `PRAGMA foreign_keys` が ON で起動。**`db.py.connect()` 経由のすべての接続で FK が立っていることを smoke test で確認**。FK 違反は SQLite エラーで上がる
- `conflict_groups.kind` が `conflict_kinds.kind` に FK で繋がり、未登録の kind での INSERT は FK 違反で reject される
- `organization.org_type` が starter 7 値 (`company`/`lab`/`team`/`university`/`government`/`nonprofit`/`other`) 以外の値で INSERT すると CHECK 制約違反で reject される
- 同じドキュメントを 2 回 ingest しても `documents` / `doc_fts` / `*_claims` に重複が出ない（`doc_fts` 同期トリガと claims `UNIQUE` で担保）
- 未知の entity 型を持つ文書を ingest しても処理が止まらず、`staging_extractions.status='pending'` に積まれる
- 未知の predicate / 低 confidence の関係は `weak_relations` に積まれ、`promoted_to` は NULL のまま
- 提案レビュー承認 → DDL 適用 → staging / weak_relations の該当行が `assigned`/`applied` / `promoted_to` 反映に遷移する replay フローが通る
- 同一 column への 2 件目の active 主張が confidence で既存を上回ると canonical が UPDATE され、旧 active 主張は `superseded` になる
- 同一 column への 2 件目の active 主張が confidence で僅差なら両 `conflicted` + `conflict_groups` に行が立ち、canonical は据え置き
- 関係の存在主張に対しても同じ規約: 既存 junction 行への 2 件目の存在主張が confidence で上回ると junction のキャッシュ列が UPDATE され、旧 active 主張は `<relation>_existence_claims.status='superseded'` になる
- 複数文書が同一 junction を裏付けたとき、`SELECT count(*) FROM <relation>_existence_claims WHERE status='active' AND <junction>_id=…` で裏付け数が取得できる
- cardinality 制約 (`UNIQUE(product_id)` 等) と矛盾する存在主張は `staging_extractions.status='pending'` に積まれ、junction 行は据え置き（自動 flip しない）
- **LLM 由来 claim の confidence は 0.95 を超えない**（extractor post-processing で cap）。`SELECT MAX(confidence) FROM person_claims WHERE document_id != 1` ≤ 0.95
- **人手 verdict (`document_id=1`) は conf=1.0** で記録され、§3 状態遷移により新 active となる。過去の人手 verdict は `superseded` で table に残る
- 人手 claim 後に conf >= 0.9 の contradicting doc claim が来ると **必ず `conflict_groups` に行が立つ**（silent supersede にならない）
- 人手 claim 後に conf < 0.9 の contradicting doc claim が来ると `superseded` で記録のみ、canonical 据え置き、claim は table に残り dashboard で「弱い反証あり」可視化可能
- 並行 ingest 2 本が同 (entity, column) に異なる高 conf claim を投げても、`BEGIN IMMEDIATE` で順次評価され race による両方上書きは起きない
- 同 doc を 2 回 ingest し同 value なら canonical 不変、claim 行の `evidence` / `confidence` のみ更新、`created_at` 据え置き
- 同 doc を 2 回 ingest し異 value なら全列 UPDATE + §3 再計算が走る
- text2sql ベンチで「型テーブル直接クエリ」プロンプト経由でも Phase 4 で確定する閾値以上のスコア（gold 再生成済み前提、目安は旧 gold スコアの 80%）
- text2sql 生成 SQL に対する §8 linter が起動: (R1) `*_claims.value` に `<`/`>`/`BETWEEN`/`LIKE`/型変換関数のいずれかが当たっている SQL は reject、(R2) `FROM/JOIN *_claims` または `*_existence_claims` に対し `status` predicate を欠く SQL は reject
- 「Alice の role 履歴」「birth_date の裏付け文書数」「現在 conflict 状態の主張」のような正当な claims クエリは linter を通過する（gold に positive sample として含める）
- ダッシュボードで `/api/proposals?status=pending` / `/api/staging?status=pending` / `/api/weak_relations?promoted_to=null` が件数を返す
- `*_claims.column_name` が対応 canonical 表の列名集合からしか取れない（CHECK 制約で担保、sentinel `'*'` は許容されない）
- `entity_mentions.entity_table` / `weak_relations.subject_table` が starter set の enum（`concept` を含まない）からしか取れない
- `schema_proposals` 承認時に AST 事前検証 → dry-run の順に走り、いずれかで失敗した DDL は `applied_migration=NULL` のまま `status='rejected'`、`decided_*` に理由が記録される
- AST 事前検証は次を reject する: `kind='new_table'` で `DROP TABLE` / `PRAGMA` / `ATTACH` / `INSERT` / `CREATE TRIGGER` / `CREATE VIEW` / `CREATE TABLE … AS SELECT` を含む DDL、`kind='new_column'` で複数の `ALTER TABLE` 文を含む DDL、`kind` と `target_table` に一致しない table 名の文を含む DDL、許容文以外を含む複文
- `kind='split'` / `kind='merge'` の proposal は AST 自動承認パスに乗らず、ダッシュボードで `requires_manual_dry_run` フラグが立ち、co-approve を経由しないと apply できない

---

## 参考: 棄却した代替案

- **(B) 単純1対多は FK 列 / 属性持ち relation のみ junction**: 「属性が後から増える」変更で junction 化が必要になり、移行コストが (A) より大きい
- **(C) generic `relations` を残して段階的に typed 化**: Greenfield 前提と矛盾。二系統の query path が長く残り、resolve 層を引きずる。**ただし長尾だけは `weak_relations` 表として復活させる**（コア関係は typed、長尾は generic）
- **canonical = claims の VIEW**: SQLite で `MAX(CASE WHEN column_name=...)` の PIVOT VIEW を組めば理論上は可能だが、(a) 型情報が VIEW では失われる（claims の `value TEXT` のまま）、(b) text2sql に対する schema 提示が複雑化（VIEW の列定義と裏の claims 構造の二重把握が必要）、(c) UNIQUE / FK を VIEW には貼れない、の 3 点で見送り。canonical 表を実体として残しつつ「最高 confidence active claim を反映する」と意味論を再定義する折衷案を採用
- **canonical 表に「最初の active claim」を写す（INSERT-once 戦略）**: 弱い先着主張が canonical を汚染するリスク。confidence ベース更新を採用
- **provenance を canonical 表の列に併設 (`name_doc_id`, `birth_date_doc_id`, ...)**: 列爆発、矛盾を表現できない、claims 形式に劣る
- **canonical 表に 1 行 = 1 主張**: 1 エンティティに複数行が並ぶ運用は dedup 側で常に絞り込みが必要。claims を別表化する現案のほうが query が素直
- **`*_claims.column_name='*'` で関係の存在主張を表現**: EAV `attribute=...` 自由文字列の縮小再導入になる。専用の `<relation>_existence_claims` 副表で表現する現案のほうが、`UNIQUE (junction_id, document_id)` などの制約が効き、設計柱「全主張を保持」と整合
- **junction 表自身に provenance 列だけを持たせ existence_claims を持たない**: 関係の存在主張が「最高 conf 1 件しか残らない」状態になり、(a) 多文書裏付けが可視化できない、(b) 高 conf 主張を後から撤回しても次点に降格できない、(c) 属性 claims との非対称性で運用が混乱する。`<relation>_existence_claims` 副表 + junction にキャッシュ列、の構成を採用してこれを解消
- **人手 verdict を専用の "resolved" state machine で immutable に守る**: 当初は `conflict_groups.resolved_at` 不変・新 contradicting claim は `kind='post_resolution'` の別 group で受ける、という設計を検討したが、(a) state machine の特例が増え `record_claim()` が複雑化、(b) 連鎖再 review の audit trail が claim 行の `conflict_group` 列 1 つでは表せない、(c) **confidence 値そのものでこの不変条件は守れる**（人手 conf=1.0、LLM cap=0.95、δ=0.1 で contradicting doc は必ず conflict トリガ）の 3 点で見送り。人手編集を `confidence=1.0` の通常 claim として扱う §5 の方式を採用
- **starter に `concept` を含める**: catch-all 化のリスクが高く、型強制の趣旨と相反する。曖昧概念は `weak_relations` / staging で別 type 提案として扱う
- **長尾関係も全部 junction proposal にする**: proposals がスパムに埋もれる。`weak_relations` で受けて頻出パターンのみ proposal に昇格する
- **`*_claims.column_name` をランタイムバリデータで担保**: CHECK enum と比べ migration ごとの追従が緩み、列名タイポが claims に紛れる。Greenfield で初期から強制した方がコストが低い
- **`future-tasks-design.md` の旧設計を維持**: 旧 generic `entities(id)` FK 前提、claims 規約なし、enum CHECK なし、循環防止なし、FTS 同期なしで、本設計の規約と複数箇所で衝突。別 doc として維持すると規約の二系統が長く残る。「tasks の扱い」節として本設計に統合し旧 doc は削除する
