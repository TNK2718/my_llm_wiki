# text2sql プロンプトのスキーマブロック動的化

> Status: **実装済 (2026-05-22)**
> Last updated: 2026-05-22

## 背景

`prompts/text2sql.txt` 冒頭 (line 3-39) のスキーマ宣言は**手書きの静的テキスト**で、`tools/schema.sql` から派生しているわけではない。`tools/query.py::text2sql()` (line 238-249) は template を `read_text()` した後 `{HINTS}` / `{FEWSHOTS}` / `{QUESTION}` の 3 プレースホルダだけを置換しており、スキーマ部分は LLM に **そのまま** 渡される。

このため `tools/schema.sql` 拡張のたびに人手追従が必要で、実際に 2 commit で text2sql.txt が取り残された:

- `ae4aa01` (2026-05-21) — contract + product_variant + governance/compliance を schema.sql に追加、`prompts/extract_graph.txt` は更新、`prompts/text2sql.txt` は**未更新**
- `0bd326a` (2026-05-21) — product に `billing_period / included_quota_units / quota_unit_name / trial_period_days` の typed 列 4 つを追加、`prompts/extract_graph.txt` は更新、`prompts/text2sql.txt` は**未更新**

結果として現状の prompts/text2sql.txt:9 `product(id, canonical_name, norm_key, release_date, category, ...)` には typed 化済みの 4 列が無く、`contract` 表自体も書かれていない。

### 実害

「Bob の無料期間は？」を `/api/ask` に投げると、LLM が見るシグナルは:

| 情報源 | trial_period_days はどこ？ |
|---|---|
| prompt のスキーマ宣言 | canonical に無い (drift) |
| prompt の R1 文言「型情報は canonical 側にしかない」 | canonical のどこかにある |
| hint vocab `product_claims.column_name の語彙` | claims にある (と読める) |
| fewshot `product-canonical-attr` | canonical にある |

2 対 1 で「claims にある」シグナルが勝ち、LLM は `JOIN product_claims` を生成。attempt 2 は別の table 選択ミス (`JOIN person_aliases`) で 0 行に陥った。fewshot だけが反対派で多数決負けする構図。

### この doc が解こうとしている問題

スキーマ宣言を**実 DB から runtime で再構築**すれば、`schema.sql` 編集時の人手追従が不要になり、drift は構造的に消える。

---

## 設計の柱

1. **スキーマ宣言は DB introspection から生成する**。`PRAGMA table_info(<name>)` を読んで `name(col1 TYPE, col2 TYPE, ...)` 形式に整形。`schema.sql` を直接パースしない (FK / CHECK 等の構文揺れに巻き込まれないため、`PRAGMA` 経由の真実を使う)。
2. **Core 表は常時 / 周辺表は質問依存で動的選択**。全表をベタ書きすると prompt が肥大化し、e2b 等の小型モデルではコンテキスト圧迫で性能低下する。Core は「documents + 5 entity canonical + 6 relation junction」の 12 表に絞り、aliases / claims / existence_claims / weak_relations / entity_mentions / doc_fts は質問との embedding similarity で top-K (デフォルト 5) を追加する。`select_fewshots()` (`tools/query.py:196`) と同じ方式で実装する。
3. **CHECK enum / 列コメントはスキーマブロックに出さない**。enum 値は `column_hints()` の vocab で既に提示しているので二重提示は避け、prompt を軽量に保つ。
4. **Internal 表は除外する**。`schema_migrations` / `staging_extractions` / `schema_proposals` / `conflict_kinds` / `conflict_groups` / `embed_cache` は LLM が触る用途が無く、提示するとノイズになる。

---

## スキーマブロックの構造 (生成例)

```
-- 中核
documents(id INTEGER, slug TEXT, title TEXT, path TEXT, body TEXT, ingested_at TEXT)

-- エンティティ canonical
person(id INTEGER, canonical_name TEXT, norm_key TEXT, birth_date TEXT, nationality TEXT, created_at TEXT, updated_at TEXT)
organization(id INTEGER, canonical_name TEXT, ..., org_type TEXT, founded_year INTEGER, headquarters TEXT, ...)
product(id INTEGER, canonical_name TEXT, ..., release_date TEXT, category TEXT, billing_period TEXT, included_quota_units REAL, quota_unit_name TEXT, trial_period_days INTEGER, ...)
project(...)
contract(...)

-- 関係 junction
employment(...)
manufacturing(...)
org_hierarchy(...)
product_variant(...)
governance(...)
compliance(...)

-- 関連 (質問に応じて自動選択, top-5)
product_claims(id INTEGER, product_id INTEGER, column_name TEXT, value TEXT, ..., status TEXT, ...)
product_aliases(product_id INTEGER, alias TEXT, norm_key TEXT)
employment_claims(...)
...
```

`product` の typed 列 4 つが自動で並ぶことで、冒頭の「Bob の無料期間」事例で起きた多数決失敗が解消する。

---

## データフロー

```
schema.sql                                          (真実)
   │
   ├── DB 初期化時に CREATE TABLE                   (tools/db.py::connect)
   │
   v
sqlite (実 DB)
   │
   ├── PRAGMA table_info(<name>)                   (tools/schema_block.py::_format_table_decl)
   │
   v
表ごとの宣言文字列
   │
   ├── core 12 表は固定順で並べる
   ├── extra 表は data/schema_docs.yml の role 文を embed → cos-sim 上位 K
   │     (tools/schema_block.py::select_extra_tables、select_fewshots と同形)
   │
   v
schema block string
   │
   v
text2sql.txt の {SCHEMA} に注入                    (tools/query.py::text2sql)
```

`data/schema_docs.yml` 例:

```yaml
version: 1
items:
  - table: person_aliases
    role: "person の別名 (英表記・略称・JP 表記の解決に使う)"
  - table: person_claims
    role: "person 属性の履歴 / 裏付け / 矛盾"
  - table: weak_relations
    role: "型不明 / confidence 閾値以下の長尾関係 (predicate ベース)"
  ...
```

このファイルは fewshot pool (`data/fewshot/text2sql.yml`) と並列の運用資産。新しい関連表が schema.sql に増えたとき、`schema_docs.yml` に role 文を 1 行加えれば LLM が拾えるようになる。何も加えなくても extra 候補から漏れるだけで実害は無いが、加えれば質問関連性が向上する。

---

## 再利用する既存関数

- `kg.connect(readonly=True)` (`tools/db.py:100`) — RO 接続
- `_embed_cached()` / `_cosine()` (`tools/query.py`) — 埋め込み計算と cos-sim
- `select_fewshots()` (`tools/query.py:196`) — top-K 選択ロジックの形をそのまま流用 (q_vec None fallback、安定 sort)
- `_load_fewshot_pool()` (`tools/query.py:166`) — YAML loader のテンプレート (version=1 / item dict / functools.cache)

---

## トレードオフ

1. **prompt サイズが質問ごとに微変動**: extra top-K が質問によって入れ替わるので、prompt の bytes 数が一定でなくなる。Ollama の KV cache hit 率は質問が違えばどのみち変動するので実害は小さい。
2. **schema_docs.yml の保守責任**: 新規 table の role 文を書く手間。ただし書き忘れても「extra 候補に出ない」だけで silent drift にはならない (core にはちゃんと出る) ので、`schema.sql` 編集忘れより重大度は低い。
3. **embed 不在 CI / Ollama 落ち時**: 質問 embedding が None になる。この場合 extra を 0 件にして core のみ提示する fallback (`select_fewshots()` と同方針) で安全に縮退する。
4. **`PRAGMA table_info` は CHECK / DEFAULT / 生成列を表現できない**: 制約情報の一部は失われる。これは CHECK enum を `column_hints()` の vocab に委譲する設計で吸収する。

---

## 将来

- **同パターンを `prompts/extract_graph.txt` へ展開できるか**: extract 側は「文書からの抽出」で、必要な情報は単なる schema 列名ではなく「抽出対象の type 一覧 + 各 type が持つ属性 + 抽出ヒント」。schema 直引きでは賄えない可能性が高いので別検討。少なくとも今回の dynamic schema は text2sql 専用とする。
- **CHECK enum の prompt 注入**: vocab と prompt の両方に同じ enum を出すのは冗長だが、prompt 側に出した方が認識率が上がる場合もある。eval で計測して必要なら拡張する。
- **drift 防止 CI**: schema.sql の変更があれば schema_block.py のテストが自動で `PRAGMA table_info` を読むので、prompt 側のスキーマ列が網羅されていることを assert する snapshot test を追加可能。

---

## 関連ドキュメント

- [typed-schema-design.md](typed-schema-design.md) — schema 自体の設計柱と claims 運用規約
- `prompts/text2sql.txt` — 変更対象の prompt template
- `tools/query.py` (`text2sql`, `select_fewshots`, `column_hints`) — 既存の動的 prompt 注入パターン
