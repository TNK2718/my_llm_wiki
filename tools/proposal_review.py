"""schema_proposals 承認パイプライン (docs/typed-schema-design.md レビューフロー §3).

1. AST 事前検証 — sqlglot で kind 別の許容形式と一致するか検査
2. dry-run — 一時 DB に exec して構文・既存制約衝突を検出
3. diff — sqlite_schema 差分
4. apply — 本 DB に DDL 実行 + schema_migrations に 1 行 + applied_migration をリンク
"""
from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

import config


# 全 kind 共通の禁止: DROP/PRAGMA/ATTACH/DETACH/VACUUM/REINDEX/CREATE TRIGGER/CREATE VIEW/CTAS/DML
COMMON_FORBIDDEN_TOKENS = (
    "DROP", "PRAGMA", "ATTACH", "DETACH", "VACUUM", "REINDEX",
    "CREATE TRIGGER", "CREATE VIEW", "AS SELECT",
    "INSERT", "UPDATE", "DELETE", "REPLACE",
)


@dataclass
class ASTValidationResult:
    ok: bool
    reason: str | None = None


@dataclass
class DryRunResult:
    ok: bool
    reason: str | None = None
    diff: list[dict] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(sql: str) -> str:
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].strip()
    return s


def validate_ast(kind: str, target_table: str | None, proposed_ddl: str) -> ASTValidationResult:
    """kind 別の許容形式と AST を突き合わせる。違反は即 reject。"""
    if kind in ("split", "merge"):
        # Phase 0 では構造的検証諦め、co-approve 必須経路へ
        return ASTValidationResult(False, "split/merge requires manual dry-run (requires_manual_dry_run)")
    if not proposed_ddl.strip():
        return ASTValidationResult(False, "empty DDL")
    # 共通禁止トークンを大文字でラフにチェック (sqlglot AST より早く落とす)
    upper = proposed_ddl.upper()
    for tok in COMMON_FORBIDDEN_TOKENS:
        if tok in upper:
            return ASTValidationResult(False, f"forbidden token in DDL: {tok}")

    try:
        stmts = sqlglot.parse(proposed_ddl, dialect="sqlite")
    except Exception as e:  # noqa: BLE001
        return ASTValidationResult(False, f"parse error: {e}")
    stmts = [s for s in stmts if s is not None]
    if not stmts:
        return ASTValidationResult(False, "no statements parsed")

    if kind == "new_table":
        return _validate_new_table(target_table, stmts)
    if kind == "new_column":
        return _validate_new_column(target_table, stmts)
    if kind == "rename":
        return _validate_rename(target_table, stmts)
    return ASTValidationResult(False, f"unknown kind: {kind}")


def _table_name(node: exp.Expression) -> str | None:
    t = node.find(exp.Table)
    if t is None:
        return None
    return (t.this.name if t.this else t.name).lower()


def _validate_new_table(target_table: str | None, stmts: list) -> ASTValidationResult:
    """ちょうど 1 本の CREATE TABLE <target> + 0+ 本の CREATE INDEX ... ON <target>。"""
    if not target_table:
        return ASTValidationResult(False, "new_table requires target_table")
    create_table = None
    create_index = []
    for s in stmts:
        if isinstance(s, exp.Create):
            kind = (s.args.get("kind") or "").upper()
            if s.args.get("exists"):
                return ASTValidationResult(False, "IF NOT EXISTS is not allowed in new_table")
            if kind == "TABLE":
                if create_table is not None:
                    return ASTValidationResult(False, "multiple CREATE TABLE not allowed")
                # CREATE TABLE ... AS SELECT は禁止
                if s.args.get("expression") is not None and isinstance(s.expression, exp.Select):
                    return ASTValidationResult(False, "CREATE TABLE AS SELECT not allowed")
                tbl = _table_name(s)
                if tbl != target_table.lower():
                    return ASTValidationResult(False, f"CREATE TABLE name {tbl!r} != target_table {target_table!r}")
                create_table = s
            elif kind == "INDEX":
                tbl = _table_name(s)
                if tbl != target_table.lower():
                    return ASTValidationResult(False, f"CREATE INDEX on {tbl!r} != target_table {target_table!r}")
                create_index.append(s)
            else:
                return ASTValidationResult(False, f"unsupported CREATE kind: {kind}")
        else:
            return ASTValidationResult(False, f"unsupported top-level: {type(s).__name__}")
    if create_table is None:
        return ASTValidationResult(False, "missing CREATE TABLE")
    return ASTValidationResult(True)


def _validate_new_column(target_table: str | None, stmts: list) -> ASTValidationResult:
    """ちょうど 1 本の ALTER TABLE <target> ADD COLUMN + 0+ 本の CREATE INDEX ... ON <target>。"""
    if not target_table:
        return ASTValidationResult(False, "new_column requires target_table")
    alter = None
    indexes = []
    for s in stmts:
        if isinstance(s, exp.Alter):
            kind = (s.args.get("kind") or "").upper()
            if kind not in ("TABLE",):
                return ASTValidationResult(False, f"unsupported ALTER kind: {kind}")
            tbl = _table_name(s)
            if tbl != target_table.lower():
                return ASTValidationResult(False, f"ALTER TABLE {tbl!r} != target_table {target_table!r}")
            actions = s.args.get("actions") or []
            for a in actions:
                if isinstance(a, exp.AlterColumn):
                    return ASTValidationResult(False, "ALTER COLUMN variant not allowed")
                if isinstance(a, exp.RenameColumn) or isinstance(a, exp.AlterRename):
                    return ASTValidationResult(False, "rename variant must use kind='rename'")
                # ADD COLUMN: sqlglot は ColumnDef を actions に直接入れる
                if not isinstance(a, exp.ColumnDef):
                    # 他の variant は禁止
                    return ASTValidationResult(False, f"unsupported ALTER action: {type(a).__name__}")
            if alter is not None:
                return ASTValidationResult(False, "multiple ALTER TABLE not allowed")
            alter = s
        elif isinstance(s, exp.Create):
            kind = (s.args.get("kind") or "").upper()
            if kind != "INDEX":
                return ASTValidationResult(False, f"unsupported CREATE in new_column: {kind}")
            tbl = _table_name(s)
            if tbl != target_table.lower():
                return ASTValidationResult(False, f"CREATE INDEX on {tbl!r} != target_table {target_table!r}")
            indexes.append(s)
        else:
            return ASTValidationResult(False, f"unsupported top-level: {type(s).__name__}")
    if alter is None:
        return ASTValidationResult(False, "missing ALTER TABLE ADD COLUMN")
    return ASTValidationResult(True)


def _validate_rename(target_table: str | None, stmts: list) -> ASTValidationResult:
    """ちょうど 1 本の ALTER TABLE <src> RENAME TO <dst> (target_table が <src>)."""
    if not target_table:
        return ASTValidationResult(False, "rename requires target_table (= source name)")
    if len(stmts) != 1:
        return ASTValidationResult(False, "rename allows exactly 1 statement")
    s = stmts[0]
    if not isinstance(s, exp.Alter):
        return ASTValidationResult(False, f"expected ALTER, got {type(s).__name__}")
    tbl = _table_name(s)
    if tbl != target_table.lower():
        return ASTValidationResult(False, f"ALTER TABLE {tbl!r} != target_table {target_table!r}")
    actions = s.args.get("actions") or []
    if len(actions) != 1 or not isinstance(actions[0], exp.AlterRename):
        return ASTValidationResult(False, "expected exactly 1 RENAME TO action")
    return ASTValidationResult(True)


def dry_run(proposed_ddl: str, source_db_path: Path | None = None) -> DryRunResult:
    """AST 検証通過後の DDL を一時 DB のコピーで実行し、衝突を検出。

    source_db_path に既存 DB のパスを渡すと、その schema を持った一時 DB を作って
    DDL を流し、適用前後の sqlite_schema 差分を返す。
    """
    src = source_db_path or config.KG_DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "dryrun.sqlite"
            if src.exists():
                # schema のみコピー (データ込みは時間がかかるので omit)
                src_db = sqlite3.connect(src)
                rows = src_db.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE type IN ('table','index','trigger','view') AND sql IS NOT NULL"
                ).fetchall()
                src_db.close()
                dst_db = sqlite3.connect(tmp_path)
                for _type, name, sql in rows:
                    try:
                        dst_db.execute(sql)
                    except sqlite3.Error:
                        # bootstrap で重複する CREATE 等は無視
                        pass
                before = _snapshot_schema(dst_db)
                try:
                    dst_db.executescript(proposed_ddl)
                except sqlite3.Error as e:
                    dst_db.close()
                    return DryRunResult(False, f"dry-run failed: {e}", None)
                after = _snapshot_schema(dst_db)
                dst_db.close()
            else:
                dst_db = sqlite3.connect(tmp_path)
                before = {}
                try:
                    dst_db.executescript(proposed_ddl)
                except sqlite3.Error as e:
                    dst_db.close()
                    return DryRunResult(False, f"dry-run failed: {e}", None)
                after = _snapshot_schema(dst_db)
                dst_db.close()
        diff = _diff_schema(before, after)
        return DryRunResult(True, None, diff)
    except Exception as e:  # noqa: BLE001
        return DryRunResult(False, f"dry-run unexpected error: {e}", None)


def _snapshot_schema(db: sqlite3.Connection) -> dict[str, str]:
    rows = db.execute(
        "SELECT type || ':' || name AS k, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _diff_schema(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    out = []
    for k, sql in after.items():
        if k not in before:
            out.append({"change": "added", "object": k, "sql": sql})
        elif before[k] != sql:
            out.append({"change": "modified", "object": k, "before": before[k], "after": sql})
    for k in before:
        if k not in after:
            out.append({"change": "removed", "object": k, "before": before[k]})
    return out


def apply(
    db: sqlite3.Connection,
    proposal_id: int,
    proposed_ddl: str,
    name: str,
) -> int:
    """本 DB に DDL を実行し、schema_migrations に行を追加。version は max+1。"""
    cur = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    next_v = (cur[0] or 0) + 1
    with db:
        db.executescript(proposed_ddl)
        db.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
            (next_v, name, _now()),
        )
        db.execute(
            "UPDATE schema_proposals SET applied_migration=?, status='approved', "
            "decided_at=? WHERE id=?",
            (next_v, _now(), proposal_id),
        )
    return next_v
