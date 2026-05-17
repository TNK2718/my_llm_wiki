# Future Work: Task / TODO 管理 設計メモ

> Status: **未着手**（将来課題）
> Last updated: 2026-05-17

## 背景

TODO / アクションアイテム管理は本リポジトリの **主要ユースケース**。ただし以下の理由で既存 KG (`entities`/`relations`/`facts`) には載せない:

- ライフサイクルが速い（open → done が数日）→ KG の長期蓄積モデルと合わない
- 件数が爆発する（議事録1本で数十件）→ entity 化すると検索ノイズになる
- 状態・期日・担当・優先度が多次元 → fact-per-attribute では構造クエリ不能

代わりに **専用テーブルを追加し、KG とは FK で繋ぐ** 二層構成にする。

## スキーマ追加（4テーブル）

```sql
-- メイン
CREATE TABLE tasks (
  id           INTEGER PRIMARY KEY,
  title        TEXT NOT NULL,
  description  TEXT,
  status       TEXT NOT NULL DEFAULT 'open',  -- open/in_progress/done/cancelled
  priority     TEXT,                           -- high/med/low（任意）
  due_date     TEXT,                           -- YYYY-MM-DD
  assignee_id  INTEGER REFERENCES entities(id),   -- 担当者 (type=person)
  project_id   INTEGER REFERENCES entities(id),   -- 主プロジェクト (type=project)
  document_id  INTEGER REFERENCES documents(id),  -- 出典文書
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  closed_at    TEXT
);

-- 依存関係（A は B が終わるまで開始できない）
CREATE TABLE task_deps (
  task_id     INTEGER NOT NULL REFERENCES tasks(id),
  depends_on  INTEGER NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on)
);

-- 多対多の関連実体（担当・主プロジェクト以外の言及対象）
CREATE TABLE task_entities (
  task_id    INTEGER NOT NULL REFERENCES tasks(id),
  entity_id  INTEGER NOT NULL REFERENCES entities(id),
  role       TEXT NOT NULL,    -- 'about'/'blocks'/'product'/'org' 等
  PRIMARY KEY (task_id, entity_id, role)
);

-- 全文検索
CREATE VIRTUAL TABLE tasks_fts USING fts5(title, description, content=tasks);

CREATE INDEX ix_task_status_due  ON tasks(status, due_date);
CREATE INDEX ix_task_assignee    ON tasks(assignee_id);
CREATE INDEX ix_task_project     ON tasks(project_id);
CREATE INDEX ix_task_dep_target  ON task_deps(depends_on);
```

設計ポイント:
- 既存 `entities` / `documents` には触らない（既存 KG 資産を壊さない）
- 担当 / 主プロジェクト / 出典の3つの FK で KG・出典に直接繋がる
- 複数 person 担当・複数 product 関連は `task_entities` で吸収
- 依存グラフは SQLite の再帰 CTE で辿れる
- FTS5 で「○○について」キーワード検索可能

## 典型クエリ

```sql
-- 山田の未完了タスク（期日順）
SELECT t.id, t.title, t.due_date, p.canonical_name project
FROM tasks t
LEFT JOIN entities p ON t.project_id = p.id
JOIN entities a ON t.assignee_id = a.id
WHERE a.canonical_name = '山田 太郎' AND t.status != 'done'
ORDER BY t.due_date;

-- 依存チェーン（このタスクを進めるのに残ってる前提）
WITH RECURSIVE chain(id, depth) AS (
  SELECT depends_on, 1 FROM task_deps WHERE task_id = ?
  UNION
  SELECT td.depends_on, c.depth+1
  FROM task_deps td JOIN chain c ON td.task_id = c.id WHERE c.depth < 10
)
SELECT t.* FROM chain JOIN tasks t ON chain.id=t.id WHERE t.status != 'done';
```

## 取り込みパイプライン拡張

新規 prompt `prompts/extract_tasks.txt`（議事録・action item 系）:

```
出力: {"tasks": [{
  "title": "...",
  "description": "...",
  "assignee_name": "山田太郎",
  "project_name": "SmartScan改修",
  "due_date": "2026-06-30",
  "priority": "high",
  "depends_on_titles": [...]
}, ...]}
```

`ingest.py` を以下の流れに拡張:

1. 既存パス（要約 → extract_graph → 突合）で entities/relations/facts を DB に入れる
2. 続けて `extract_tasks` prompt を呼ぶ
3. 各 task の `assignee_name` `project_name` を **既存 dedup ロジックで entity_id に解決**（KG の norm_key / aliases 資産を再利用）
4. `tasks` INSERT、`task_deps` / `task_entities` を埋める

## UI 拡張

ダッシュボードに **Tasks タブ** を追加:

- フィルタ: status / assignee / project / due_date 範囲
- 行クリック → 詳細（依存・関連実体・出典文書リンク）
- **Dependency view**: cytoscape 既存実装を流用してタスク依存 DAG を可視化
- Entities タブの実体詳細にも「このプロジェクトに紐づく未完了タスク N 件」を表示

## 段階導入

1. **Phase 1**: スキーマ追加だけ。CLI で `tasks` テーブルに手動 INSERT → 検索/依存クエリの感触を掴む
2. **Phase 2**: ingest 拡張で議事録系から自動抽出
3. **Phase 3**: ダッシュボードに Tasks タブ + 依存ビュー

## スコープ感

- スキーマ追加: 約30行 SQL、`schema.sql` 追記のみ
- ingest 拡張: 新 prompt 1本 + `ingest.py` に40〜80行追加
- API: `/api/tasks` `/api/tasks/{id}` `/api/task_deps/{id}` 等
- UI: タスク一覧 + 依存グラフで HTML/JS 200〜400 行

## 議論済みの判断

- **task を `entities` に入れない**理由: ライフサイクル / 件数 / 多次元属性が KG モデルに合わない
- **task を `facts` で表現しない**理由: 状態・期日・担当・優先度の多次元クエリができない
- **専用テーブル + FK 連携**: KG の正規化・出典管理資産を再利用しつつ、tasks ドメインの構造化を独立に進化させられる
