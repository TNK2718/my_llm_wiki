"""query.sql_docs の単体テスト。

text2sql rows から entity の canonical_name を逆引きして
`*_claims.document_id → documents.slug` を返す経路を担保する。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import db as kg  # noqa: E402
import query  # noqa: E402


_DEFAULT_COL = {
    "person": ("nationality", "JP"),
    "organization": ("org_type", "company"),
    "product": ("category", "saas"),
    "project": ("started_at", "2024-01-01"),
    "contract": ("contract_type", "service_description"),
}


def _seed_entity_with_claim(
    conn, table: str, name: str, doc_slug: str, body: str = "doc body",
) -> tuple[int, int]:
    """entity と何らかの claim を 1 つ作る。戻り値 (entity_id, doc_id)。

    canonical_name claim は ingest 実装が作らないので、テストは別 column の
    claim を作って「entity-doc 関係の証跡」とする。
    """
    doc_id = kg.upsert_document(conn, doc_slug, title=doc_slug, path=None, body=body)
    entity_id, _ = kg.upsert_entity(conn, table, name)
    col, val = _DEFAULT_COL[table]
    kg.record_claim(
        conn, table, entity_id, col, val, doc_id,
        evidence=f"{name} 出典", confidence=0.9,
    )
    conn.commit()
    return entity_id, doc_id


def test_sql_docs_resolves_canonical_name(tmp_db):
    _seed_entity_with_claim(tmp_db, "organization", "アクメ株式会社", "acme-overview")
    result = query.sql_docs([{"name": "アクメ株式会社"}])
    assert len(result) == 1
    assert result[0]["slug"] == "acme-overview"
    assert result[0]["entity_table"] == "organization"
    assert result[0]["matched_value"] == "アクメ株式会社"


def test_sql_docs_empty_rows_returns_empty(tmp_db):
    assert query.sql_docs([]) == []


def test_sql_docs_ignores_numeric_strings(tmp_db):
    _seed_entity_with_claim(tmp_db, "person", "山田", "yamada-doc")
    # count や id を含むだけの rows → candidate 0 → 結果空
    assert query.sql_docs([{"count": "2"}]) == []
    assert query.sql_docs([{"id": "1", "ratio": "0.5"}]) == []


def test_sql_docs_returns_multiple_entity_tables(tmp_db):
    # 同名 entity が person と organization 両方に存在するケース
    _seed_entity_with_claim(tmp_db, "person", "Alice", "alice-person-doc")
    _seed_entity_with_claim(tmp_db, "organization", "Alice", "alice-org-doc")
    result = query.sql_docs([{"name": "Alice"}])
    assert len(result) == 2
    tables = {r["entity_table"] for r in result}
    assert tables == {"person", "organization"}
    slugs = {r["slug"] for r in result}
    assert slugs == {"alice-person-doc", "alice-org-doc"}
