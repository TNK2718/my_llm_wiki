# Typed Schema Redesign: 型テーブル + Claims + 人間レビュー付きスキーマ進化

> Status: **未着手**（設計合意、実装前）
> Last updated: 2026-05-19
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
3. **Relations は原則すべて junction テーブル + 自身に provenance 列**: エンティティ表に inter-entity FK 列を持たない。関係の存在主張は junction 表に行があること自体で表現し、`document_id` / `evidence` / `confidence` は junction 表に直接持つ（claims sentinel `'*'` を使わない）。属性主張のみ `<relation>_claims` で扱う。
4. **長尾関係は `weak_relations` に逃がす**: LLM が抽出するゆるい関係（言及・並列・比較など）すべてを新 junction proposal で受けると proposals がスパムに埋もれる。confidence 閾値以下や型不明の関係は専用の `weak_relations` 表に積み、人間が「これは typed 化すべき」と判断したものだけ proposal 経由で昇格させる。
5. **スキーマ進化はトリアージ→ステージ→一括レビュー**: ingest は止めない。未知の型 / 列 / マッピングが曖昧な抽出は `staging_extractions` と `schema_proposals` に積み、後でダッシュボードからまとめて承認する。
6. **starter エンティティから `concept` を外す**: catch-all 型は型強制の趣旨と相性が悪く、ガベージダンプ化するリスクが大きい。曖昧概念は staging で別 type 提案として扱う。starter は person / organization / product / project の 4 型のみ。
7. **LLM 提案は必ず「既存テーブルとの類似度 + 新設提案」を両出し**: 最終判断は人間。誤分裂（既存があるのに新設）と過剰新設（似ているのに分けすぎ）を両方抑える。**類似度スコアは LLM ではなく決定論的に算出**（後段 D1 参照）。

---

## なぜ Relations を全部 junction テーブルにするか

候補は (A) 全 junction、(B) 単純1対多は FK 列・属性持ち relation のみ junction、(C) generic relations 残し段階的に typed 化、の 3 案。**Greenfield 前提では (A) + 長尾は `weak_relations` を推奨**。理由:

1. **claims モデルと一様に乗る** — `employment` に provenance 列を持たせれば `<entity>_claims` と同じ confidence-based 戦略で動かせる
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

CREATE TABLE conflict_groups (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,         -- 'person_birth_date' | 'employment_role' | ...
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
  org_type       TEXT,                -- 'company'|'lab'|'team'|... 後で別表化検討
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

-- 雇用（属性 role / start_date / end_date を持つ）
CREATE TABLE employment (
  id              INTEGER PRIMARY KEY,
  person_id       INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  role            TEXT,
  start_date      TEXT,
  end_date        TEXT,
  -- 存在主張の provenance（最高 confidence の主張を反映）
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group  INTEGER REFERENCES conflict_groups(id),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (person_id, organization_id)   -- 同一ペアは 1 行（履歴は role/start/end の claims で表現）
);
CREATE TABLE employment_claims (
  id             INTEGER PRIMARY KEY,
  employment_id  INTEGER NOT NULL REFERENCES employment(id) ON DELETE CASCADE,
  column_name    TEXT NOT NULL
                 CHECK (column_name IN ('role','start_date','end_date')),
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
CREATE INDEX ix_employment_person ON employment(person_id);
CREATE INDEX ix_employment_org    ON employment(organization_id);

-- 製造（属性なし。存在主張のみなので junction 表だけで完結、_claims は持たない）
CREATE TABLE manufacturing (
  id              INTEGER PRIMARY KEY,
  product_id      INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
  organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  document_id     INTEGER NOT NULL REFERENCES documents(id),
  evidence        TEXT,
  confidence      REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group  INTEGER REFERENCES conflict_groups(id),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  UNIQUE (product_id)                  -- 1 product : 1 manufacturer
);
-- 複数主張時は最高 confidence の主張で UPDATE。矛盾は conflict_group で管理（§3 参照）

-- 親組織関係（自己参照）
CREATE TABLE org_hierarchy (
  id            INTEGER PRIMARY KEY,
  parent_org_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  child_org_id  INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
  document_id   INTEGER NOT NULL REFERENCES documents(id),
  evidence      TEXT,
  confidence    REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  conflict_group INTEGER REFERENCES conflict_groups(id),
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (child_org_id)                -- 子は単一の親
);

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
- 各 `*_claims` に `UNIQUE (entity_id, column_name, document_id)` を初期から課し、同文書からの重複主張を弾く（claims 肥大化対策の最初の一手）

---

## Claims の運用規約

claims 表は「監査ログ・矛盾履歴・属性主張集約」の三役で、canonical 表とは責務を分ける。

### 1. column_name の意味と enum

- `column_name` は対応 canonical 表の列名集合に限定する（**`CHECK` enum で静的に列挙**）。canonical 表の列を追加するときは migration で claims の CHECK も同期更新。
- 代替案: ランタイムバリデータ（`db.py.record_claim()` で `PRAGMA table_info` と突き合わせ）。CHECK ほど強くないが migration コストが低い。**Greenfield では CHECK を採用**。
- **関係の存在主張のための sentinel は持たない**: 関係の存在は junction 表に行があること自体で表現する。`document_id` / `confidence` / `evidence` は junction 表の列に直接持つ。`<relation>_claims` は属性（`role` など）の主張のみを扱う。属性を持たない関係（`manufacturing` / `org_hierarchy`）は claims 表自体を持たない。

### 2. value は TEXT 一律

- 型は **canonical 側で担保**。claims 側は `value TEXT` 一律で構わない。
- range クエリ・型検査は常に canonical 表を引く。**text2sql は claims 表を SELECT 対象に選ばない**（受け入れ基準で担保）。
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
| レビュー解決 | 人手で「正」を 1 件選択 | 該当 claim を `active`、他を `superseded`、canonical を当該値で UPDATE、`conflict_groups.resolved_at` 更新 |

- 閾値 δ は初期値 0.1。Phase 4 でメトリクスを見ながら調整する。
- 同じ規約を **関係 junction 表の存在主張**にも適用する: 同一 (subject, object) ペアへの再主張は junction 表の `confidence` と比較し、新主張が高ければ `document_id` / `evidence` / `confidence` を UPDATE、属性列（`employment.role` など）は claims 経由で別管理。
- **初期 claim の confidence 下限**: 0.3 未満の主張は claims に入れず `weak_relations` 相当の扱いで staging へ。これで canonical が著しく弱い主張で初期化される事故を防ぐ。

### 4. 関係の存在主張と列値主張

- **存在主張**: 関係 junction 表に行があること自体で表現。重複主張は同表の `confidence` 比較で UPDATE（§3）。`column_name='*'` のような sentinel は使わない。
- **列値主張**: junction 表が属性列を持つ場合（`employment.role` など）、それは `<relation>_claims` に column_name='role' などで積み上げ、§3 の状態遷移に従って canonical を反映。
- **属性のない関係**（`manufacturing` / `org_hierarchy`）は claims 表自体を持たず、junction 表だけで完結する。

---

## ビルトイン starter set

最初の DB に ship する構成（旧 `entities.type` 5 種のうち `concept` を除く 4 型と、最小の関係集合）:

| 種別 | テーブル |
|---|---|
| エンティティ | `person`, `organization`, `product`, `project` |
| 関係 (junction) | `employment`, `manufacturing`, `org_hierarchy`, `entity_mentions` |
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
            ├─ 既存 junction にマップ可能 & confidence >= 0.3 → INSERT or UPDATE（§3）+ 属性 claims
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
3. schema_proposal 承認時の安全運用（3 段階）:
   1. **dry-run**: 本 DB のスナップショット（一時 DB）に DDL を `exec()` して構文・既存制約との衝突を検出
   2. **diff 表示**: 適用前後の `sqlite_schema` 差分を UI に表示し、承認者が最終確認
   3. **apply**: 本 DB に DDL 実行 → `schema_migrations` に 1 行追加 → `applied_migration` をリンク
   - dry-run 失敗時は `applied_migration` を NULL のまま `status='rejected'`、`decided_*` に理由を記録
4. apply 成功後、関連 staging_extractions / weak_relations を再 replay（または `assigned` 状態へ、`weak_relations.promoted_to` を埋める）
5. 監査ログとして `schema_proposals.decided_*` に承認者・日時を残す

`staging_extractions` / `weak_relations` の TTL や差し戻し時の挙動は実装段階で詰める。最初は手動運用で進める。

---

## eval gold への影響

既存 gold（`data/eval/gold/extract/*.yaml`, `dedup/*.yaml`, `query/*.yaml`）は**新スキーマに合わせて作り直し**:

- `extract` gold: 期待 `person` / `organization` / `employment` / `*_claims` 行のセットで記述
- `query` gold: text2sql 期待 SQL を typed table を使う形に書き直す。`*_claims` を SELECT で参照しないことも検査
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
| `tools/db.py` | 大半書き換え。型別 `upsert_<type>()` / `add_<junction>()` / `record_claim()` に分割。`record_claim()` は §3 状態遷移を実装。dedup ロジック（`norm_key` / aliases）はテーブル単位で再利用（`tools/normalize.py` 系の正規化規則を継承）。`connect()` で毎回 `PRAGMA foreign_keys=ON` |
| `tools/ingest.py` | LLM プロンプトを「typed extract + match-or-propose」へ刷新。staging / weak_relations への積み上げを追加 |
| `tools/query.py` | text2sql の system prompt とスキーマ提示を刷新。`column_hints` は型 schema の中で動くため大半不要に。FTS fallback は残す。生成 SQL の SELECT に `*_claims` が含まれないことを linter で検査 |
| `tools/server.py` | `/api/entities` 系を型別エンドポイントへ分割、`/api/proposals` / `/api/staging` / `/api/weak_relations` を新設 |
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
5. **claims のスナップショット化** — claims が増え続けるとサイズ問題が出る。**推奨**: 初期から `UNIQUE (entity_id, column_name, document_id)` で同文書 × 同列の重複主張を弾く（DDL 反映済み）。さらに `*_claims` の合計行数が 100 万を超えたら `*_claims_archive` への moving 運用を導入。閾値と運用は Phase 5 で詰める。
6. **text2sql のスキーマ提示戦略** — typed schema は表数が増えるため system prompt サイズが膨張する。(a) 質問から関連表を retrieve して 3-5 表だけ提示、(b) ER 図 ASCII 圧縮、(c) `query.py` で starter set のみ常駐＋proposal 由来の表は別経路、のいずれを採るかは Phase 4 で評価しながら決定。
7. **`weak_relations` 自動昇格の閾値** — 同一 predicate が N 件超 蓄積したら schema_proposal を自動起票する案を Ingest フローに書いたが、N の値と「同一 predicate 判定」（exact 文字列か embedding 類似度か）は未定。**推奨**: 初期は exact 文字列で N=5、Phase 3 のダッシュボード実装時に embedding クラスタリングへ拡張。
8. **`concept` 廃止に伴う「曖昧概念」の扱い** — 旧 KG では「アルゴリズム」「規格」のような抽象が `concept` に積まれていた。**推奨**: 初期は `weak_relations.object_text` に文字列として残し、頻出するクラスタが見えた段階で `algorithm` / `standard` 等の新 entity 型として proposal で起こす。`concept` を catch-all として復活させない。

---

## 受け入れ基準（実装後）

- `PRAGMA foreign_keys` が ON で起動。**`db.py.connect()` 経由のすべての接続で FK が立っていることを smoke test で確認**。FK 違反は SQLite エラーで上がる
- 同じドキュメントを 2 回 ingest しても `documents` / `doc_fts` / `*_claims` に重複が出ない（`doc_fts` 同期トリガと claims `UNIQUE` で担保）
- 未知の entity 型を持つ文書を ingest しても処理が止まらず、`staging_extractions.status='pending'` に積まれる
- 未知の predicate / 低 confidence の関係は `weak_relations` に積まれ、`promoted_to` は NULL のまま
- 提案レビュー承認 → DDL 適用 → staging / weak_relations の該当行が `assigned`/`applied` / `promoted_to` 反映に遷移する replay フローが通る
- 同一 column への 2 件目の active 主張が confidence で既存を上回ると canonical が UPDATE され、旧 active 主張は `superseded` になる
- 同一 column への 2 件目の active 主張が confidence で僅差なら両 `conflicted` + `conflict_groups` に行が立ち、canonical は据え置き
- text2sql ベンチで「型テーブル直接クエリ」プロンプト経由でも従来同等以上のスコア（gold 再生成済み前提）。**生成 SQL の SELECT 対象に `*_claims` 表が含まれない**ことを linter で検査
- ダッシュボードで `/api/proposals?status=pending` / `/api/staging?status=pending` / `/api/weak_relations?promoted_to=null` が件数を返す
- `*_claims.column_name` が対応 canonical 表の列名集合からしか取れない（CHECK 制約で担保、sentinel `'*'` は許容されない）
- `entity_mentions.entity_table` / `weak_relations.subject_table` が starter set の enum（`concept` を含まない）からしか取れない
- `schema_proposals` 承認時に dry-run が走り、dry-run 失敗 DDL は `applied_migration=NULL` のまま `status='rejected'` に遷移する

---

## 参考: 棄却した代替案

- **(B) 単純1対多は FK 列 / 属性持ち relation のみ junction**: 「属性が後から増える」変更で junction 化が必要になり、移行コストが (A) より大きい
- **(C) generic `relations` を残して段階的に typed 化**: Greenfield 前提と矛盾。二系統の query path が長く残り、resolve 層を引きずる。**ただし長尾だけは `weak_relations` 表として復活させる**（コア関係は typed、長尾は generic）
- **canonical = claims の VIEW**: SQLite で `MAX(CASE WHEN column_name=...)` の PIVOT VIEW を組めば理論上は可能だが、(a) 型情報が VIEW では失われる（claims の `value TEXT` のまま）、(b) text2sql に対する schema 提示が複雑化（VIEW の列定義と裏の claims 構造の二重把握が必要）、(c) UNIQUE / FK を VIEW には貼れない、の 3 点で見送り。canonical 表を実体として残しつつ「最高 confidence active claim を反映する」と意味論を再定義する折衷案を採用
- **canonical 表に「最初の active claim」を写す（INSERT-once 戦略）**: 弱い先着主張が canonical を汚染するリスク。confidence ベース更新を採用
- **provenance を canonical 表の列に併設 (`name_doc_id`, `birth_date_doc_id`, ...)**: 列爆発、矛盾を表現できない、claims 形式に劣る
- **canonical 表に 1 行 = 1 主張**: 1 エンティティに複数行が並ぶ運用は dedup 側で常に絞り込みが必要。claims を別表化する現案のほうが query が素直
- **`*_claims.column_name='*'` で関係の存在主張を表現**: EAV `attribute=...` 自由文字列の縮小再導入になる。関係 junction 表自身に provenance 列を持たせる現案のほうが「型強制」の趣旨と整合
- **starter に `concept` を含める**: catch-all 化のリスクが高く、型強制の趣旨と相反する。曖昧概念は `weak_relations` / staging で別 type 提案として扱う
- **長尾関係も全部 junction proposal にする**: proposals がスパムに埋もれる。`weak_relations` で受けて頻出パターンのみ proposal に昇格する
- **`*_claims.column_name` をランタイムバリデータで担保**: CHECK enum と比べ migration ごとの追従が緩み、列名タイポが claims に紛れる。Greenfield で初期から強制した方がコストが低い
- **`future-tasks-design.md` の旧設計を維持**: 旧 generic `entities(id)` FK 前提、claims 規約なし、enum CHECK なし、循環防止なし、FTS 同期なしで、本設計の規約と複数箇所で衝突。別 doc として維持すると規約の二系統が長く残る。「tasks の扱い」節として本設計に統合し旧 doc は削除する
