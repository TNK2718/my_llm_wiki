# LLM Wiki — ナレッジグラフ版（ローカル SLM / Ollama）

テキスト Wiki ではなく、**取り込み時に実体・関係・属性を抽出して DB に格納**し、
**重複・矛盾をルール中心（曖昧時のみ SLM）で管理**、
**検索時はテンプレート優先＋ text2sql で構造化取得 → 出典付き回答**するパターンの雛形。
ビジネス/チーム用途・ローカル SLM 駆動・docling 抽出を前提。

## 役割分担（弱い SLM で破綻させない要点）

| 仕事 | 担当 |
|---|---|
| テキスト抽出 (pdf/docx/pptx→docling, xlsx→pandas) | `extract.py` |
| 取り込み全体制御・分割 | `ingest.py` |
| 正規化・候補絞り込み・矛盾検出 | `db.py`（決定的ルール） |
| 索引/射影ページ生成 | `project.py`（DB→markdown, モデル不介入） |
| 健康診断（矛盾・孤立・統計） | `lint.py` |
| 検索テンプレート・SQL 検証・自己修復 | `query.py` |
| ダッシュボード API + 静的配信 | `server.py`（FastAPI, DB は read-only） |
| 要約・グラフ抽出・同一判定・SQL・回答生成 | SLM（1コール1タスク） |

**正本は `tools/kg.sqlite`。** ダッシュボードはそこを read-only で参照。`/api/ask` のみ Ollama を使用。

## セットアップ

```bash
ollama pull qwen2.5:14b
uv sync   # pyproject.toml から venv 作成 & 依存インストール
# 以降の python コマンドは `uv run python ...` で実行（venv を自動有効化）
# DB はスクリプト初回実行時に schema.sql から自動作成
```

`tools/config.py` でモデル名・`NUM_CTX`・正規化除去語・関数的述語・突合しきい値・HOST/PORT を調整。

## フロー

```bash
# 取り込み
cp 提案書.pdf 議事録.docx 実績.xlsx raw/sources/
python tools/extract.py
python tools/ingest.py raw/extracted/提案書.md      # → DB へ。矛盾はレビュー要約に出る

# ダッシュボード（推奨）
python tools/server.py                              # http://127.0.0.1:8000
#   Overview / Entities / Graph / Conflicts / Ask を1画面で

# 任意: Obsidian で見たい場合の read-only 射影
python tools/project.py

# 健康診断（CLI）
python tools/lint.py

# 質問（CLI でも可。ダッシュボードの Ask と同じパイプライン）
python tools/query.py "Acme の CEO は誰？"
python tools/query.py --sql "解約率の推移を出して"
```

## ダッシュボード

`server.py` が DB と検索パイプラインを HTTP 化し、単一 HTML（`web/index.html`）を配信します。
ビルド工程なし。画面構成:

- **Overview**: KPI・実体タイプ分布・最近の取り込み文書
- **Entities**: 検索/タイプ絞り込み → 実体詳細（属性・関係・別名、矛盾フラグ付き、リンク辿り）
- **Graph**: Cytoscape による関係グラフ。ノードクリックで近傍展開、矛盾エッジは赤
- **Conflicts**: 関係/属性の矛盾グループを出典付きで一覧（レビュー裁定の入口）
- **Ask**: 質問 → 回答＋経路バッジ＋生成 SQL＋構造化結果＋関連文書（出典の透明性を担保）

フォント（IBM Plex）と Cytoscape は CDN 参照。**完全オフラインにする場合**は両者をローカルに vendor して `index.html` の参照先を差し替えてください。

## 重複・矛盾の方針

- **正規化（決定的）**: NFKC・小文字化・法人格/敬称除去・記号除去で `norm_key` を生成。完全一致は即同一。
- **候補絞り込み（決定的）**: 同 type 内でトリグラム Jaccard 類似がしきい値以上のものだけ候補に。
- **同一判定（SLM, 曖昧時のみ）**: 候補ペアに `same/different/unsure` を1語で。閾値で件数を絞るので呼び出しは少数。
- **非破壊**: 矛盾は上書きせず両論を `status=conflicted` ＋ `conflict_group` で保持。関数的述語（`config.FUNCTIONAL_PREDICATES`）と属性値の不一致をルール検出。裁定は人間が PR レビューで行う。

## text2sql の安全設計（小型モデル対策）

- 主経路はパラメータ化テンプレート（実体名一致で関係/属性を取得）。生成 SQL はフォールバック。
- read-only 接続・`SELECT` 限定・複文/DDL/PRAGMA 禁止・`LIMIT` 強制・`EXPLAIN` 構文検証。
- 失敗時はエラー文を食わせて **1回だけ自己修復**。それでも駄目ならテンプレート＋全文検索のみで回答。

## 既知の論点 / 今後

- 画像はスコープ外（docling がプレースホルダ化）。将来 OCR/画像説明を別パスで。
- Excel は表 markdown 化。複雑シートは抽出が荒くなる → シート単位推奨。
- グラフが大きくなり SQL 表現力が要るなら DuckDB / Kuzu への移行余地（テンプレート層はそのまま流用可）。
- `qmd` 等の高度検索を文書側に足すと全文検索の質が上がる。
