"""text2sql linter R1/R2 のテスト (docs/typed-schema-design.md §8)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import sql_linter  # noqa: E402


# ---------- R1: claims.value への range / 比較 / 型変換 禁止 ----------

def test_r1_range_on_canonical_ok():
    sql = "SELECT canonical_name FROM person WHERE birth_date >= '1990-01-01'"
    assert sql_linter.lint(sql) == []


def test_r1_range_on_claims_value_blocked():
    sql = "SELECT * FROM person_claims pc WHERE pc.value > '1990-01-01' AND pc.status='active'"
    v = sql_linter.lint(sql)
    assert any(x.rule == "R1" for x in v)


def test_r1_between_on_claims_value_blocked():
    sql = "SELECT * FROM organization_claims oc WHERE oc.value BETWEEN '2000' AND '2010' AND oc.status='active'"
    v = sql_linter.lint(sql)
    assert any(x.rule == "R1" for x in v)


def test_r1_like_wildcard_on_claims_value_blocked():
    sql = "SELECT * FROM person_claims pc WHERE pc.value LIKE '%foo%' AND pc.status='active'"
    v = sql_linter.lint(sql)
    assert any(x.rule == "R1" for x in v)


def test_r1_like_no_wildcard_on_claims_value_ok():
    sql = "SELECT * FROM person_claims pc WHERE pc.value LIKE 'foo' AND pc.status='active'"
    assert sql_linter.lint(sql) == []


def test_r1_cast_on_claims_value_blocked():
    sql = "SELECT * FROM person_claims pc WHERE CAST(pc.value AS INTEGER) > 1990 AND pc.status='active'"
    v = sql_linter.lint(sql)
    assert any(x.rule == "R1" for x in v)


def test_r1_date_func_on_claims_value_blocked():
    sql = "SELECT * FROM person_claims pc WHERE DATE(pc.value) >= '1990-01-01' AND pc.status='active'"
    v = sql_linter.lint(sql)
    assert any(x.rule == "R1" for x in v)


def test_r1_equality_on_claims_value_ok():
    sql = "SELECT * FROM person_claims pc WHERE pc.value = '1990-01-01' AND pc.status='active'"
    assert sql_linter.lint(sql) == []


# ---------- R2: claims アクセスには status フィルタ必須 ----------

def test_r2_missing_status_blocked():
    sql = "SELECT value FROM person_claims WHERE person_id=1 AND column_name='birth_date'"
    v = sql_linter.lint(sql)
    assert any(x.rule == "R2" for x in v)


def test_r2_status_in_where_ok():
    sql = "SELECT value FROM person_claims WHERE person_id=1 AND status='active'"
    assert sql_linter.lint(sql) == []


def test_r2_status_in_list_ok():
    sql = "SELECT value FROM person_claims WHERE status IN ('active','superseded')"
    assert sql_linter.lint(sql) == []


def test_r2_existence_claims_need_status():
    sql = (
        "SELECT COUNT(*) FROM employment_existence_claims ec "
        "WHERE ec.employment_id=1"
    )
    v = sql_linter.lint(sql)
    assert any(x.rule == "R2" for x in v)


def test_r2_aggregate_status_exempt():
    """SELECT status, COUNT(*) FROM ... GROUP BY status は status フィルタ免除."""
    sql = "SELECT status, COUNT(*) FROM person_claims GROUP BY status"
    assert sql_linter.lint(sql) == []


def test_r2_join_alias_status_filter_ok():
    sql = (
        "SELECT c.value FROM employment_claims c JOIN employment e ON e.id=c.employment_id "
        "WHERE c.column_name='role' AND c.status='active'"
    )
    assert sql_linter.lint(sql) == []


def test_r2_join_alias_status_missing_blocked():
    sql = (
        "SELECT c.value FROM employment_claims c JOIN employment e ON e.id=c.employment_id "
        "WHERE c.column_name='role'"
    )
    v = sql_linter.lint(sql)
    assert any(x.rule == "R2" for x in v)


# ---------- 受け入れ基準: 正当な claims クエリは通る ----------

def test_role_history_query_passes():
    sql = (
        "SELECT c.value, c.created_at, c.document_id FROM employment_claims c "
        "JOIN employment e ON e.id=c.employment_id "
        "WHERE e.person_id=1 AND c.column_name='role' AND c.status IN ('active','superseded') "
        "ORDER BY c.created_at"
    )
    assert sql_linter.lint(sql) == []


def test_evidence_count_query_passes():
    sql = (
        "SELECT COUNT(DISTINCT document_id) FROM person_claims "
        "WHERE person_id=1 AND column_name='birth_date' AND status='active'"
    )
    assert sql_linter.lint(sql) == []


def test_current_conflicts_query_passes():
    sql = "SELECT * FROM person_claims WHERE status='conflicted'"
    assert sql_linter.lint(sql) == []


# ---------- autofix: 決定論的に status='active' を注入 ----------

def test_autofix_r2_no_alias_adds_status():
    sql = "SELECT value FROM person_claims WHERE person_id=1 AND column_name='birth_date'"
    fixed, injected = sql_linter.autofix(sql)
    assert injected == ["person_claims"]
    assert sql_linter.lint(fixed) == []


def test_autofix_r2_with_alias():
    sql = "SELECT pc.value FROM person_claims pc WHERE pc.person_id=1"
    fixed, injected = sql_linter.autofix(sql)
    assert injected == ["pc"]
    assert sql_linter.lint(fixed) == []


def test_autofix_r2_join_with_alias():
    sql = (
        "SELECT c.value FROM employment_claims c "
        "JOIN employment e ON e.id=c.employment_id "
        "WHERE c.column_name='role'"
    )
    fixed, injected = sql_linter.autofix(sql)
    assert injected == ["c"]
    assert sql_linter.lint(fixed) == []


def test_autofix_r2_multiple_claims():
    sql = (
        "SELECT pc.value, oc.value FROM person_claims pc "
        "JOIN organization_claims oc ON oc.organization_id = pc.person_id"
    )
    fixed, injected = sql_linter.autofix(sql)
    assert set(injected) == {"pc", "oc"}
    assert sql_linter.lint(fixed) == []


def test_autofix_r2_aggregate_exempt_no_change():
    sql = "SELECT status, COUNT(*) FROM person_claims GROUP BY status"
    fixed, injected = sql_linter.autofix(sql)
    assert injected == []
    assert sql_linter.lint(fixed) == []


def test_autofix_r2_status_present_no_change():
    sql = "SELECT value FROM person_claims WHERE person_id=1 AND status='active'"
    _fixed, injected = sql_linter.autofix(sql)
    assert injected == []


def test_autofix_r2_status_in_list_preserved():
    sql = (
        "SELECT c.value FROM employment_claims c "
        "WHERE c.column_name='role' AND c.status IN ('active','superseded')"
    )
    fixed, injected = sql_linter.autofix(sql)
    assert injected == []
    # 既存の status IN (...) が autofix で壊れていない
    assert "active" in fixed and "superseded" in fixed


def test_autofix_or_raise_r1_still_raises():
    # R1 は autofix されず、autofix 後の lint で raise する
    sql = (
        "SELECT * FROM person_claims pc WHERE pc.value > '1990-01-01' "
        "AND pc.status='active'"
    )
    with pytest.raises(ValueError, match="R1"):
        sql_linter.autofix_or_raise(sql)


def test_autofix_parse_failure_passthrough():
    sql = "this is not sql at all"
    fixed, injected = sql_linter.autofix(sql)
    assert fixed == sql
    assert injected == []


def test_autofix_or_raise_clean_sql_idempotent():
    sql = "SELECT canonical_name FROM person WHERE birth_date >= '1990-01-01' LIMIT 50"
    out = sql_linter.autofix_or_raise(sql)
    assert sql_linter.lint(out) == []


def test_autofix_typed_text2sql_failure_recovered():
    # 直近 eval で実際に落ちた SQL (i127-9285 query gold)
    sql = (
        "SELECT T2.value FROM product_claims AS T2 "
        "JOIN product_aliases AS T1 ON T1.product_id = T2.product_id "
        "WHERE T1.alias = 'IBM Bob Enterprise' AND T2.column_name = 'quota_unit_name'"
    )
    fixed = sql_linter.autofix_or_raise(sql)
    assert sql_linter.lint(fixed) == []
    assert "active" in fixed.lower()
