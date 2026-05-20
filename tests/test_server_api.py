"""server.py の主要 endpoint を TestClient 経由で軽く叩く."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "KG_DB", tmp_path / "kg.sqlite")
    # 再ロードで monkeypatch を効かせる
    import importlib
    import db
    importlib.reload(db)
    import proposal_review
    importlib.reload(proposal_review)
    import query
    importlib.reload(query)
    import server
    importlib.reload(server)
    # 空 DB を事前作成 (PRAGMA / migration が走る)
    init = db.connect()
    init.close()
    from fastapi.testclient import TestClient
    return TestClient(server.app), server, db


def test_stats_endpoint(client):
    c, _, _ = client
    r = c.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    assert "entities" in body
    assert set(body["entities"].keys()) >= {"person", "organization", "product", "project"}


def test_entity_list_endpoint(client):
    c, _, kg = client
    with kg.writer(kg.connect()) as conn:
        kg.upsert_entity(conn, "person", "Alice")
        kg.upsert_entity(conn, "person", "Bob")
    r = c.get("/api/person/list")
    assert r.status_code == 200
    names = [row["canonical_name"] for row in r.json()]
    assert "Alice" in names and "Bob" in names


def test_entity_detail_endpoint(client):
    c, _, kg = client
    with kg.writer(kg.connect()) as conn:
        pid, _ = kg.upsert_entity(conn, "person", "Alice")
        doc = kg.upsert_document(conn, "d1", "doc1", None, "body")
        kg.record_claim(conn, "person", pid, "birth_date", "1990-01-01", doc, confidence=0.8)
    r = c.get(f"/api/person/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["entity"]["canonical_name"] == "Alice"
    assert any(cl["column_name"] == "birth_date" for cl in body["claims"])


def test_proposal_lifecycle(client):
    c, _, kg = client
    with kg.writer(kg.connect()) as conn:
        kg.add_schema_proposal(
            conn, kind="new_table", target_table="algorithm",
            proposed_ddl="CREATE TABLE algorithm (id INTEGER PRIMARY KEY, name TEXT)",
            rationale="test",
        )
    r = c.get("/api/proposals")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= 1
    pid = rows[0]["id"]

    # validate
    r = c.get(f"/api/proposals/{pid}/validate")
    assert r.status_code == 200
    assert r.json()["ast_ok"] is True

    # approve
    r = c.post(f"/api/proposals/{pid}/approve", json={"by": "tester"})
    assert r.status_code == 200, r.text
    assert r.json()["applied_migration"] >= 2  # 0001 は bootstrap


def test_staging_endpoint(client):
    c, _, kg = client
    with kg.writer(kg.connect()) as conn:
        doc = kg.upsert_document(conn, "d1", "doc1", None, "body")
        kg.add_staging_extraction(
            conn, doc, raw_payload='{"foo": 1}', proposed_table="algorithm",
        )
    r = c.get("/api/staging")
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_weak_relations_summary(client):
    c, _, kg = client
    with kg.writer(kg.connect()) as conn:
        doc = kg.upsert_document(conn, "d1", "doc1", None, "body")
        pid, _ = kg.upsert_entity(conn, "person", "Alice")
        kg.add_weak_relation(
            conn, subject_table="person", subject_id=pid, predicate="mentions",
            document_id=doc, object_text="something", confidence=0.3,
        )
    r = c.get("/api/weak_relations")
    assert r.status_code == 200
    rows = r.json()
    assert any(row.get("predicate") == "mentions" for row in rows)


def test_proposal_approval_rejects_bad_ddl(client):
    c, _, kg = client
    with kg.writer(kg.connect()) as conn:
        kg.add_schema_proposal(
            conn, kind="new_table", target_table="bad_ddl",
            proposed_ddl="DROP TABLE person; CREATE TABLE bad_ddl (id INT)",
            rationale="should be rejected",
        )
    rows = c.get("/api/proposals").json()
    pid = rows[0]["id"]
    r = c.post(f"/api/proposals/{pid}/approve")
    assert r.status_code == 400
    assert "ast_validation_failed" in r.text
