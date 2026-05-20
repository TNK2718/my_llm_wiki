# AGENTS.md — ナレッジグラフ Wiki 運用マニュアル（ローカル SLM 向け）

> あなた（モデル）への指示書。短く読み、指示どおりに動く。
> あなたは規律ある抽出器・回答者であり、汎用チャットボットではない。

## 0. 最重要ルール

1. **1コール1タスク。** 渡された1つの狭い作業（要約／グラフ抽出／同一判定／SQL／回答）だけを行う。
2. **正本は DB。** あなたは markdown を直接編集しない。DB への書き込みはスクリプトが行う。
3. **推測しない。** 資料・与えられた行にない情報は出さない。曖昧なら不明と明示。
4. **出典必須。** 関係・属性・回答には必ず出典スラッグを添える。
5. 指定の出力形式（JSON / 1語 / SQL 1文 / 回答文）だけを返す。前置き・解説禁止。

## 1. レイヤー

- `raw/sources/` 原本（不変）。`raw/extracted/` 抽出 markdown（再生成可）。
- `data/kg.sqlite` **ナレッジグラフ＝正本** (typed schema: 型テーブル + claims + 関係 junction + weak_relations、`docs/typed-schema-design.md`)。
- `web/` + `server.py` 人間用ダッシュボード（DB を read-only 参照、proposals/staging/weak のレビュー UI）。あなたは関与しない。
- `tools/` 決定的スクリプト。突合・矛盾管理・schema 進化・claims 状態遷移はここの責務。

## 2. あなたが担当するタスク

- **グラフ抽出** (`prompts/extract_graph.txt`): typed JSON で entities (proposed_type: person/organization/product/project) + relations (proposed_junction: employment/manufacturing/org_hierarchy) + weak_relations。confidence ≤ 0.95 (人手 1.0 と区別)。starter に当てはまらない型は `new_table_proposal` を埋めて出す。
- **text2sql** (`prompts/text2sql.txt`): typed schema 用 SELECT 1 文。`*_claims.value` への range/比較/型変換は禁止 (R1)、`*_claims`/`*_existence_claims` 参照は `status` フィルタ必須 (R2)。`tools/sql_linter.py` で AST 検査。
- **回答生成** (`prompts/answer.txt`): 構造化結果 + 文書抜粋から出典付きで簡潔に。

## 3. 突合・矛盾の扱い

- 正規化 (norm_key) は決定的 (`tools/normalize.py`)。あなたは突合ロジックに介入しない。
- canonical 表は「最高 confidence active claim」を反映するマテリアライズドビュー。新 claim の confidence と既存 active を比較し:
  - δ=0.1 以上高ければ canonical UPDATE + 旧 active を `superseded` に降格
  - δ 以内なら両 `conflicted` + `conflict_groups` 起票
  - δ 以上低ければ新 claim を `superseded` で記録
- 人手 verdict は `confidence=1.0` の通常 claim として `__human__` document (id=1) を参照して INSERT。
- 矛盾は消さない。`*_claims` / `*_existence_claims` に全主張を保持し、人間がレビューで裁定する。

## 4. 質問対応の流れ

1. text2sql で SELECT 1 文を生成し、`sql_linter.lint_or_raise` で R1/R2 を検査。
2. linter 違反・実行エラー・0 行のいずれかなら、column_hints + 違反内容を注入して 1 回 retry。
3. それでも駄目なら FTS のみで継続し、`answer.txt` で出典付き回答。

## 5. ビジネス/チーム運用

- リポジトリは git。取り込み後のレビュー要約を人間が確認し PR でマージ。
- 機密の扱いは人間の指示に従う。判断に迷えば止まり `[要確認]` を残す。
