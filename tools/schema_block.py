"""text2sql プロンプトのスキーマブロックを DB introspection から動的生成する。

設計の詳細は docs/text2sql-dynamic-schema.md を参照。

- Core 12 表 (documents + 5 entity canonical + 6 relation junction) は常時注入。
- それ以外は質問との embedding cos-sim で top-K 選択 (select_fewshots と同形)。
- 列宣言は PRAGMA table_info から `name(col1 TYPE, col2 TYPE, ...)` 形式に整形。
"""
from __future__ import annotations

import functools
import sqlite3

import yaml

import config
import db as kg


CORE_TABLES = (
    "documents",
    # entity canonical (5)
    "person", "organization", "product", "project", "contract",
    # relation junction (6)
    "employment", "manufacturing", "org_hierarchy",
    "product_variant", "governance", "compliance",
)

# LLM が直接触る用途のない管理テーブル。提示するとノイズになるので除外。
INTERNAL_TABLES = frozenset({
    "schema_migrations",
    "staging_extractions",
    "schema_proposals",
    "conflict_kinds",
    "conflict_groups",
    "embed_cache",
})

# FTS5 が裏側で作る shadow テーブルの suffix。doc_fts 本体 (virtual table) は残し、
# doc_fts_data / doc_fts_idx / doc_fts_content / doc_fts_docsize / doc_fts_config は除外。
_FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")


def _format_table_decl(db: sqlite3.Connection, name: str) -> str:
    """PRAGMA table_info から `name(col1 TYPE, col2 TYPE, ...)` 形式の宣言文字列を返す。

    - 列順は cid 昇順 (PRAGMA が返す順)。
    - type 列が空ならカラム名のみ (`name(col1, col2)`)。
    - テーブル不在なら空文字を返す (defensive)。
    """
    rows = list(db.execute(f"PRAGMA table_info({name})"))
    if not rows:
        return ""
    parts: list[str] = []
    for r in rows:
        col = r["name"] if isinstance(r, sqlite3.Row) else r[1]
        typ = (r["type"] if isinstance(r, sqlite3.Row) else r[2]) or ""
        typ = typ.strip()
        parts.append(f"{col} {typ}".strip())
    return f"{name}(" + ", ".join(parts) + ")"


@functools.cache
def _load_schema_docs() -> tuple[dict, ...]:
    """data/schema_docs.yml を読んで items を返す。

    ファイル不在 / 空 / version 不一致 → 空タプル。
    item は {table: str, role: str}。両フィールド必須、片方欠落でスキップ。
    tuple を返すのは @cache 用 (mutable list を返すと呼び出し側で破壊できてしまう)。
    """
    path = config.SCHEMA_DOCS
    if not path.exists():
        return ()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return ()
    if data.get("version") != 1:
        return ()
    items = data.get("items") or []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        table, role = it.get("table"), it.get("role")
        if not table or not role:
            continue
        out.append({"table": table, "role": role})
    return tuple(out)


def select_extra_tables(question: str, *, available: set[str], k: int) -> list[str]:
    """質問に近い extra テーブルを embedding cos-sim で top-k 取る。

    - schema_docs.yml の items のうち table が available に含まれるものだけ対象。
    - 質問 embed が None → schema_docs の出現順で先頭 k 件 (CI で Ollama 不在でも動かす)。
    - role 側 embed が None / 次元不一致 → スキップ (= 取得しない)。
    - 同点は YAML 出現順で安定化。
    """
    if k <= 0:
        return []
    docs = _load_schema_docs()
    if not docs:
        return []
    eligible = [(i, it) for i, it in enumerate(docs) if it["table"] in available]
    if not eligible:
        return []

    # 遅延 import: query.py が schema_block を import するので、トップレベル import すると循環する。
    from query import _embed_cached, _cosine  # noqa: PLC0415

    q_vec = _embed_cached(question)
    if q_vec is None:
        return [it["table"] for _, it in eligible[:k]]
    scored: list[tuple[float, int, str]] = []
    for i, it in eligible:
        v = _embed_cached(it["role"])
        if v is None or len(v) != len(q_vec):
            continue
        scored.append((_cosine(q_vec, v), i, it["table"]))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [name for _, _, name in scored[:k]]


def _list_tables(db: sqlite3.Connection) -> set[str]:
    """ユーザ可視のテーブル / view 名を列挙。internal / FTS5 shadow / sqlite_* を除外。

    FTS5 shadow: virtual table `<base>` があるとき、`<base>_data` / `<base>_idx` /
    `<base>_content` / `<base>_docsize` / `<base>_config` の 5 種を内部生成する。
    これは LLM が触る用途が無いので除外する。
    """
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%'"
    )
    all_names = {(r["name"] if isinstance(r, sqlite3.Row) else r[0]) for r in rows}
    out: set[str] = set()
    for n in all_names:
        if n in INTERNAL_TABLES:
            continue
        is_shadow = any(
            n.endswith(suf) and n[: -len(suf)] in all_names
            for suf in _FTS_SHADOW_SUFFIXES
        )
        if is_shadow:
            continue
        out.add(n)
    return out


def build_schema_block(question: str, *, db: sqlite3.Connection | None = None) -> str:
    """text2sql.txt の {SCHEMA} に注入するスキーマブロック文字列を返す。

    Layout (doc §「スキーマブロックの構造」より):

        -- 中核
        documents(...)

        -- エンティティ canonical
        person(...)
        organization(...)
        ...

        -- 関係 junction
        employment(...)
        ...

        -- 関連 (質問に応じて自動選択, top-K)
        product_claims(...)
        ...
    """
    if db is None:
        db = kg.connect(readonly=True)

    available = _list_tables(db)

    lines: list[str] = []

    # Core: documents
    if "documents" in available:
        lines.append("-- 中核")
        decl = _format_table_decl(db, "documents")
        if decl:
            lines.append(decl)
        lines.append("")

    # Core: entity canonical
    entity_core = ("person", "organization", "product", "project", "contract")
    entity_lines = [_format_table_decl(db, t) for t in entity_core if t in available]
    entity_lines = [d for d in entity_lines if d]
    if entity_lines:
        lines.append("-- エンティティ canonical")
        lines.extend(entity_lines)
        lines.append("")

    # Core: relation junction
    relation_core = (
        "employment", "manufacturing", "org_hierarchy",
        "product_variant", "governance", "compliance",
    )
    relation_lines = [_format_table_decl(db, t) for t in relation_core if t in available]
    relation_lines = [d for d in relation_lines if d]
    if relation_lines:
        lines.append("-- 関係 junction")
        lines.extend(relation_lines)
        lines.append("")

    # Extra: question-dependent
    extra_candidates = available - set(CORE_TABLES)
    extras = select_extra_tables(
        question, available=extra_candidates, k=config.SCHEMA_EXTRA_TOPK
    )
    if extras:
        lines.append(f"-- 関連 (質問に応じて自動選択, top-{config.SCHEMA_EXTRA_TOPK})")
        for t in extras:
            decl = _format_table_decl(db, t)
            if decl:
                lines.append(decl)
        lines.append("")

    # 末尾改行 1 個で終わるよう trailing blank を 1 個に正規化
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"
