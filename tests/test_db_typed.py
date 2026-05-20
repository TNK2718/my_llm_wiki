"""typed-schema db.py の §3/§5/§6/§7 を検証する smoke test."""
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import config  # noqa: E402
import db as kg  # noqa: E402


def _doc(conn, slug="doc1", title="t", body="b") -> int:
    return kg.upsert_document(conn, slug, title, None, body)


def _person(conn, name="Alice") -> int:
    eid, _ = kg.upsert_entity(conn, "person", name)
    return eid


# ---------- 受け入れ基準: PRAGMA / FK / 起動 ----------

def test_pragma_foreign_keys_on_every_connection(tmp_db):
    """db.py.connect() で FK が ON。受け入れ基準 1."""
    assert tmp_db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_bootstrap_seed_loaded(tmp_db):
    """migration 0001 で __human__ document と conflict_kinds が seed される。"""
    human = tmp_db.execute("SELECT slug FROM documents WHERE id=1").fetchone()
    assert human["slug"] == "__human__"
    n = tmp_db.execute("SELECT COUNT(*) FROM conflict_kinds").fetchone()[0]
    assert n >= 15  # starter 18 種


def test_fk_violation_rejected(tmp_db):
    """conflict_groups.kind が conflict_kinds に無い値で INSERT すると FK 違反。"""
    with pytest.raises(sqlite3.IntegrityError):
        with kg.writer(tmp_db):
            tmp_db.execute(
                "INSERT INTO conflict_groups(kind, created_at) VALUES (?,?)",
                ("nonexistent_kind", "2026-01-01"),
            )


def test_org_type_check_constraint(tmp_db):
    with kg.writer(tmp_db):
        oid, _ = kg.upsert_entity(tmp_db, "organization", "Acme")
    with pytest.raises(sqlite3.IntegrityError):
        with kg.writer(tmp_db):
            tmp_db.execute(
                "UPDATE organization SET org_type='invalid_type' WHERE id=?", (oid,),
            )


# ---------- §3 状態遷移 ----------

def test_claim_first_sets_canonical(tmp_db):
    with kg.writer(tmp_db):
        doc = _doc(tmp_db)
        pid = _person(tmp_db)
        r = kg.record_claim(
            tmp_db, "person", pid, "birth_date", "1990-01-01", doc, confidence=0.8,
        )
    assert r.result == kg.RecordClaimResult.INSERTED_ACTIVE
    assert r.canonical_updated is True
    val = tmp_db.execute("SELECT birth_date FROM person WHERE id=?", (pid,)).fetchone()[0]
    assert val == "1990-01-01"


def test_claim_same_value_multi_evidence(tmp_db):
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        d2 = _doc(tmp_db, "d2")
        pid = _person(tmp_db)
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1990-01-01", d1, confidence=0.8)
        r = kg.record_claim(
            tmp_db, "person", pid, "birth_date", "1990-01-01", d2, confidence=0.7,
        )
    assert r.result == kg.RecordClaimResult.INSERTED_ACTIVE
    assert r.canonical_updated is False
    actives = tmp_db.execute(
        "SELECT COUNT(*) FROM person_claims WHERE person_id=? AND column_name='birth_date' "
        "AND status='active'", (pid,),
    ).fetchone()[0]
    assert actives == 2


def test_claim_higher_conf_supersedes(tmp_db):
    """δ=0.1 を上回る新 claim → 旧 active を superseded、canonical UPDATE."""
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        d2 = _doc(tmp_db, "d2")
        pid = _person(tmp_db)
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1990", d1, confidence=0.5)
        r = kg.record_claim(tmp_db, "person", pid, "birth_date", "1991", d2, confidence=0.8)
    assert r.result == kg.RecordClaimResult.INSERTED_ACTIVE
    assert r.canonical_updated is True
    val = tmp_db.execute("SELECT birth_date FROM person WHERE id=?", (pid,)).fetchone()[0]
    assert val == "1991"
    superseded = tmp_db.execute(
        "SELECT COUNT(*) FROM person_claims WHERE status='superseded'",
    ).fetchone()[0]
    assert superseded == 1


def test_claim_close_conf_creates_conflict(tmp_db):
    """δ 以内の異 value 主張 → 両 conflicted、canonical 据え置き、conflict_group 起票."""
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        d2 = _doc(tmp_db, "d2")
        pid = _person(tmp_db)
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1990", d1, confidence=0.7)
        r = kg.record_claim(tmp_db, "person", pid, "birth_date", "1991", d2, confidence=0.75)
    assert r.result == kg.RecordClaimResult.INSERTED_CONFLICTED
    assert r.canonical_updated is False
    val = tmp_db.execute("SELECT birth_date FROM person WHERE id=?", (pid,)).fetchone()[0]
    assert val == "1990"  # 据え置き
    grp_count = tmp_db.execute("SELECT COUNT(*) FROM conflict_groups").fetchone()[0]
    assert grp_count == 1


def test_claim_lower_conf_superseded(tmp_db):
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        d2 = _doc(tmp_db, "d2")
        pid = _person(tmp_db)
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1990", d1, confidence=0.9)
        r = kg.record_claim(tmp_db, "person", pid, "birth_date", "1991", d2, confidence=0.4)
    assert r.result == kg.RecordClaimResult.INSERTED_SUPERSEDED
    assert r.canonical_updated is False
    val = tmp_db.execute("SELECT birth_date FROM person WHERE id=?", (pid,)).fetchone()[0]
    assert val == "1990"


# ---------- §5 人手 verdict ----------

def test_human_verdict_overrides_doc_with_conflict(tmp_db):
    """LLM cap=0.95 と人手 1.0 の差 0.05 ≤ δ=0.1 → conflict をトリガする規約."""
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        pid = _person(tmp_db)
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1990", d1, confidence=0.95)
        # 人手 verdict
        r = kg.record_claim(
            tmp_db, "person", pid, "birth_date", "1991",
            config.HUMAN_DOCUMENT_ID, confidence=1.0,
            evidence="resolver: alice; verdict",
        )
    assert r.result == kg.RecordClaimResult.INSERTED_CONFLICTED


def test_human_verdict_supersedes_weaker_doc(tmp_db):
    """人手 1.0 vs doc 0.5 → δ 大きく超え → 人手が active、doc が superseded."""
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        pid = _person(tmp_db)
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1990", d1, confidence=0.5)
        r = kg.record_claim(
            tmp_db, "person", pid, "birth_date", "1991",
            config.HUMAN_DOCUMENT_ID, confidence=1.0,
        )
    assert r.result == kg.RecordClaimResult.INSERTED_ACTIVE
    val = tmp_db.execute("SELECT birth_date FROM person WHERE id=?", (pid,)).fetchone()[0]
    assert val == "1991"


# ---------- §6 UPSERT ----------

def test_upsert_same_value_updates_evidence(tmp_db):
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        pid = _person(tmp_db)
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1990", d1,
                        evidence="first", confidence=0.5)
        r = kg.record_claim(
            tmp_db, "person", pid, "birth_date", "1990", d1,
            evidence="second", confidence=0.6,
        )
    assert r.result == kg.RecordClaimResult.UPDATED_EVIDENCE
    row = tmp_db.execute(
        "SELECT evidence, confidence FROM person_claims WHERE id=?", (r.claim_id,),
    ).fetchone()
    assert row["evidence"] == "second"
    assert row["confidence"] == 0.6


def test_upsert_diff_value_recomputes(tmp_db):
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        pid = _person(tmp_db)
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1990", d1, confidence=0.8)
        r = kg.record_claim(
            tmp_db, "person", pid, "birth_date", "1991", d1, confidence=0.9,
        )
    assert r.result == kg.RecordClaimResult.UPSERT_REVALUE
    val = tmp_db.execute("SELECT birth_date FROM person WHERE id=?", (pid,)).fetchone()[0]
    assert val == "1991"


# ---------- §7 低 conf ルーティング ----------

def test_low_confidence_rejected(tmp_db):
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        pid = _person(tmp_db)
        r = kg.record_claim(
            tmp_db, "person", pid, "birth_date", "1990", d1, confidence=0.2,
        )
    assert r.result == kg.RecordClaimResult.REJECTED_LOW_CONFIDENCE
    n = tmp_db.execute(
        "SELECT COUNT(*) FROM person_claims WHERE person_id=?", (pid,),
    ).fetchone()[0]
    assert n == 0


# ---------- existence claims ----------

def test_existence_claim_first_active(tmp_db):
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        pid = _person(tmp_db)
        oid, _ = kg.upsert_entity(tmp_db, "organization", "Acme")
        eid, how = kg.find_or_create_employment(
            tmp_db, pid, oid, "2020-01-01", d1, confidence=0.7,
        )
        r = kg.record_existence_claim(
            tmp_db, "employment", eid, d1, confidence=0.7,
        )
    assert how == "new"
    assert r.result == kg.RecordClaimResult.INSERTED_ACTIVE
    count = tmp_db.execute(
        "SELECT COUNT(*) FROM employment_existence_claims WHERE status='active'",
    ).fetchone()[0]
    assert count == 1


def test_existence_claim_multi_doc(tmp_db):
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        d2 = _doc(tmp_db, "d2")
        pid = _person(tmp_db)
        oid, _ = kg.upsert_entity(tmp_db, "organization", "Acme")
        eid, _ = kg.find_or_create_employment(tmp_db, pid, oid, "2020-01-01", d1, confidence=0.6)
        kg.record_existence_claim(tmp_db, "employment", eid, d1, confidence=0.6)
        kg.record_existence_claim(tmp_db, "employment", eid, d2, confidence=0.7)
    count = tmp_db.execute(
        "SELECT COUNT(*) FROM employment_existence_claims WHERE status='active' AND employment_id=?",
        (eid,),
    ).fetchone()[0]
    assert count == 2


# ---------- cardinality 違反 ----------

def test_manufacturing_cardinality_violation(tmp_db):
    """UNIQUE(product_id)。同 product に異 org の主張は staging へ振る信号を返す."""
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        prod, _ = kg.upsert_entity(tmp_db, "product", "Widget")
        org_a, _ = kg.upsert_entity(tmp_db, "organization", "Acme")
        org_b, _ = kg.upsert_entity(tmp_db, "organization", "Bravo")
        mid, how, _ = kg.find_or_create_manufacturing(tmp_db, prod, org_a, d1, confidence=0.6)
        assert how == "new"
        mid2, how2, conflict_with = kg.find_or_create_manufacturing(
            tmp_db, prod, org_b, d1, confidence=0.6,
        )
    assert how2 == "cardinality_violation"
    assert conflict_with == mid


# ---------- 並行 ingest BEGIN IMMEDIATE ----------

def test_writer_serializes_concurrent_inserts(tmp_path, monkeypatch):
    """BEGIN IMMEDIATE で writer が serialize される（race による両方上書きが起きない）."""
    db_path = tmp_path / "kg.sqlite"
    monkeypatch.setattr(config, "KG_DB", db_path)
    # init schema in main thread
    main = kg.connect()
    with kg.writer(main):
        d1 = _doc(main, "d1")
        pid = _person(main)
    main.close()

    results = []
    errors = []

    def worker(value: str, conf: float):
        try:
            c = sqlite3.connect(db_path, timeout=10)
            c.execute("PRAGMA foreign_keys = ON")
            c.row_factory = sqlite3.Row
            with kg.writer(c):
                r = kg.record_claim(
                    c, "person", pid, "birth_date", value, d1, confidence=conf,
                )
            results.append(r.result)
            c.close()
        except Exception as e:
            errors.append(e)

    # 同 (entity, column, doc_id) で UPSERT 競合する 2 thread。
    # BEGIN IMMEDIATE で serialize されるので race にならず順次評価される。
    t1 = threading.Thread(target=worker, args=("1990", 0.8))
    t2 = threading.Thread(target=worker, args=("1991", 0.9))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert not errors, errors
    # どちらの順序でも最終 canonical は決定的に書き込まれた行で確定する
    final = sqlite3.connect(db_path)
    final.row_factory = sqlite3.Row
    val = final.execute("SELECT birth_date FROM person WHERE id=?", (pid,)).fetchone()[0]
    assert val in ("1990", "1991")
    # claim 数は 1 件のみ (同 (entity, column, doc_id) UPSERT)
    n = final.execute("SELECT COUNT(*) FROM person_claims WHERE person_id=?", (pid,)).fetchone()[0]
    assert n == 1
    final.close()


# ---------- linter R1/R2 は別ファイル test_query_linter.py で扱う ----------
