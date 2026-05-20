"""schema_proposal AST 事前検証と dry-run のテスト."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import proposal_review as pr  # noqa: E402


# ---------- AST validation: new_table ----------

def test_new_table_ok():
    ddl = "CREATE TABLE algorithm (id INTEGER PRIMARY KEY, name TEXT)"
    r = pr.validate_ast("new_table", "algorithm", ddl)
    assert r.ok, r.reason


def test_new_table_with_index_ok():
    ddl = (
        "CREATE TABLE algorithm (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE INDEX ix_algo_name ON algorithm(name);"
    )
    r = pr.validate_ast("new_table", "algorithm", ddl)
    assert r.ok, r.reason


def test_new_table_if_not_exists_rejected():
    ddl = "CREATE TABLE IF NOT EXISTS algorithm (id INTEGER PRIMARY KEY)"
    r = pr.validate_ast("new_table", "algorithm", ddl)
    assert not r.ok


def test_new_table_ctas_rejected():
    ddl = "CREATE TABLE algorithm AS SELECT * FROM person"
    r = pr.validate_ast("new_table", "algorithm", ddl)
    assert not r.ok


def test_new_table_drop_rejected():
    ddl = "DROP TABLE person; CREATE TABLE algorithm (id INTEGER PRIMARY KEY)"
    r = pr.validate_ast("new_table", "algorithm", ddl)
    assert not r.ok


def test_new_table_name_mismatch_rejected():
    ddl = "CREATE TABLE wrong_name (id INTEGER PRIMARY KEY)"
    r = pr.validate_ast("new_table", "algorithm", ddl)
    assert not r.ok


def test_new_table_trigger_rejected():
    ddl = (
        "CREATE TABLE algorithm (id INTEGER PRIMARY KEY);"
        "CREATE TRIGGER t AFTER INSERT ON algorithm BEGIN SELECT 1; END;"
    )
    r = pr.validate_ast("new_table", "algorithm", ddl)
    assert not r.ok


def test_new_table_pragma_rejected():
    ddl = "CREATE TABLE algorithm (id INTEGER PRIMARY KEY); PRAGMA foreign_keys=OFF;"
    r = pr.validate_ast("new_table", "algorithm", ddl)
    assert not r.ok


def test_new_table_dml_rejected():
    ddl = "CREATE TABLE algorithm (id INTEGER PRIMARY KEY); INSERT INTO algorithm VALUES(1);"
    r = pr.validate_ast("new_table", "algorithm", ddl)
    assert not r.ok


# ---------- AST validation: new_column ----------

def test_new_column_ok():
    ddl = "ALTER TABLE person ADD COLUMN gender TEXT"
    r = pr.validate_ast("new_column", "person", ddl)
    assert r.ok, r.reason


def test_new_column_with_index_ok():
    ddl = (
        "ALTER TABLE person ADD COLUMN gender TEXT;"
        "CREATE INDEX ix_person_gender ON person(gender);"
    )
    r = pr.validate_ast("new_column", "person", ddl)
    assert r.ok, r.reason


def test_new_column_multiple_alters_rejected():
    ddl = "ALTER TABLE person ADD COLUMN a TEXT; ALTER TABLE person ADD COLUMN b TEXT;"
    r = pr.validate_ast("new_column", "person", ddl)
    assert not r.ok


def test_new_column_wrong_table_rejected():
    ddl = "ALTER TABLE organization ADD COLUMN gender TEXT"
    r = pr.validate_ast("new_column", "person", ddl)
    assert not r.ok


# ---------- AST validation: rename ----------

def test_rename_ok():
    ddl = "ALTER TABLE person RENAME TO human"
    r = pr.validate_ast("rename", "person", ddl)
    assert r.ok, r.reason


def test_rename_multiple_rejected():
    ddl = "ALTER TABLE person RENAME TO human; ALTER TABLE organization RENAME TO co;"
    r = pr.validate_ast("rename", "person", ddl)
    assert not r.ok


# ---------- AST validation: split / merge ----------

def test_split_requires_manual_dry_run():
    ddl = "CREATE TABLE a (id INT); CREATE TABLE b (id INT); DROP TABLE old;"
    r = pr.validate_ast("split", "old", ddl)
    assert not r.ok
    assert "manual" in (r.reason or "").lower()


# ---------- dry-run ----------

def test_dry_run_on_new_db(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "KG_DB", tmp_path / "kg.sqlite")
    ddl = "CREATE TABLE algorithm (id INTEGER PRIMARY KEY, name TEXT)"
    r = pr.dry_run(ddl)
    assert r.ok, r.reason
    assert any(d["change"] == "added" and d["object"] == "table:algorithm" for d in r.diff)


def test_dry_run_against_existing(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "KG_DB", tmp_path / "kg.sqlite")
    import db as kg
    conn = kg.connect()
    conn.close()
    # 既存 person 表に gender 列を足す DDL
    ddl = "ALTER TABLE person ADD COLUMN gender TEXT"
    r = pr.dry_run(ddl)
    assert r.ok, r.reason


def test_dry_run_conflict_detected(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "KG_DB", tmp_path / "kg.sqlite")
    import db as kg
    conn = kg.connect()
    conn.close()
    # 既存テーブル名と衝突
    ddl = "CREATE TABLE person (id INTEGER PRIMARY KEY)"
    r = pr.dry_run(ddl)
    assert not r.ok
