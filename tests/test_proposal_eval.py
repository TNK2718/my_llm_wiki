"""Phase 4: proposal_eval.compute_metrics の純関数テスト。

LLM 経由 ingest はテストしない (slow + non-deterministic)。compute_metrics() は DB と
gold dict を受け取って PRF / canonical_stability を出すだけなので、テストは「DB を
手で作る → expected gold を渡す → メトリクスが正しい」だけで完結する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as kg  # noqa: E402
from tools.eval import proposal_eval  # noqa: E402


def _doc(conn, slug="d1") -> int:
    return kg.upsert_document(conn, slug, slug, None, "body")


def _add_proposal(conn, kind: str, target_table: str) -> None:
    kg.add_schema_proposal(
        conn,
        kind=kind,
        target_table=target_table,
        proposed_ddl=f"-- {kind} {target_table}",
        rationale="test",
    )


# ---------- schema_proposal_precision ----------

def test_proposal_precision_perfect_match(tmp_db):
    with kg.writer(tmp_db):
        _add_proposal(tmp_db, "new_table", "startup")
        _add_proposal(tmp_db, "new_column", "organization")
    gold = {
        "expected_proposals": [
            {"kind": "new_table", "target_table": "startup"},
            {"kind": "new_column", "target_table": "organization"},
        ],
    }
    m = proposal_eval.compute_metrics(tmp_db, gold)
    assert m["schema_proposal_precision"]["precision"] == 1.0
    assert m["schema_proposal_precision"]["recall"] == 1.0


def test_proposal_precision_partial(tmp_db):
    with kg.writer(tmp_db):
        _add_proposal(tmp_db, "new_table", "startup")
        _add_proposal(tmp_db, "new_table", "unrelated")  # extra
    gold = {"expected_proposals": [
        {"kind": "new_table", "target_table": "startup"},
        {"kind": "new_column", "target_table": "organization"},  # missed
    ]}
    m = proposal_eval.compute_metrics(tmp_db, gold)
    p = m["schema_proposal_precision"]
    assert p["tp"] == 1
    assert p["fp"] == 1
    assert p["fn"] == 1


# ---------- staging_replay_correctness ----------

def test_staging_correctness(tmp_db):
    with kg.writer(tmp_db):
        doc = _doc(tmp_db)
        kg.add_staging_extraction(
            tmp_db, doc, raw_payload="{}", proposed_table="startup",
            match_summary='{"reason":"non_starter_type"}',
        )
    gold = {"expected_staging": [
        {"reason": "non_starter_type", "proposed_table": "startup"},
    ]}
    m = proposal_eval.compute_metrics(tmp_db, gold)
    assert m["staging_replay_correctness"]["f1"] == 1.0


def test_staging_handles_invalid_json_in_match_summary(tmp_db):
    with kg.writer(tmp_db):
        doc = _doc(tmp_db)
        kg.add_staging_extraction(
            tmp_db, doc, raw_payload="{}", proposed_table="x",
            match_summary="not-json",
        )
    m = proposal_eval.compute_metrics(tmp_db, {"expected_staging": []})
    # invalid JSON は staging set から除外され、空 vs 空 で precision/recall は 0/0
    assert m["staging_replay_correctness"]["fp"] == 0


# ---------- canonical_stability ----------

def test_canonical_stability_zero_flip_passes(tmp_db):
    with kg.writer(tmp_db):
        doc = _doc(tmp_db)
        pid, _ = kg.upsert_entity(tmp_db, "person", "Alice")
        # 同 (entity, column) 1 文書のみ
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1990-01-01", doc, confidence=0.8)
    m = proposal_eval.compute_metrics(tmp_db, {"canonical_stability": {"max_flips_per_entity_column": 0}})
    assert m["canonical_stability"]["passed"] is True
    assert m["canonical_stability"]["max_flips_observed"] == 0


def test_canonical_stability_detects_flip(tmp_db):
    with kg.writer(tmp_db):
        d1 = _doc(tmp_db, "d1")
        d2 = _doc(tmp_db, "d2")
        pid, _ = kg.upsert_entity(tmp_db, "person", "Alice")
        # 異 value を 2 文書から → distinct_vals=2 → flips=1
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1990-01-01", d1, confidence=0.6)
        kg.record_claim(tmp_db, "person", pid, "birth_date", "1991-02-02", d2, confidence=0.9)
    m = proposal_eval.compute_metrics(tmp_db, {"canonical_stability": {"max_flips_per_entity_column": 0}})
    assert m["canonical_stability"]["passed"] is False
    assert m["canonical_stability"]["max_flips_observed"] >= 1


# ---------- weak_relation_promotion_recall ----------

def test_promotion_recall(tmp_db):
    with kg.writer(tmp_db):
        doc = _doc(tmp_db)
        org_a, _ = kg.upsert_entity(tmp_db, "organization", "A")
        kg.add_weak_relation(
            tmp_db, subject_table="organization", subject_id=org_a,
            predicate="invested_in", object_table=None, object_id=None,
            object_text="X", document_id=doc, evidence=None, confidence=0.3,
        )
        # promote 済みとマーク
        tmp_db.execute(
            "UPDATE weak_relations SET promoted_to='investment' WHERE predicate='invested_in'"
        )
    gold = {"expected_weak_promotion": [{"predicate": "invested_in", "becomes_junction": "investment"}]}
    m = proposal_eval.compute_metrics(tmp_db, gold)
    assert m["weak_relation_promotion_recall"]["recall"] == 1.0


def test_promotion_recall_misses(tmp_db):
    with kg.writer(tmp_db):
        pass  # no promotions at all
    gold = {"expected_weak_promotion": [{"predicate": "invested_in", "becomes_junction": "investment"}]}
    m = proposal_eval.compute_metrics(tmp_db, gold)
    assert m["weak_relation_promotion_recall"]["recall"] == 0.0
    assert m["weak_relation_promotion_recall"]["fn"] == 1


# ---------- 統合: gold yml 読込 ----------

def test_acme_overview_gold_loads_and_passes_on_clean_db(tmp_db):
    """acme-overview の gold (全部空 expected) は、何も ingest していない空 DB で
    is_empty=True (採点対象なし) → precision/recall/f1 は None。
    canonical_stability は 0 flip で pass。
    """
    gold_path = Path(__file__).resolve().parent.parent / "data" / "eval" / "gold" / "schema_proposals" / "acme-overview.yml"
    if not gold_path.exists():
        pytest.skip("gold fixture not present")
    from tools.eval import io as eio
    gold = eio.load_yaml(gold_path)
    m = proposal_eval.compute_metrics(tmp_db, gold)
    # 期待ゼロ vs 予測ゼロ → 採点対象なし。tp=0, precision=None
    assert m["schema_proposal_precision"]["tp"] == 0
    assert m["schema_proposal_precision"]["precision"] is None
    assert m["canonical_stability"]["passed"] is True
