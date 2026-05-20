"""text2sql 生成 SQL の R1/R2 linter (docs/typed-schema-design.md §8)。

R1: *_claims.value への range/比較 predicate と型変換関数を禁止。許可: =, IN, IS [NOT] NULL.
R2: *_claims / *_existence_claims を FROM/JOIN するとき、その別名に対する status フィルタ必須。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp


CLAIM_SUFFIXES = ("_claims", "_existence_claims")

R1_FORBIDDEN_OPS = (exp.LT, exp.LTE, exp.GT, exp.GTE, exp.Between)
# CAST(value AS INTEGER), DATE(value), JULIANDAY(value), STRFTIME(...), TIME(...), DATETIME(...)
R1_FORBIDDEN_FUNCS = {"DATE", "JULIANDAY", "STRFTIME", "TIME", "DATETIME"}


@dataclass
class LintViolation:
    rule: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.message}"


def _is_claims_table(name: str | None) -> bool:
    if not name:
        return False
    name = name.lower()
    return any(name.endswith(suf) for suf in CLAIM_SUFFIXES)


def _table_aliases(tree: exp.Expression) -> dict[str, str]:
    """{alias_or_name: real_table_name} を返す。alias なしは name 自身を key にする。"""
    out: dict[str, str] = {}
    for t in tree.find_all(exp.Table):
        name = (t.this and t.this.name) or t.name
        if not name:
            continue
        alias = t.alias_or_name
        out[alias.lower()] = name.lower()
    return out


def _column_table(col: exp.Column, aliases: dict[str, str]) -> str | None:
    """Column が指す real table name を解決。table 修飾が無ければ None。"""
    if col.table:
        return aliases.get(col.table.lower())
    return None


def _check_r1(tree: exp.Expression, aliases: dict[str, str]) -> list[LintViolation]:
    out: list[LintViolation] = []
    # 比較演算子の左右が *_claims.value なら NG
    for op_cls in R1_FORBIDDEN_OPS:
        for node in tree.find_all(op_cls):
            if op_cls is exp.Between:
                sides = [node.this] + list(node.args.get("expressions", []) or [])
                # BETWEEN は this BETWEEN low AND high。low/high は args.get('low')/('high')
                low = node.args.get("low")
                high = node.args.get("high")
                if low is not None: sides.append(low)
                if high is not None: sides.append(high)
            else:
                sides = [node.this, node.args.get("expression")]
            for side in sides:
                if isinstance(side, exp.Column) and side.name.lower() == "value":
                    tbl = _column_table(side, aliases)
                    if _is_claims_table(tbl):
                        out.append(LintViolation(
                            "R1",
                            f"forbidden range/compare on {tbl}.value (op={op_cls.__name__})",
                        ))
    # LIKE 'pattern%' / '%pattern' 系の wildcard
    for node in tree.find_all(exp.Like):
        col = node.this
        if isinstance(col, exp.Column) and col.name.lower() == "value":
            tbl = _column_table(col, aliases)
            if _is_claims_table(tbl):
                # LIKE 'foo' (wildcard なし) なら = と同等で許可、それ以外は禁止
                rhs = node.expression
                if isinstance(rhs, exp.Literal) and rhs.is_string and "%" in (rhs.this or ""):
                    out.append(LintViolation(
                        "R1",
                        f"forbidden LIKE wildcard on {tbl}.value",
                    ))
    # 型変換関数: CAST(value AS ...) / DATE(value) / JULIANDAY(value) ...
    for node in tree.find_all(exp.Cast):
        operand = node.this
        if isinstance(operand, exp.Column) and operand.name.lower() == "value":
            tbl = _column_table(operand, aliases)
            if _is_claims_table(tbl):
                out.append(LintViolation(
                    "R1",
                    f"forbidden CAST on {tbl}.value",
                ))
    for node in tree.find_all(exp.Func):
        fname = (node.sql_name() or "").upper()
        if fname in R1_FORBIDDEN_FUNCS:
            for arg in node.args.values():
                args_iter = arg if isinstance(arg, list) else [arg]
                for a in args_iter:
                    if isinstance(a, exp.Column) and a.name.lower() == "value":
                        tbl = _column_table(a, aliases)
                        if _is_claims_table(tbl):
                            out.append(LintViolation(
                                "R1",
                                f"forbidden {fname}() on {tbl}.value",
                            ))
    return out


def _aliases_in_status_predicates(tree: exp.Expression, claim_aliases: list[tuple[str, str]]) -> set[str]:
    """status predicate が当てられた alias_or_name の集合 (lowercase)。

    対象: `<alias>.status = ...` / `<alias>.status IN (...)` の右辺は問わない。
    table 修飾なしの `status = ...` は claim_aliases 内の全 claim 表に効くものとして扱う
    (SQLite の name resolution は曖昧なら error になるので、その時は実行で死ぬ)。
    """
    out: set[str] = set()
    for col in tree.find_all(exp.Column):
        if col.name.lower() != "status":
            continue
        parent = col.parent
        while parent is not None:
            if isinstance(parent, (exp.EQ, exp.NEQ, exp.In, exp.Is, exp.Like)):
                if col.table:
                    out.add(col.table.lower())
                else:
                    # 修飾なし → scope 内の全 claim 表に効く
                    for alias, _real in claim_aliases:
                        out.add(alias)
                break
            if isinstance(parent, (exp.Where, exp.Join, exp.Select)):
                break
            parent = parent.parent
    return out


def _claim_aliases(tree: exp.Expression) -> list[tuple[str, str]]:
    """SQL 中で参照される claims 別名のリスト [(alias_lower, real_table_lower), ...]"""
    out: list[tuple[str, str]] = []
    for t in tree.find_all(exp.Table):
        name = (t.this and t.this.name) or t.name
        if name and _is_claims_table(name):
            alias = t.alias_or_name.lower()
            out.append((alias, name.lower()))
    return out


def _check_r2(tree: exp.Expression) -> list[LintViolation]:
    out: list[LintViolation] = []
    aliases = _claim_aliases(tree)
    if not aliases:
        return out
    statused = _aliases_in_status_predicates(tree, aliases)
    for alias, real in aliases:
        # SELECT 内の status 集計のみ (e.g. SELECT status, COUNT(*) FROM ... GROUP BY status) は許可
        if _has_select_status_aggregate(tree, alias):
            continue
        if alias not in statused:
            out.append(LintViolation(
                "R2", f"missing status filter on {real} (alias={alias})",
            ))
    return out


def _has_select_status_aggregate(tree: exp.Expression, alias: str) -> bool:
    """SELECT <alias>.status, COUNT(*) ... GROUP BY <alias>.status のパターンを検出。"""
    select = tree.find(exp.Select) if not isinstance(tree, exp.Select) else tree
    if select is None:
        return False
    selects_status = any(
        isinstance(p, exp.Column) and p.name.lower() == "status"
        and (not p.table or p.table.lower() == alias)
        for p in select.expressions
    )
    group = select.args.get("group")
    grouped_status = False
    if group is not None:
        grouped_status = any(
            isinstance(g, exp.Column) and g.name.lower() == "status"
            and (not g.table or g.table.lower() == alias)
            for g in group.expressions
        )
    return selects_status and grouped_status


def lint(sql: str) -> list[LintViolation]:
    """SQL を parse して R1/R2 違反を返す。parse 失敗時は空 list (validate_sql 側で扱う)。"""
    try:
        tree = sqlglot.parse_one(sql, dialect="sqlite")
    except Exception:
        return []
    aliases = _table_aliases(tree)
    return _check_r1(tree, aliases) + _check_r2(tree)


def lint_or_raise(sql: str) -> None:
    violations = lint(sql)
    if violations:
        raise ValueError("text2sql linter R1/R2 violations: " + "; ".join(str(v) for v in violations))
